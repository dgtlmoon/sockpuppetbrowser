#!/usr/bin/env python3
"""Does the container's /tmp stay clean as connections come and go?

The proxy SIGKILLs Chrome's process tree at teardown, which is fast but means nothing Chrome
(or xvfb-run) would normally tidy up on the way out gets tidied. Two things used to pile up in
/tmp, one per connection, which on a tmpfs /tmp is a slow leak of the container's memory:

  * org.chromium.Chromium.XXXXXX - ProcessSingleton's socket dir, which the profile symlinks
    to. Created in headful mode; chromium's old --headless does not start a ProcessSingleton,
    so a headless-only test would not notice this at all.
  * .X<n>-lock and .X11-unix/X<n> - the display lock Xvfb holds. Leaking these also makes
    `xvfb-run -a` climb to a higher display number on every connection.

backend/chrome.py now gives each browser a private TMPDIR inside a scratch dir it deletes
wholesale, and releases the X display it used, so /tmp should look untouched afterwards.

Needs docker: it inspects /tmp inside CONTAINER_NAME.
"""

import re

from common import CONTAINER_NAME, Checks, fetch, in_container, run, wait_for_idle

# Things Chrome or the proxy puts in /tmp and should have taken away again.
LEAK_PATTERNS = (
    re.compile(r'^\.?org\.chromium\.Chromium\.'),   # chromium's singleton socket dir
    re.compile(r'^\.?com\.google\.Chrome\.'),       # ...named differently in google-chrome
    re.compile(r'^chrome-puppeteer-proxy'),         # the proxy's own per-connection scratch dir
    re.compile(r'^\.X\d+-lock$'),                   # Xvfb display lock
    re.compile(r'^xvfb-run\.'),                     # xvfb-run's Xauthority dir
)

# Profiles the *client* asked for. The proxy must never delete these - they are not its to
# remove, and a client may well be reusing one across connections.
CLIENT_PROFILES = ['/tmp/test-profile-cleanup-1', '/tmp/test-profile-cleanup-2']


def tmp_listing():
    """Entries in the container's /tmp, once the proxy has finished tearing down.

    A client returns from disconnect() before the proxy has killed Chrome and removed the
    scratch dir, so listing straight away would race the cleanup rather than test it.
    """
    wait_for_idle()
    return sorted(e for e in in_container('ls', '-A', '/tmp').split('\n') if e)


def leaks(listing):
    return [e for e in listing if any(p.match(e) for p in LEAK_PATTERNS)]


def x_sockets():
    """X server sockets in the container, one per live display."""
    out = in_container('sh', '-c', 'ls -A /tmp/.X11-unix 2>/dev/null || true')
    return sorted(e for e in out.split('\n') if e)


def stray_processes():
    """Chrome or Xvfb processes still alive in the container."""
    out = in_container('sh', '-c', 'pgrep -a chromium; pgrep -a Xvfb; true')
    return [line for line in out.split('\n') if line.strip()]


async def main():
    c = Checks(f"Temp dir cleanup inside {CONTAINER_NAME}")

    baseline = tmp_listing()
    c.ok(not leaks(baseline), "no leftovers in /tmp before we start", f"found: {leaks(baseline)}")

    # Two connections reusing a client-supplied profile, torn down the way most clients do it:
    # drop the websocket and leave the proxy to kill Chrome.
    for profile in CLIENT_PROFILES:
        status, _, _ = await fetch(screenshot=False, **{'--user-data-dir': profile})
        c.ok(status == 200, f"fetched with --user-data-dir={profile}", f"HTTP {status}")

    # ...one where the proxy has to invent the profile itself...
    status, _, _ = await fetch(screenshot=False)
    c.ok(status == 200, "fetched with no --user-data-dir", f"HTTP {status}")

    # ...and three headful ones, the mode that actually creates the singleton dir and takes an
    # X display. Sequential, so a leaked display lock would show up as a climbing number.
    for i in range(3):
        status, _, _ = await fetch(screenshot=False, headful='true',
                                   **{'--user-data-dir': f'/tmp/test-profile-headful-{i}'})
        c.ok(status == 200, f"headful fetch {i + 1} of 3", f"HTTP {status}")

    after = tmp_listing()
    found = leaks(after)
    c.ok(not found, "no Chrome or Xvfb leftovers in /tmp after teardown",
         f"leaked {len(found)}: {found}\nfull /tmp: {after}")

    # A set, because the client profiles may already exist from an earlier run of this test.
    expected = set(baseline) | {p.rsplit('/', 1)[-1] for p in CLIENT_PROFILES} | {
        f'test-profile-headful-{i}' for i in range(3)} | {'.X11-unix'}
    c.ok(set(after) <= expected, "/tmp gained nothing but the client's own profiles",
         f"unexpected: {sorted(set(after) - expected)}")

    for profile in CLIENT_PROFILES:
        c.ok(in_container('sh', '-c', f'test -d {profile} && echo yes || echo no').strip() == 'yes',
             f"client's own profile {profile} was left alone")

    sockets = x_sockets()
    c.ok(not sockets, "every X display was released", f"sockets still present: {sockets}")

    alive = stray_processes()
    c.ok(not alive, "no Chrome or Xvfb processes left running", "still alive:\n" + '\n'.join(alive))

    c.done()


if __name__ == '__main__':
    run(main())
