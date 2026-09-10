"""Shared helpers for the pyppeteer-ng container tests.

The tests drive a *running* sockpuppetbrowser - the docker container in CI, or a local
`python3 backend/server.py` - over CDP with pyppeteer-ng, the same client
changedetection.io uses.

Configuration comes from the environment so the same scripts work in both places:
    CDP_URL         websocket endpoint of the proxy   (default ws://127.0.0.1:3000)
    STATS_URL       the /stats http endpoint          (default http://127.0.0.1:8080/stats)
    CONTAINER_NAME  container to inspect with docker exec, for the tests that need to look
                    inside it                         (default sockpuppet-test)
    TEST_URL        page to load                      (default https://example.com)
    ARTIFACT_DIR    where to write screenshots/HTML   (default ./artifacts)
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import pyppeteer

CDP_URL = os.getenv('CDP_URL', 'ws://127.0.0.1:3000')
STATS_URL = os.getenv('STATS_URL', 'http://127.0.0.1:8080/stats')
CONTAINER_NAME = os.getenv('CONTAINER_NAME', 'sockpuppet-test')
TEST_URL = os.getenv('TEST_URL', 'https://example.com')

# Screenshots and page HTML land here for the CI job to upload, so a failure can be looked at
# rather than guessed at.
ARTIFACT_DIR = os.getenv('ARTIFACT_DIR', os.path.join(os.path.dirname(__file__), 'artifacts'))


def save_artifact(name, data):
    """Write a screenshot or page dump to ARTIFACT_DIR and return its path."""
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    path = os.path.join(ARTIFACT_DIR, name)
    with open(path, 'wb' if isinstance(data, bytes) else 'w') as f:
        f.write(data)
    print(f"  saved {path} ({len(data)} bytes)")
    return path


def endpoint(**query):
    """Proxy endpoint, with query args the proxy converts into Chrome flags."""
    if not query:
        return CDP_URL
    return f"{CDP_URL}?{urllib.parse.urlencode(query)}"


async def connect(**query):
    """Connect to the proxy, with query args it turns into Chrome flags.

    The viewport is taken from the connection's own --window-size when it has one. It has to
    be passed explicitly: pyppeteer-ng coerces defaultViewport=None to 800x600 rather than
    treating it as "do not emulate" (launcher.py, BaseBrowserLauncher.connect), so every new
    page gets an Emulation.setDeviceMetricsOverride at that size and a screenshot would show
    800x600 no matter what the browser was actually started with.
    """
    viewport = None
    window_size = query.get('--window-size')
    if window_size:
        width, _, height = window_size.partition(',')
        viewport = {'width': int(width), 'height': int(height)}
    return await pyppeteer.connect(browserWSEndpoint=endpoint(**query),
                                   defaultViewport=viewport)


async def fetch(url=TEST_URL, screenshot=True, graceful_close=False, **query):
    """Load `url` through the proxy. Returns (status, html, screenshot_bytes_or_None).

    graceful_close=False disconnects the websocket and leaves it to the proxy to tear Chrome
    down (it SIGKILLs the process tree) - the path most clients actually take, and the one
    where Chrome never gets to clean up its own temp dirs.
    graceful_close=True sends Browser.close instead, so Chrome exits by itself.
    """
    browser = await connect(**query)
    try:
        page = await browser.newPage()
        page.setDefaultNavigationTimeout(30000)
        response = await page.goto(url, waitUntil='load')
        html = await page.content
        shot = None
        if screenshot:
            shot = await page.screenshot(type_='jpeg', quality=60, encoding='binary')
        await page.close()
        return response.status, html, shot
    finally:
        if graceful_close:
            await browser.close()
        else:
            await browser.disconnect()


def stats():
    """Current /stats payload as a dict."""
    with urllib.request.urlopen(STATS_URL, timeout=10) as r:
        return json.loads(r.read())


def wait_for_idle(timeout=15):
    """Block until the proxy reports no active connections. Returns the final /stats.

    A client returns from disconnect()/close() as soon as its websocket is gone, while the
    proxy is still killing Chrome and reaping it. Anything that inspects server-side state
    after a connection has to wait for that to finish first.
    """
    deadline = time.monotonic() + timeout
    while True:
        current = stats()
        if current['active_connections'] == 0 or time.monotonic() > deadline:
            return current
        time.sleep(0.25)


def docker(*argv):
    """Run a docker command and return its stdout (raises on non-zero)."""
    cmd = ['docker'] + list(argv)
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed rc={done.returncode}: {done.stderr.strip()}")
    return done.stdout


def in_container(*argv, container=None):
    """Run a command inside the container and return its stdout (raises on non-zero)."""
    return docker('exec', container or CONTAINER_NAME, *argv)


def layer_diff(container=None):
    """Everything the container has changed on top of its image, as (change, path) pairs.

    This is the overlay2 upper dir the inode-exhaustion report in #50 was measuring, read
    through docker so it needs no root: 'A' added, 'C' changed, 'D' deleted.
    """
    out = docker('diff', container or CONTAINER_NAME)
    entries = []
    for line in out.split('\n'):
        if line.strip():
            change, _, path = line.partition(' ')
            entries.append((change, path))
    return sorted(entries)


def wait_until_ready(timeout=60):
    """Block until the proxy answers on /stats. True if it came up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            stats()
            return True
        except Exception:
            time.sleep(0.5)
    return False


class Checks:
    """Minimal assert-and-keep-going harness, so one run reports every failure it found."""

    def __init__(self, title):
        self.failures = []
        print(f"\n=== {title}\n")

    def ok(self, condition, description, detail=''):
        if condition:
            print(f"  PASS  {description}")
        else:
            self.failures.append(description)
            print(f"  FAIL  {description}" + (f"\n        {detail}" if detail else ''))
        return bool(condition)

    def done(self):
        if self.failures:
            print(f"\n{len(self.failures)} check(s) FAILED:")
            for f in self.failures:
                print(f"  - {f}")
            sys.exit(1)
        print("\nAll checks passed.")
        sys.exit(0)


def run(coro):
    """asyncio.run, but reporting an unhandled exception as a test failure."""
    try:
        asyncio.run(coro)
    except Exception as e:
        print(f"\nUNEXPECTED {type(e).__name__}: {e}")
        raise
