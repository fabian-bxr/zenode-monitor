"""Logs: every node's published log records in one filtered stream."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import flet as ft
from zenode.msgs import LogRecordMsg

from . import widgets as w
from .overview import Page

if TYPE_CHECKING:
    from .shell import App

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ROW_HEIGHT = 26
MAX_ROWS = 500
LIST_INTERVAL_S = 0.2
ALL = "__all__"


class LogsPage(Page):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.min_level = 0
        self.node = ALL
        self.query = ""
        self._seen = 0
        self._list_at = 0.0
        self._node_names: list[str] = []
        self.level_dd = ft.Dropdown(
            label="Level",
            value="DEBUG",
            width=220,
            text_size=14,
            dense=True,
            options=[ft.DropdownOption(key=lv, text="All levels" if lv == "DEBUG" else f"{lv.title()} and above") for lv in LEVELS[:4]],
            on_select=self._on_filter,
        )
        self.node_dd = ft.Dropdown(label="Node", value=ALL, width=200, text_size=14, dense=True, on_select=self._on_filter)
        self.count = w.muted("")
        self.list = ft.ListView([], item_extent=ROW_HEIGHT, auto_scroll=True, auto_scroll_animation=0, expand=True)
        self.empty = ft.Container(expand=True, visible=False)
        self.root = ft.Column(
            [
                ft.Row(
                    [
                        self.level_dd,
                        self.node_dd,
                        ft.Container(w.search_field("Search messages", self._on_query), expand=True),
                        self.count,
                        ft.OutlinedButton("Clear", icon=ft.Icons.CLEAR_ALL, on_click=self._clear),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                panel := w.panel(ft.Column([self.list, self.empty], spacing=0), padding=10, expand=True),
            ],
            spacing=12,
            expand=True,
        )
        self.panel = panel

    def reset(self) -> None:
        self._seen = 0
        self.node = ALL
        self._node_names = []
        self.list.controls = []

    def _on_filter(self, e: Any = None) -> None:
        self.min_level = LEVELS.index(self.level_dd.value or "DEBUG")
        self.node = self.node_dd.value or ALL
        self._rebuild()
        self.root.update()

    def _on_query(self, text: str) -> None:
        self.query = text.strip().lower()
        self._rebuild()
        self.root.update()

    def _clear(self, e: Any = None) -> None:
        self.store.logs.clear()
        self._rebuild()
        self.root.update()

    def _matches(self, r: LogRecordMsg) -> bool:
        level = LEVELS.index(r.level) if r.level in LEVELS else 1
        if level < self.min_level or (self.node != ALL and r.node != self.node):
            return False
        return not self.query or self.query in r.message.lower() or self.query in r.logger.lower()

    def _row(self, r: LogRecordMsg) -> ft.Container:
        color = w.LEVEL_COLOR.get(r.level, w.MUTED)
        return ft.Container(
            content=ft.Row(
                [
                    w.mono(w.clock(r.ts_ns), color=w.MUTED, width=100, no_wrap=True),
                    w.mono(r.level, color=color, width=76, no_wrap=True),
                    w.mono(r.node, width=120, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    w.mono(r.message.replace("\n", " "), expand=True, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                ],
                spacing=8,
            ),
            height=ROW_HEIGHT,
            padding=ft.Padding.symmetric(horizontal=4),
            border_radius=4,
            ink=True,
            on_click=lambda e: self._open(r),
        )

    def _open(self, r: LogRecordMsg) -> None:
        facts = [("Node", r.node), ("Level", r.level), ("Logger", f"{r.logger}:{r.line}" if r.line else r.logger), ("Time", w.clock(r.ts_ns))]
        if r.trace:
            facts.append(("Trace", r.trace))
        facts += list(r.fields.items())
        dialog = ft.AlertDialog(
            title=ft.Text("Log record"),
            content=ft.Container(
                width=640,
                content=ft.Column(
                    [
                        w.mono(r.message, size=13, selectable=True),
                        ft.Divider(height=1),
                        *[
                            ft.Row([w.muted(k, width=90), w.mono(str(v), selectable=True, expand=True)], vertical_alignment=ft.CrossAxisAlignment.START)
                            for k, v in facts
                        ],
                    ],
                    spacing=8,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton("Copy message", on_click=lambda e: self.app.copy(r.message)),
                ft.TextButton("Open node", on_click=lambda e: (self.app.page.pop_dialog(), self.app.open_node(r.node))),
                ft.FilledButton("Close", on_click=lambda e: self.app.page.pop_dialog()),
            ],
        )
        self.app.page.show_dialog(dialog)

    def _rebuild(self) -> None:
        records = [r for r in self.store.logs if self._matches(r)][-MAX_ROWS:]
        self.list.controls = [self._row(r) for r in records]
        self._seen = self.store.logs_appended
        self._after()

    def refresh(self, dirty: set[str] | None) -> list[ft.Control] | None:
        names = sorted(self.store.nodes)
        whole = dirty is None or names != self._node_names
        if names != self._node_names:
            self._node_names = names
            self.node_dd.options = [ft.DropdownOption(key=ALL, text="All nodes")] + [
                ft.DropdownOption(key=n, text=n) for n in names
            ]
        if dirty is None:
            self._rebuild()
            return None
        if "logs" not in dirty:
            return None if whole else []
        now = time.monotonic()
        if now - self._list_at < LIST_INTERVAL_S:
            self.store.dirty.add("logs")  # rows are waiting: come back next tick
            return None if whole else []
        self._list_at = now
        new = min(self.store.logs_appended - self._seen, len(self.store.logs))
        self._seen = self.store.logs_appended
        if new > 0:
            fresh = [self._row(r) for r in list(self.store.logs)[-new:] if self._matches(r)]
            if fresh:
                self.list.controls = (self.list.controls + fresh)[-MAX_ROWS:]
        self._after()
        return None if whole else [self.panel, self.count]

    def _after(self) -> None:
        shown, total = len(self.list.controls), len(self.store.logs)
        self.count.value = f"{shown} of {total}" if shown != total else f"{total} record{'s' if total != 1 else ''}"
        self.list.visible = bool(shown)
        self.empty.visible = not shown
        if not shown:
            if total:
                self.empty.content = w.empty_state(ft.Icons.FILTER_ALT_OFF_OUTLINED, "Nothing matches", "No log record matches the current filters.")
            else:
                self.empty.content = w.empty_state(
                    ft.Icons.RECEIPT_LONG_OUTLINED,
                    "No log records yet",
                    "Nodes publish WARNING and above by default (publish_logs_at), and only records emitted after the monitor connected arrive here.",
                )

