#!/usr/bin/env python3
"""What happens to Chrome's leftovers when the proxy itself is killed mid-connection?

Per-connection cleanup runs in the proxy's teardown path, so it cannot help when the proxy
never gets there - an OOM kill, `docker kill`, a hard restart. Those leave orphaned scratch
dirs behind, which is what the sweep in backend/chrome.py (startup, then periodically) is for.

This kills the container with SIGKILL while browsers are live, checks the orphans really are
there, then starts it again and expects the sweep to have removed them - without touching the
client's own profile dirs.

Needs docker: it stops and starts CONTAINER_NAME.
"""

import asyncio
import re

from common import (CONTAINER_NAME, Checks, connect, docker, fetch, in_container, layer_diff,
                    run, wait_until_ready)

ORPHAN_PATTERNS = (
    re.compile(r'^chrome-puppeteer-proxy'),
    re.compile(r'^\.?org\.chromium\.Chromium\.'),
    re.compile(r'^\.?com\.google\.Chrome\.'),
    re.compile(r'^\.X\d+-lock$'),
    re.compile(r'^xvfb-run\.'),
)

CLIENT_PROFILE = '/tmp/killed-profile'


def orphans_in_layer():
    """Leftovers visible in /tmp of the container's layer. Works while it is stopped."""
    found = []
    for _, path in layer_diff():
        parent, _, name = path.rpartition('/')
        if parent == '/tmp' and any(p.match(name) for p in ORPHAN_PATTERNS):
            found.append(name)
    return sorted(found)


async def hold_open_connections():
    """Open a headless and a headful browser and leave them running. Returns the browsers."""
    browsers = []
    for query in ({'--user-data-dir': CLIENT_PROFILE},
                  {'--user-data-dir': CLIENT_PROFILE + '-headful', 'headful': 'true'}):
        browser = await connect(**query)
        page = await browser.newPage()
        page.setDefaultNavigationTimeout(30000)
        await page.goto('about:blank', waitUntil='load')
        browsers.append(browser)
    return browsers


async def main():
    c = Checks(f"Orphans left when {CONTAINER_NAME} is killed mid-connection")

    await hold_open_connections()
    live = in_container('sh', '-c', 'ls -A /tmp')
    print(f"  /tmp while both browsers are live: {' '.join(live.split())}")

    # SIGKILL to PID 1 (the proxy): no teardown, no cleanup, exactly like an OOM kill.
    docker('kill', '--signal=KILL', CONTAINER_NAME)
    print("  container killed")

    orphaned = orphans_in_layer()
    c.ok(orphaned, "the kill really did leave orphans behind (otherwise this proves nothing)",
         "found none - has the layout changed?")
    print(f"  orphans in the layer: {orphaned}")

    docker('start', CONTAINER_NAME)
    c.ok(wait_until_ready(), "proxy came back up after the restart")

    remaining = orphans_in_layer()
    c.ok(not remaining, "the startup sweep removed every orphan",
         f"still there: {remaining}")

    # The sweep must be able to tell an orphan from a profile it has no business deleting.
    for profile in (CLIENT_PROFILE, CLIENT_PROFILE + '-headful'):
        kept = in_container('sh', '-c', f'test -d {profile} && echo yes || echo no').strip()
        c.ok(kept == 'yes', f"client's own profile {profile} survived the sweep")

    # And the proxy still works afterwards.
    status, html, _ = await fetch(screenshot=False, **{'--user-data-dir': CLIENT_PROFILE})
    c.ok(status == 200 and 'Example Domain' in html,
         f"a fetch after the restart still works: HTTP {status}")

    c.done()


if __name__ == '__main__':
    run(main())
