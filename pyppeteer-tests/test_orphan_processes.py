#!/usr/bin/env python3
"""Is everything Chrome started gone once the connection ends - even if Chrome exited first?

Teardown kills the browser's process tree, but a tree can only be enumerated while the browser
is alive. Once it has exited - and it usually has, because teardown asks it to shut down
gracefully first - psutil finds nothing, and anything Chrome left behind survives. Chrome's
crashpad handlers do exactly that on purpose: they outlive the browser, so they end up
orphaned onto PID 1 and stay there, a pair per connection, until the container restarts.

Rather than hope the timing lines up, this kills the browser process itself mid-connection and
then ends the connection, which is the same state teardown normally finds.

Needs docker.
"""

import ast
import time

from common import CONTAINER_NAME, Checks, connect, in_container, run, wait_for_idle

PROFILE = '/tmp/orphan-test-profile'

# Match on process name so nothing is missed: the browser and its renderers are both "chrome",
# the crash handlers are "chrome_crashpad_handler", and headful adds Xvfb.
CHROME_PROCS = r"""
import os
NAMES = ('chrome', 'chromium', 'chrome_crashpad', 'chrome_crashpad_handler',
         'Xvfb', 'xvfb-run')
found = []
for pid in os.listdir('/proc'):
    if not pid.isdigit():
        continue
    try:
        stat = open('/proc/' + pid + '/stat').read()
        cmdline = open('/proc/' + pid + '/cmdline').read()
    except OSError:
        continue
    comm = stat[stat.index('(') + 1:stat.rindex(')')]
    fields = stat[stat.rindex(')') + 2:].split()
    if comm in NAMES:
        found.append((pid, comm, fields[0], fields[1], '--type=' in cmdline))
print(found)
"""


def chrome_processes():
    """(pid, name, state, ppid, is_child_process) for every Chrome-family process."""
    return ast.literal_eval(in_container('python3', '-c', CHROME_PROCS).strip())


def browser_pid():
    """The browser process for our test profile - no --type=, so not a renderer."""
    out = in_container('python3', '-c', r"""
import os
for pid in os.listdir('/proc'):
    if not pid.isdigit():
        continue
    try:
        cmdline = open('/proc/' + pid + '/cmdline').read()
    except OSError:
        continue
    if '--user-data-dir=""" + PROFILE + r"""' in cmdline and '--type=' not in cmdline:
        print(pid)
        break
""")
    return out.strip()


async def main():
    c = Checks(f"Orphaned Chrome processes after teardown ({CONTAINER_NAME})")

    in_container('sh', '-c', f'rm -rf {PROFILE}')
    before = chrome_processes()
    c.ok(not before, "no Chrome processes before we start", f"found {before[:6]}")

    browser = await connect(**{'--user-data-dir': PROFILE})
    page = await browser.newPage()
    await page.goto('https://example.com', waitUntil='load')

    pid = browser_pid()
    c.ok(pid, "found the browser process for our profile")
    running = chrome_processes()
    c.ok(len(running) > 2, f"Chrome is running with children ({len(running)} processes)")

    # SIGSTOP one renderer, then kill the browser. A stopped process cannot notice its
    # parent's pipe closing, so it survives and reparents to PID 1 - which is how Chrome's
    # crashpad handlers behave in production, where they outlive the browser by design. Doing
    # it this way makes the condition deterministic instead of load-dependent.
    renderers = [p for p in running if p[4] and p[1] in ('chrome', 'chromium')]
    c.ok(renderers, "found a renderer to stop")
    stopped = renderers[0][0]
    in_container('sh', '-c', f'kill -STOP {stopped}')
    time.sleep(0.5)

    # Checked before the browser goes, because once it does the proxy tears everything down
    # immediately - the CDP socket closing is what ends the connection.
    state = [p for p in chrome_processes() if p[0] == stopped]
    c.ok(state and state[0][2] == 'T',
         "a renderer is stopped, so it cannot exit on its own (test is meaningful)",
         f"state: {state} - expected 'T'")

    in_container('sh', '-c', f'kill -9 {pid}')

    # Now end the connection, which is when the proxy tears everything down.
    await browser.disconnect()
    wait_for_idle()

    for _ in range(10):
        left = chrome_processes()
        if not left:
            break
        time.sleep(0.5)

    c.ok(not left, f"teardown cleaned up every Chrome process ({len(left)} left)",
         "still there (pid, name, state, ppid, is_child):\n        "
         + '\n        '.join(str(p) for p in left[:12])
         + "\n        the browser had already exited, so psutil could not enumerate its tree "
           "- killing the process group instead would have caught these")

    in_container('sh', '-c', f'rm -rf {PROFILE}')
    c.done()


if __name__ == '__main__':
    run(main())
