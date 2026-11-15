#!/usr/bin/env python3
"""CHROME_DISABLE_WEB_SECURITY=false must hold whatever the client sends.

Clients commonly put --disable-web-security=true on the connection URL themselves. Removing it
only from the built-in flag list left the client's copy on Chrome's command line, so the setting
looked applied and changed nothing.

No container and no real Chrome needed: this only builds the command line.

    python3 test/test_web_security_flag.py
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))

import chrome  # noqa: E402

FAILURES = []


def check(condition, description, detail=''):
    print(f"{'ok  ' if condition else 'FAIL'} {description}" + (f" - {detail}" if detail and not condition else ''))
    if not condition:
        FAILURES.append(description)


def web_security_flags(disable_web_security, query):
    chrome.CHROME_DISABLE_WEB_SECURITY = disable_web_security
    client_flags, _options = chrome.parse_query_args(query)
    argv, temp_dir = chrome.build_chrome_args(client_flags)
    shutil.rmtree(temp_dir, ignore_errors=True)
    return [a for a in argv if a.startswith('--disable-web-security')], argv


CLIENT_URL = 'ws://browser:3000?stealth=1&--disable-web-security=true&--window-size=1280,1024'
PLAIN_URL = 'ws://browser:3000?--window-size=1280,1024'

flags, _ = web_security_flags(True, PLAIN_URL)
check(flags == ['--disable-web-security=true'], "default: the flag is on Chrome's command line", flags)

flags, _ = web_security_flags(False, PLAIN_URL)
check(flags == [], "false: the built-in flag is removed", flags)

flags, argv = web_security_flags(False, CLIENT_URL)
check(flags == [], "false: a flag the client sends on the connection URL is dropped too", flags)
check('--window-size=1280,1024' in argv, "false: the client's other flags are still passed on")

flags, _ = web_security_flags(True, CLIENT_URL)
check('--disable-web-security=true' in flags, "default: a client sending the flag still gets it", flags)

if FAILURES:
    print(f"\n{len(FAILURES)} check(s) failed")
    sys.exit(1)
print("\nall good")
