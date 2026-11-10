"""Nodes: a list on the left, everything one node reports on the right."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import flet as ft

from .. import state
from . import widgets as w
from .overview import Page, waiting_for_nodes

if TYPE_CHECKING:
    from .shell import App

RUNTIME_ORDER = [
    "cpu_percent",
    "rss_bytes",
    "sent",
    "received",
    "queue_max_depth",
    "age_mean_ms",
    "age_max_ms",
    "handler_mean_ms",
    "handler_max_ms",
    "dropped",
    "stale",
    "handler_errors",
    "timer_overruns",
    "deadline_misses",
    "logs_dropped",
    "shm_fallbacks",
]


class NodesPage(Page):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.selected: str | None = None
        self.rows: dict[str, w.ListRow] = {}
        self.dots: dict[str, ft.Container] = {}
        self.list = ft.ListView([], spacing=2, expand=True)
        self.detail = ft.Container(expand=True)
        self.body = ft.Row(
            [
                ft.Container(self.list, width=280),
                ft.VerticalDivider(width=1),
                ft.Container(self.detail, expand=True, padding=ft.Padding.only(left=16)),
            ],
            expand=True,
            spacing=8,
        )
        self.root = ft.Container(expand=True)
        self._built_for: str | None = None
        self.tiles: dict[str, w.MetricTile] = {}
        self._logs_shown: tuple = ()

    def reset(self) -> None:
        self.selected = None
        self.rows.clear()
        self.dots.clear()
        self.list.controls = []
        self._built_for = None

    def select(self, name: str) -> None:
        if name != self.selected:
            self.selected = name
            self._built_for = None
        for key, row in self.rows.items():
            row.select(key == name)

    def _on_pick(self, name: str) -> None:
        self.select(name)
        self.refresh(None)
        self.root.update()

    def refresh(self, dirty: set[str] | None) -> list[ft.Control] | None:
        store = self.store
        if not store.nodes:
            self.root.content = waiting_for_nodes(self.app)
            return None
        whole = dirty is None or self.root.content is not self.body
        self.root.content = self.body
        if self.selected not in store.nodes:
            self.select(sorted(store.nodes)[0])

        touched: list[ft.Control] = []
        if dirty is None or dirty & {"status", "info"}:
            self._refresh_list()
            touched.append(self.list)
        name = self.selected
        assert name is not None
        node = store.nodes[name]
        if self._built_for != name or (dirty and f"info:{name}" in dirty):
            self._build_detail(node)
            whole, dirty = True, None
        # Other nodes' heartbeats do not concern this page: only the selected node's do.
        if dirty is None or f"health:{name}" in dirty:
            self._refresh_header(node)
            for metric, tile in self.tiles.items():
                tile.set(node, metric)
            touched.append(self.detail)
        elif dirty & {"status", "clock"}:
            self._refresh_header(node)
            touched.append(self.header)
        if (dirty is None or "logs" in dirty) and self._refresh_logs(node):
            touched.append(self.logs_col)
        return None if whole else touched

    # -- list --------------------------------------------------------------------

    def _refresh_list(self) -> None:
        names = sorted(self.store.nodes)
        if names != list(self.rows):
            rows = {}
            for n in names:
                if n not in self.rows:
                    self.dots[n] = w.dot()
                rows[n] = self.rows.get(n) or w.ListRow(n, self._on_pick, leading=self.dots[n])
            self.rows = rows
            self.list.controls = [r.root for r in rows.values()]
        for name, row in self.rows.items():
            node = self.store.nodes[name]
            status = node.status
            row.title.value = name
            row.subtitle.value = " · ".join(x for x in (w.STATUS_LABEL[status], node.host) if x)
            self.dots[name].bgcolor = w.STATUS_COLOR[status]
            row.root.opacity = 0.6 if status == state.OFFLINE else 1.0
            row.select(name == self.selected)

    # -- detail ------------------------------------------------------------------

    def _build_detail(self, node: state.Node) -> None:
        self._built_for = node.name
        info = node.info
        self.h_dot = w.dot(size=12)
        self.h_status = w.pill("")
        self.h_facts = ft.Row([], spacing=28, wrap=True, run_spacing=8)
        self.tiles = {}

        runtime = [m for m in RUNTIME_ORDER if m in state.RUNTIME]
        custom = [m.id for m in info.measures] if info else []
        for metric in runtime + custom:
            desc = node.descriptor(metric)
            self.tiles[metric] = w.MetricTile(
                w.humanize(metric) if metric in state.RUNTIME else metric,
                desc.description if desc else "",
                alert_when_positive=metric in w.ALERT_METRICS,
            )

        self.logs_col = ft.Column([], spacing=4)
        self.header = ft.Column(
            [
                ft.Row(
                    [self.h_dot, w.mono(node.name, size=22, weight=ft.FontWeight.W_600, selectable=True), self.h_status],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.h_facts,
            ],
            spacing=14,
        )
        blocks: list[ft.Control] = [self.header]
        if info is None:
            blocks.append(
                w.panel(
                    ft.Row(
                        [ft.Icon(ft.Icons.INFO_OUTLINE, color=w.MUTED, size=18), w.muted("This node has not published a descriptor, so its topics, services and metric catalog are unknown. Nodes with health_interval = None publish neither heartbeat nor descriptor.", size=13, expand=True)],
                        spacing=10,
                    ),
                    padding=12,
                )
            )
        if custom:
            blocks += [w.section("Metrics"), ft.ResponsiveRow([self.tiles[m].root for m in custom], spacing=10, run_spacing=10)]
        blocks += [w.section("Runtime"), ft.ResponsiveRow([self.tiles[m].root for m in runtime], spacing=10, run_spacing=10)]

        if info is not None:
            pubs = [e for e in info.publishes if not self.store.topics.get(e.key, _NOT_SYSTEM).system]
            subs = [e for e in info.subscribes if not self.store.topics.get(e.key, _NOT_SYSTEM).system]
            serves = [s for s in info.serves if not self.store.is_system_service(s.key)]
            blocks += self._entities("Publishes", pubs, self.app.open_topic, "This node publishes no application topics.")
            blocks += self._entities("Subscribes", subs, self.app.open_topic, "This node subscribes to no topics.")
            blocks += [w.section("Services", w.muted(str(len(serves))))]
            if serves:
                blocks.append(
                    ft.Column(
                        [
                            _link_row(s.key, f"{s.request or 'bytes'} → {s.reply or 'bytes'}", [s.diagnostic] if s.diagnostic else [], self.app.open_service)
                            for s in serves
                        ],
                        spacing=2,
                    )
                )
            else:
                blocks.append(w.muted("This node serves no services.", size=13))
            if info.config:
                blocks += [
                    w.section("Configuration", w.muted("read-only snapshot")),
                    w.panel(w.mono(state.to_jsonable(info.config), selectable=True), padding=12),
                ]
        blocks += [w.section("Recent logs"), self.logs_col, ft.Container(height=8)]
        self.detail.content = ft.Column(blocks, spacing=14, scroll=ft.ScrollMode.AUTO, expand=True)

    def _entities(self, title: str, items: list, on_click, empty: str) -> list[ft.Control]:
        out: list[ft.Control] = [w.section(title, w.muted(str(len(items))))]
        if items:
            out.append(ft.Column([_link_row(e.key, e.schema_name, e.flags, on_click) for e in items], spacing=2))
        else:
            out.append(w.muted(empty, size=13))
        return out

    def _refresh_header(self, node: state.Node) -> None:
        status = node.status
        color = w.STATUS_COLOR[status]
        self.h_dot.bgcolor = color
        self.h_status.content.value = w.STATUS_LABEL[status]
        self.h_status.content.color = color
        self.h_status.border = ft.Border.all(1, color)
        health, info = node.health, node.info
        facts = [("Host", node.host or "–")]
        if info is not None:
            facts.append(("zenode", info.zenode or "–"))
        facts.append(("Uptime", state.fmt_duration(health.uptime_s) if health and node.alive else "–"))
        if node.last_beat is not None and node.alive:
            facts.append(("Last heartbeat", f"{time.monotonic() - node.last_beat:.0f} s ago"))
        facts.append(("Heartbeat interval", f"{node.interval:g} s" if (info and info.health_interval) else "–"))
        self.h_facts.controls = [
            ft.Column([w.muted(label, size=11), ft.Text(value, size=14, selectable=True)], spacing=0)
            for label, value in facts
        ]

    def _refresh_logs(self, node: state.Node) -> bool:
        """Returns whether the shown records changed."""
        records = [r for r in self.store.logs if r.node == node.name][-8:]
        shown = (node.name, len(records), records[-1].ts_ns if records else 0)
        if shown == self._logs_shown and self.logs_col.controls:
            return False
        self._logs_shown = shown
        if not records:
            self.logs_col.controls = [
                w.muted("No log records from this node since the monitor connected. Nodes publish WARNING and above by default.", size=13)
            ]
            return True
        self.logs_col.controls = [
            ft.Row(
                [
                    w.mono(w.clock(r.ts_ns, millis=False), color=w.MUTED),
                    w.mono(f"{r.level:<8}", color=w.LEVEL_COLOR.get(r.level, w.MUTED)),
                    w.mono(r.message, expand=True, selectable=True),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )
            for r in reversed(records)
        ]
        return True


_NOT_SYSTEM = state.TopicEntry("")


def _link_row(key: str, label: str, flags: list[str], on_click) -> ft.Container:
    return ft.Container(
        content=ft.Row(
            [
                w.mono(key, size=13, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS, expand=True),
                w.muted(label),
                *[w.pill(f) for f in flags],
                ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16, color=w.MUTED),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.Padding.symmetric(horizontal=10, vertical=7),
        border_radius=8,
        ink=True,
        on_click=lambda e: on_click(key),
    )
