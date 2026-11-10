"""The zenoh side: one plain session that watches a zenode system.

Not a ``zenode.Node`` on purpose — a monitor that announced itself with a
presence token and a heartbeat would show up in the system it observes.

Zenoh invokes callbacks on its own threads. Each one does a single thing: hand
the raw data to the event loop, where the :class:`Store` is updated.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any

import zenoh
import zenoh.ext as zext
from zenode import PresenceWatcher, Service, ServiceError, ServiceTimeout, TransportConfig
from zenode.codec import RawCodec
from zenode.envelope import decode_envelope
from zenode.msgs import health_pattern, info_pattern, log_pattern
from zenode.service import call_service

from .state import Store, TopicSample

MAX_PENDING = 2000
"""Topic samples allowed in flight to the loop before new ones are dropped."""

_JSON = RawCodec(zenoh.Encoding.APPLICATION_JSON)
_RAW = RawCodec()


@dataclass
class CallResult:
    ok: bool
    text: str
    elapsed_ms: float
    kind: str = "reply"  # reply | error | timeout


class MonitorBus:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.transport: TransportConfig | None = None
        self._session: zenoh.Session | None = None
        self._owns_session = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._presence: PresenceWatcher | None = None
        self._subs: list[Any] = []
        self._topic_sub: Any = None
        self._topic_gen = 0
        self._pending = 0

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def connect(self, transport: TransportConfig, session: zenoh.Session | None = None) -> None:
        """Open a session and start watching. Raises on failure, leaving the bus disconnected."""
        await self.disconnect()
        self._loop = asyncio.get_running_loop()
        self._owns_session = session is None
        if session is None:
            session = await asyncio.to_thread(zenoh.open, transport.to_zenoh_config())
        self._session, self.transport = session, transport
        ns = transport.namespace
        self.store.reset(ns)
        try:
            self._presence = PresenceWatcher(session, ns, self.store.on_presence, self._loop)
            self._presence.start()
            # The descriptor is latched: only an advanced subscriber receives the cached copy.
            self._subs.append(
                zext.declare_advanced_subscriber(
                    session,
                    info_pattern(ns),
                    self._forward(self.store.on_info),
                    history=zext.HistoryConfig(detect_late_publishers=True, max_samples=1),
                )
            )
            self._subs.append(
                session.declare_subscriber(health_pattern(ns), self._forward(self.store.on_health))
            )
            self._subs.append(
                session.declare_subscriber(log_pattern(ns), self._forward(self.store.on_log))
            )
        except Exception:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        self.unwatch_topic()
        if self._presence is not None:
            with contextlib.suppress(Exception):
                self._presence.stop()
            self._presence = None
        for sub in self._subs:
            with contextlib.suppress(Exception):
                sub.undeclare()
        self._subs.clear()
        session, self._session = self._session, None
        if session is not None and self._owns_session:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(session.close)

    def _forward(self, handler: Any) -> Any:
        loop = self._loop
        assert loop is not None

        def on_sample(sample: zenoh.Sample) -> None:  # zenoh thread
            with contextlib.suppress(RuntimeError):  # loop closed during shutdown
                loop.call_soon_threadsafe(handler, sample.payload.to_bytes())

        return on_sample

    # -- the one topic open in the UI ------------------------------------------

    def watch_topic(self, key: str, latched: bool = False) -> None:
        self.unwatch_topic()
        session, loop = self._session, self._loop
        if session is None or loop is None:
            return
        self.store.stream.reset(key)
        self._topic_gen += 1
        gen = self._topic_gen

        def deliver(sample: TopicSample) -> None:
            self._pending -= 1
            if gen == self._topic_gen:
                self.store.on_sample(sample)

        def on_sample(sample: zenoh.Sample) -> None:  # zenoh thread
            if self._pending >= MAX_PENDING:
                self.store.dropped_events += 1
                return
            attachment = sample.attachment
            key_expr = str(sample.key_expr)
            item = TopicSample(
                recv_ns=time.time_ns(),
                key=key_expr,
                payload=sample.payload.to_bytes(),
                encoding=str(sample.encoding),
                envelope=decode_envelope(attachment.to_bytes() if attachment else None, key_expr),
            )
            self._pending += 1
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver, item)

        if latched:
            self._topic_sub = zext.declare_advanced_subscriber(
                session,
                key,
                on_sample,
                history=zext.HistoryConfig(detect_late_publishers=True, max_samples=1),
            )
        else:
            self._topic_sub = session.declare_subscriber(key, on_sample)

    def unwatch_topic(self) -> None:
        self._topic_gen += 1
        sub, self._topic_sub = self._topic_sub, None
        if sub is not None:
            with contextlib.suppress(Exception):
                sub.undeclare()

    # -- services ----------------------------------------------------------------

    async def call(self, key: str, payload: bytes, timeout: float = 2.0) -> CallResult:
        """Call the service on ``key`` with a JSON request body."""
        session = self._session
        if session is None:
            return CallResult(False, "Not connected", 0.0, "error")
        service: Service[bytes, bytes] = Service(
            key, bytes, bytes, request_codec=_JSON, reply_codec=_RAW
        )
        started = time.perf_counter()
        try:
            data = await call_service(
                session, service, key, payload or b"{}", timeout=timeout, node="zenode-monitor"
            )
        except ServiceTimeout:
            return CallResult(False, f"No reply within {timeout:g} s", _ms(started), "timeout")
        except ServiceError as e:
            lines = str(e).removeprefix(f"{key}: ").splitlines()
            # pydantic appends a documentation link to every validation error; it is noise here.
            text = "\n".join(ln for ln in lines if "errors.pydantic.dev" not in ln)
            return CallResult(False, text, _ms(started), "error")
        except Exception as e:  # zenoh-level failure
            return CallResult(False, f"{type(e).__name__}: {e}", _ms(started), "error")
        return CallResult(True, data.decode("utf-8", "replace"), _ms(started))


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
