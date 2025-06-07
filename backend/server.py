#!/usr/bin/env python3
from distutils.util import strtobool

# Auto scaling websocket proxy for Chrome CDP

from distutils.util import strtobool
from http_server import start_http_server
from ports import PortSelector
from loguru import logger
import argparse
import asyncio
import json
import os
import psutil
import requests
import subprocess
import sys
import tempfile
import time
import websockets

stats = {
    'confirmed_data_received': 0,
    'connection_count': 0,
    'connection_count_total': 0,
    'dropped_threshold_reached': 0,
    'dropped_waited_too_long': 0,
    'special_counter': [],
    'chrome_start_failures': 0,
}

connection_count_max = int(os.getenv('MAX_CONCURRENT_CHROME_PROCESSES', 10))
port_selector = PortSelector()
shutdown = False
memory_use_limit_percent = int(os.getenv('HARD_MEMORY_USAGE_LIMIT_PERCENT', 90))
stats_refresh_time = int(os.getenv('STATS_REFRESH_SECONDS', 3))
STARTUP_DELAY = int(os.getenv('STARTUP_DELAY', 0))

# When we are over memory limit or hit connection_count_max
DROP_EXCESS_CONNECTIONS = strtobool(os.getenv('DROP_EXCESS_CONNECTIONS', 'False'))

# @todo Some UI where you can change loglevel on a UI?
# @todo Some way to change connection threshold via UI
# @todo Could have a configurable list of rotatable devtools endpoints?
# @todo Add `ulimit` config for max-memory-per-chrome
# @todo manage a hard 'MAX_CHROME_RUN_TIME` default 60sec
# @todo use chrome remote debug by unix pipe, instead of socket

def getBrowserArgsFromQuery(query, dashdash=True):
    if dashdash:
        extra_args = []
    else:
        extra_args = {}
    from urllib.parse import urlparse, parse_qs
    parsed_url = urlparse(query)
    for k, v in parse_qs(parsed_url.query).items():
        if dashdash:
            if k.startswith('--'):
                extra_args.append(f"{k}={v[0]}")
        else:
            if not k.startswith('--'):
                extra_args[k] = v[0]

    return extra_args


async def launch_chrome(port=19222, user_data_dir="/tmp", url_query="", headful=False):
    args = getBrowserArgsFromQuery(url_query)
    # CHROME_BIN set in Dockerfile
    chrome_location = os.getenv("CHROME_BIN", "/usr/bin/google-chrome")
    # Needs chrome 121+ or so, Defaults taken from a live Puppeteer
    # https://github.com/GoogleChrome/chrome-launcher/blob/main/docs/chrome-flags-for-tools.md
    chrome_run = [
        chrome_location,
        "--allow-pre-commit-input",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-blink-features=AutomationControlled",
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-component-update",
        "--disable-dev-shm-usage",
        # # UserAgentClientHint - Say no to https://www.chromium.org/updates/ua-ch/ and force sites to rely on HTTP_USER_AGENT
        "--disable-features=AutofillServerCommunication,Translate,AcceptCHFrame,MediaRouter,OptimizationHints,Prerender2,UserAgentClientHint",
        "--disable-gpu",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--disable-popup-blocking",
        "--disable-prompt-on-repost",
        "--disable-remote-fonts",
        "--disable-renderer-backgrounding",
        "--disable-search-engine-choice-screen",
        "--disable-sync",
        "--disable-web-security=true",
        #        "--enable-automation", # Leave out off the notification that the browser is driven by automation
        "--enable-blink-features=IdleDetection",
        "--enable-features=NetworkServiceInProcess2",
        "--enable-logging=stderr",
        "--export-tagged-pdf",
        "--force-color-profile=srgb",
        "--hide-scrollbars",
        "--log-level=2",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-first-run",
        "--no-sandbox",
        "--password-store=basic",
        "--use-mock-keychain",
        "--v1=1",
        f"--remote-debugging-port={port}"
    ]

    # Add headless flag only if not in headful mode
    if not headful:
        chrome_run.append("--headless")
    
    # Additional anti-detection flags for headful mode
    if headful:
        chrome_run.extend([
            "--start-maximized",
            "--disable-infobars",
            "--disable-default-apps",
            "--disable-extensions-file-access-check",
            "--disable-plugins-discovery",
            "--disable-translate",
            "--disable-plugins",
            "--disable-geolocation"
        ])
        # Remove some automation-detection flags when in headful mode
        automation_flags_to_remove = [
            "--disable-blink-features=AutomationControlled",
            "--enable-blink-features=IdleDetection"
        ]
        for flag in automation_flags_to_remove:
            if flag in chrome_run:
                chrome_run.remove(flag)

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
        # Run tempfile.mkdtemp in executor to prevent blocking
        loop = asyncio.get_event_loop()
        try:
            tmp_user_data_dir = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: tempfile.mkdtemp(prefix="chrome-puppeteer-proxy", dir="/tmp")),
                timeout=3.0
            )
            chrome_run.append(f"--user-data-dir={tmp_user_data_dir}")
            logger.debug(f"No user-data-dir in query, using {tmp_user_data_dir}")
        except asyncio.TimeoutError:
            logger.warning("Creating temp directory timed out, using default")
            chrome_run.append("--user-data-dir=/tmp/chrome-puppeteer-proxy-default")

    # Set up environment for headful mode (Xvfb should already be running)
    chrome_env = os.environ.copy()
    
    if headful:
        # Ensure DISPLAY is set for headful mode
        chrome_env["DISPLAY"] = os.getenv("DISPLAY", ":99")
        logger.debug(f"Using headful mode with display {chrome_env['DISPLAY']}")

    # Run Popen in executor to prevent blocking with a 20-second timeout
    try:
        # Always get a fresh event loop reference to ensure it's defined
        loop = asyncio.get_event_loop()
            
        # Wrap subprocess.Popen in a lambda for the executor
        def create_chrome_process():
            return subprocess.Popen(
                args=chrome_run, 
                shell=False, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                bufsize=1, 
                universal_newlines=True,
                env=chrome_env
            )
            
        process = await asyncio.wait_for(
            loop.run_in_executor(None, create_chrome_process),
            timeout=20.0  # 20 second timeout as requested
        )
    except asyncio.TimeoutError:
        logger.critical("Chrome process creation timed out after 20 seconds")
        raise RuntimeError("Chrome startup timed out")
    except FileNotFoundError as e:
        logger.critical(f"Chrome binary was not found at {chrome_location}, aborting!")
        raise e
    except Exception as e:
        logger.critical(f"Unexpected error launching Chrome: {str(e)}")
        raise RuntimeError(f"Chrome startup failed: {str(e)}")

    # Run poll in executor to prevent blocking
    try:
        # Always get a fresh event loop reference to ensure it's defined
        loop = asyncio.get_event_loop()
        process_poll_status = await asyncio.wait_for(
            loop.run_in_executor(None, process.poll),
            timeout=20.0
        )
    except asyncio.TimeoutError:
        logger.warning("Process poll timed out, assuming process is running")
        process_poll_status = None
        
    if process_poll_status is not None:
        # Process exited immediately, collect output
        try:
            # Always get a fresh event loop reference to ensure it's defined
            loop = asyncio.get_event_loop()
            stdout, stderr = await asyncio.wait_for(
                loop.run_in_executor(None, process.communicate),
                timeout=25.0
            )
            logger.critical(f"Chrome process did not launch cleanly code {process_poll_status} '{stderr}' '{stdout}'")
        except asyncio.TimeoutError:
            logger.critical("Chrome process output retrieval timed out")

    return process


async def close_socket(websocket: websockets.WebSocketServerProtocol = None):
    logger.debug(f"WebSocket: {websocket.id} Closing websocket to puppeteer")

    try:
        await websocket.close()

    except Exception as e:
        # Handle other exceptions
        logger.error(f"WebSocket: {websocket.id} - While closing - error: {e}")
    finally:
        # Any cleanup or additional actions you want to perform
        pass

async def stats_disconnect(time_at_start=0.0, websocket: websockets.WebSocketServerProtocol = None):
    global stats
    stats['connection_count'] -= 1

    logger.debug(
        f"Websocket {websocket.id} - Connection ended, processed in {time.time() - time_at_start:.3f}s")

async def cleanup_chrome_by_pid(chrome_process, user_data_dir="/tmp", time_at_start=0.0, websocket: websockets.WebSocketServerProtocol = None):
    import signal
    import psutil
    
    # Always get a fresh event loop reference
    loop = asyncio.get_event_loop()
    MAX_CLEANUP_TIME = 10  # Reduced time to avoid long blocks
    cleanup_start_time = time.time()
    
    try:
        logger.debug(f"WebSocket ID: {websocket.id} Cleaning up Chrome subprocess PID {chrome_process.pid}")
        
        # More direct approach - get process children first before killing
        try:
            # First try to get parent process info with timeout
            parent_process = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: psutil.Process(chrome_process.pid)),
                timeout=3.0
            )
            
            # Get children with short timeout
            try:
                children = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: parent_process.children(recursive=True)),
                    timeout=3.0
                )
                
                # Kill children processes first - collect their PIDs
                child_pids = [child.pid for child in children]
                
                if child_pids:
                    logger.debug(f"WebSocket ID: {websocket.id} - Killing {len(child_pids)} Chrome child processes")
                    
                    # Kill all children at once using a single SIGKILL in parallel
                    kill_tasks = []
                    for pid in child_pids:
                        kill_tasks.append(
                            asyncio.wait_for(
                                loop.run_in_executor(None, lambda p=pid: _kill_process_safe(p, signal.SIGKILL)),
                                timeout=1.0
                            )
                        )
                    # Wait for all kills to complete with a short timeout
                    await asyncio.wait(kill_tasks, timeout=2.0)
            except (asyncio.TimeoutError, psutil.NoSuchProcess, psutil.AccessDenied, OSError) as e:
                logger.warning(f"WebSocket ID: {websocket.id} - Error getting/killing child processes: {str(e)}")
                
            # Now kill the parent process
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _kill_process_safe(chrome_process.pid, signal.SIGTERM)),
                timeout=2.0
            )
            
            # Short wait for process to terminate
            await asyncio.sleep(0.5)
            
            # If the process is still running, use SIGKILL
            if await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _check_process_exists(chrome_process.pid)),
                timeout=1.0
            ):
                logger.debug(f"WebSocket ID: {websocket.id} - Process still exists after SIGTERM, sending SIGKILL")
                await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: _kill_process_safe(chrome_process.pid, signal.SIGKILL)),
                    timeout=1.0
                )
                
        except (asyncio.TimeoutError, psutil.NoSuchProcess, psutil.AccessDenied, OSError) as e:
            # If we can't use psutil, fall back to direct kill
            logger.warning(f"WebSocket ID: {websocket.id} - Error with psutil approach, trying direct kill: {str(e)}")
            try:
                # Try direct kill of the process itself
                chrome_process.kill()
            except OSError:
                # Process might already be gone
                pass
        
        # Final check - if process still exists, log a warning but continue
        try:
            is_still_running = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _check_process_exists(chrome_process.pid)),
                timeout=1.0
            )
            if is_still_running:
                logger.warning(f"WebSocket ID: {websocket.id} - Chrome PID {chrome_process.pid} might still be running after cleanup")
            else:
                logger.debug(f"WebSocket ID: {websocket.id} - Chrome PID {chrome_process.pid} successfully terminated")
        except asyncio.TimeoutError:
            logger.warning(f"WebSocket ID: {websocket.id} - Final process check timed out")
    except Exception as e:
        logger.error(f"WebSocket ID: {websocket.id} - Error in Chrome cleanup: {str(e)}")

    # Always ensure the socket is closed, regardless of Chrome cleanup results
    await close_socket(websocket)

def _kill_process_safe(pid, sig):
    """Helper function to kill a process safely, handling exceptions"""
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        # Process doesn't exist or we don't have permission
        return False
    
def _check_process_exists(pid):
    """Helper function to check if a process exists"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

async def _request_retry(url, num_retries=20, success_list=[200, 404], **kwargs):
    # On a healthy machine with no load, Chrome is usually fired up in 100ms
    timeout = kwargs.pop('timeout', 5)  # Default timeout of 5 seconds
    start_time = time.time()
    websocket_id = kwargs.pop('websocket_id', 'unknown')
    
    for retry_count in range(num_retries):
        # Check if we've spent too much time already (overall timeout)
        if time.time() - start_time > 60:  # 1-minute overall timeout
            logger.error(f"WebSocket ID: {websocket_id} - _request_retry exceeded overall timeout (60s) after {retry_count} attempts for {url}")
            raise asyncio.TimeoutError("Overall retry timeout exceeded")
            
        # This sleep is crucial for Chrome CDP interface stability under high loads
        # Use a shorter initial sleep and gradually increase if needed
        sleep_time = min(1.0 + (retry_count * 0.2), 3.0)  # Start at 1s, max 3s
        await asyncio.sleep(sleep_time)

        try:
            # Use a separate thread to handle the HTTP request
            loop = asyncio.get_event_loop()
            # Gradually increase per-request timeout on retries
            current_timeout = min(timeout + (retry_count * 0.5), 15)  # Start at timeout, max 15s
            
            logger.debug(f"WebSocket ID: {websocket_id} - _request_retry attempt {retry_count+1}/{num_retries} for {url} (timeout={current_timeout:.1f}s)")
            
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: requests.get(url, timeout=current_timeout, **kwargs)
                ),
                timeout=current_timeout + 1  # Add 1 second buffer for executor overhead
            )
            
            if response.status_code in success_list:
                elapsed = time.time() - start_time
                logger.debug(f"WebSocket ID: {websocket_id} - _request_retry succeeded after {retry_count+1} attempts in {elapsed:.2f}s")
                return response
                
            logger.warning(f"WebSocket ID: {websocket_id} - Unexpected status code {response.status_code} from Chrome at {url}, retrying...")
            
        except (requests.exceptions.ConnectionError, 
                requests.exceptions.Timeout):
            logger.warning(f"WebSocket ID: {websocket_id} - Network error connecting to Chrome at {url}, retrying (attempt {retry_count+1}/{num_retries})...")
            continue
        except asyncio.TimeoutError:
            logger.warning(f"WebSocket ID: {websocket_id} - Request timed out after {current_timeout+1:.1f}s connecting to Chrome at {url}, retrying (attempt {retry_count+1}/{num_retries})...")
            continue
        except Exception as e:
            logger.warning(f"WebSocket ID: {websocket_id} - Unexpected error connecting to Chrome: {str(e)}, retrying (attempt {retry_count+1}/{num_retries})...")
            continue

    elapsed = time.time() - start_time
    logger.error(f"WebSocket ID: {websocket_id} - _request_retry failed after {num_retries} attempts over {elapsed:.2f}s for {url}")
    raise requests.exceptions.ConnectionError(f"Failed to connect to {url} after {num_retries} attempts")


async def debug_log_line(logfile_path, text):
    if logfile_path is None:
        return
    
    try:
        # Run file I/O in executor to avoid blocking the event loop
        # Always get a fresh event loop reference
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: _write_log_line(logfile_path, text)
            ),
            timeout=1.0  # Timeout after 1 second
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
    '''Called whenever a new connection is made to the server, Incoming connection, connect to CDP and start proxying'''
    global stats
    global connection_count_max

    now = time.time()
    closed = asyncio.ensure_future(websocket.wait_closed())
    closed.add_done_callback(lambda task: asyncio.ensure_future(stats_disconnect(time_at_start=now, websocket=websocket)))

    stats['connection_count_total'] += 1
    logger.debug(
        f"WebSocket ID: {websocket.id} Got new incoming connection ID from {websocket.remote_address[0]}:{websocket.remote_address[1]} ({path})")

    if stats['connection_count'] > connection_count_max:
        logger.warning(
            f"WebSocket ID: {websocket.id} - Throttling/waiting, max connection limit reached {stats['connection_count']} of max {connection_count_max}  ({time.time() - now:.1f}s)")

    # Memory monitoring removed

    # Connections that joined but had to wait a long time before being processed
    if DROP_EXCESS_CONNECTIONS:
        while stats['connection_count'] > connection_count_max:
            await asyncio.sleep(3)
            if time.time() - now > 120:
                logger.critical(
                    f"WebSocket ID: {websocket.id} - Waiting for existing connection count to drop took too long! dropping connection. ({time.time() - now:.1f}s)")
                await close_socket(websocket)
                stats['dropped_waited_too_long'] += 1
                return

    stats['connection_count'] += 1

    now_before_chrome_launch = time.time()

    port = next(port_selector)
    
    # Check for headful mode from query string or environment
    args = getBrowserArgsFromQuery(path, dashdash=False)
    headful_mode = (
        args.get('headful', '').lower() in ['true', '1'] or 
        os.getenv('CHROME_HEADFUL', 'false').lower() in ['true', '1']
    )
    
    try:
        # Make sure to handle asyncio properly
        chrome_process = await launch_chrome(port=port, url_query=path, headful=headful_mode)
    except (asyncio.TimeoutError, RuntimeError) as e:
        logger.critical(f"WebSocket ID: {websocket.id} - Chrome launch failed: {str(e)}")
        stats['chrome_start_failures'] += 1
        await close_socket(websocket)
        stats['connection_count'] -= 1
        return
    except Exception as e:
        logger.critical(f"WebSocket ID: {websocket.id} - Unexpected error during Chrome launch: {str(e)}")
        stats['chrome_start_failures'] += 1
        await close_socket(websocket)
        stats['connection_count'] -= 1
        return

    closed.add_done_callback(lambda task: asyncio.ensure_future(
        cleanup_chrome_by_pid(chrome_process=chrome_process, user_data_dir='@todo', time_at_start=now, websocket=websocket))
                             )

    chrome_json_info_url = f"http://localhost:{port}/json/version"
    # https://chromedevtools.github.io/devtools-protocol/
    try:
        # Define the retry strategy with websocket ID for better logging
        response = await _request_retry(chrome_json_info_url, websocket_id=websocket.id)
        if not response.status_code == 200:
            logger.critical(f"WebSocket ID: {websocket.id} - Chrome did not report the correct list of interfaces at {chrome_json_info_url}, aborting :(")
            stats['chrome_start_failures'] += 1
            await close_socket(websocket)
            return
    except requests.exceptions.ConnectionError as e:
        # Instead of trying to analyse the output in a non-blocking way, we can assume that if we cant connect that something went wrong.
        logger.critical(f"WebSocket ID: {websocket.id} -Uhoh! Looks like Chrome did not start! do you need --cap-add=SYS_ADMIN added to start this container? permissions are OK? Disk is full?")
        logger.critical(f"WebSocket ID: {websocket.id} -While trying to connect to {chrome_json_info_url} - {str(e)}, Closing attempted chrome process")
        # Increment the chrome_start_failures counter
        stats['chrome_start_failures'] += 1
        # Use non-blocking kill and communicate
        chrome_process.kill()
        
        # Use run_in_executor to handle communicate asynchronously
        loop = asyncio.get_event_loop()
        try:
            stdout, stderr = await asyncio.wait_for(
                loop.run_in_executor(None, chrome_process.communicate),
                timeout=10.0
            )
            logger.critical(f"WebSocket ID: {websocket.id} - Chrome debug output STDERR: {stderr} STDOUT: {stdout}")
        except asyncio.TimeoutError:
            logger.warning(f"WebSocket ID: {websocket.id} - Timed out getting Chrome debug output")
        
        await close_socket(websocket)
        return

    # On exception, flush and print debug

    logger.trace(f"WebSocket ID: {websocket.id} time to launch browser {time.time() - now_before_chrome_launch:.3f}s ")

    chrome_websocket_url = response.json().get("webSocketDebuggerUrl")
    logger.debug(f"WebSocket ID: {websocket.id} proxying to local Chrome instance via CDP {chrome_websocket_url}")

    args = getBrowserArgsFromQuery(path, dashdash=False)
    debug_log = args.get('log-cdp') if args.get('log-cdp') and strtobool(os.getenv('ALLOW_CDP_LOG', 'False')) else None

    if debug_log and os.path.isfile(debug_log):
        os.unlink(debug_log)


    # 10mb, keep in mind theres screenshots.
    try:
        await debug_log_line(text=f"Attempting connection to {chrome_websocket_url}", logfile_path=debug_log)
        async with websockets.connect(chrome_websocket_url, max_size=None, max_queue=None) as ws:
            await debug_log_line(text=f"Connected to {chrome_websocket_url}", logfile_path=debug_log)
            taskA = asyncio.create_task(hereToChromeCDP(puppeteer_ws=ws, chrome_websocket=websocket, debug_log=debug_log))
            taskB = asyncio.create_task(puppeteerToHere(puppeteer_ws=ws, chrome_websocket=websocket, debug_log=debug_log))
            await taskA
            await taskB
    except Exception as e:
        # Kill chrome first to ensure it stops
        chrome_process.kill()
        
        # Use non-blocking approach to get debug output
        loop = asyncio.get_event_loop()
        try:
            stdout, stderr = await asyncio.wait_for(
                loop.run_in_executor(None, chrome_process.communicate),
                timeout=10.0
            )
            logger.critical(f"WebSocket ID: {websocket.id} - Chrome debug output STDERR: {stderr} STDOUT: {stdout}")
        except asyncio.TimeoutError:
            logger.warning(f"WebSocket ID: {websocket.id} - Timed out getting Chrome debug output")
            
        txt = f"Something bad happened when connecting to Chrome CDP at {chrome_websocket_url} (After getting good Chrome CDP URL from {chrome_json_info_url}) - '{str(e)}'"
        logger.error(f"WebSocket ID: {websocket.id} - "+txt)
        await debug_log_line(text="Exception: " + txt, logfile_path=debug_log)



    logger.success(f"Websocket {websocket.id} - Connection done!")
    await debug_log_line(text=f"Websocket {websocket.id} - Connection done!", logfile_path=debug_log)

async def hereToChromeCDP(puppeteer_ws, chrome_websocket, debug_log=None):
    # Buffer size - how many characters to process at once, to avoid blocking on large messages
    buffer_size = 8192
    
    try:
        async for message in puppeteer_ws:
            if debug_log:
                await debug_log_line(text=f"Chrome -> Puppeteer: {message[:1000]}", logfile_path=debug_log)
            logger.trace(message[:1000])

            # If it has the special counter, record it, this is handy for recording that the browser session actually sent a shutdown/ "IM DONE" message
            if 'SOCKPUPPET.specialcounter' in message[:200] and puppeteer_ws.id not in stats['special_counter']:
                stats['special_counter'].append(puppeteer_ws.id)

            # Large message handling - break it into chunks if needed
            if len(message) > buffer_size:
                # Log when processing large messages
                logger.debug(f"WebSocket ID: {puppeteer_ws.id} - Processing large message of size {len(message)} bytes")
                
                # Process the message in executor to avoid blocking event loop with large JSON processing
                try:
                    await asyncio.wait_for(
                        chrome_websocket.send(message),
                        timeout=25.0  # Add timeout for large message sending
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"WebSocket ID: {puppeteer_ws.id} - Timeout sending large message of size {len(message)}")
            else:
                await chrome_websocket.send(message)
    except websockets.exceptions.ConnectionClosed:
        logger.debug(f"WebSocket ID: {puppeteer_ws.id} - Connection closed normally while sending")
    except Exception as e:
        logger.error(f"WebSocket ID: {puppeteer_ws.id} - Error in hereToChromeCDP: {str(e)}")


async def puppeteerToHere(puppeteer_ws, chrome_websocket, debug_log=None):

    try:
        async for message in chrome_websocket:
            if debug_log:
                await debug_log_line(text=f"Puppeteer -> Chrome: {message[:1000]}", logfile_path=debug_log)

            logger.trace(message[:1000])
            
            # For debugging navigation events
            if message.startswith("{") and message.endswith("}") and 'Page.navigate' in message:
                # Run JSON parsing in executor for larger messages
                if len(message) > 1000:
                    try:
                        loop = asyncio.get_event_loop()
                        m = await asyncio.wait_for(
                            loop.run_in_executor(None, lambda: json.loads(message)),
                            timeout=5.0
                        )
                        # Print out some debug so we know roughly whats going on
                        logger.debug(f"{chrome_websocket.id} Page.navigate request called to '{m['params']['url']}'")
                    except (asyncio.TimeoutError, json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"Error parsing navigation event: {str(e)}")
                else:
                    # For smaller messages, parse directly
                    try:
                        m = json.loads(message)
                        logger.debug(f"{chrome_websocket.id} Page.navigate request called to '{m['params']['url']}'")
                    except Exception as e:
                        pass

            await puppeteer_ws.send(message)
                
    except websockets.exceptions.ConnectionClosed:
        logger.debug(f"WebSocket ID: {chrome_websocket.id} - Connection closed normally while receiving")
    except Exception as e:
        logger.error(f"WebSocket ID: {chrome_websocket.id} - Error in puppeteerToHere: {str(e)}")


async def stats_thread_func():
    global connection_count_max
    global shutdown
    
    while True:
        try:
            # Log connection stats only
            logger.info(f"Connections: Active count {stats['connection_count']} of max {connection_count_max}, Total processed: {stats['connection_count_total']}.")
            if stats['connection_count'] > connection_count_max:
                logger.warning(f"{stats['connection_count']} of max {connection_count_max} over threshold, incoming connections will be delayed.")
            
            # Collect process counts in a non-blocking way
            try:
                loop = asyncio.get_event_loop()
                parent_task = asyncio.create_task(
                    asyncio.wait_for(
                        loop.run_in_executor(None, lambda: psutil.Process(os.getpid())),
                        timeout=1.0
                    )
                )
                parent = await parent_task
                
                child_task = asyncio.create_task(
                    asyncio.wait_for(
                        loop.run_in_executor(None, lambda: len(parent.children(recursive=False))),
                        timeout=1.0
                    )
                )
                child_count = await child_task
                
                logger.info(f"Process info: {child_count} child processes")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"Process count check failed: {str(e) if isinstance(e, Exception) else 'timeout'}")
        
        except Exception as e:
            logger.error(f"Unexpected error in stats thread: {str(e)}")
        
        # Always wait before next iteration, regardless of any errors
        try:
            await asyncio.sleep(stats_refresh_time)
        except Exception as e:
            # Defensive coding - sleep should never fail, but just in case
            logger.error(f"Error in stats sleep: {str(e)}")
            # Emergency fallback sleep to avoid tight loop
            time.sleep(stats_refresh_time)


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

    start_server = websockets.serve(launchPuppeteerChromeProxy, args.host, args.port)
    http_server = start_http_server(host=args.host, port=args.sport, stats=stats)

    asyncio.get_event_loop().run_until_complete(asyncio.gather(start_server, http_server))

    poll = asyncio.get_event_loop().create_task(stats_thread_func())

    try:
        chrome_path = os.getenv("CHROME_BIN", "/usr/bin/google-chrome")
        logger.success(f"Starting Chrome proxy, Listening on ws://{args.host}:{args.port} -> {chrome_path}")
        asyncio.get_event_loop().run_forever()


    except KeyboardInterrupt:
        logger.success("Got CTRL+C/interrupt, shutting down.")
        # At this point, all child processes including Chrome should be terminated
