#!/usr/bin/env python3
"""Does anything accumulate in the container's writable layer, anywhere at all?

test_temp_dir_cleanup.py watches /tmp, which is where the leaks we know about happen. This one
watches the whole overlay2 upper layer instead - the same thing the inode-exhaustion report in
issue #50 was measuring with `find /var/lib/docker/overlay2/<id>/diff | wc -l`, read through
`docker diff` so it needs no root on the runner.

It works by running the same batch of connections twice and comparing: anything created once
and reused (the .pyc cache, Xvfb's shadow framebuffer, a client's profile) looks identical in
both rounds, while anything leaked per connection shows up as growth. No allowlist to keep up
to date, so a brand new kind of leftover fails this too.

Needs docker.
"""

import re

from common import CONTAINER_NAME, Checks, fetch, layer_diff, run, wait_for_idle

# Subtrees the client asked for and is responsible for. Chrome rewrites a profile's innards on
# every run, so comparing them between rounds would be noise, not signal. The pattern covers
# the profiles the other tests in this directory create too, since they share the container.
CLIENT_PROFILE_DIR = re.compile(r'^/tmp/[^/]*profile[^/]*')
CLIENT_PROFILES = ('/tmp/layer-profile-headless', '/tmp/layer-profile-headful')

# Leftovers that must never appear in /tmp, whoever created them.
LEAK_PATTERNS = (
    re.compile(r'^chrome-puppeteer-proxy'),
    re.compile(r'^\.?org\.chromium\.Chromium\.'),
    re.compile(r'^\.?com\.google\.Chrome\.'),
    re.compile(r'^\.X\d+-lock$'),
    re.compile(r'^xvfb-run\.'),
)


def layer_entries():
    """The container's layer changes, minus any client-owned profile subtree."""
    wait_for_idle()
    return [(change, path) for change, path in layer_diff()
            if not CLIENT_PROFILE_DIR.match(path)]


def summarise(entries):
    """Entry count per top-level directory, so the CI log shows where the layer grew."""
    counts = {}
    for _, path in entries:
        top = '/' + path.lstrip('/').split('/')[0]
        counts[top] = counts.get(top, 0) + 1
    return '  '.join(f"{k}={v}" for k, v in sorted(counts.items()))


async def one_round(label):
    """Two headless connections and one headful, the same way every time."""
    statuses = []
    for _ in range(2):
        status, _, _ = await fetch(screenshot=False,
                                   **{'--user-data-dir': CLIENT_PROFILES[0]})
        statuses.append(status)
    status, _, _ = await fetch(screenshot=False, headful='true',
                               **{'--user-data-dir': CLIENT_PROFILES[1]})
    statuses.append(status)
    print(f"  round {label}: connection statuses {statuses}")
    return statuses


async def main():
    c = Checks(f"Writable layer of {CONTAINER_NAME} after repeated connections")

    first_statuses = await one_round('one')
    first = layer_entries()
    print(f"  layer after round one: {len(first)} entries -> {summarise(first)}")

    second_statuses = await one_round('two')
    second = layer_entries()
    print(f"  layer after round two: {len(second)} entries -> {summarise(second)}")

    c.ok(all(s == 200 for s in first_statuses + second_statuses),
         "both rounds fetched successfully",
         f"{first_statuses} then {second_statuses}")

    added = sorted(set(second) - set(first))
    removed = sorted(set(first) - set(second))
    c.ok(not added, "round two added nothing new to the layer",
         "grew by:\n        " + '\n        '.join(f"{ch} {p}" for ch, p in added))
    c.ok(not removed, "round two removed nothing that round one left",
         "disappeared:\n        " + '\n        '.join(f"{ch} {p}" for ch, p in removed))

    # ...and nothing that looks like a known leftover, whichever round produced it.
    in_tmp = [p.rpartition('/')[2] for _, p in second if p.rpartition('/')[0] == '/tmp']
    leaked = [name for name in in_tmp if any(pat.match(name) for pat in LEAK_PATTERNS)]
    c.ok(not leaked, "no Chrome or Xvfb leftovers anywhere in the layer's /tmp",
         f"found: {leaked}")

    c.done()


if __name__ == '__main__':
    run(main())
