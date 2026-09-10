![Sock Puppet(eer) Browser](docs/sock-puppet-header.png?raw=true "Sock Puppet(eer) Browser Logo Image")
# Sock Puppet(eer) Browser.

## What is this?

This is a high-performance proxy for Chrome so that you can drive many simultaneous Chrome browsers easily and efficiently.

When you connect on `ws://127.0.0.1:3000` as your "CDP Chrome Browser URL" URL it will always spin up a new fresh Chrome instance.


This project is the OpenSource'ed browser back-end for the amazing [opensource web page change detection](https://changedetection.io/) project.

When a request for a new Chrome CDP starts, this software will launch an individual isolated-ish Chrome process
for just that request (This is a Chrome CDP "Proxy")

It is based on the excellent https://github.com/Zenika/alpine-chrome, and we add our own wrapper to launch
individual chrome instances on demand.

When ever something requiring puppeteer connects via `ws://..` it will spin up a new Chrome browser
instance and connect you through (proxy you through) to that Chrome's DevTools connection.

It also handles throttling, scaling, and accepting extra Chrome settings on the connection query.

Under-the-hood it is a simple Python websockets wrapper using a [puppeteer](https://pptr.dev/) image, so 
that we can be sure that all the basic configuration required for Chrome to work will function well.

## Why do I need this?

This provides a Chrome interface to applications that need it, usually for example as required 
when using Playwright - Playwright will launch a `node` instance and start issuing `CDP` (Chrome protocol)
commands to drive the actual project. So you need this project.

(Playwright gives a high-level command set, which talks to `node`, that `node` then does the low-level CDP
commands to drive Chrome directly)

It is also more efficient to not need that extra `node` process like with some other systems 
(you would end up with two node processes).

`playwright -> node -> [sockpuppetserver] -> CDP protocol todo the browser business`

Because this method is always built ontop of the latest puppeteer release, it's a lot more secure and reliable
than relying on projects to invidually update their Chrome browsers and configurations.

You can skip the whole `python` -> `node` mess by using https://github.com/pyppeteer/pyppeteer and talk to this 
container directly.


## How to run

```bash
wget https://raw.githubusercontent.com/dgtlmoon/sockpuppetbrowser/refs/heads/master/chrome.json
docker run --rm --security-opt seccomp=$(pwd)/chrome.json -p 127.0.0.1:3000:3000 dgtlmoon/sockpuppetbrowser
```

`seccomp` security setting is _highly_ recommended https://github.com/Zenika/alpine-chrome?tab=readme-ov-file#-the-best-with-seccomp

### Windows

**Not supported** - but it might work. The proxy is written for, tested on and shipped as a
Linux container; that is the only configuration CI covers. Running `server.py` natively on
Windows is not stopped from working, and the obvious platform bits are handled: `CHROME_BIN`
defaults to `C:\Program Files\Google\Chrome\Application\chrome.exe`, profiles go to the
platform temp dir, and the process tree is torn down through `psutil` rather than anything
POSIX-specific.

Expect these differences, all of them untested by us:

- **Headful mode does not work.** It runs Chrome under `xvfb-run` on a virtual X display.
- **No graceful shutdown.** Chrome flushes cookies and Local Storage when it is asked to exit
  with SIGHUP, which Windows has no equivalent of, so a reused `--user-data-dir` may lose
  whatever was written in the last ~30 seconds of a connection.
- **No orphan sweep.** Cleaning up after a browser whose proxy was killed relies on unix
  sockets and X display locks, so it is skipped.

Patches are welcome, but please open them as their own PR against the current `master` and say
what you tested on - a Windows change that has to be guessed at is worse than none.

### Headful Mode with Virtual Display

By default, Chrome runs in headless mode for maximum performance. However, you can enable "headful" mode which runs Chrome with a virtual X server (Xvfb) for scenarios requiring visual rendering or when certain websites detect headless browsers.

**Enable headful mode:**
```
ws://127.0.0.1:3000/?headful=true
```

Or via environment variable:
```bash
docker run --rm -e CHROME_HEADFUL=true --security-opt seccomp=$(pwd)/chrome.json -p 127.0.0.1:3000:3000 dgtlmoon/sockpuppetbrowser
```

**Headful mode features:**
- Each Chrome instance gets its own isolated virtual display using `xvfb-run -a`
- Automatic display allocation and cleanup when Chrome exits
- Scales efficiently - hundreds of concurrent headful browsers supported
- Better compatibility with websites that detect automation
- Supports visual rendering, screenshots, and DOM operations that require a display

**Performance considerations:**
- Headless mode: ~150+ concurrent browsers per 16-core CPU
- Headful mode: Slightly higher memory usage due to Xvfb processes, but still scales well

### Statistics

Access `http://127.0.0.1:8080/stats` or which ever hostname you bind to, use `--sport` to specify something other than `8080`

```
{
  "active_connections": 158,
  "connection_count_total": 8383,
  "mem_use_percent": 46.9,
  "special_counter_len": 0
}
```

You can also add this to your fetch and access `'special_counter_len'` at the `/stats` URL, this is good for adding at the end of your scripts so you know the actual script ran all steps.

```
        try:
            await self.page._client.send("SOCKPUPPET.specialcounter")
        except:
            pass

```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MAX_CONCURRENT_CHROME_PROCESSES` | `10` | Maximum browsers running at once. |
| `DROP_EXCESS_CONNECTIONS` | `False` | At capacity: `True` refuses new connections immediately, `False` queues them. |
| `CONNECTION_QUEUE_TIMEOUT` | `120` | Seconds a queued connection waits for a slot before being dropped. |
| `CHROME_BIN` | `/usr/bin/google-chrome` | Chrome binary. |
| `CHROME_HEADFUL` | `false` | Run headful under `xvfb-run`. |
| `CHROME_START_TIMEOUT` | `25` | Seconds to wait for Chrome to report its CDP endpoint. |
| `CHROME_SHUTDOWN_GRACE` | `3` | Seconds to let Chrome exit on its own (SIGHUP) before SIGKILL, so it flushes cookies and Local Storage. `0` kills immediately. |
| `SCREEN_WIDTH` / `SCREEN_HEIGHT` | unset | Fallback `--window-size` when the connection URL doesn't set one. |
| `WS_PING_INTERVAL` | `20` | Websocket keepalive ping interval, seconds. |
| `WS_PING_TIMEOUT` | `20` | Seconds to wait for a pong before dropping the connection. |
| `WS_MAX_QUEUE` | `128` | Per-connection receive queue depth (backpressure). |
| `WS_CLOSE_TIMEOUT` | `5` | Seconds to wait for a client's closing handshake before dropping the connection. |
| `SOCKPUPPET_TEMP_ROOT` | `/tmp` | Where each browser's scratch dir (profile + Chrome's own `TMPDIR`) is created. |
| `SOCKPUPPET_SWEEP_MIN_AGE` | `300` | Seconds before an unclaimed scratch dir or X lock is treated as an orphan. |
| `SOCKPUPPET_X_DISPLAY_FLOOR` | `99` | Lowest X display the orphan sweep will release; below this is assumed to be someone else's. |
| `LOG_LEVEL` | `DEBUG` | `TRACE`, `DEBUG`, `INFO`, `SUCCESS`, `WARNING`, `ERROR`, `CRITICAL`. |
| `STATS_REFRESH_SECONDS` | `3` | How often the stats line is logged. |
| `STARTUP_DELAY` | `0` | Sleep before binding, seconds. |
| `ALLOW_CDP_LOG` | `False` | Permit `&log-cdp=` per-connection CDP dumps. |

> **Behaviour change:** `DROP_EXCESS_CONNECTIONS` previously did the *opposite* of its name -
> setting it to `True` made connections queue, and leaving it `False` made them drop. It now
> matches its name. If you had set it to `True` to get queueing, remove it (or set it to `False`).

If Chrome sessions are dying unexpectedly, `WS_PING_TIMEOUT` is worth raising: a busy page can
stall long enough for the keepalive to close an otherwise healthy connection, which surfaces
client-side as `Session closed. Most likely the page has been closed.`

### Diagnosing a dead CDP session

At `DEBUG` the proxy traces the CDP lifecycle, so you can see what the browser was doing:

```
cdp <id> | -> Page.navigate id=12 'https://example.com/opere'
cdp <id> | <- Page.frameNavigated 'https://example.com/opere'
cdp <id> | <- Page.loadEventFired (+2.67s since navigate)
cdp <id> | <- id=41 Runtime.evaluate OK 184320 bytes in 0.31s (payload returning)
cdp <id> | <- Inspector.detached reason='target_closed'
```

When a connection ends, a teardown summary attributes the death:

```
cdp <id> | teardown after 12.05s: CHROME side closed first (code=1006 reason='')
    chrome process: rc=-9 (UNEXPECTED - Chrome died on its own)
    2 commands in flight unanswered: Page.stopLoading(id=44, 2.1s), Runtime.evaluate(id=45, 1.8s)
```

The three cases it distinguishes:

- **`CHROME side closed first` + `UNEXPECTED - Chrome died on its own`** - Chrome crashed (often
  renderer OOM). Check memory and `--disable-dev-shm-usage`.
- **`CHROME side closed first` + `chrome process: still running`** - the CDP websocket dropped
  while Chrome was healthy, i.e. the keepalive. Raise `WS_PING_TIMEOUT`.
- **`CLIENT side closed first`** - your script disconnected; the browser was shut down normally.

Commands listed as *in flight unanswered* are exactly the ones that surface client-side as
`Session closed`.

### Debug CDP session logs

Sometimes you need to examine the low-level Chrome CDP protocol interaction, enable `ALLOW_CDP_LOG=yes` environment 
variable and add `&log-cdp=/path/somefile.txt` to the connection URL.

Then the log will contain the CDP session, for example:

```
1712224824.5491815 - Attempting connection to ws://localhost:56745/devtools/browser/899f78ce-e7c8-4ad1-b8c9-a7aa449a93ef
1712224824.5528538 - Connected to ws://localhost:56745/devtools/browser/899f78ce-e7c8-4ad1-b8c9-a7aa449a93ef
1712224824.5529754 - Puppeteer -> Chrome: {"method": "Target.getBrowserContexts", "params": {}, "id": 1}
1712224824.553542 - Chrome -> Puppeteer: {"id":1,"result":{"browserContextIds":[]}}
...
```

### Setting Viewport Size

Control the browser viewport dimensions for screenshots and page rendering:

**Via connection URL (recommended):**

When running from in thedocker-compose.yml of changedetection.io project.
( https://github.com/dgtlmoon/changedetection.io/blob/dev/docker-compose.yml#L21 )
```
- PLAYWRIGHT_DRIVER_URL=ws://browser-sockpuppet-chrome:3000/?--window-size=1920,1080
```

Or configured from inside the changedetection.io interface as a [**Extra Browsers**] from the settings tab. (when run from the same docker-compose.yml)

<img src=docs/cdio-config-extra-browsers.png alt="Setup and choose mobile browser and other dimensions">

Then you can select the viewport size you like in the browser for checking the web-page for changes.

<img src=docs/cdio-browser-selection.png alt="Choosing a different size browser for checking a page for changes.">


**Or, via environment variables:**

You can run this container with a different default size.

```bash
docker run -e SCREEN_WIDTH=1920 -e SCREEN_HEIGHT=1080 --security-opt seccomp=$(pwd)/chrome.json -p 127.0.0.1:3000:3000 dgtlmoon/sockpuppetbrowser
```

If neither is specified, Chrome will use its default viewport size.

### Profile directories (`--user-data-dir`)

- **No `--user-data-dir` on the connection URL** - the proxy creates a throwaway profile for
  that connection and deletes it at teardown, along with everything else Chrome wrote to temp.
  Nothing to clean up. (One profile per *connection*, not per page: it lives as long as the
  websocket, however many pages you load over it. The only leftovers are from the proxy itself
  being killed, and the next start-up sweeps those.)
- **`--user-data-dir` supplied** - it is yours. The proxy never creates it, never deletes it
  and never looks inside, so the profile persists and can be reused by later connections.

Which one you pick decides whether you run out of inodes. A profile is ~198 files and 2.3 MB
(cookies, history, login data, caches):

| what you pass | left behind per connection | at 100k pages/day |
|---|---|---|
| nothing | nothing | zero |
| one path, reused | +0 files (plateaus at ~217 files) | ~2.3 MB per path, flat |
| a fresh path each time | ~198 files, 2.3 MB | ~20M files, ~228 GB - inode exhaustion |

So only pass `--user-data-dir` when you need the profile to persist, and reuse a bounded set of
paths rather than one per watch. A profile serves **one connection at a time** - two at once
and the second Chrome exits with code 21 (`The profile appears to be in use...`) - so size that
set to your concurrency.

Sessions do survive a reused profile: on teardown the proxy SIGHUPs Chrome and gives it up to
`CHROME_SHUTDOWN_GRACE` seconds to exit on its own, which is what makes it flush its cookie
store and Local Storage. It normally takes ~0.2s. Without that Chrome gets SIGKILLed and
anything written in the last 30 seconds is lost - Chrome batches cookie writes on a 30s timer,
so a login done in a short session used to vanish silently. `sessionStorage` never carries
over, by definition.

> **@todo for changedetection.io:** reusing a profile dir now keeps a login, so this is no
> longer a correctness problem - but at volume the cheaper option is not to use a profile dir
> at all. Pull the cookies out over CDP with `Network.getAllCookies` at the end of a session,
> keep them client-side, and re-inject with `Network.setCookies` on the next connection. No
> `--user-data-dir` means no ~198 files per watch to clean up, no one-connection-at-a-time
> limit on a shared profile, and the login survives even a hard kill of the container, where
> the proxy never gets to shut Chrome down politely.

### Tuning

Some tips on high-concurrency scraping and tuning where you have a lot of chrome browsers running simultaneously

- Understand different Chrome command line options https://github.com/GoogleChrome/chrome-launcher/blob/main/docs/chrome-flags-for-tools.md and specify them on the connection URL
- Set your `inotify` values higher https://stackoverflow.com/questions/32281277/too-many-open-files-failed-to-initialize-inotify-the-user-limit-on-the-total
- Don't burn out your disk!! Keep browser profiles on a RAM disk/tmpfs - point
  `SOCKPUPPET_TEMP_ROOT` at it (or mount your own `--user-data-dir` there). This also speeds
  Chrome up. See [Profile directories](#profile-directories---user-data-dir) first: at volume,
  *which* profile path you use matters more than what it is mounted on

On a `Intel(R) Xeon(R) E-2288G CPU @ 3.70GHz` (16 core), it will sustain 150 concurrent browser sessions with a load average of about 65-70 (about 3-4 browsers per CPU core it means).

Most of the CPU load seems to occur when starting a browser, maybe in the future 1 browser could processes multiple requests.

### Running the tests

`pyppeteer-tests/` drives a running container over CDP with pyppeteer-ng - the same client
changedetection.io uses - and checks that pages load and that nothing is left behind
afterwards. Against a container already listening on the default ports:

```bash
pip3 install -r pyppeteer-tests/requirements.txt
cd pyppeteer-tests
python3 test_fetch_page.py          # loads example.com headless, headful and concurrently
python3 test_temp_dir_cleanup.py    # /tmp is untouched after every teardown
python3 test_container_layer.py     # nothing accumulates in the container's writable layer
python3 test_proxy_killed.py        # orphans are swept after a SIGKILL mid-connection
```

`CDP_URL`, `STATS_URL`, `CONTAINER_NAME` and `TEST_URL` override where they point. The three
that inspect the container need docker; `test_proxy_killed.py` stops and starts it.

CI runs all of it against several browser builds in parallel - current Chrome Stable, a pinned
Chrome, and the Chromium 119 image (`Dockerfile.chromium119`) that earlier releases shipped -
because they do not behave identically. Chromium 119's old `--headless` has no
`ProcessSingleton` and so creates no singleton socket dir, while current Chrome's does, and
`chrome.json`'s seccomp profile has to suit both musl and glibc. To test another version:

```bash
docker build -t sock:test --build-arg CHROME_VERSION=151.0.7922.173-1 .   # or "current"
```

Google's deb pool only keeps recent releases, so old pins will 404.

### Docker healthcheck

Add this to your `docker-compose.yml`, it will check port 3000 answers and that the `/stats` endpoint on port 8080 responds

```
    healthcheck:
      test: "python3 /usr/src/app/docker-health-check.py --host http://localhost"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
```

To review deeper docker container information about the containers health
```
docker inspect --format='{{json .State.Health}}' browser-sockpuppetbrowser-1
```

### Future ideas

- Some super cool "on the wire" hacks to add custom functionality to CDP, like issuing single commands to download files (PDF) to location https://github.com/dgtlmoon/changedetection.io/issues/2019


Have fun!
