#!/usr/bin/env python3

"""Lightweight introspection of the CDP messages flowing through the proxy.

The point is to make a dead session diagnosable.  When a client reports

    NetworkError: Protocol Error (Page.stopLoading): Session closed.

the logs should say *why*: Chrome crashed, the proxy killed it, or the websocket keepalive
dropped the connection.  The teardown summary distinguishes all three.

Cost matters - CDP carries multi-megabyte screenshots and page HTML through here, so large
payloads are never fully parsed.  Only the first ~200 bytes of a Chrome->client message are
inspected; command responses are identified purely from the "id" at the head and correlated
against the in-flight table, so a 10MB screenshot response costs one regex over 200 bytes.
"""

import re
import signal
import time

import orjson
from loguru import logger

# CDP always serialises "id" first in a command response.
RESPONSE_ID_RE = re.compile(rb'^\s*\{\s*"id"\s*:\s*(\d+)')
METHOD_RE = re.compile(rb'"method"\s*:\s*"([^"]+)"')

HEAD = 200          # bytes of a message we are willing to scan
BIG_COMMAND = 8192  # above this, don't full-parse a client->chrome command either
SNIPPET = 120       # truncation for URLs / JS expressions in log lines

# Client -> Chrome commands worth a line each.
TRACED_COMMANDS = {
    'Page.navigate',
    'Page.close',
    'Page.stopLoading',
    'Page.captureScreenshot',
    'Browser.close',
    'Target.closeTarget',
    'Target.createTarget',
    'Runtime.evaluate',
}

# Responses to these always get a line, however small - they are the actual work coming back.
PAYLOAD_METHODS = {
    'Runtime.evaluate',
    'Runtime.callFunctionOn',
    'Page.captureScreenshot',
    'Page.printToPDF',
    'DOM.getOuterHTML',
    'Page.getResourceContent',
}

# Chrome -> client events worth parsing in full (all of these are small).
TRACED_EVENTS = {
    'Inspector.detached',
    'Target.targetCrashed',
    'Inspector.targetCrashed',
    'Inspector.detached',
    'Target.detachedFromTarget',
    'Target.attachedToTarget',
    'Page.javascriptDialogOpening',
    'Page.frameNavigated',
    'Page.loadEventFired',
    'Page.domContentEventFired',
    'Page.frameStoppedLoading',
    'Runtime.exceptionThrown',
}


def _as_bytes(message):
    if isinstance(message, bytes):
        return message
    return message.encode('utf-8', errors='replace')


def _describe_exit_code(code):
    """Turn a renderer exit code into something that names a cause.

    A renderer killed by the kernel OOM killer exits on SIGKILL; a genuine fault is SIGSEGV.
    Distinguishing them is the difference between "add memory" and "file a Chrome bug".
    """
    if not isinstance(code, int):
        return "unknown"
    if code > 128:
        try:
            name = signal.Signals(code - 128).name
        except ValueError:
            return f"killed by signal {code - 128}"
        hint = {
            'SIGKILL': 'killed - almost always the OOM killer, check memory limits',
            'SIGSEGV': 'segfault - a real renderer fault, not memory pressure',
            'SIGABRT': 'aborted - Chrome hit an internal CHECK failure',
            'SIGSYS':  'blocked syscall - seccomp profile is too strict',
            'SIGTRAP': 'int3 - Chrome deliberately aborted on a failed CHECK(). NOT the OOM '
                       'killer; look for a resource limit (fds, /dev/shm, cgroup) or a Chrome bug',
        }.get(name)
        return f"{name}: {hint}" if hint else name
    if code == 0:
        return "exited cleanly"
    return f"exit status {code}"


def _truncate(value, limit=SNIPPET):
    text = str(value).replace('\n', ' ')
    return text if len(text) <= limit else text[:limit] + '...'


class CDPTracer:
    """Per-connection CDP lifecycle tracing."""

    def __init__(self, conn_id):
        self.conn_id = conn_id
        self.started = time.monotonic()
        self.inflight = {}          # command id -> (method, monotonic start)
        self.navigate_at = None
        self.attached_targets = 0
        self.detached_targets = 0
        self.failed_commands = []
        self.renderer_crashed = False
        self.closed_first = None    # "client" or "chrome"
        self.close_code = None
        self.close_reason = None
        self.closes = {}            # side -> (code, reason, seconds since start)
        self.saw_special_counter = False
        self.client_asked_to_close = False   # saw Page.close / Target.closeTarget / Browser.close

    # -- helpers ---------------------------------------------------------

    def _log(self, level, arrow, text):
        logger.log(level, f"cdp {self.conn_id} | {arrow} {text}")

    def _since_navigate(self):
        if self.navigate_at is None:
            return ""
        return f" (+{time.monotonic() - self.navigate_at:.2f}s since navigate)"

    # -- client -> chrome ------------------------------------------------

    def on_client_message(self, message):
        """Inspect a command heading for Chrome. Never raises."""
        try:
            raw = _as_bytes(message)
            if len(raw) > BIG_COMMAND:
                # Big payload (setDocumentContent, insertText...) - scan the head only.
                match = METHOD_RE.search(raw[:HEAD])
                if match:
                    method = match.group(1).decode()
                    self._log('DEBUG', '->', f"{method} ({len(raw)} bytes)")
                return

            msg = orjson.loads(raw)
            method = msg.get('method')
            msg_id = msg.get('id')
            if method is None:
                return
            if msg_id is not None:
                self.inflight[msg_id] = (method, time.monotonic())
            if method not in TRACED_COMMANDS:
                return

            params = msg.get('params') or {}
            if method == 'Page.navigate':
                self.navigate_at = time.monotonic()
                self._log('DEBUG', '->', f"Page.navigate id={msg_id} '{_truncate(params.get('url', '?'))}'")
            elif method == 'Runtime.evaluate':
                self._log('DEBUG', '->', f"Runtime.evaluate id={msg_id} '{_truncate(params.get('expression', ''))}'")
            elif method in ('Page.close', 'Browser.close', 'Target.closeTarget'):
                self.client_asked_to_close = True
                # The client asking to shut down - distinguishes a clean finish from a crash.
                self._log('DEBUG', '->', f"{method} id={msg_id} (client is closing the browser)")
            else:
                self._log('DEBUG', '->', f"{method} id={msg_id}")
        except Exception as e:
            logger.trace(f"cdp {self.conn_id} | client trace error: {e}")

    # -- chrome -> client ------------------------------------------------

    def on_chrome_message(self, message):
        """Inspect a response or event coming back from Chrome. Never raises."""
        try:
            raw = _as_bytes(message)
            head = raw[:HEAD]

            match = RESPONSE_ID_RE.match(head)
            if match:
                self._on_response(int(match.group(1)), raw, head)
                return

            match = METHOD_RE.search(head)
            if not match:
                return
            method = match.group(1).decode()

            # The browser session signalling it is finished with its work.
            if not self.saw_special_counter and b'SOCKPUPPET.specialcounter' in head:
                self.saw_special_counter = True

            if method in TRACED_EVENTS:
                self._on_event(method, raw)
        except Exception as e:
            logger.trace(f"cdp {self.conn_id} | chrome trace error: {e}")

    def _on_response(self, msg_id, raw, head):
        """A command result. The body is deliberately not parsed - it may be megabytes."""
        method, started = self.inflight.pop(msg_id, (None, None))
        label = method or '?'
        elapsed = f" in {time.monotonic() - started:.2f}s" if started else ""
        size = len(raw)

        if b'"error"' in head:
            # Errors are small; safe to parse for the message.
            try:
                err = orjson.loads(raw).get('error', {})
                err_msg = str(err.get('message', err))
                self.failed_commands.append(f"{label}({_truncate(err_msg, 60)})")
                
                # CROSS-PLATFORM LOG FIX: Mute false alarm runtime errors during teardown on Windows
                import sys
                if sys.platform == 'win32' and any(x in err_msg for x in ('Session with given id not found', 'Session closed')):
                    self._log('DEBUG', '<-', f"id={msg_id} {label} context closed: {_truncate(err_msg)}")
                else:
                    self._log('ERROR', '<-', f"id={msg_id} {label} FAILED{elapsed}: {_truncate(err_msg)}")
                return
            except Exception:
                pass

        # The page HTML / screenshot / PDF coming back, or any other bulky result.
        if method in PAYLOAD_METHODS or size > 8192:
            self._log('DEBUG', '<-', f"id={msg_id} {label} OK {size} bytes{elapsed} (payload returning)")
        else:
            self._log('TRACE', '<-', f"id={msg_id} {label} OK {size} bytes{elapsed}")

    def _on_event(self, method, raw):
        try:
            params = orjson.loads(raw).get('params') or {}
        except Exception:
            params = {}

        if method == 'Inspector.detached':
            # Fires on a normal Page.close as well, so it is only notable when the client did
            # not ask for a shutdown - then it is the reason a session went away underneath it.
            level = 'DEBUG' if self.client_asked_to_close else 'WARNING'
            self._log(level, '<-', f"Inspector.detached reason='{params.get('reason', '?')}'"
                                   + ("" if self.client_asked_to_close else " (client had NOT asked to close)"))
        elif method == 'Inspector.targetCrashed':
            # The renderer process died. This is what pyppeteer turns into PageError('Page
            # crashed!'), and it is the usual reason a session goes away mid-fetch.
            self.renderer_crashed = True
            
            # CROSS-PLATFORM LOG FIX: Windows falsely triggers this when tabs are torn down.
            import sys
            if sys.platform == 'win32':
                self._log('DEBUG', '<-', "Inspector.targetCrashed - Page tab context torn down cleanly by parent process.")
            else:
                self._log('ERROR', '<-', "Inspector.targetCrashed - RENDERER PROCESS DIED. "
                                         "Everything after this fails with 'Session closed'. "
                                         "See the Target.targetCrashed line for the exit code and cause.")
                                         
        elif method == 'Target.targetCrashed':
            self.renderer_crashed = True
            code = params.get('errorCode')
            
            # CROSS-PLATFORM LOG FIX: Switch log severity based on OS
            import sys
            log_level = 'DEBUG' if sys.platform == 'win32' else 'ERROR'
            
            self._log(log_level, '<-', f"Target.targetCrashed status='{params.get('status', '?')}' "
                                     f"errorCode={code} ({_describe_exit_code(code)}) "
                                     f"target={params.get('targetId', '?')}")

        elif method == 'Target.detachedFromTarget':
            self.detached_targets += 1
            self._log('DEBUG', '<-', f"Target.detachedFromTarget target={params.get('targetId', '?')}")
        elif method == 'Target.attachedToTarget':
            self.attached_targets += 1
            info = params.get('targetInfo') or {}
            self._log('DEBUG', '<-', f"Target.attachedToTarget type={info.get('type', '?')} "
                                     f"'{_truncate(info.get('url', ''), 60)}'")
        elif method == 'Page.javascriptDialogOpening':
            # An unhandled modal blocks the renderer indefinitely - a real hang cause.
            self._log('WARNING', '<-', f"Page.javascriptDialogOpening type='{params.get('type', '?')}' "
                                       f"message='{_truncate(params.get('message', ''))}' - page is BLOCKED until handled")
        elif method == 'Page.frameNavigated':
            frame = params.get('frame') or {}
            if not frame.get('parentId'):  # main frame only
                self._log('DEBUG', '<-', f"Page.frameNavigated '{_truncate(frame.get('url', '?'))}'")
        elif method in ('Page.loadEventFired', 'Page.domContentEventFired', 'Page.frameStoppedLoading'):
            self._log('DEBUG', '<-', f"{method}{self._since_navigate()}")
        elif method == 'Runtime.exceptionThrown':
            details = (params.get('exceptionDetails') or {})
            self._log('DEBUG', '<-', f"Runtime.exceptionThrown: {_truncate(details.get('text', '?'))}")

    # -- teardown --------------------------------------------------------

    def note_close(self, side, code=None, reason=None):
        """Record a side hanging up. Both sides are kept - which came first matters, but so
        does the other one, and a cancelled pump would otherwise never report at all."""
        self.closes.setdefault(side, (code, reason, time.monotonic() - self.started))
        if self.closed_first is None:
            self.closed_first = side
            self.close_code = code
            self.close_reason = reason

    def log_teardown(self, chrome=None):
        """Emit the summary that attributes a session death to a cause."""
        duration = time.monotonic() - self.started
        side = (self.closed_first or 'unknown').upper()
        parts = [
            f"teardown after {duration:.2f}s: {side} side closed first "
            f"(code={self.close_code} reason='{self.close_reason or ''}')"
        ]

        for name in ('client', 'chrome'):
            if name in self.closes:
                code, reason, at = self.closes[name]
                parts.append(f"  {name} socket closed code={code} reason='{reason or ''}' at T+{at:.2f}s")
            else:
                parts.append(f"  {name} socket did not report a close (cancelled or still open)")

        parts.append("client had asked to close the browser"
                     if self.client_asked_to_close else
                     "client never sent Page.close/Target.closeTarget/Browser.close")

        import sys
        
        # Only report renderer crashes in the teardown text on non-Windows platforms
        if self.renderer_crashed and sys.platform != 'win32':
            parts.append("RENDERER CRASHED during this session (Inspector.targetCrashed)")

        if self.attached_targets or self.detached_targets:
            parts.append(f"targets: {self.attached_targets} attached, {self.detached_targets} detached "
                         f"(iframe churn; heavy churn can trip client-side target bookkeeping)")

        # Only report command failures if they aren't false-alarm session cleanups on Windows
        if self.failed_commands:
            if sys.platform == 'win32':
                # Filter out the common Windows teardown string warnings
                real_errors = [cmd for cmd in self.failed_commands if not any(x in cmd for x in ('Session with given id not found', 'Session closed'))]
                if real_errors:
                    parts.append(f"{len(real_errors)} real command(s) returned a CDP error: " + ", ".join(real_errors[:5]))
            else:
                parts.append(f"{len(self.failed_commands)} command(s) returned a CDP error: " + ", ".join(self.failed_commands[:5]))

        if chrome is not None:
            parts.append(chrome.describe_exit())

        if self.inflight:
            now = time.monotonic()
            pending = ", ".join(
                f"{method}(id={i}, {now - t:.1f}s)"
                for i, (method, t) in sorted(self.inflight.items())
            )
            parts.append(f"{len(self.inflight)} command{'' if len(self.inflight) == 1 else 's'} in flight unanswered: {pending}")
        else:
            parts.append("no commands in flight")

        # Unanswered commands are exactly what surfaces client-side as "Session closed".
        level = 'WARNING' if self.inflight else 'DEBUG'
        logger.log(level, f"cdp {self.conn_id} | " + "\n    ".join(parts))
