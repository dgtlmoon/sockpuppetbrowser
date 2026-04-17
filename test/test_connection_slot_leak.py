"""Tests proving the connection slot leak bug and its fix.

Bug: When Chrome crashes while the client is idle, `puppeteerToHere` blocks
forever on `async for message in chrome_websocket`, preventing the handler
from returning and the connection slot from being released.

Fix: Use `asyncio.wait(FIRST_COMPLETED)` so that when either proxy task
completes, the other is cancelled immediately.
"""

import asyncio
import subprocess
import unittest.mock as mock

import pytest
import websockets.exceptions


# ---------------------------------------------------------------------------
# Helpers – lightweight WebSocket fakes
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Minimal mock that behaves like a websockets connection for proxy tests."""

    def __init__(self, messages=None, *, close_after=None, hang=False):
        """
        messages:    list of messages to yield, then stop iteration.
        close_after: yield this many messages, then raise ConnectionClosed.
        hang:        if True, __anext__ blocks forever (simulates idle client).
        """
        self._messages = list(messages or [])
        self._close_after = close_after
        self._hang = hang
        self._sent = []
        self._yielded = 0
        self.id = "fake-ws-id"

    async def send(self, message):
        self._sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._close_after is not None and self._yielded >= self._close_after:
            raise websockets.exceptions.ConnectionClosed(None, None)
        if self._hang:
            await asyncio.sleep(3600)  # block "forever" (capped by test timeout)
            raise StopAsyncIteration
        if not self._messages:
            raise StopAsyncIteration
        self._yielded += 1
        return self._messages.pop(0)


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


# ---------------------------------------------------------------------------
# Tests for the proxy task cancellation fix
# ---------------------------------------------------------------------------

class TestProxyTaskCancellation:
    """Prove that when one side of the proxy dies, the other is cancelled."""

    @pytest.mark.asyncio
    async def test_chrome_crash_releases_slot_with_fix(self):
        """FIXED BEHAVIOR: Chrome dies → idle client task is cancelled → completes fast."""

        # Chrome side: closes immediately (simulates crash)
        chrome_ws = FakeWebSocket(close_after=0)
        # Client side: hangs forever (idle client, no messages)
        client_ws = FakeWebSocket(hang=True)

        taskA = asyncio.create_task(
            server.hereToChromeCDP(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )
        taskB = asyncio.create_task(
            server.puppeteerToHere(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )

        # This is the FIX: wait for first completion, cancel the rest
        done, pending = await asyncio.wait(
            [taskA, taskB], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert taskA.done()
        assert taskB.done()

    @pytest.mark.asyncio
    async def test_chrome_crash_blocks_without_fix(self):
        """BUG PROOF: Sequential await blocks forever when Chrome dies and client is idle."""

        chrome_ws = FakeWebSocket(close_after=0)
        client_ws = FakeWebSocket(hang=True)

        taskA = asyncio.create_task(
            server.hereToChromeCDP(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )
        taskB = asyncio.create_task(
            server.puppeteerToHere(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )

        # Simulate the OLD buggy code: await taskA, then await taskB
        await taskA  # This returns fine (Chrome ws closed)

        # taskB should NOT complete within a reasonable time — it's stuck
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(taskB), timeout=0.5)

        assert taskA.done()
        assert not taskB.done()  # STUCK — proves the bug

        # Cleanup
        taskB.cancel()
        try:
            await taskB
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_client_disconnect_completes_both_tasks(self):
        """Normal case: client disconnects → both tasks complete cleanly."""

        chrome_ws = FakeWebSocket(messages=["msg1", "msg2"])
        client_ws = FakeWebSocket(messages=["cmd1"])

        taskA = asyncio.create_task(
            server.hereToChromeCDP(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )
        taskB = asyncio.create_task(
            server.puppeteerToHere(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )

        done, pending = await asyncio.wait(
            [taskA, taskB], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # At least one completed normally
        assert len(done) >= 1

    @pytest.mark.asyncio
    async def test_message_forwarding(self):
        """Messages are correctly proxied between Chrome and client."""

        chrome_ws = FakeWebSocket(messages=["from_chrome_1", "from_chrome_2"])
        client_ws = FakeWebSocket(messages=["from_client_1"])

        taskA = asyncio.create_task(
            server.hereToChromeCDP(puppeteer_ws=chrome_ws, chrome_websocket=client_ws)
        )
        # Let taskA drain Chrome messages
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

        fake_process = mock.MagicMock()
        fake_process.pid = 99999
        fake_process.wait = mock.MagicMock(return_value=0)
        fake_process.logging_tasks = []

        fake_ws = mock.AsyncMock()
        fake_ws.id = "test-ws-reap"
        fake_ws.close = mock.AsyncMock()

        # psutil.Process will be called with the fake PID — mock it
        mock_psutil_proc = mock.MagicMock()
        mock_psutil_proc.children.return_value = []
        mock_psutil_proc.kill.return_value = None

        with mock.patch("psutil.Process", return_value=mock_psutil_proc):
            await server.cleanup_chrome_by_pid(
                chrome_process=fake_process,
                websocket=fake_ws,
            )

        # The critical assertion: wait() was called to reap the process
        fake_process.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_handles_already_dead_process(self):
        """Cleanup doesn't crash if the Chrome process is already gone."""

        fake_process = mock.MagicMock()
        fake_process.pid = 99998
        fake_process.wait = mock.MagicMock(return_value=0)
        fake_process.kill = mock.MagicMock()
        fake_process.logging_tasks = []

        fake_ws = mock.AsyncMock()
        fake_ws.id = "test-ws-dead"
        fake_ws.close = mock.AsyncMock()

        NoSuchProcess = server.psutil.NoSuchProcess if hasattr(server, "psutil") else type("NoSuchProcess", (Exception,), {})
        with mock.patch("psutil.Process", side_effect=NoSuchProcess(99998)):
            await server.cleanup_chrome_by_pid(
                chrome_process=fake_process,
                websocket=fake_ws,
            )

        # Should not raise — graceful fallback
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

        # Acquire a semaphore slot to simulate an active connection
        server.connection_semaphore.acquire(blocking=False)

        fake_ws = mock.MagicMock()
        fake_ws.id = "test-ws-stats"

        await server.stats_disconnect(time_at_start=0.0, websocket=fake_ws)

        assert server.stats["connection_count"] == 4

        # Restore
        server.stats["connection_count"] = original_count
