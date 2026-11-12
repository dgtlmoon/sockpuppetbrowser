#!/usr/bin/env python3
"""Do the /stats failure counters actually move when things fail?

Three ways a connection can go wrong, and they need telling apart:

  chrome_start_failures  Chrome never came up (missing binary, exited during startup, or the
                         profile was locked by another browser).
  cdp_connect_failures   Chrome came up and announced an endpoint, but was gone or unreachable
                         by the time the proxy dialled it. Without a counter this looks like a
                         perfectly ordinary connection from the outside.
  quiet_sessions         The proxy attached, and then almost nothing crossed the wire. Not a
                         failure exactly, but a client that connected and did nothing with the
                         browser is worth seeing.

The cdp_connect_failures case needs a Chrome that lies about its endpoint, so that part runs
its own container with CHROME_BIN pointed at a script which prints a DevTools URL for a port
nothing is listening on.

Needs docker.
"""

import asyncio
import os
import sys
import time

import websockets

import common
from common import Checks, docker, fetch, run, stats, wait_for_idle

FAKE_CHROME = '/tmp/fake-chrome-liar.sh'
LIAR_NAME = 'sockpuppet-liar'
LIAR_CDP, LIAR_STATS = 3200, 8200


async def quiet_connection():
    """Connect to the proxy and hang up without sending a single CDP command."""
    async with websockets.connect(common.CDP_URL, max_size=None):
        await asyncio.sleep(0.2)


async def check_start_failure(c):
    before = stats()
    # A profile dir Chrome cannot possibly create: /proc rejects mkdir.
    try:
        await fetch(screenshot=False, **{'--user-data-dir': '/proc/impossible-profile'})
        launched = True
    except Exception:
        launched = False
    wait_for_idle()
    after = stats()
    c.ok(not launched, "a browser with an impossible --user-data-dir does not launch")
    c.ok(after['chrome_start_failures'] == before['chrome_start_failures'] + 1,
         "chrome_start_failures counted it",
         f"{before['chrome_start_failures']} -> {after['chrome_start_failures']}")
    c.ok(after['cdp_connect_failures'] == before['cdp_connect_failures'],
         "and it was not also counted as a CDP connect failure")


async def check_quiet_session(c):
    before = stats()
    await quiet_connection()
    wait_for_idle()
    after = stats()
    c.ok(after['quiet_sessions'] == before['quiet_sessions'] + 1,
         "quiet_sessions counted a connection that sent nothing",
         f"{before['quiet_sessions']} -> {after['quiet_sessions']}")
    c.ok(after['chrome_start_failures'] == before['chrome_start_failures'],
         "a quiet session is not counted as a start failure")

    # ...and a real fetch must not be mistaken for one.
    before = stats()
    status, _, _ = await fetch(screenshot=False)
    wait_for_idle()
    after = stats()
    c.ok(status == 200 and after['quiet_sessions'] == before['quiet_sessions'],
         "a real fetch is not counted as quiet",
         f"{before['quiet_sessions']} -> {after['quiet_sessions']}")


async def check_cdp_connect_failure(c):
    """Own container: CHROME_BIN prints an endpoint nothing is listening on."""
    image = os.getenv('TEST_IMAGE', 'sockpuppetbrowser:test')
    docker('rm', '-f', LIAR_NAME)
    docker('run', '-d', '--name', LIAR_NAME, '--init', '--cap-add=SYS_ADMIN',
           f'--security-opt=seccomp={os.path.abspath("../chrome.json")}',
           '-p', f'127.0.0.1:{LIAR_CDP}:3000', '-p', f'127.0.0.1:{LIAR_STATS}:8080',
           '-e', 'LOG_LEVEL=DEBUG', '-e', f'CHROME_BIN={FAKE_CHROME}', image)

    saved = (common.CDP_URL, common.STATS_URL, common.CONTAINER_NAME)
    common.CDP_URL = f'ws://127.0.0.1:{LIAR_CDP}'
    common.STATS_URL = f'http://127.0.0.1:{LIAR_STATS}/stats'
    common.CONTAINER_NAME = LIAR_NAME
    try:
        c.ok(common.wait_until_ready(), "the second container came up")

        # Announces a CDP endpoint on a port nothing is listening on, then sits there so the
        # proxy sees a live process with a dead endpoint.
        common.in_container('sh', '-c',
                            f'printf "#!/bin/sh\\n'
                            f'echo \\"DevTools listening on ws://127.0.0.1:1/devtools/browser/nope\\" >&2\\n'
                            f'sleep 60\\n" > {FAKE_CHROME}; chmod +x {FAKE_CHROME}')

        before = stats()
        try:
            await fetch(screenshot=False)
        except Exception:
            pass
        wait_for_idle()
        after = stats()

        c.ok(after['cdp_connect_failures'] == before['cdp_connect_failures'] + 1,
             "cdp_connect_failures counted a browser that announced a dead endpoint",
             f"{before['cdp_connect_failures']} -> {after['cdp_connect_failures']}")
        c.ok(after['chrome_start_failures'] == before['chrome_start_failures'],
             "and it was not counted as a start failure - Chrome did launch",
             f"{before['chrome_start_failures']} -> {after['chrome_start_failures']}")
    finally:
        docker('rm', '-f', LIAR_NAME)
        common.CDP_URL, common.STATS_URL, common.CONTAINER_NAME = saved


async def main():
    c = Checks("/stats failure counters")
    keys = stats()
    for key in ('chrome_start_failures', 'cdp_connect_failures', 'quiet_sessions'):
        c.ok(key in keys, f"/stats exposes {key}")

    await check_start_failure(c)
    await check_quiet_session(c)
    await check_cdp_connect_failure(c)
    c.done()


if __name__ == '__main__':
    run(main())
