"""asyncio JSON-RPC client for signal-cli's call API.

Connects to the signal-cli daemon (via the signal-api container's
jsonrpc-forward), subscribes to call events and reconnects with backoff.
Every connection also gets "receive" notifications for normal messages;
those are ignored here (nixclaw gets messages via the REST webhook).
"""

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

LOG = logging.getLogger(__name__)

CallEventHandler = Callable[[dict], Awaitable[None]]

READ_LIMIT = 16 * 1024 * 1024  # receive notifications can be large
MAX_BACKOFF_SECONDS = 30.0


class RpcError(Exception):
    pass


class SignalCallRpc:
    def __init__(self, host: str, port: int, account: str, on_call_event: CallEventHandler):
        self.host = host
        self.port = port
        self.account = account
        self._on_call_event = on_call_event
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._next_id = 0
        self._events: asyncio.Queue[dict] = asyncio.Queue()
        self._stopping = False
        self.connected = asyncio.Event()

    async def run(self) -> None:
        """Connect, subscribe, read until disconnected; repeat until stop()."""
        dispatcher = asyncio.create_task(self._dispatch_events())
        backoff = 1.0
        try:
            while not self._stopping:
                try:
                    reader, writer = await asyncio.open_connection(
                        self.host, self.port, limit=READ_LIMIT)
                except OSError as e:
                    LOG.warning(f"voice: cannot reach signal-cli JSON-RPC at "
                                f"{self.host}:{self.port}: {e}; retry in {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                self._writer = writer
                reader_task = asyncio.create_task(self._read_loop(reader))
                try:
                    await self.call("subscribeCallEvents")
                    LOG.info(f"voice: subscribed to call events on {self.host}:{self.port}")
                    self.connected.set()
                    backoff = 1.0
                    await reader_task
                except (RpcError, asyncio.TimeoutError, ConnectionError) as e:
                    LOG.warning(f"voice: JSON-RPC session failed: {e}")
                finally:
                    self.connected.clear()
                    reader_task.cancel()
                    self._fail_pending(ConnectionError("JSON-RPC connection lost"))
                    writer.close()
                    self._writer = None
                if not self._stopping:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        finally:
            dispatcher.cancel()

    def stop(self) -> None:
        self._stopping = True
        if self._writer is not None:
            self._writer.close()

    async def call(self, method: str, timeout: float = 15.0, **params: Any) -> Any:
        if self._writer is None:
            raise ConnectionError("JSON-RPC not connected")
        self._next_id += 1
        rid = str(self._next_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        req = {"jsonrpc": "2.0", "id": rid, "method": method,
               "params": {"account": self.account, **params}}
        try:
            self._writer.write(json.dumps(req).encode() + b"\n")
            await self._writer.drain()
            msg = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
        if "error" in msg:
            raise RpcError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result")

    async def accept(self, call_id: int) -> dict:
        return await self.call("acceptCall", callId=call_id)

    async def reject(self, call_id: int) -> None:
        await self.call("rejectCall", callId=call_id)

    async def hangup(self, call_id: int) -> None:
        await self.call("hangupCall", callId=call_id)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                LOG.warning("voice: JSON-RPC connection closed by signal-cli")
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending[rid]
                if not fut.done():
                    fut.set_result(msg)
            elif msg.get("method") == "callEvent":
                result = (msg.get("params") or {}).get("result")
                if isinstance(result, dict):
                    self._events.put_nowait(result)

    async def _dispatch_events(self) -> None:
        # Separate task: handlers make RPC calls themselves, which need the
        # read loop running; events stay in order.
        while True:
            event = await self._events.get()
            try:
                await self._on_call_event(event)
            except Exception:
                LOG.exception(f"voice: call event handler failed for {event}")

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
