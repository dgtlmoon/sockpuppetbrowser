#!/usr/bin/env python3

# Auto scaling websocket proxy for Chrome CDP


from loguru import logger
import argparse
import asyncio
import json
import os
import psutil
import random
import requests
import subprocess
import sys
import tempfile
import time
import websockets

connection_count = 0
connection_count_max = int(os.getenv('MAX_CONCURRENT_CHROME_PROCESSES', 10))
connection_count_total = 0
shutdown = False
memory_use_limit_percent = int(os.getenv('HARD_MEMORY_USAGE_LIMIT_PERCENT', 90))


# @todo Some UI where you can change loglevel on a UI?
# @todo Some way to change connection threshold via UI
# @todo Could have a configurable list of rotatable devtools endpoints?
# @todo Add `ulimit` config for max-memory-per-chrome
# @todo manage a hard 'MAX_CHROME_RUN_TIME` default 60sec
# @todo use chrome remote debug by unix pipe, instead of socket

def getBrowserArgsFromQuery(query):
    extra_args = []
    from urllib.parse import urlparse, parse_qs
    parsed_url = urlparse(query)
    for k, v in parse_qs(parsed_url.query).items():
        if k.startswith('--'):
            extra_args.append(f"{k}={v[0]}")
    return extra_args


def launch_chrome(port=19222, user_data_dir="/tmp", url_query=""):
    args = getBrowserArgsFromQuery(url_query)
    # CHROME_BIN set in Dockerfile
    chrome_location = os.getenv("CHROME_BIN", "/usr/bin/google-chrome")
    # Needs chrome 121+ or so, Defaults taken from a live Puppeteer
    # https://github.com/GoogleChrome/chrome-launcher/blob/main/docs/chrome-flags-for-tools.md
    chrome_run = [
        chrome_location,
        "--allow-pre-commit-input",
        "--disable-background-networking",
        "--enable-features=NetworkServiceInProcess2",
        "--headless",
#        "--hide-scrollbars",
#        "--mute-audio",
        f"--remote-debugging-port={port}",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-component-extensions-with-background-pages",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-features=Translate,AcceptCHFrame,MediaRouter,OptimizationHints,ProcessPerSiteUpToMainFrameThreshold",
        "--disable-field-trial-config",
        "--disable-hang-monitor",
        "--disable-infobars",
        "--disable-ipc-flooding-protection",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-renderer-backgrounding",
        "--disable-search-engine-choice-screen",
        "--disable-sync",
        "--enable-automation",
        "--export-tagged-pdf",
        "--force-color-profile=srgb",
        "--generate-pdf-document-outline",
        "--metrics-recording-only",
        "--no-first-run",
        "--password-store=basic",
        "--use-mock-keychain",
    ]

    chrome_run += args

    # If window-size was not the query (it would be inserted above) so fall back to env vars
    if not '--window-size' in url_query:
        if os.getenv('SCREEN_WIDTH') and os.getenv('SCREEN_HEIGHT'):
            screen_wh_arg=f"--window-size={int(os.getenv('SCREEN_WIDTH'))},{int(os.getenv('SCREEN_HEIGHT'))}"
            logger.debug(f"No --window-size in start query, falling back to env var {screen_wh_arg}")
            chrome_run.append(screen_wh_arg)
        else:
            logger.warning(f"No --window-size in query, and no SCREEN_HEIGHT + SCREEN_WIDTH env vars found :-(")

    if not '--user-data-dir' in url_query:
        tmp_user_data_dir = tempfile.mkdtemp(prefix="chrome-puppeteer-proxy", dir="/tmp")
        chrome_run.append(f"--user-data-dir={tmp_user_data_dir}")
        logger.debug(f"No user-data-dir in query, using {tmp_user_data_dir}")

    # start_new_session not (makes the main one keep running?)
    # Shell has to be false or it wont process the args
    try:
        process = subprocess.Popen(args=chrome_run, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
                                   universal_newlines=True)
    except FileNotFoundError as e:
        logger.critical(f"Chrome binary was not found at {chrome_location}, aborting!")
        raise e

    # Check if the process crashed on startup, print some debug if it did
    return process


def get_next_open_port(start=10000, end=60000):
    import psutil
    used_ports = []
    for conn in psutil.net_connections(kind="inet4"):
        if conn.status == 'LISTEN' and conn.laddr.port >= start and conn.laddr.port <= end:
            used_ports.append(conn.laddr.port)

    r = next(rng for rng in iter(lambda: random.randint(start, end), None) if rng not in used_ports)

    return r

async def stats_disconnect(time_at_start=0.0, websocket: websockets.WebSocketServerProtocol = None):
    global connection_count
    connection_count -= 1

    logger.debug(
        f"Websocket {websocket.id} - Connection ended, processed in {time.time() - time_at_start:.3f}s")

async def cleanup_chrome_by_pid(p, user_data_dir="/tmp", time_at_start=0.0, websocket: websockets.WebSocketServerProtocol = None):
    import signal

    logger.debug(
        f"Websocket {websocket.id} - Cleaning up chrome pid: {p.pid}")
    p.kill()
    # Flush IO queue
    p.communicate()

    # @todo while not dead try for 10 sec..,
    # does the pid disappear when killed?
    await asyncio.sleep(2)

    try:
        os.kill(p.pid, 0)
    except OSError:
        logger.success(f"Websocket {websocket.id} - Chrome PID {p.pid} died cleanly, good.")
    else:
        logger.error(f"Websocket {websocket.id} - Looks like {p.pid} didnt die, sending SIGKILL.")
        os.kill(int(p.pid), signal.SIGKILL)

    # @todo context for cleaning up datadir? some auto-cleanup flag?
    # shutil.rmtree(user_data_dir)

async def _request_retry(url, num_retries=20, success_list=[200, 404], **kwargs):
    # On a healthy machine with no load, Chrome is usually fired up in 100ms
    await asyncio.sleep(0.1)
    for _ in range(num_retries):
        try:
            response = requests.get(url, **kwargs)
            if response.status_code in success_list:
                ## Return response if successful
                return response
        except requests.exceptions.ConnectionError:
            logger.warning("No response from Chrome, retrying..")
            await asyncio.sleep(0.2)
            pass

    raise requests.exceptions.ConnectionError


async def launchPlaywrightChromeProxy(websocket, path):
    '''Called whenever a new connection is made to the server, Incoming connection, connect to CDP and start proxying'''
    global connection_count
    global connection_count_max
    global connection_count_total

    now = time.time()
    closed = asyncio.ensure_future(websocket.wait_closed())
    closed.add_done_callback(lambda task: asyncio.ensure_future(stats_disconnect(time_at_start=now, websocket=websocket)))

    svmem = psutil.virtual_memory()

    logger.debug(
        f"WebSocket ID: {websocket.id} Got new incoming connection ID from {websocket.remote_address[0]}:{websocket.remote_address[1]} ({path})")

    connection_count += 1
    connection_count_total += 1

    if connection_count > connection_count_max:
        logger.warning(
            f"WebSocket ID: {websocket.id} - Throttling/waiting, max connection limit reached {connection_count} of max {connection_count_max}  ({time.time() - now:.1f}s)")

    while svmem.percent > memory_use_limit_percent:
        logger.warning(f"WebSocket ID: {websocket.id} - {svmem.percent}% was > {memory_use_limit_percent}%.. delaying connecting and waiting for more free RAM  ({time.time() - now:.1f}s)")
        await asyncio.sleep(5)
        if time.time() - now > 60:
            logger.critical(
                f"WebSocket ID: {websocket.id} - Too long waiting for memory usage to drop, dropping connection. {svmem.percent}% was > {memory_use_limit_percent}%  ({time.time() - now:.1f}s)")
            return

    while connection_count > connection_count_max:
        await asyncio.sleep(3)
        if time.time() - now > 120:
            logger.critical(
                f"WebSocket ID: {websocket.id} - Waiting for existing connection count to drop took too long! dropping connection. ({time.time() - now:.1f}s)")
            return

    now_before_chrome_launch = time.time()

    port = get_next_open_port()
    chrome_process = launch_chrome(port=port, url_query=path)

    closed.add_done_callback(lambda task: asyncio.ensure_future(
        cleanup_chrome_by_pid(p=chrome_process, user_data_dir='@todo', time_at_start=now, websocket=websocket))
                             )

    chrome_json_info_url = f"http://localhost:{port}/json/version"
    # https://chromedevtools.github.io/devtools-protocol/
    try:
        # Define the retry strategy
        response = await _request_retry(chrome_json_info_url)
        if not response.status_code == 200:
            logger.critical(f"Chrome did not report the correct list of interfaces to at {chrome_json_info_url}, aborting :(")
            # @todo return 500 with text
            return
    except requests.exceptions.ConnectionError as e:
        # Instead of trying to analyse the output in a non-blocking way, we can assume that if we cant connect that something went wrong.
        logger.critical(f"Uhoh! Looks like Chrome did not start! do you need --cap-add=SYS_ADMIN added to start this container?")
        logger.critical(f"While trying to connect to {chrome_json_info_url} - {str(e)}")
        process_output = chrome_process.communicate()
        logger.critical(f"STDOUT '{process_output[0]}'")
        logger.critical(f"STDERR '{process_output[1]}'")
        chrome_process.terminate()
        return

    # On exception, flush and print debug

    logger.trace(f"WebSocket ID: {websocket.id} time to launch browser {time.time() - now_before_chrome_launch:.3f}s ")

    chrome_websocket_url = response.json().get("webSocketDebuggerUrl")
    logger.debug(f"WebSocket ID: {websocket.id} proxying to local Chrome instance via CDP {chrome_websocket_url}")

    # 10mb, keep in mind theres screenshots.
    try:
        async with websockets.connect(chrome_websocket_url, max_size=1024 * 1024 * 10) as ws:
            taskA = asyncio.create_task(hereToChromeCDP(ws, websocket))
            taskB = asyncio.create_task(chromeCDPtoPlaywright(ws, websocket))
            await taskA
            await taskB
    except TimeoutError as e:
        logger.error(f"Connection Timeout Out when connecting to Chrome CDP at {chrome_websocket_url}")
    except Exception as e:
        logger.error(f"Something bad happened: when connecting to Chrome CDP at {chrome_websocket_url}")
        logger.error(e)

    logger.success(f"Websocket {websocket.id} - Connection done!")


async def hereToChromeCDP(ws, websocket):
    try:
        async for message in ws:
            logger.trace(message[:1000])
            await websocket.send(message)
    except Exception as e:
        logger.error(e)


async def chromeCDPtoPlaywright(ws, websocket):
    try:
        async for message in websocket:
            logger.trace(message[:1000])
            if message.startswith("{") and message.endswith("}") and 'Page.navigate' in message:
                try:
                    m = json.loads(message)
                    # Print out some debug so we know roughly whats going on
                    logger.debug(f"{websocket.id} Page.navigate called to '{m['params']['url']}'")
                except Exception as e:
                    pass

            await ws.send(message)
    except Exception as e:
        logger.error(e)


async def stats_thread_func():
    global connection_count
    global connection_count_max
    global shutdown

    while True:
        logger.info(f"Current connection count: {connection_count} of max {connection_count_max}")
        logger.info(f"Total connections processed: {connection_count_total}")
        if connection_count > connection_count_max:
            logger.warning(f"{connection_count} of max {connection_count_max} over threshold, incoming connections will be delayed.")

        svmem = psutil.virtual_memory()
        logger.info(f"Memory: Used {svmem.percent}% (Limit {memory_use_limit_percent}%) - Available {svmem.free / 1024 / 1024:.1f}MB ")

        await asyncio.sleep(20)


if __name__ == '__main__':
    # Set a default logger level
    logger_level = os.getenv('LOG_LEVEL', 'DEBUG')
    logger.remove()
    try:
        log_level_for_stdout = {'DEBUG', 'SUCCESS'}
        logger.configure(handlers=[
            {"sink": sys.stdout, "level": logger_level,
             "filter": lambda record: record['level'].name in log_level_for_stdout},
            {"sink": sys.stderr, "level": logger_level,
             "filter": lambda record: record['level'].name not in log_level_for_stdout},
        ])
    # Catch negative number or wrong log level name
    except ValueError:
        print("Available log level names: TRACE, DEBUG(default), INFO, SUCCESS,"
              " WARNING, ERROR, CRITICAL")
        sys.exit(2)

    parser = argparse.ArgumentParser(description='websocket proxy.')
    parser.add_argument('--host', help='Host to bind to.',
                        default='0.0.0.0')
    parser.add_argument('--port', help='Port to bind to.',
                        default=3000)

    args = parser.parse_args()

    start_server = websockets.serve(launchPlaywrightChromeProxy, args.host, args.port)
    poll = asyncio.get_event_loop().create_task(stats_thread_func())
    asyncio.get_event_loop().run_until_complete(start_server)

    try:
        chrome_path = os.getenv("CHROME_BIN", "/usr/bin/google-chrome")
        logger.success(f"Starting Chrome proxy, Listening on ws://{args.host}:{args.port} -> {chrome_path}")
        asyncio.get_event_loop().run_forever()


    except KeyboardInterrupt:
        logger.success("Got CTRL+C/interrupt, shutting down.")
        # At this point, all child processes including Chrome should be terminated
