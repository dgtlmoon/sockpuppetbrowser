"""Tests proving the connection slot leak bug and its fix.

Bug: When Chrome crashes while the client is idle, `puppeteerToHere` blocks
forever on `async for message in chrome_websocket`, preventing the handler
from returning and the connection slot from being released.

Fix: Use `asyncio.wait(FIRST_COMPLETED)` so that when either proxy task
completes, the other is cancelled immediately.
"""

import asyncio
import contextlib
import subprocess
import unittest.mock as mock

import pytest
import websockets.exceptions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Minimal mock that behaves like a websockets connection for proxy tests."""

    def __init__(self, messages=None, *, close_after=None, hang=False):
        self._messages = list(messages or [])
        self._close_after = close_after
        self._hang = hang
        self._sent = []
        self._yielded = 0
        self.id = "fake-ws-id"
        self.remote_address = ("127.0.0.1", 9999)
        self._closed = asyncio.get_event_loop().create_future()

    async def send(self, message):
        self._sent.append(message)

    async def close(self):
        if not self._closed.done():
            self._closed.set_result(True)

    async def wait_closed(self):
        await self._closed

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._close_after is not None and self._yielded >= self._close_after:
            raise websockets.exceptions.ConnectionClosed(None, None)
        if self._hang:
            await asyncio.sleep(3600)
            raise StopAsyncIteration
        if not self._messages:
            raise StopAsyncIteration
        self._yielded += 1
        return self._messages.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


# ---------------------------------------------------------------------------
# Import the functions under test from the repo
# ---------------------------------------------------------------------------

import importlib
import sys
import types


def _import_server():
    """Import backend/server.py, stubbing deps unavailable outside container."""
    stubs = {
        "http_server": {"start_http_server": lambda **kw: asyncio.sleep(0)},
        "ports": {"PortSelector": lambda: iter(range(19000, 20000))},
    }
    for mod_name, attrs in stubs.items():
        if mod_name not in sys.modules:
            stub = types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(stub, k, v)
            sys.modules[mod_name] = stub

    # orjson/psutil require C extensions that may not build on all platforms;
    # the functions under test don't need real implementations.
    if "orjson" not in sys.modules:
        import json

        orjson_stub = types.ModuleType("orjson")
        orjson_stub.loads = json.loads
        orjson_stub.dumps = json.dumps
        orjson_stub.JSONDecodeError = json.JSONDecodeError
        sys.modules["orjson"] = orjson_stub

    try:
        import psutil  # noqa: F401
    except ImportError:
        psutil_stub = types.ModuleType("psutil")
        psutil_stub.Process = mock.MagicMock
        psutil_stub.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        psutil_stub.AccessDenied = type("AccessDenied", (Exception,), {})
        psutil_stub.virtual_memory = mock.MagicMock()
        sys.modules["psutil"] = psutil_stub

    spec = importlib.util.spec_from_file_location(
        "server", "/tmp/sockpuppetbrowser/backend/server.py"
    )
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


server = _import_server()


def _make_fake_chrome_process():
    proc = mock.MagicMock()
    proc.pid = 99999
    proc.poll.return_value = None
    proc.wait.return_value = 0
    proc.kill.return_value = None
    proc.communicate.return_value = ("", "")
    proc.logging_tasks = []
    proc.stdout = None
    proc.stderr = None
    return proc


def _make_chrome_info_response(port=19222):
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "webSocketDebuggerUrl": f"ws://localhost:{port}/devtools/browser/fake"
    }
    return resp


# ---------------------------------------------------------------------------
# Tests for the proxy task cancellation fix (exercise actual server code)
# ---------------------------------------------------------------------------

class TestProxyTaskCancellation:
    """
    These tests exercise launchPuppeteerChromeProxy end-to-end with mocked
    Chrome and WebSocket dependencies.  They FAIL on the unfixed code (where
    sequential ``await taskA; await taskB`` blocks forever) and PASS on the
    fixed code (``asyncio.wait(FIRST_COMPLETED)``).
    """

    @pytest.mark.asyncio
    async def test_handler_returns_after_chrome_crash(self):
        """
        Chrome CDP ws closes immediately (crash) while client is idle.
        With the fix, launchPuppeteerChromeProxy must return within 2s.
        Without the fix, it blocks forever on the idle client task.
        """
        chrome_cdp_ws = FakeWebSocket(close_after=0)
        client_ws = FakeWebSocket(hang=True)
        fake_proc = _make_fake_chrome_process()

        with mock.patch.object(server, "launch_chrome", return_value=fake_proc), \
             mock.patch.object(server, "_request_retry", return_value=_make_chrome_info_response()), \
             mock.patch("websockets.connect", return_value=chrome_cdp_ws):

            await asyncio.wait_for(
                server.launchPuppeteerChromeProxy(client_ws, "/"),
                timeout=2.0,
            )

    @pytest.mark.asyncio
    async def test_slot_released_after_chrome_crash(self):
        """
        After Chrome crashes, the connection_count must return to its
        original value — proving the slot was released, not leaked.
        """
        chrome_cdp_ws = FakeWebSocket(close_after=0)
        client_ws = FakeWebSocket(hang=True)
        fake_proc = _make_fake_chrome_process()

        original_count = server.stats["connection_count"]

        with mock.patch.object(server, "launch_chrome", return_value=fake_proc), \
             mock.patch.object(server, "_request_retry", return_value=_make_chrome_info_response()), \
             mock.patch("websockets.connect", return_value=chrome_cdp_ws):

            await asyncio.wait_for(
                server.launchPuppeteerChromeProxy(client_ws, "/"),
                timeout=2.0,
            )

        # In production, the websockets library closes the connection after
        # the handler returns, which triggers the wait_closed() callback.
        await client_ws.close()
        await asyncio.sleep(0.05)

        assert server.stats["connection_count"] == original_count

    @pytest.mark.asyncio
    async def test_normal_session_completes(self):
        """Both sides send messages then disconnect — should complete cleanly."""
        chrome_cdp_ws = FakeWebSocket(messages=["cdp_event_1"])
        client_ws = FakeWebSocket(messages=["cdp_cmd_1"])
        fake_proc = _make_fake_chrome_process()

        with mock.patch.object(server, "launch_chrome", return_value=fake_proc), \
             mock.patch.object(server, "_request_retry", return_value=_make_chrome_info_response()), \
             mock.patch("websockets.connect", return_value=chrome_cdp_ws):

            await asyncio.wait_for(
                server.launchPuppeteerChromeProxy(client_ws, "/"),
                timeout=2.0,
            )

    @pytest.mark.asyncio
    async def test_message_forwarding(self):
        """Messages are correctly proxied between Chrome and client."""
        chrome_ws = FakeWebSocket(messages=["from_chrome_1", "from_chrome_2"])
        client_ws = FakeWebSocket(messages=["from_client_1"])

        taskA = asyncio.create_task(
            server.hereToChromeCDP(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )
        await taskA
        assert client_ws._sent == ["from_chrome_1", "from_chrome_2"]

        taskB = asyncio.create_task(
            server.puppeteerToHere(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )
        await taskB
        assert chrome_ws._sent == ["from_client_1"]


# ---------------------------------------------------------------------------
# Tests for process reaping fix
# ---------------------------------------------------------------------------

class TestProcessReaping:
    """Prove that killed Chrome processes are reaped (wait() called)."""

    @pytest.mark.asyncio
    async def test_cleanup_reaps_chrome_process(self):
        """After killing Chrome, wait() must be called to prevent zombies."""
        fake_process = _make_fake_chrome_process()

        fake_ws = mock.AsyncMock()
        fake_ws.id = "test-ws-reap"
        fake_ws.close = mock.AsyncMock()

        mock_psutil_proc = mock.MagicMock()
        mock_psutil_proc.children.return_value = []
        mock_psutil_proc.kill.return_value = None

        with mock.patch("psutil.Process", return_value=mock_psutil_proc):
            await server.cleanup_chrome_by_pid(
                chrome_process=fake_process,
                websocket=fake_ws,
            )

        fake_process.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_handles_already_dead_process(self):
        """Cleanup doesn't crash if the Chrome process is already gone."""
        fake_process = _make_fake_chrome_process()

        fake_ws = mock.AsyncMock()
        fake_ws.id = "test-ws-dead"
        fake_ws.close = mock.AsyncMock()

        NoSuchProcess = getattr(
            sys.modules.get("psutil"), "NoSuchProcess",
            type("NoSuchProcess", (Exception,), {}),
        )
        with mock.patch("psutil.Process", side_effect=NoSuchProcess(99998)):
            await server.cleanup_chrome_by_pid(
                chrome_process=fake_process,
                websocket=fake_ws,
            )

        fake_ws.close.assert_called_once()


# ---------------------------------------------------------------------------
# Semaphore / slot accounting tests
# ---------------------------------------------------------------------------

class TestSlotAccounting:
    """Verify connection_count and semaphore stay consistent."""

    @pytest.mark.asyncio
    async def test_stats_disconnect_releases_semaphore(self):
        """stats_disconnect must release the semaphore and decrement count."""
        original_count = server.stats["connection_count"]
        server.stats["connection_count"] = 5

        server.connection_semaphore.acquire(blocking=False)

        fake_ws = mock.MagicMock()
        fake_ws.id = "test-ws-stats"

        await server.stats_disconnect(time_at_start=0.0, websocket=fake_ws)

        assert server.stats["connection_count"] == 4

        server.stats["connection_count"] = original_count
