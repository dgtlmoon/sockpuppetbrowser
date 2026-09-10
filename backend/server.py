#!/usr/bin/env python3

# Auto scaling websocket proxy for Chrome CDP


def strtobool(val):
    """Convert a string representation of truth to true (1) or false (0).

    True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
    are 'n', 'no', 'f', 'false', 'off', and '0'.
    """
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    elif val in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    else:
        raise ValueError(f"invalid truth value {val!r}")


import argparse
import asyncio
import os
import signal
import sys
import time

import websockets
from loguru import logger

from cdp_trace import CDPTracer
from chrome import (DEFAULT_CHROME_BIN, ChromeInstance, ChromeStartupError,
                    parse_query_args, sweep_orphans)
from http_server import start_http_server

stats = {
    'confirmed_data_received': 0,
    'connection_count': 0,
    'connection_count_total': 0,
    'dropped_threshold_reached': 0,
    'dropped_waited_too_long': 0,
    'special_counter': 0,
    'chrome_start_failures': 0,
    'cdp_connect_failures': 0,
    'quiet_sessions': 0,
}

connection_count_max = int(os.getenv('MAX_CONCURRENT_CHROME_PROCESSES', 10))
stats_refresh_time = int(os.getenv('STATS_REFRESH_SECONDS', 3))
STARTUP_DELAY = int(os.getenv('STARTUP_DELAY', 0))

# When at capacity: drop new connections immediately (True) or queue them (False).
DROP_EXCESS_CONNECTIONS = strtobool(os.getenv('DROP_EXCESS_CONNECTIONS', 'False'))
QUEUE_TIMEOUT = int(os.getenv('CONNECTION_QUEUE_TIMEOUT', 120))

# Keepalive. A too-eager ping timeout closes healthy connections whenever Chrome (or this
# event loop) stalls, which surfaces client-side as "Session closed. Most likely the page has
# been closed." Both sides are configurable so it can be tuned without a code change.
WS_PING_INTERVAL = int(os.getenv('WS_PING_INTERVAL', 20))
WS_PING_TIMEOUT = int(os.getenv('WS_PING_TIMEOUT', 20))

# How long to wait for a client's closing handshake. websockets defaults to 10s, and spends it
# twice (once waiting for the peer's close frame, once for the TCP close) - a long time to sit
# on a connection that is already finished. pyppeteer's browser.close() drops its socket
# without a close frame, so this is the normal path, not an edge case.
WS_CLOSE_TIMEOUT = int(os.getenv('WS_CLOSE_TIMEOUT', 5))

# A session that relays less than this in total did no real work - a single Target.getTargets
# round trip is already more than this - so it is counted separately from a failure. Set to 0
# to stop counting.
CDP_QUIET_SESSION_BYTES = int(os.getenv('CDP_QUIET_SESSION_BYTES', 100))

# Bounded so a slow reader applies TCP backpressure instead of buffering screenshots in RAM.
WS_MAX_QUEUE = int(os.getenv('WS_MAX_QUEUE', 128))

# Created inside the event loop; asyncio.Semaphore must not be built at import time.
connection_semaphore = None

# Live browsers, so a SIGTERM can take them down with us.
live_chrome = set()

# @todo Some UI where you can change loglevel on a UI?
# @todo Some way to change connection threshold via UI
# @todo Could have a configurable list of rotatable devtools endpoints?
# @todo Add `ulimit` config for max-memory-per-chrome
# @todo manage a hard 'MAX_CHROME_RUN_TIME` default 60sec
# @todo use chrome remote debug by unix pipe, instead of socket


async def close_socket(websocket):
    logger.debug(f"WebSocket: {websocket.id} Closing websocket to puppeteer")
    try:
        # Hard bound on top of close_timeout: a peer that never answers must not keep the
        # handler alive, so drop the connection on the floor instead of waiting for it.
        await asyncio.wait_for(websocket.close(), timeout=WS_CLOSE_TIMEOUT * 2 + 1)
    except asyncio.TimeoutError:
        logger.debug(f"WebSocket: {websocket.id} - Client never finished the closing "
                     f"handshake, aborting the connection")
        try:
            websocket.transport.abort()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"WebSocket: {websocket.id} - While closing - error: {e}")


async def acquire_slot(websocket):
    """Take a concurrency slot, or refuse the connection. True if we got one."""
    peer = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"

    if connection_semaphore.locked() and DROP_EXCESS_CONNECTIONS:
        logger.warning(
            f"WebSocket ID: {websocket.id} - DROPPING connection from {peer} - at capacity "
            f"({stats['connection_count']} of max {connection_count_max} active), "
            f"DROP_EXCESS_CONNECTIONS is enabled")
        stats['dropped_threshold_reached'] += 1
        await close_socket(websocket)
        return False

    waited_from = time.time()
    try:
        await asyncio.wait_for(connection_semaphore.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.critical(
            f"WebSocket ID: {websocket.id} - DROPPING connection from {peer} - waited "
            f"{QUEUE_TIMEOUT}s for a free slot ({stats['connection_count']} of max "
            f"{connection_count_max} active) and gave up")
        stats['dropped_waited_too_long'] += 1
        await close_socket(websocket)
        return False

    waited = time.time() - waited_from
    if waited > 1:
        logger.info(f"WebSocket ID: {websocket.id} - Got a connection slot after waiting {waited:.1f}s")
    return True


async def debug_log_line(logfile_path, text):
    if logfile_path is None:
        return
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _write_log_line(logfile_path, text)),
            timeout=1.0,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Log file write timed out for {logfile_path}")
    except Exception as e:
        logger.warning(f"Error writing to log file {logfile_path}: {str(e)}")


def _write_log_line(logfile_path, text):
    """Synchronous helper for file writing operation"""
    with open(logfile_path, 'a') as f:
        f.write(f"{time.time()} - {text}\n")


async def launchPuppeteerChromeProxy(websocket, path):
    """Called whenever a new connection is made to the server, Incoming connection, connect to CDP and start proxying"""
    now = time.time()
    stats['connection_count_total'] += 1
    logger.debug(
        f"WebSocket ID: {websocket.id} Got new incoming connection ID from "
        f"{websocket.remote_address[0]}:{websocket.remote_address[1]} ({path})")

    if not await acquire_slot(websocket):
        return

    stats['connection_count'] += 1
    chrome_flags, options = parse_query_args(path)
    headful = (options.get('headful', '').lower() in ('true', '1')
               or os.getenv('CHROME_HEADFUL', 'false').lower() in ('true', '1'))

    debug_log = options.get('log-cdp') if options.get('log-cdp') and strtobool(os.getenv('ALLOW_CDP_LOG', 'False')) else None
    if debug_log and os.path.isfile(debug_log):
        os.unlink(debug_log)

    tracer = CDPTracer(websocket.id)
    chrome = ChromeInstance(chrome_flags=chrome_flags, headful=headful, conn_id=websocket.id)

    try:
        now_before_chrome_launch = time.time()
        try:
            await chrome.start()
        except ChromeStartupError as e:
            logger.critical(f"WebSocket ID: {websocket.id} - Chrome launch failed: {e}")
            stats['chrome_start_failures'] += 1
            await close_socket(websocket)
            return

        live_chrome.add(chrome)
        logger.trace(
            f"WebSocket ID: {websocket.id} time to launch browser {time.time() - now_before_chrome_launch:.3f}s ")
        logger.debug(
            f"WebSocket ID: {websocket.id} proxying to local Chrome instance via CDP {chrome.devtools_url}")

        cdp_connected = False
        try:
            await debug_log_line(text=f"Attempting connection to {chrome.devtools_url}", logfile_path=debug_log)
            async with websockets.connect(chrome.devtools_url,
                                          max_size=None,
                                          max_queue=WS_MAX_QUEUE,
                                          ping_interval=WS_PING_INTERVAL,
                                          ping_timeout=WS_PING_TIMEOUT) as chrome_ws:
                cdp_connected = True
                await debug_log_line(text=f"Connected to {chrome.devtools_url}", logfile_path=debug_log)
                await relay(client_ws=websocket, chrome_ws=chrome_ws, tracer=tracer, debug_log=debug_log)
        except Exception as e:
            # Only a failure to attach counts: Chrome launched and announced an endpoint, then
            # was gone or unreachable by the time we dialled it. An error raised once the relay
            # is running is a different animal and the teardown summary covers it.
            if not cdp_connected:
                stats['cdp_connect_failures'] += 1
            txt = (f"Something bad happened when connecting to Chrome CDP at {chrome.devtools_url} "
                   f"- '{str(e)}'")
            logger.error(f"WebSocket ID: {websocket.id} - " + txt)
            await debug_log_line(text="Exception: " + txt, logfile_path=debug_log)

        if cdp_connected and CDP_QUIET_SESSION_BYTES and tracer.quiet_session(CDP_QUIET_SESSION_BYTES):
            stats['quiet_sessions'] += 1
            logger.warning(
                f"WebSocket ID: {websocket.id} - Connected to Chrome but only relayed "
                f"{tracer.bytes_relayed} bytes; the client did nothing with the browser")
    finally:
        # If Chrome's side dropped first, wait briefly for the process exit to be observed so
        # the summary can say whether Chrome died or merely closed its socket.
        if tracer.closed_first == 'chrome':
            await chrome.settle(timeout=1.0)
        tracer.log_teardown(chrome=chrome)
        # aclose() first, then drop it: while it is still in live_chrome the temp-dir sweep
        # knows to leave its scratch dir alone.
        await chrome.aclose()
        live_chrome.discard(chrome)
        # Give the slot back now that the browser is gone. Closing the client socket can take
        # seconds if the peer walked away without a close frame (pyppeteer's browser.close()
        # does exactly that), and nothing about that wait needs to occupy a slot.
        connection_semaphore.release()
        stats['connection_count'] -= 1
        if tracer.saw_special_counter:
            stats['special_counter'] += 1
        await close_socket(websocket)
        logger.debug(f"Websocket {websocket.id} - Connection ended, processed in {time.time() - now:.3f}s")

    logger.success(f"Websocket {websocket.id} - Connection done!")
    await debug_log_line(text=f"Websocket {websocket.id} - Connection done!", logfile_path=debug_log)


async def relay(client_ws, chrome_ws, tracer, debug_log=None):
    """Pump messages both ways until either side hangs up, then stop immediately.

    Both directions are cancelled as soon as one finishes. Awaiting them in sequence meant that
    when Chrome went away first the proxy sat waiting on the client, holding a Chrome process
    and a concurrency slot until the client eventually noticed.
    """
    conn_id = client_ws.id
    to_chrome = asyncio.create_task(
        pump(client_ws, chrome_ws, tracer, 'client', conn_id, debug_log, "Puppeteer -> Chrome"))
    to_client = asyncio.create_task(
        pump(chrome_ws, client_ws, tracer, 'chrome', conn_id, debug_log, "Chrome -> Puppeteer"))

    done, pending = await asyncio.wait({to_chrome, to_client}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    # The cancelled direction never reaches its own except/else, so read the socket state
    # directly - otherwise only ever hearing from the winner hides half the story.
    for side, sock in (('client', client_ws), ('chrome', chrome_ws)):
        if sock.close_code is not None:
            tracer.note_close(side, code=sock.close_code, reason=sock.close_reason)

    for task in done:
        exc = task.exception()
        if exc:
            logger.error(f"WebSocket ID: {conn_id} - Relay error: {exc}")


async def pump(source, dest, tracer, side, conn_id, debug_log, label):
    """Forward every message from source to dest, tracing as it goes.

    conn_id is always the incoming connection's id - the Chrome-side socket has a UUID of its
    own, and logging that instead makes the two halves of one session look unrelated.
    """
    inspect = tracer.on_client_message if side == 'client' else tracer.on_chrome_message
    try:
        async for message in source:
            if debug_log:
                await debug_log_line(text=f"{label}: {message[:1000]}", logfile_path=debug_log)
            inspect(message)
            await dest.send(message)
    except websockets.exceptions.ConnectionClosed as e:
        tracer.note_close(side, code=e.code, reason=e.reason)
        logger.debug(f"WebSocket ID: {conn_id} - {side} side closed the connection "
                     f"(code={e.code} reason='{e.reason or ''}')")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        tracer.note_close(side, reason=str(e))
        logger.error(f"WebSocket ID: {conn_id} - Error pumping {label}: {str(e)}")
    else:
        tracer.note_close(side, code=getattr(source, 'close_code', None))


async def stats_thread_func():
    import psutil

    while True:
        try:
            logger.info(
                f"Connections: Active count {stats['connection_count']} of max {connection_count_max}, "
                f"Total processed: {stats['connection_count_total']}.")

            loop = asyncio.get_running_loop()
            parent = psutil.Process(os.getpid())
            child_count = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: len(parent.children(recursive=False))),
                timeout=1.0,
            )
            logger.info(f"Process info: {child_count} child processes")

            # Chrome only removes its own temp dirs on a graceful exit and we SIGKILL, so
            # mop up anything a crashed proxy (or a Chrome we didn't launch) left behind.
            # Scratch dirs of live browsers are excluded by path, everything else has to
            # prove itself dead - see sweep_orphans().
            in_use = {c.temp_dir for c in live_chrome}
            await loop.run_in_executor(None, lambda: sweep_orphans(exclude=in_use))
        except asyncio.TimeoutError:
            logger.warning("Process count check failed: timeout")
        except Exception as e:
            logger.error(f"Unexpected error in stats thread: {str(e)}")

        await asyncio.sleep(stats_refresh_time)


async def shutdown_all_chrome():
    """Kill any browsers still running so we don't orphan them on exit."""
    if not live_chrome:
        return
    logger.warning(f"Shutting down {len(live_chrome)} live Chrome instance(s)...")
    await asyncio.gather(*(c.aclose() for c in list(live_chrome)), return_exceptions=True)
    live_chrome.clear()


async def main(args):
    global connection_semaphore
    connection_semaphore = asyncio.Semaphore(connection_count_max)

    stop = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(
                sig, lambda: stop.done() or stop.set_result(None))
        except NotImplementedError:
            pass  # Not available on all platforms

    # Nothing of ours is running yet, so anything still lying around is an orphan. Called
    # inline rather than in an executor: it is quick, nothing is competing with it yet, and
    # this is the first thread the process would ask for - a container that cannot spawn one
    # (an old seccomp profile blocking clone3, a pids limit) would fail here instead of
    # somewhere that says what is actually wrong.
    try:
        sweep_orphans(min_age=0)
    except Exception as e:
        logger.warning(f"Startup sweep of orphaned temp dirs failed, continuing anyway: {e}")

    await start_http_server(host=args.host, port=args.sport, stats=stats)

    # max_size=None to match the Chrome side; the 1MiB default silently killed connections
    # carrying large Runtime.evaluate / Input.insertText payloads.
    async with websockets.serve(launchPuppeteerChromeProxy, args.host, args.port,
                                max_size=None,
                                max_queue=WS_MAX_QUEUE,
                                ping_interval=WS_PING_INTERVAL,
                                ping_timeout=WS_PING_TIMEOUT,
                                close_timeout=WS_CLOSE_TIMEOUT):
        chrome_path = os.getenv("CHROME_BIN", DEFAULT_CHROME_BIN)
        logger.success(f"Starting Chrome proxy, Listening on ws://{args.host}:{args.port} -> {chrome_path}")
        poll = asyncio.create_task(stats_thread_func())
        try:
            await stop
        finally:
            logger.success("Shutting down.")
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
            await shutdown_all_chrome()


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
                        default=3000, type=int)
    parser.add_argument('--sport', help='Port to bind to for http statistics /stats request.',
                        default=8080, type=int)

    args = parser.parse_args()

    if STARTUP_DELAY:
        logger.info(f"Start-up delay {STARTUP_DELAY} seconds...")
        time.sleep(STARTUP_DELAY)

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        logger.success("Got CTRL+C/interrupt, shutting down.")
