#!/usr/bin/env python3
"""Does a reused --user-data-dir actually keep the session it was given?

A client that supplies its own profile dir usually does so to keep a login. Chrome writes
cookies to disk in 30s batches and flushes Local Storage and the cookie store on a graceful
shutdown - but not when it is SIGKILLed, which is how the proxy tears every browser down. So
anything written in the last moments of a connection can be silently lost, and a login done
in a short session never survives to the next one.

The scenario is deliberately the impatient one: write, then hand the connection back
immediately, the way a real client does after logging in.

Needs docker: it wipes the test profile in the container between runs.
"""

from common import CONTAINER_NAME, Checks, connect, in_container, run, wait_for_idle

PROFILE = '/tmp/persist-profile'
COOKIE = 'sp_session=itsme'
LS_VALUE = 'LSNEEDLE42'

WRITE = (f"document.cookie='{COOKIE}; max-age=99999';"
         f"localStorage.setItem('lskey','{LS_VALUE}');"
         f"sessionStorage.setItem('sskey','{LS_VALUE}'); 'written'")
READ = ("({cookie: document.cookie,"
        " ls: localStorage.getItem('lskey'),"
        " ss: sessionStorage.getItem('sskey')})")


async def session(script, graceful=False):
    """One connection on the shared profile, running `script` on example.com."""
    browser = await connect(**{'--user-data-dir': PROFILE})
    try:
        page = await browser.newPage()
        page.setDefaultNavigationTimeout(30000)
        await page.goto('https://example.com', waitUntil='load')
        return await page.evaluate(script)
    finally:
        if graceful:
            await browser.close()
        else:
            await browser.disconnect()
    

def cookies_on_disk():
    """Rows in the profile's cookie DB, read inside the container."""
    out = in_container('sh', '-c', f'''python3 -c "
import sqlite3
try:
    print(sqlite3.connect('file:{PROFILE}/Default/Cookies?mode=ro', uri=True).execute(
        'select count(*) from cookies').fetchone()[0])
except Exception as e:
    print(-1)
" 2>/dev/null || echo -1''')
    return int(out.strip() or -1)


async def check_round(c, label, graceful):
    in_container('sh', '-c', f'rm -rf {PROFILE}')

    wrote = await session(WRITE, graceful=graceful)
    c.ok(wrote == 'written', f"[{label}] wrote a cookie and localStorage")
    wait_for_idle()

    c.ok(cookies_on_disk() > 0,
         f"[{label}] the cookie reached the profile's cookie DB",
         f"rows on disk: {cookies_on_disk()} - Chrome batches cookie writes and only flushes "
         f"on a graceful shutdown, so a SIGKILLed browser loses them")

    seen = await session(READ)
    c.ok(seen['cookie'] and COOKIE in seen['cookie'],
         f"[{label}] the next connection still has the cookie", f"saw {seen['cookie']!r}")
    c.ok(seen['ls'] == LS_VALUE,
         f"[{label}] the next connection still has localStorage", f"saw {seen['ls']!r}")
    # sessionStorage is per-session by definition; asserting it is *gone* keeps us honest
    # about what a profile dir does and does not carry over.
    c.ok(seen['ss'] is None,
         f"[{label}] sessionStorage did not carry over (expected - it never should)",
         f"saw {seen['ss']!r}")
    wait_for_idle()


async def main():
    c = Checks(f"Session persistence in a reused profile ({CONTAINER_NAME})")
    # The path every client takes: finish the work, drop the websocket, let the proxy tear the
    # browser down. This is the one that used to lose the login.
    await check_round(c, 'client disconnects', graceful=False)
    # And the polite path, for comparison.
    await check_round(c, 'client sends Browser.close', graceful=True)
    c.done()


if __name__ == '__main__':
    run(main())
