#!/usr/bin/env python3

"""Launch and teardown of a single Chrome instance for one proxied connection.

Chrome is started with --remote-debugging-port=0 and the resulting CDP websocket URL is read
straight off its stderr ("DevTools listening on ws://..."), which is both faster and more
reliable than polling /json/version on a port we guessed.

Everything here uses asyncio's native subprocess pipes.  The previous implementation read
Chrome's stdout/stderr via loop.run_in_executor(None, stream.readline), which parked two
default-executor threads per browser for the lifetime of the connection and exhausted the pool
(min(32, cpu_count+4) workers) well before MAX_CONCURRENT_CHROME_PROCESSES was reached.
"""

import asyncio
import os
import re
import shutil
import tempfile
import time
from asyncio.subprocess import DEVNULL, PIPE
from collections import deque
from urllib.parse import parse_qs, urlparse

import psutil
from loguru import logger

# "DevTools listening on ws://127.0.0.1:36743/devtools/browser/9cc0662e-..."
DEVTOOLS_RE = re.compile(r'DevTools listening on (ws://\S+)')

CHROME_START_TIMEOUT = float(os.getenv('CHROME_START_TIMEOUT', 25))

# How often to check whether Chrome has died. Only used for logging, so coarse is fine.
EXIT_POLL_INTERVAL = 0.25

# Chrome's stderr is noisy; keep the tail around so a startup failure can be reported usefully.
STARTUP_LOG_LINES = 50

XVFB_SCREEN_ARGS = (
    "-screen 0 1920x1080x24 -ac +extension GLX +extension RANDR +extension RENDER "
    "+extension DAMAGE +extension XINERAMA +extension MIT-SHM +extension XTEST "
    "+extension SYNC -dpi 96 -fbdir /var/tmp "
    "-fp /usr/share/fonts/X11/misc,/usr/share/fonts/X11/Type1"
)


def parse_query_args(query):
    """Split a connection query string into Chrome flags (--foo) and proxy options (bar).

    Returns (chrome_flags, options) where chrome_flags is a list of "--k=v" strings and
    options is a dict of the non-dashdash keys.
    """
    chrome_flags = []
    options = {}
    for k, v in parse_qs(urlparse(query).query).items():
        if k.startswith('--'):
            chrome_flags.append(f"{k}={v[0]}")
        else:
            options[k] = v[0]
    return chrome_flags, options


def build_chrome_args(chrome_flags, headful=False):
    """Build the Chrome command line. Returns (argv, user_data_dir_we_created_or_None)."""
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
        # UserAgentClientHint - Say no to https://www.chromium.org/updates/ua-ch/ and force sites to rely on HTTP_USER_AGENT
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
        # Port 0 = let the OS pick a free one; we read the real port back off stderr.
        "--remote-debugging-port=0",
        "about:blank",
    ]

    if not headful:
        chrome_run.append("--headless")
    else:
        # Additional anti-detection flags for headful mode
        chrome_run.extend([
            #            "--start-maximized",
            "--disable-infobars",
            "--disable-default-apps",
            "--disable-extensions-file-access-check",
            "--disable-plugins-discovery",
            "--disable-translate",
            "--disable-plugins",
            "--disable-geolocation",
        ])
        # Remove some automation-detection flags when in headful mode
        for flag in ("--disable-blink-features=AutomationControlled",
                     "--enable-blink-features=IdleDetection"):
            if flag in chrome_run:
                chrome_run.remove(flag)

    chrome_run += chrome_flags

    # Decide from the parsed flags, not a substring search of the raw query string.
    supplied = {f.split('=', 1)[0] for f in chrome_flags}

    if '--window-size' not in supplied:
        if os.getenv('SCREEN_WIDTH') and os.getenv('SCREEN_HEIGHT'):
            screen_wh_arg = f"--window-size={int(os.getenv('SCREEN_WIDTH'))},{int(os.getenv('SCREEN_HEIGHT'))}"
            logger.debug(f"No --window-size in start query, falling back to env var {screen_wh_arg}")
            chrome_run.append(screen_wh_arg)
        else:
            logger.warning("No --window-size in query, and no SCREEN_HEIGHT + SCREEN_WIDTH env vars found :-(")

    owned_user_data_dir = None
    if '--user-data-dir' not in supplied:
        owned_user_data_dir = tempfile.mkdtemp(prefix="chrome-puppeteer-proxy", dir="/tmp")
        chrome_run.append(f"--user-data-dir={owned_user_data_dir}")
        logger.debug(f"No user-data-dir in query, using {owned_user_data_dir}")

    return chrome_run, owned_user_data_dir


def _user_data_dir_of(argv):
    for a in argv:
        if a.startswith('--user-data-dir='):
            return a.split('=', 1)[1]
    return None


class ChromeStartupError(RuntimeError):
    pass


class ChromeInstance:
    """One Chrome browser, as an async context manager.

    Usage:
        async with ChromeInstance(query, conn_id) as chrome:
            ...  # chrome.devtools_url is ready
        # process tree killed, temp profile removed
    """

    def __init__(self, chrome_flags, headful=False, conn_id="unknown"):
        self.conn_id = conn_id
        self.headful = headful
        self.chrome_flags = chrome_flags

        self.proc = None
        self.devtools_url = None
        self.returncode = None
        self.exited_at = None

        self._argv = None
        self._owned_user_data_dir = None
        self._killed_by_us = False
        self._exit_reported = False
        self._tasks = []
        self._startup_log = deque(maxlen=STARTUP_LOG_LINES)

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
        return False

    async def start(self):
        loop = asyncio.get_running_loop()
        self._argv, self._owned_user_data_dir = await loop.run_in_executor(
            None, lambda: build_chrome_args(self.chrome_flags, self.headful)
        )

        argv = self._argv
        if self.headful:
            logger.debug("Using headful mode with xvfb-run (auto display allocation)")
            argv = ["xvfb-run", "-a", "-s", XVFB_SCREEN_ARGS] + argv

        logger.debug(f"WebSocket ID: {self.conn_id} - launching: {' '.join(argv)}")

        try:
            # stdout to /dev/null: Chrome puts everything we care about on stderr, and an
            # undrained pipe blocks the browser once the 64KB kernel buffer fills.
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdout=DEVNULL, stderr=PIPE
            )
        except FileNotFoundError:
            self._cleanup_profile()
            raise ChromeStartupError(
                f"Chrome binary not found at {argv[0]}, aborting!"
            )
        except Exception as e:
            self._cleanup_profile()
            raise ChromeStartupError(f"Chrome startup failed: {e}")

        try:
            self.devtools_url = await asyncio.wait_for(
                self._await_devtools_url(), timeout=CHROME_START_TIMEOUT
            )
        except asyncio.TimeoutError:
            # xvfb-run can swallow stderr; the profile dir also records the port.
            self.devtools_url = self._devtools_url_from_profile()
            if not self.devtools_url:
                await self.aclose()
                raise ChromeStartupError(
                    f"Chrome did not report a DevTools URL within {CHROME_START_TIMEOUT}s. "
                    f"Last stderr: {self._tail()}"
                )
        except ChromeStartupError:
            await self.aclose()
            raise

        logger.debug(f"WebSocket ID: {self.conn_id} - Chrome PID {self.pid} ready at {self.devtools_url}")

        self._tasks.append(asyncio.create_task(self._drain_stderr()))
        self._tasks.append(asyncio.create_task(self._watch_exit()))
        return self

    async def _await_devtools_url(self):
        """Read stderr until Chrome announces its CDP endpoint."""
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                # EOF - Chrome exited before telling us anything useful.
                rc = await self.proc.wait()
                raise ChromeStartupError(
                    f"Chrome exited with code {rc} during startup. Do you need --cap-add=SYS_ADMIN? "
                    f"Permissions OK? Disk full? Last stderr: {self._tail()}"
                )
            text = line.decode(errors='replace').rstrip()
            if not text:
                continue
            self._startup_log.append(text)
            match = DEVTOOLS_RE.search(text)
            if match:
                return match.group(1)
            logger.debug(f"WebSocket ID: {self.conn_id} Chrome stderr PID {self.pid}: {text}")

    def _devtools_url_from_profile(self):
        """Fallback: <user-data-dir>/DevToolsActivePort holds "<port>\\n<browser path>"."""
        user_data_dir = _user_data_dir_of(self._argv)
        if not user_data_dir:
            return None
        try:
            with open(os.path.join(user_data_dir, 'DevToolsActivePort')) as f:
                port = f.readline().strip()
                path = f.readline().strip()
            if port and path:
                logger.warning(f"WebSocket ID: {self.conn_id} - Recovered DevTools URL from DevToolsActivePort")
                return f"ws://127.0.0.1:{port}{path}"
        except (OSError, ValueError):
            pass
        return None

    def _tail(self):
        return " | ".join(self._startup_log) or "(nothing on stderr)"

    async def _drain_stderr(self):
        """Keep reading stderr for the connection's lifetime, or Chrome stalls on a full pipe."""
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors='replace').rstrip()
                if text:
                    logger.debug(f"WebSocket ID: {self.conn_id} Chrome stderr PID {self.pid}: {text}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"WebSocket ID: {self.conn_id} - Error draining Chrome stderr: {e}")

    async def _wait_for_exit(self, timeout=None):
        """Wait for the process to die, polling Process.returncode.

        Deliberately not proc.wait(): asyncio's subprocess transport only completes wait()
        once the process has exited AND every pipe has hit EOF. Chrome's renderer children
        inherit our stderr pipe, so a dead browser process with surviving children never
        satisfies that - and the exit goes unnoticed. Process.returncode, by contrast, is set
        by the child watcher as soon as the process is reaped.
        """
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while self.proc.returncode is None:
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(EXIT_POLL_INTERVAL)
        self._report_exit()
        return self.proc.returncode

    def _report_exit(self):
        """Log the exit exactly once, whichever waiter notices it first."""
        if self._exit_reported or self.proc is None or self.proc.returncode is None:
            return
        self._exit_reported = True
        self.returncode = self.proc.returncode
        self.exited_at = time.monotonic()
        if self._killed_by_us:
            logger.debug(f"WebSocket ID: {self.conn_id} - Chrome PID {self.pid} "
                         f"exited rc={self.returncode} (expected, proxy cleanup)")
        else:
            logger.error(
                f"WebSocket ID: {self.conn_id} - Chrome PID {self.pid} exited rc={self.returncode} "
                f"(UNEXPECTED - Chrome died on its own). Last stderr: {self._tail()}"
            )

    async def _watch_exit(self):
        """Log the moment Chrome dies, and whether we were the ones who killed it.

        Nothing used to report this, which made "Session closed. Most likely the page has been
        closed." on the client side impossible to attribute.
        """
        try:
            await self._wait_for_exit()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def settle(self, timeout=1.0):
        """Give a just-observed death a moment to be reaped before we attribute blame.

        Chrome's CDP socket drops in the same event-loop tick that the process dies, so a
        teardown summary written immediately would still see returncode=None and wrongly
        report "still running". The child watcher needs one dispatch to catch up.
        """
        if self.proc is None or self.proc.returncode is not None:
            return
        await self._wait_for_exit(timeout=timeout)

    def describe_exit(self):
        """One-line process state for the teardown summary."""
        self._report_exit()  # may have died since the last poll
        if self.returncode is None:
            return "chrome process: still running at teardown"
        why = "expected, proxy cleanup" if self._killed_by_us else "UNEXPECTED - Chrome died on its own"
        return f"chrome process: rc={self.returncode} ({why})"

    async def aclose(self):
        if self.proc is None:
            self._cleanup_profile()
            return

        self._killed_by_us = True
        logger.debug(f"WebSocket ID: {self.conn_id} Cleaning up Chrome subprocess PID {self.pid}")

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        self._kill_tree()

        try:
            rc = await self._wait_for_exit(timeout=5.0)
            if rc is None:
                logger.warning(f"WebSocket ID: {self.conn_id} - Chrome PID {self.pid} did not reap within 5s")
            else:
                self.returncode = rc
        except Exception as e:
            logger.warning(f"WebSocket ID: {self.conn_id} - Error reaping Chrome: {e}")

        self._cleanup_profile()

    def _kill_tree(self):
        """SIGKILL the browser and every renderer/GPU child it spawned."""
        try:
            parent = psutil.Process(self.proc.pid)
            procs = [parent] + parent.children(recursive=True)
            if len(procs) > 1:
                logger.debug(f"WebSocket ID: {self.conn_id} - Killing {len(procs)} Chrome processes")
            for proc in procs:
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            try:
                self.proc.kill()
            except (OSError, ProcessLookupError):
                pass  # Already gone

    def _cleanup_profile(self):
        """Remove the temp profile, but only if we were the ones who made it."""
        if not self._owned_user_data_dir:
            return
        shutil.rmtree(self._owned_user_data_dir, ignore_errors=True)
        self._owned_user_data_dir = None
