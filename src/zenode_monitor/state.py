"""Plain-Python model of the observed zenode system. No Flet, no zenoh I/O.

Everything here runs on the UI event loop: the bus marshals zenoh callbacks onto
the loop before calling in, so no locking is needed.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import zenoh
from zenode.envelope import Envelope
from zenode.msgs import (
    RUNTIME_MEASURES,
    LogRecordMsg,
    MeasureDescriptor,
    NodeHealth,
    NodeInfo,
    ServiceInfo,
)

HISTORY = 300
"""Points kept per metric: ten minutes at the default 2 s heartbeat."""
LOG_CAPACITY = 2000
STREAM_CAPACITY = 500
STREAM_BYTE_BUDGET = 64 * 1024 * 1024
"""Payload bytes the open topic may hold; beyond it, older large payloads are cut to a head."""
PAYLOAD_HEAD = 4096
SILENT_AFTER_BEATS = 3
DEFAULT_HEALTH_INTERVAL = 2.0

RUNTIME: dict[str, MeasureDescriptor] = {m.id: m for m in RUNTIME_MEASURES}
_SYSTEM_KEY = re.compile(r"(^|/)node/[^/]+/(health|info|log|trace)$")

# Status values, in the order the UI sorts them.
RUNNING, STARTING, STOPPING, STOPPED, SILENT, ONLINE, OFFLINE = (
    "running",
    "starting",
    "stopping",
    "stopped",
    "silent",
    "online",
    "offline",
)


@dataclass
class Node:
    name: str
    alive: bool = False
    health: NodeHealth | None = None
    info: NodeInfo | None = None
    last_beat: float | None = None
    """``time.monotonic()`` of the last heartbeat."""
    history: dict[str, deque[tuple[float, float]]] = field(default_factory=dict)
    """Metric id -> (wall time, value). Counters are stored as per-second rates."""
    silent: bool = False

    @property
    def interval(self) -> float:
        if self.info is not None and self.info.health_interval:
            return self.info.health_interval
        return DEFAULT_HEALTH_INTERVAL

    @property
    def status(self) -> str:
        if not self.alive:
            return OFFLINE
        if self.health is None:
            return ONLINE  # token held, no heartbeat seen (yet, or disabled)
        if self.silent:
            return SILENT
        return self.health.state

    @property
    def host(self) -> str:
        if self.health is not None and self.health.host:
            return self.health.host
        return self.info.host if self.info is not None else ""

    def descriptor(self, metric: str) -> MeasureDescriptor | None:
        if metric in RUNTIME:
            return RUNTIME[metric]
        if self.info is not None:
            for m in self.info.measures:
                if m.id == metric:
                    return m
        return None

    def latest(self, metric: str) -> float | None:
        series = self.history.get(metric)
        return series[-1][1] if series else None


@dataclass
class TopicEntry:
    key: str
    schema_name: str = ""
    flags: list[str] = field(default_factory=list)
    publishers: set[str] = field(default_factory=set)
    subscribers: set[str] = field(default_factory=set)
    system: bool = False
    matched: bool = True
    """False when nodes subscribe but nothing publishes a matching key."""

    @property
    def latched(self) -> bool:
        return any(f.startswith("latched") for f in self.flags)

    @property
    def wildcard(self) -> bool:
        return "*" in self.key


@dataclass
class ServiceEntry:
    key: str
    info: ServiceInfo
    nodes: set[str] = field(default_factory=set)


@dataclass
class TopicSample:
    recv_ns: int
    key: str
    payload: bytes
    encoding: str
    envelope: Envelope
    size: int = -1
    """Bytes on the wire; ``payload`` may later be cut down to its head."""
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.size < 0:
            self.size = len(self.payload)

    @property
    def is_json(self) -> bool:
        return "json" in self.encoding

    @property
    def is_image(self) -> bool:
        return self.encoding.startswith("image/") and not self.truncated

    def preview(self, limit: int = 160) -> str:
        if self.is_json or self.encoding.startswith("text/"):
            text = self.payload[: limit * 4].decode("utf-8", "replace").replace("\n", " ")
            return text if len(text) <= limit else text[: limit - 1] + "…"
        head = self.payload[:24].hex(" ")
        more = "…" if self.size > 24 else ""
        return f"{fmt_bytes(self.size)}  {head}{more}"

    def pretty(self) -> str:
        if self.is_json:
            try:
                return json.dumps(json.loads(self.payload), indent=2, ensure_ascii=False)
            except ValueError:
                pass
        if self.is_json or self.encoding.startswith("text/"):
            return self.payload.decode("utf-8", "replace")
        text = hexdump(self.payload, total=self.size)
        if self.truncated:
            text += "\n(only the first bytes are kept for older large messages)"
        return text


class Stream:
    """Samples of the one topic currently open in the UI."""

    def __init__(self) -> None:
        self.key: str | None = None
        self.samples: deque[TopicSample] = deque(maxlen=STREAM_CAPACITY)
        self.paused = False
        self.count = 0
        self.total_bytes = 0
        self.appended = 0
        """Samples ever appended to ``samples``; the UI diffs against it."""
        self._window: deque[tuple[float, int]] = deque()
        self._heavy: deque[TopicSample] = deque()
        self._heavy_bytes = 0

    def reset(self, key: str | None) -> None:
        self.__init__()
        self.key = key

    def clear(self) -> None:
        self.samples.clear()
        self._heavy.clear()
        self._heavy_bytes = 0
        self.count = 0
        self.total_bytes = 0

    def add(self, sample: TopicSample) -> None:
        now = time.monotonic()
        self.count += 1
        self.total_bytes += sample.size
        self._window.append((now, sample.size))
        if self.paused:
            return
        self.samples.append(sample)
        self.appended += 1
        if sample.size > PAYLOAD_HEAD:
            # A 30 Hz camera would otherwise pin hundreds of megabytes in the row buffer.
            self._heavy.append(sample)
            self._heavy_bytes += sample.size
            while self._heavy_bytes > STREAM_BYTE_BUDGET and len(self._heavy) > 1:
                old = self._heavy.popleft()
                self._heavy_bytes -= old.size
                old.payload = old.payload[:PAYLOAD_HEAD]
                old.truncated = True

    def rate(self, span: float = 2.0) -> tuple[float, float]:
        """(messages/s, bytes/s) over the last ``span`` seconds."""
        cutoff = time.monotonic() - span
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        if not self._window:
            return 0.0, 0.0
        return len(self._window) / span, sum(b for _, b in self._window) / span


@dataclass
class Notice:
    """Something worth a snackbar."""

    text: str
    level: str = "info"  # info | ok | warn | error


class Store:
    def __init__(self) -> None:
        self.namespace = ""
        self.nodes: dict[str, Node] = {}
        self.topics: dict[str, TopicEntry] = {}
        self.services: dict[str, ServiceEntry] = {}
        self.logs: deque[LogRecordMsg] = deque(maxlen=LOG_CAPACITY)
        self.logs_appended = 0
        self.stream = Stream()
        self.notices: list[Notice] = []
        self.dirty: set[str] = set()
        self.dropped_events = 0
        self._settled_at = 0.0

    def reset(self, namespace: str = "") -> None:
        self.__init__()
        self.namespace = namespace
        # Tokens replayed right after connecting are the existing system, not news.
        self._settled_at = time.monotonic() + 2.0

    def take_dirty(self) -> set[str]:
        dirty, self.dirty = self.dirty, set()
        return dirty

    def take_notices(self) -> list[Notice]:
        notices, self.notices = self.notices, []
        return notices

    def _node(self, name: str) -> Node:
        node = self.nodes.get(name)
        if node is None:
            node = self.nodes[name] = Node(name)
        return node

    # -- ingestion ---------------------------------------------------------

    def on_presence(self, name: str, alive: bool) -> None:
        node = self._node(name)
        if node.alive == alive:
            return
        node.alive = alive
        node.silent = False
        if alive:
            node.last_beat = None
            node.health = None
        if time.monotonic() >= self._settled_at:
            self.notices.append(
                Notice(f"Node {name} joined", "ok") if alive else Notice(f"Node {name} left", "warn")
            )
        self._reindex()
        self.dirty.update({"nodes", "status"})

    def on_health(self, data: bytes) -> None:
        try:
            health = NodeHealth.model_validate_json(data)
        except ValueError:
            return
        node = self._node(health.node)
        now_mono, now = time.monotonic(), time.time()
        prev, prev_beat = node.health, node.last_beat
        if node.silent:
            node.silent = False
            self.notices.append(Notice(f"Node {node.name} is responding again", "ok"))
            self.dirty.add("status")
        if prev is None or prev.state != health.state:
            self.dirty.add("status")
        node.health, node.last_beat = health, now_mono
        dt = (now_mono - prev_beat) if prev_beat is not None else None
        values: dict[str, float | None] = dict(health.measures)
        for metric in RUNTIME:
            values[metric] = getattr(health, metric, None)
        for metric, value in values.items():
            if value is None:
                continue
            desc = node.descriptor(metric)
            if desc is not None and desc.kind == "counter":
                before = _previous(prev, metric)
                if before is None or dt is None or dt <= 0 or value < before:
                    continue  # first beat or a restart: no rate yet
                value = (value - before) / dt
            series = node.history.get(metric)
            if series is None:
                series = node.history[metric] = deque(maxlen=HISTORY)
            series.append((now, float(value)))
        self.dirty.add("nodes")
        self.dirty.add(f"health:{node.name}")

    def on_info(self, data: bytes) -> None:
        try:
            info = NodeInfo.model_validate_json(data)
        except ValueError:
            return
        self._node(info.node).info = info
        self._reindex()
        self.dirty.update({"nodes", "info", f"info:{info.node}"})

    def on_log(self, data: bytes) -> None:
        try:
            record = LogRecordMsg.model_validate_json(data)
        except ValueError:
            return
        self.logs.append(record)
        self.logs_appended += 1
        self.dirty.add("logs")

    def on_sample(self, sample: TopicSample) -> None:
        self.stream.add(sample)
        self.dirty.add("stream")

    def tick(self) -> None:
        """Flag nodes that hold their token but stopped sending heartbeats."""
        now = time.monotonic()
        for node in self.nodes.values():
            if not node.alive or node.silent or node.last_beat is None:
                continue
            if now - node.last_beat > SILENT_AFTER_BEATS * node.interval:
                node.silent = True
                self.notices.append(
                    Notice(f"Node {node.name} went silent (no heartbeat)", "error")
                )
                self.dirty.update({"nodes", "status"})

    # -- derived indexes -----------------------------------------------------

    def _reindex(self) -> None:
        topics: dict[str, TopicEntry] = {}
        services: dict[str, ServiceEntry] = {}
        for node in self.nodes.values():
            if node.info is None or not node.alive:
                continue
            for entities, attr in ((node.info.publishes, "publishers"), (node.info.subscribes, "subscribers")):
                for e in entities:
                    entry = topics.get(e.key)
                    if entry is None:
                        entry = topics[e.key] = TopicEntry(
                            e.key, system=bool(_SYSTEM_KEY.search(e.key))
                        )
                    getattr(entry, attr).add(node.name)
                    entry.schema_name = entry.schema_name or e.schema_name
                    if attr == "publishers" or not entry.flags:
                        entry.flags = list(e.flags)
            for s in node.info.serves:
                svc = services.get(s.key)
                if svc is None:
                    svc = services[s.key] = ServiceEntry(s.key, s)
                svc.nodes.add(node.name)
        # A wildcard subscription (sensors/*/range) and the concrete keys it matches are
        # separate entries; let each show the other side's nodes.
        for wild in [t for t in topics.values() if t.wildcard]:
            for other in topics.values():
                if other is wild or other.wildcard or not _intersects(wild.key, other.key):
                    continue
                wild.publishers |= other.publishers
                other.subscribers |= wild.subscribers
        for entry in topics.values():
            entry.matched = bool(entry.publishers)
        self.topics, self.services = topics, services
        self.dirty.update({"topics", "services"})

    # -- summaries -----------------------------------------------------------

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes.values():
            counts[node.status] = counts.get(node.status, 0) + 1
        return counts

    def total(self, metric: str) -> float:
        return sum(n.latest(metric) or 0.0 for n in self.nodes.values() if n.alive)

    def is_system_service(self, key: str) -> bool:
        return bool(_SYSTEM_KEY.search(key))


def _previous(health: NodeHealth | None, metric: str) -> float | None:
    if health is None:
        return None
    if metric in RUNTIME:
        return getattr(health, metric, None)
    return health.measures.get(metric)


def _intersects(a: str, b: str) -> bool:
    if a == b:
        return True
    if "*" not in a and "*" not in b:
        return False
    try:
        return zenoh.KeyExpr(a).intersects(zenoh.KeyExpr(b))
    except Exception:
        return False


# -- formatting helpers shared by the UI -------------------------------------


def fmt_bytes(n: float) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def fmt_number(value: float | None, integral: bool = False) -> str:
    if value is None:
        return "–"
    if integral and float(value).is_integer():
        return f"{int(value):,}"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:.1f}"
    return f"{value:.2f}"


def fmt_unit(unit: str) -> str:
    """UCUM to something a person reads: ``{message}`` -> ``msg``, ``By`` -> ``B``."""
    if unit in ("", "1"):
        return ""
    if unit == "By":
        return "B"
    if unit.startswith("{") and unit.endswith("}"):
        inner = unit[1:-1]
        return {"message": "msg"}.get(inner, inner)
    return unit


def fmt_metric(node: Node, metric: str) -> tuple[str, str]:
    """(value, unit) of a metric's latest point, as shown on a tile."""
    desc = node.descriptor(metric)
    value = node.latest(metric)
    unit = fmt_unit(desc.unit) if desc else ""
    if value is None:
        return "–", unit
    if metric == "uptime_s":
        return fmt_duration(value), ""
    if desc is not None and desc.unit == "%":
        return f"{value:.1f}", "%"
    if desc is not None and desc.unit == "By":
        text = fmt_bytes(value)
        number, _, suffix = text.partition(" ")
        return number, suffix + ("/s" if desc.kind == "counter" else "")
    if desc is not None and desc.kind == "counter":
        return fmt_number(value), f"{unit}/s" if unit else "/s"
    return fmt_number(value, bool(desc and desc.integral)), unit


def hexdump(data: bytes, limit: int = 512, total: int | None = None) -> str:
    """Eight bytes per line, so it fits a narrow side panel."""
    total = len(data) if total is None else total
    lines = []
    for off in range(0, min(len(data), limit), 8):
        chunk = data[off : off + 8]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:06x}  {chunk.hex(' '):<23}  {text}")
    if total > limit:
        lines.append(f"… {fmt_bytes(total - limit)} more")
    return "\n".join(lines)


def to_jsonable(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)
