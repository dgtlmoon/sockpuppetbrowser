#!/usr/bin/env python3
"""Can a pyppeteer client actually load a page through the proxy?

Covers the shapes real clients use: a client-supplied --user-data-dir (what
changedetection.io does), no user-data-dir at all, headful mode under xvfb-run, several
connections at once, and both teardown paths (plain disconnect vs Browser.close).
"""

import asyncio

from common import Checks, TEST_URL, fetch, run, stats, wait_for_idle


async def main():
    c = Checks(f"Fetching {TEST_URL} through the proxy")
    before = stats()

    # The common case: the client brings its own profile dir.
    status, html, shot = await fetch(**{'--user-data-dir': '/tmp/test-profile-fetch',
                                        '--window-size': '1280,1024'})
    c.ok(status == 200, f"client-supplied --user-data-dir: HTTP {status}", f"got {status}")
    c.ok('Example Domain' in html, "page content came back through the proxy",
         f"html starts: {html[:120]!r}")
    c.ok(shot and shot[:2] == b'\xff\xd8', f"screenshot is a JPEG ({len(shot or '')} bytes)")

    # No --user-data-dir: the proxy makes a throwaway profile itself.
    status, html, _ = await fetch(screenshot=False)
    c.ok(status == 200 and 'Example Domain' in html, f"no --user-data-dir: HTTP {status}")

    # Headful, i.e. Chrome under xvfb-run on its own virtual display.
    status, html, shot = await fetch(headful='true',
                                     **{'--user-data-dir': '/tmp/test-profile-headful'})
    c.ok(status == 200 and 'Example Domain' in html, f"headful mode: HTTP {status}")
    c.ok(shot and shot[:2] == b'\xff\xd8', f"headful screenshot is a JPEG ({len(shot or '')} bytes)")

    # Graceful teardown: Chrome exits on its own instead of being killed.
    status, html, _ = await fetch(screenshot=False, graceful_close=True,
                                  **{'--user-data-dir': '/tmp/test-profile-graceful'})
    c.ok(status == 200 and 'Example Domain' in html, f"Browser.close teardown: HTTP {status}")

    # Several browsers at once, each with its own profile.
    results = await asyncio.gather(*(
        fetch(screenshot=False, **{'--user-data-dir': f'/tmp/test-profile-concurrent-{i}'})
        for i in range(3)
    ), return_exceptions=True)
    failed = [r for r in results if isinstance(r, Exception)]
    c.ok(not failed, "3 concurrent connections all succeeded", f"errors: {failed}")
    c.ok(all(r[0] == 200 for r in results if not isinstance(r, Exception)),
         "3 concurrent connections all returned HTTP 200")

    after = wait_for_idle()
    c.ok(after['connection_count_total'] - before['connection_count_total'] == 7,
         "/stats counted all 7 connections",
         f"before {before['connection_count_total']} after {after['connection_count_total']}")
    c.ok(after['active_connections'] == 0, "no connections left active",
         f"active_connections={after['active_connections']}")
    c.ok(after['chrome_start_failures'] == before['chrome_start_failures'],
         "no Chrome start failures", f"{after['chrome_start_failures']} failure(s)")

    c.done()


if __name__ == '__main__':
    run(main())
