"""Shared helpers for the pyppeteer container tests.

The tests drive a *running* sockpuppetbrowser - the docker container in CI, or a local
`python3 backend/server.py` - over CDP with pyppeteer, the same client changedetection.io uses.

Configuration comes from the environment so the same scripts work in both places:
    CDP_URL         websocket endpoint of the proxy   (default ws://127.0.0.1:3000)
    STATS_URL       the /stats http endpoint          (default http://127.0.0.1:8080/stats)
    CONTAINER_NAME  container to inspect with docker exec, for the tests that need to look
                    inside it                         (default sockpuppet-test)
    TEST_URL        page to load                      (default https://example.com)
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


def endpoint(**query):
    """Proxy endpoint, with query args the proxy converts into Chrome flags."""
    if not query:
        return CDP_URL
    return f"{CDP_URL}?{urllib.parse.urlencode(query)}"


async def fetch(url=TEST_URL, screenshot=True, graceful_close=False, **query):
    """Load `url` through the proxy. Returns (status, html, screenshot_bytes_or_None).

    graceful_close=False disconnects the websocket and leaves it to the proxy to tear Chrome
    down (it SIGKILLs the process tree) - the path most clients actually take, and the one
    where Chrome never gets to clean up its own temp dirs.
    graceful_close=True sends Browser.close instead, so Chrome exits by itself.
    """
    browser = await pyppeteer.launcher.connect(browserWSEndpoint=endpoint(**query))
    try:
        page = await browser.newPage()
        page.setDefaultNavigationTimeout(30000)
        response = await page.goto(url, {'waitUntil': 'load'})
        html = await page.content()
        shot = None
        if screenshot:
            shot = await page.screenshot({'encoding': 'binary', 'type': 'jpeg', 'quality': 60})
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


def in_container(*argv, container=None):
    """Run a command inside the container and return its stdout (raises on non-zero)."""
    cmd = ['docker', 'exec', container or CONTAINER_NAME] + list(argv)
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed rc={done.returncode}: {done.stderr.strip()}")
    return done.stdout


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
