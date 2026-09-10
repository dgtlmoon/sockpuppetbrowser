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
import glob
import os
import re
import shutil
import socket
import tempfile
import time
from asyncio.subprocess import DEVNULL, PIPE
from collections import deque
from urllib.parse import parse_qs, urlparse

import psutil
from loguru import logger

# "DevTools listening on ws://127.0.0.1:36743/devtools/browser/9cc0662e-..."
DEVTOOLS_RE = re.compile(r'DevTools listening on (ws://\S+)')

# The display argument in an Xvfb command line, e.g. "Xvfb :100 -screen 0 1920x1080x24 ...".
XVFB_DISPLAY_RE = re.compile(r'^:(\d+)$')

# X11 hardcodes both of these, TMPDIR has no say in it.
X11_LOCK = '/tmp/.X{display}-lock'
X11_SOCKET = '/tmp/.X11-unix/X{display}'

CHROME_START_TIMEOUT = float(os.getenv('CHROME_START_TIMEOUT', 25))

# Where the per-connection scratch dirs go. In the docker image this is usually a tmpfs/ramdisk.
TEMP_ROOT = os.getenv('SOCKPUPPET_TEMP_ROOT', '/tmp')

# Prefix of the one dir we create per browser. Both the profile and Chrome's own TMPDIR live
# inside it, so a single rmtree at teardown gets everything Chrome put in temp.
TEMP_DIR_PREFIX = 'chrome-puppeteer-proxy'

# Chrome's ProcessSingleton names its socket dir "<product>.XXXXXX" (base::ScopedTempDir +
# base::TempFileName); the product part depends on the build and the leading dot on the
# version. Only used by the orphan sweep - browsers we launch get a private TMPDIR instead.
SINGLETON_DIR_GLOBS = (
    'org.chromium.Chromium.*', '.org.chromium.Chromium.*',
    'com.google.Chrome.*', '.com.google.Chrome.*',
)

# Don't sweep anything younger than this: a browser needs a moment to create its socket, and a
# dir with no socket in it yet must not be mistaken for an orphan.
SWEEP_MIN_AGE = float(os.getenv('SOCKPUPPET_SWEEP_MIN_AGE', 300))

# How often to check whether Chrome has died. Only used for logging, so coarse is fine.
EXIT_POLL_INTERVAL = 0.25

# Chrome's stderr is noisy; keep the tail around so a startup failure can be reported usefully.
STARTUP_LOG_LINES = 50

# Lines Chrome always emits in a container and which never indicate a problem. Keeping them
# out of the kept tail matters: they are the FIRST thing Chrome prints, so otherwise they are
# what gets quoted back as "Last stderr" when something actually goes wrong.
BENIGN_STDERR = (
    "Failed to connect to the bus",
    "dbus_bus_get_private",
    "Floating point exception",   # from the GPU probe on headless hosts, harmless
    "vkCreateInstance",           # no Vulkan driver in the container
    "Failed to load libEGL",
    "Cloud management controller",  # CBCM not enabled, logged at ERROR on every startup
)


def _is_benign(line):
    return any(marker in line for marker in BENIGN_STDERR)

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
    """Build the Chrome command line. Returns (argv, owned_temp_dir)."""
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

    # One scratch dir per browser. It doubles as Chrome's TMPDIR (see ChromeInstance.start),
    # which is what keeps /tmp clean: Chrome's ProcessSingleton creates
    # <TMPDIR>/org.chromium.Chromium.XXXXXX to hold the SingletonSocket that the profile
    # symlinks to, and only removes it on a graceful shutdown. We SIGKILL the tree at teardown,
    # so that dir used to leak - one per connection, forever. Anything Chrome writes to temp
    # now lands in here (including the shared-memory files --disable-dev-shm-usage sends to
    # temp) and goes away with the rmtree in _cleanup_temp_dir().
    owned_temp_dir = tempfile.mkdtemp(prefix=TEMP_DIR_PREFIX, dir=TEMP_ROOT)

    if '--user-data-dir' not in supplied:
        # Kept short: the singleton socket path has to fit in sockaddr_un (108 bytes).
        user_data_dir = os.path.join(owned_temp_dir, 'profile')
        os.mkdir(user_data_dir)
        chrome_run.append(f"--user-data-dir={user_data_dir}")
        logger.debug(f"No user-data-dir in query, using {user_data_dir}")

    return chrome_run, owned_temp_dir


def _user_data_dir_of(argv):
    for a in argv:
        if a.startswith('--user-data-dir='):
            return a.split('=', 1)[1]
    return None


def _singleton_socket_is_live(path):
    """Is a Chrome still listening on the SingletonSocket inside this dir?

    Same test Chrome uses on a profile it finds already locked: a running browser accepts the
    connection, a dead one's socket is refused. The socket is at <dir>/SingletonSocket for a
    bare singleton dir, or one level down for a scratch dir of ours (which is the browser's
    TMPDIR, so the singleton dir sits inside it). Anything we cannot classify counts as live,
    so a sweep never races a browser that is still working.
    """
    sockets = glob.glob(os.path.join(path, 'SingletonSocket'))
    sockets += glob.glob(os.path.join(path, '*', 'SingletonSocket'))
    for sock_path in sockets:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(0.25)
            s.connect(sock_path)
            return True
        except (ConnectionRefusedError, FileNotFoundError):
            continue
        except OSError:
            return True  # permissions, ENOTSOCK, timeout - don't touch what we can't judge
        finally:
            s.close()
    return False


def sweep_orphan_temp_dirs(exclude=(), min_age=SWEEP_MIN_AGE, temp_root=TEMP_ROOT):
    """Delete Chrome temp dirs whose browser is gone. Returns the number removed.

    Two kinds get left behind:
      * scratch dirs of ours (TEMP_DIR_PREFIX*) from a proxy that was killed before teardown,
      * bare singleton dirs (org.chromium.Chromium.*) from a Chrome that had no private TMPDIR,
        i.e. one started before this cleanup existed, or one we did not launch.

    Only dirs that are older than min_age, not in `exclude`, and have no browser listening on
    their SingletonSocket are removed.
    """
    now = time.time()
    exclude = {os.path.realpath(p) for p in exclude if p}
    removed = 0

    for pattern in (TEMP_DIR_PREFIX + '*',) + SINGLETON_DIR_GLOBS:
        for path in glob.glob(os.path.join(temp_root, pattern)):
            if not os.path.isdir(path) or os.path.realpath(path) in exclude:
                continue
            try:
                if now - os.stat(path).st_mtime < min_age:
                    continue
            except OSError:
                continue
            if _singleton_socket_is_live(path):
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1

    if removed:
        logger.info(f"Swept {removed} orphaned Chrome temp dir(s) from {temp_root}")
    return removed


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
        self._owned_temp_dir = None
        self._xvfb_displays = set()
        self._killed_by_us = False
        self._exit_reported = False
        self._tasks = []
        self._startup_log = deque(maxlen=STARTUP_LOG_LINES)

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    @property
    def temp_dir(self):
        """The scratch dir this browser owns, so a sweep can skip it while we are running."""
        return self._owned_temp_dir

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
        return False

    async def start(self):
        loop = asyncio.get_running_loop()
        self._argv, self._owned_temp_dir = await loop.run_in_executor(
            None, lambda: build_chrome_args(self.chrome_flags, self.headful)
        )

        argv = self._argv
        if self.headful:
            logger.debug("Using headful mode with xvfb-run (auto display allocation)")
            argv = ["xvfb-run", "-a", "-s", XVFB_SCREEN_ARGS] + argv

        logger.debug(f"WebSocket ID: {self.conn_id} - launching: {' '.join(argv)}")

        # Point Chrome's temp dir at our scratch dir so the socket dir it never cleans up after
        # a kill is somewhere we delete wholesale. Also covers xvfb-run's Xauthority file.
        env = dict(os.environ, TMPDIR=self._owned_temp_dir)

        try:
            # stdout to /dev/null: Chrome puts everything we care about on stderr, and an
            # undrained pipe blocks the browser once the 64KB kernel buffer fills.
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdout=DEVNULL, stderr=PIPE, env=env
            )
        except FileNotFoundError:
            self._cleanup_temp_dir()
            raise ChromeStartupError(
                f"Chrome binary not found at {argv[0]}, aborting!"
            )
        except Exception as e:
            self._cleanup_temp_dir()
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
            match = DEVTOOLS_RE.search(text)
            if match:
                return match.group(1)
            if _is_benign(text):
                logger.trace(f"WebSocket ID: {self.conn_id} Chrome stderr PID {self.pid}: {text}")
                continue
            self._startup_log.append(text)
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
                if not text:
                    continue
                level = logger.trace if _is_benign(text) else logger.debug
                level(f"WebSocket ID: {self.conn_id} Chrome stderr PID {self.pid}: {text}")
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
        elif self.returncode == 0:
            # Chrome shut itself down cleanly - normally because the client sent Browser.close.
            logger.debug(f"WebSocket ID: {self.conn_id} - Chrome PID {self.pid} "
                         f"exited rc=0 (clean shutdown, client asked it to close)")
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
        if self._killed_by_us:
            why = "expected, proxy cleanup"
        elif self.returncode == 0:
            why = "clean shutdown, client asked it to close"
        else:
            why = "UNEXPECTED - Chrome died on its own"
        return f"chrome process: rc={self.returncode} ({why})"

    async def aclose(self):
        if self.proc is None:
            self._cleanup_temp_dir()
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

        self._cleanup_temp_dir()
        self._cleanup_xvfb()

    def _note_xvfb_displays(self, procs):
        """Remember which X display our xvfb-run took, before we kill everything.

        Xvfb removes /tmp/.X<n>-lock and /tmp/.X11-unix/X<n> when it shuts down on its own,
        and xvfb-run's shell trap tidies up after it - neither of which survives a SIGKILL.
        Unremoved, the locks build up one per headful connection, and `xvfb-run -a` has to
        climb past every stale one to find a free display number.
        """
        for proc in procs:
            try:
                if proc.name() != 'Xvfb':
                    continue
                for arg in proc.cmdline()[1:]:
                    match = XVFB_DISPLAY_RE.match(arg)
                    if match:
                        self._xvfb_displays.add(match.group(1))
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def _cleanup_xvfb(self):
        """Drop the X lock and socket that Xvfb never got the chance to remove.

        Safe to unlink: while our Xvfb held the display nobody else could claim that number,
        and once it is dead the leftover lock only stops the number being reused.
        """
        for display in self._xvfb_displays:
            for path in (X11_LOCK.format(display=display), X11_SOCKET.format(display=display)):
                try:
                    os.unlink(path)
                except OSError:
                    pass  # never existed, or not ours to remove
        if self._xvfb_displays:
            logger.debug(f"WebSocket ID: {self.conn_id} - Released X display(s) "
                         f"{', '.join(sorted(self._xvfb_displays))}")
        self._xvfb_displays = set()

    def _kill_tree(self):
        """SIGKILL the browser and every renderer/GPU child it spawned."""
        try:
            parent = psutil.Process(self.proc.pid)
            procs = [parent] + parent.children(recursive=True)
            self._note_xvfb_displays(procs)
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

    def _cleanup_temp_dir(self):
        """Remove our scratch dir: profile, Chrome's singleton socket dir, temp shmem files."""
        if not self._owned_temp_dir:
            return
        shutil.rmtree(self._owned_temp_dir, ignore_errors=True)
        self._owned_temp_dir = None
