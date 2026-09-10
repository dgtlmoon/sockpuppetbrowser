#!/usr/bin/env python3
"""The DevToolsActivePort fallback must only ever trust its own browser's file.

Chrome writes <user-data-dir>/DevToolsActivePort at startup and removes it on a clean exit.
The proxy SIGKILLs instead, so a profile dir the client reuses keeps the port and browser id
of the browser that was killed. That file is the fallback used when Chrome's CDP endpoint
never turns up on stderr (xvfb-run can swallow it), so trusting a stale one hands the client
an endpoint for a dead browser - or, since these are ephemeral ports, for whichever browser
holds that port now.

No container and no real Chrome needed: this drives ChromeInstance directly.

    python3 test/test_devtools_port_guard.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))

from chrome import ChromeInstance, _discard_stale_devtools_port  # noqa: E402

FAILURES = []


def check(condition, description, detail=''):
    if condition:
        print(f"  PASS  {description}")
    else:
        FAILURES.append(description)
        print(f"  FAIL  {description}" + (f"\n        {detail}" if detail else ''))


def write_port_file(profile, port, browser_id, age_seconds=0):
    """Write a DevToolsActivePort, optionally backdated to look like an older browser's."""
    path = os.path.join(profile, 'DevToolsActivePort')
    with open(path, 'w') as f:
        f.write(f"{port}\n/devtools/browser/{browser_id}\n")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def instance_for(profile):
    """A ChromeInstance far enough set up to exercise the fallback, without launching Chrome."""
    inst = ChromeInstance(chrome_flags=[f'--user-data-dir={profile}'], conn_id='test')
    inst._argv = ['/usr/bin/google-chrome', f'--user-data-dir={profile}', 'about:blank']
    inst._launched_at = time.time()
    return inst


def test_stale_file_is_ignored(profile):
    write_port_file(profile, 37435, 'dead-browser-uuid', age_seconds=120)
    url = instance_for(profile)._devtools_url_from_profile()
    check(url is None, "a DevToolsActivePort older than our launch is ignored",
          f"returned {url!r} - that port may belong to a different browser now")


def test_own_file_is_used(profile):
    write_port_file(profile, 45069, 'our-browser-uuid')
    url = instance_for(profile)._devtools_url_from_profile()
    check(url == 'ws://127.0.0.1:45069/devtools/browser/our-browser-uuid',
          "a DevToolsActivePort written after our launch is still used", f"returned {url!r}")


def test_launch_discards_inherited_file(profile):
    path = write_port_file(profile, 37435, 'dead-browser-uuid', age_seconds=120)
    _discard_stale_devtools_port(profile)
    check(not os.path.exists(path), "launching removes a DevToolsActivePort left by an earlier browser")


def test_missing_file_is_not_an_error(profile):
    _discard_stale_devtools_port(profile)  # nothing there - must not raise
    url = instance_for(profile)._devtools_url_from_profile()
    check(url is None, "no DevToolsActivePort at all is handled quietly", f"returned {url!r}")


def test_startup_failure_reports_the_timeout(profile):
    """End to end: a Chrome that never announces itself must fail, not reuse a stale port."""
    write_port_file(profile, 37435, 'dead-browser-uuid', age_seconds=120)

    fake_chrome = os.path.join(profile, 'fake-chrome.sh')
    with open(fake_chrome, 'w') as f:
        f.write('#!/bin/sh\nsleep 30\n')      # starts, says nothing, writes no port file
    os.chmod(fake_chrome, 0o755)

    os.environ['CHROME_BIN'] = fake_chrome
    temp_root = tempfile.mkdtemp(prefix='portguard.')
    os.environ['SOCKPUPPET_TEMP_ROOT'] = temp_root

    import importlib
    import chrome as chrome_module
    importlib.reload(chrome_module)          # pick up the env vars read at import time
    chrome_module.CHROME_START_TIMEOUT = 3

    async def run():
        inst = chrome_module.ChromeInstance(
            chrome_flags=[f'--user-data-dir={profile}'], conn_id='test')
        try:
            await inst.start()
            return inst.devtools_url
        except chrome_module.ChromeStartupError as e:
            return f"ChromeStartupError: {e}"
        finally:
            await inst.aclose()

    result = asyncio.run(run())
    check(str(result).startswith('ChromeStartupError'),
          "a browser that never reports its endpoint fails instead of inheriting a stale port",
          f"got {str(result)[:120]!r}")
    shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == '__main__':
    print("\n=== DevToolsActivePort fallback guard\n")
    for test in (test_stale_file_is_ignored, test_own_file_is_used,
                 test_launch_discards_inherited_file, test_missing_file_is_not_an_error,
                 test_startup_failure_reports_the_timeout):
        profile = tempfile.mkdtemp(prefix='portguard-profile.')
        try:
            test(profile)
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")
