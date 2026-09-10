#!/usr/bin/env python3
"""Do the POSIX-only parts of teardown degrade instead of throwing off-container?

The proxy is normally the Linux container, but it runs directly on a host too, and Windows has
no SIGHUP, no AF_UNIX and no X displays. Those paths must skip rather than raise: an
AttributeError in teardown would leak a browser on every connection.

Windows is simulated by taking the attributes away, which is the same thing the code tests for.

    python3 test/test_platform_guards.py
"""

import os
import signal
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))

import chrome  # noqa: E402

FAILURES = []


def check(condition, description, detail=''):
    if condition:
        print(f"  PASS  {description}")
    else:
        FAILURES.append(description)
        print(f"  FAIL  {description}" + (f"\n        {detail}" if detail else ''))


class without:
    """Temporarily remove an attribute, the way it is absent on Windows."""

    def __init__(self, module, name):
        self.module, self.name = module, name

    def __enter__(self):
        self.value = getattr(self.module, self.name)
        delattr(self.module, self.name)

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.value)


def test_socket_state_without_af_unix():
    with without(socket, 'AF_UNIX'):
        state = chrome._unix_socket_state('/tmp/nothing-here')
    check(state == 'unknown', "no AF_UNIX: socket liveness reports 'unknown', so nothing is swept",
          f"returned {state!r}")


def test_singleton_check_without_af_unix():
    with without(socket, 'AF_UNIX'):
        live = chrome._singleton_socket_is_live('/tmp')
    check(live is True, "no AF_UNIX: a profile counts as in use rather than sweepable",
          f"returned {live!r}")


def test_x_display_sweep_skipped():
    original = chrome.WINDOWS
    chrome.WINDOWS = True
    try:
        released = chrome.sweep_orphan_x_displays(min_age=0)
    finally:
        chrome.WINDOWS = original
    check(released == 0, "on Windows: the X display sweep is a no-op", f"released {released}")


def test_graceful_stop_without_sighup():
    import asyncio

    class FakeProc:
        returncode = None
        pid = os.getpid()

    inst = chrome.ChromeInstance(chrome_flags=[], conn_id='test')
    inst.proc = FakeProc()
    signalled = []
    inst._browser_process = lambda: (_ for _ in ()).throw(AssertionError("should not be reached"))

    with without(signal, 'SIGHUP'):
        try:
            asyncio.run(inst._graceful_stop())
            ok = True
        except Exception as e:
            ok = False
            detail = f"{type(e).__name__}: {e}"
    check(ok, "no SIGHUP: the graceful stop returns quietly and leaves the kill to _kill_tree",
          locals().get('detail', ''))


def test_defaults_are_platform_shaped():
    check(chrome.DEFAULT_CHROME_BIN.startswith('/usr/bin/') or chrome.DEFAULT_CHROME_BIN.endswith('.exe'),
          f"CHROME_BIN default suits the platform ({chrome.DEFAULT_CHROME_BIN})")
    check(os.path.isdir(chrome.TEMP_ROOT), f"TEMP_ROOT exists ({chrome.TEMP_ROOT})")


if __name__ == '__main__':
    print("\n=== Platform guards in teardown\n")
    for test in (test_socket_state_without_af_unix, test_singleton_check_without_af_unix,
                 test_x_display_sweep_skipped, test_graceful_stop_without_sighup,
                 test_defaults_are_platform_shaped):
        test()

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll checks passed.")
