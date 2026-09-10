#!/usr/bin/env python3
"""Are Chrome's orphans reaped, in a container started *without* `docker run --init`?

The proxy ends up as PID 1 (entrypoint.sh execs it), and killing a browser reparents its
renderers, crashpad handlers, Xvfb and xvfb-run's helper processes onto PID 1. The proxy cannot
wait() on processes it never spawned, so without an init in front of it they accumulate as
zombies - measured at ~7 per connection, which at any real volume exhausts the PID space.

The image therefore runs itself under tini (see entrypoint.sh). This test deliberately starts
its own container *without* `--init` so that Docker's reaper cannot mask a missing one, which
is exactly what the other tests in this directory would do.

    TEST_IMAGE=sockpuppetbrowser:test python3 test_no_zombies.py
"""

import ast
import os
import time

import common
from common import Checks, docker, fetch, run, wait_for_idle

IMAGE = os.getenv('TEST_IMAGE', 'sockpuppetbrowser:test')
NAME = 'sockpuppet-noinit'
CDP_PORT, STATS_PORT = 3100, 8100


def start_container_without_init():
    docker('rm', '-f', NAME)
    docker('run', '-d', '--name', NAME,
           '--cap-add=SYS_ADMIN',
           f'--security-opt=seccomp={os.path.abspath("../chrome.json")}',
           '-p', f'127.0.0.1:{CDP_PORT}:3000',
           '-p', f'127.0.0.1:{STATS_PORT}:8080',
           '-e', 'LOG_LEVEL=WARNING',
           '-e', 'SCREEN_WIDTH=1920', '-e', 'SCREEN_HEIGHT=1024',
           IMAGE)
    # Point the shared helpers at this container instead of the usual one.
    common.CDP_URL = f'ws://127.0.0.1:{CDP_PORT}'
    common.STATS_URL = f'http://127.0.0.1:{STATS_PORT}/stats'
    common.CONTAINER_NAME = NAME


# Read /proc rather than calling ps: busybox ps (Alpine) and procps ps (Debian) disagree on
# flags, and the state field in /proc/<pid>/stat is the same everywhere.
ZOMBIE_SCRIPT = r"""
import os
found = []
for pid in os.listdir('/proc'):
    if not pid.isdigit():
        continue
    try:
        stat = open('/proc/%s/stat' % pid).read()
    except OSError:
        continue          # exited while we were looking
    comm = stat[stat.index('(') + 1:stat.rindex(')')]
    state = stat[stat.rindex(')') + 2]
    if state == 'Z':
        found.append((pid, comm))
print(found)
"""


def pid1():
    return common.in_container('sh', '-c', "tr '\\0' ' ' < /proc/1/cmdline").strip()


def zombies():
    """Zombie processes in the container, as (pid, name) pairs."""
    return ast.literal_eval(common.in_container('python3', '-c', ZOMBIE_SCRIPT).strip())


async def main():
    c = Checks(f"Orphan reaping in {IMAGE} started without --init")
    start_container_without_init()
    try:
        c.ok(common.wait_until_ready(), "the container came up")

        c.ok('tini' in pid1(), "PID 1 is an init that can reap orphans",
             f"PID 1 is {pid1()!r} - Chrome's orphans will have nobody to wait() on them")

        before = zombies()
        c.ok(not before, "no zombies before we start", f"found {before}")

        # Headful included on purpose: xvfb-run contributes Xvfb and a couple of `cat`
        # helpers on top of Chrome's own children.
        for n in range(1, 5):
            headful = n % 2 == 0
            status, _, _ = await fetch(screenshot=False,
                                       headful='true' if headful else '',
                                       **{'--user-data-dir': f'/tmp/zombie-profile-{n}'})
            c.ok(status == 200, f"connection {n} of 4 ({'headful' if headful else 'headless'})",
                 f"HTTP {status}")
            wait_for_idle()

        # Reaping is immediate, but give it a moment rather than racing it.
        for _ in range(8):
            found = zombies()
            if not found:
                break
            time.sleep(0.5)

        c.ok(not found, f"no zombie processes after 4 connections",
             f"{len(found)} left: {found[:10]}")
    finally:
        docker('rm', '-f', NAME)

    c.done()


if __name__ == '__main__':
    run(main())
