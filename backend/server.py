#!/usr/bin/env python3

# Auto scaling websocket proxy for Chrome CDP


from loguru import logger
import argparse
import asyncio
import os
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


# @todo Some UI where you can change loglevel on a UI?
# @todo Some way to change connection threshold via UI
# @todo Could have a configurable list of rotatable devtools endpoints?

def launch_chrome(port=19222, user_data_dir="/tmp"):
    # needs chrome 121+ or so
    # Taken from a live Puppeteer
    chrome_run = [
        "/usr/bin/google-chrome",
        "--allow-pre-commit-input",
        "--disable-background-networking",
        "--enable-features=NetworkServiceInProcess2",
        "--headless",
        "--hide-scrollbars",
        "--mute-audio",
        f"--remote-debugging-port={port}",
        "--user-data-dir=/tmp/puppeteer_dev_chrome_profile-64lAN0",
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

    # start_new_session not (makes the main one keep running?)
    # Shell has to be false or it wont process the args
    process = subprocess.Popen(args=chrome_run, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
                               universal_newlines=True)
    return process


def get_next_open_port(start=10000, end=60000):
    import psutil
    used_ports = []
    for conn in psutil.net_connections(kind="inet4"):
        if conn.status == 'LISTEN' and conn.laddr.port >= start and conn.laddr.port <= end:
            used_ports.append(conn.laddr.port)

    r = next(rng for rng in iter(lambda: random.randint(start, end), None) if rng not in used_ports)

    return r


async def cleanup_chrome_by_pid(p, user_data_dir="/tmp", time_at_start=0.0, websocket: websockets.WebSocketServerProtocol = None):
    import signal
    import shutil

    global connection_count
    connection_count -= 1

    logger.debug(f"Websocket {websocket.id} - Connection ended, processed in {time.time() - time_at_start:.3f}s cleaning up chrome pid: {p.pid}")
    p.kill()
    p.communicate()

    # @todo while not dead try for 10 sec..
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


async def launchPlaywrightChromeProxy(websocket, path):
    '''Called whenever a new connection is made to the server, Incoming connection, connect to CDP and start proxying'''
    global connection_count
    global connection_count_max
    global connection_count_total

    now = time.time()

    logger.debug(f"WebSocket ID: {websocket.id} Got new incoming connection ID from {websocket.remote_address[0]}:{websocket.remote_address[1]}")
    connection_count += 1
    connection_count_total += 1
    if connection_count > connection_count_max:
        logger.warning(
            f"WebSocket ID: {websocket.id} - Throttling/waiting, max connection limit reached {connection_count} of max {connection_count_max}")

    while connection_count > connection_count_max:
        await asyncio.sleep(3)
        if time.time()-now > 120:
            logger.critical(f"WebSocket ID: {websocket.id} - Waiting for existing connections took too long! dropping connection.")
            return

    port = get_next_open_port()
    # @todo use user-data-dir in query instead
    tmp_user_data_dir = tempfile.mkdtemp(prefix="chrome-puppeteer-proxy", dir="/tmp")
    chrome_process = launch_chrome(port=port)
    closed = asyncio.ensure_future(websocket.wait_closed())
    closed.add_done_callback(lambda task: asyncio.ensure_future(
        cleanup_chrome_by_pid(p=chrome_process, user_data_dir=tmp_user_data_dir, time_at_start=now, websocket=websocket))
                             )

    # Wait for startup, @todo some smarter way to check the socket? check for errors?
    # After spending hours trying to find a good non-blocking way to examine the stderr/stdin I couldnt find a solution
    await asyncio.sleep(3)

    # https://chromedevtools.github.io/devtools-protocol/

    response = requests.get(f"http://localhost:{port}/json/version")
    websocket_url = response.json().get("webSocketDebuggerUrl")
    logger.debug(f"WebSocket ID: {websocket.id} proxying to local Chrome instance via CDP {websocket_url}")

    # 10mb, keep in mind theres screenshots.
    try:
        async with websockets.connect(websocket_url, max_size=1024 * 1024 * 10) as ws:
            taskA = asyncio.create_task(hereToChromeCDP(ws, websocket))
            taskB = asyncio.create_task(chromeCDPtoPlaywright(ws, websocket))
            await taskA
            await taskB
    except TimeoutError as e:
        logger.error(f"Connection Timeout Out when connecting to Chrome CDP at {websocket_url}")
    except Exception as e:
        logger.error(f"Something bad happened: when connecting to Chrome CDP at {websocket_url}")
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
            await ws.send(message)
    except Exception as e:
        logger.error(e)


async def stats_thread_func():
    global connection_count
    global connection_count_max
    global shutdown

    while True:
        logger.info(f"Connection count: {connection_count} of max {connection_count_max}")
        logger.info(f"Total connections processed: {connection_count_total}")
        if connection_count > connection_count_max:
            logger.warning(f"{connection_count} of max {connection_count_max} over threshold, incoming connections will be delayed.")
        await asyncio.sleep(20)


if __name__ == '__main__':
    # Set a default logger level
    logger_level = 'DEBUG'
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
        logger.success(f"Starting puppeteer proxy on ws://{args.host}:{args.port}")
        asyncio.get_event_loop().run_forever()


    except KeyboardInterrupt:
        logger.success("Got CTRL+C/interrupt, shutting down.")
        # At this point, all child processes including Chrome should be terminated
