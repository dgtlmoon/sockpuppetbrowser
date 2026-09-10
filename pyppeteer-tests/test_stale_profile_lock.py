#!/usr/bin/env python3
"""Can a profile dir be reused after the container that last used it is gone?

Chrome's ProcessSingleton writes <profile>/SingletonLock as a symlink naming
"<hostname>-<pid>", and it removes it on a clean exit - but not when it is killed. A container
gets a new hostname every time it is recreated, and if the profiles live on a bind-mounted
volume (a ramdisk at /tmp, say) they outlive the container. Chrome then sees a lock from
"another computer" and refuses the profile *permanently*:

    The profile appears to be in use by another Google Chrome process (323270)
    on another computer (989ba27971e3).

That is not a transient failure - every connection using that profile fails until someone
deletes the lock, which in production shows up as a handful of watches that never work.

The proxy therefore clears those links before launching, but only when nothing is listening on
the socket. The second half of this test is the one that matters for safety: a profile a live
browser is genuinely using must still be protected.

Needs docker: it plants a lock inside the container.
"""

from common import CONTAINER_NAME, Checks, connect, fetch, in_container, run, wait_for_idle

PROFILE = '/tmp/stale-lock-profile'
FOREIGN_HOST = 'some-old-container'


def plant_stale_lock():
    """Make PROFILE look like a browser in a previous container left it behind."""
    in_container('sh', '-c',
                 f'rm -rf {PROFILE}; mkdir -p {PROFILE}; '
                 f'ln -s "{FOREIGN_HOST}-1234" {PROFILE}/SingletonLock; '
                 f'ln -s "/nonexistent/SingletonSocket" {PROFILE}/SingletonSocket; '
                 f'ln -s "1234567890" {PROFILE}/SingletonCookie')


async def try_fetch(**query):
    """fetch(), but a refused launch comes back as None instead of an exception."""
    try:
        return await fetch(screenshot=False, **query)
    except Exception as e:
        print(f"    (connection refused: {type(e).__name__}: {str(e)[:70]})")
        return None


def links_in(profile):
    out = in_container('sh', '-c', f'ls -A {profile} 2>/dev/null | grep Singleton || true')
    return sorted(x for x in out.split('\n') if x.strip())


async def main():
    c = Checks(f"Reusing a profile locked by a previous container ({CONTAINER_NAME})")

    plant_stale_lock()
    c.ok(links_in(PROFILE) == ['SingletonCookie', 'SingletonLock', 'SingletonSocket'],
         "planted a stale lock naming another host", f"found {links_in(PROFILE)}")

    result = await try_fetch(**{'--user-data-dir': PROFILE})
    c.ok(result is not None and result[0] == 200 and 'Example Domain' in result[1],
         "a profile locked by a dead container is still usable",
         "Chrome exits 21 ('profile appears to be in use ... on another computer') unless the "
         "stale links are cleared before launch")
    wait_for_idle()

    # ...and again, to be sure we did not just get lucky with a one-off.
    result = await try_fetch(**{'--user-data-dir': PROFILE})
    c.ok(result is not None and result[0] == 200, "and again on the next connection")
    wait_for_idle()

    # The safety property: a lock held by a *live* browser must never be cleared, or two
    # browsers would share one profile and corrupt it.
    browser = await connect(**{'--user-data-dir': PROFILE})
    try:
        page = await browser.newPage()
        await page.goto('about:blank', waitUntil='load')

        c.ok(await try_fetch(**{'--user-data-dir': PROFILE}) is None, "a profile in use by a live browser is still refused to a second connection",
             "the second connection succeeded - Chrome's own protection has been defeated and "
             "two browsers are sharing one profile")
    finally:
        await browser.disconnect()
    wait_for_idle()

    in_container('sh', '-c', f'rm -rf {PROFILE}')
    c.done()


if __name__ == '__main__':
    run(main())
