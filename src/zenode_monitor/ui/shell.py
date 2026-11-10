"""The window frame: top bar, navigation rail, page switching and the refresh loop."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import flet as ft

from ..bus import MonitorBus
from ..profiles import Profile
from ..state import Store
from . import widgets as w
from .connect import ConnectView
from .logs import LogsPage
from .nodes import NodesPage
from .overview import OverviewPage
from .services import ServicesPage
from .topics import TopicsPage

log = logging.getLogger("zenode_monitor")

REFRESH_S = 0.05
"""Seconds between refresh ticks. A tick is cheap: it redraws only what changed."""
NOTICE_STYLE = {
    "info": (ft.Icons.INFO_OUTLINE, w.BLUE),
    "ok": (ft.Icons.CHECK_CIRCLE_OUTLINE, w.GREEN),
    "warn": (ft.Icons.WARNING_AMBER, w.AMBER),
    "error": (ft.Icons.ERROR_OUTLINE, w.RED),
}


class App:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.store = Store()
        self.bus = MonitorBus(self.store)
        self.profile: Profile | None = None
        self._logs_seen = 0
        self.clipboard = ft.Clipboard()
        self.file_picker = ft.FilePicker()

        self.connect_view = ConnectView(self)
        self.pages: list[Any] = [
            OverviewPage(self),
            NodesPage(self),
            TopicsPage(self),
            ServicesPage(self),
            LogsPage(self),
        ]
        self.index = 0

        self.status_dot = w.dot(w.GREY)
        self.status_text = ft.Text("Not connected", size=13)
        self.status_detail = w.muted("", size=12)
        self.disconnect_btn = ft.OutlinedButton(
            "Disconnect", icon=ft.Icons.LINK_OFF, visible=False, on_click=self._on_disconnect
        )
        self.theme_btn = ft.IconButton(
            ft.Icons.LIGHT_MODE_OUTLINED, tooltip="Switch to light theme", on_click=self._toggle_theme
        )
        top_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.HUB_OUTLINED, color=ft.Colors.PRIMARY),
                    ft.Text("Zenode Monitor", size=16, weight=ft.FontWeight.W_600),
                    ft.Container(expand=True),
                    status_chip := ft.Container(
                        content=ft.Row(
                            [self.status_dot, self.status_text, self.status_detail],
                            spacing=8,
                            tight=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                        border_radius=16,
                        border=ft.Border.all(1, w.LINE),
                    ),
                    self.disconnect_btn,
                    self.theme_btn,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
        )

        self.status_chip = status_chip
        self._status_shown: tuple = ()
        self._badge_shown = 0
        self.logs_dest = ft.NavigationRailDestination(
            icon=ft.Icons.RECEIPT_LONG_OUTLINED, selected_icon=ft.Icons.RECEIPT_LONG, label="Logs"
        )
        self.rail = ft.NavigationRail(
            selected_index=0,
            label_type=ft.NavigationRailLabelType.ALL,
            min_width=84,
            bgcolor=ft.Colors.SURFACE,
            destinations=[
                ft.NavigationRailDestination(
                    icon=ft.Icons.DASHBOARD_OUTLINED, selected_icon=ft.Icons.DASHBOARD, label="Overview"
                ),
                ft.NavigationRailDestination(
                    icon=ft.Icons.MEMORY_OUTLINED, selected_icon=ft.Icons.MEMORY, label="Nodes"
                ),
                ft.NavigationRailDestination(
                    icon=ft.Icons.SENSORS_OUTLINED, selected_icon=ft.Icons.SENSORS, label="Topics"
                ),
                ft.NavigationRailDestination(
                    icon=ft.Icons.BOLT_OUTLINED, selected_icon=ft.Icons.BOLT, label="Services"
                ),
                self.logs_dest,
            ],
            on_change=lambda e: self.show(int(e.control.selected_index)),
        )
        self.content = ft.Container(expand=True, padding=16)
        self.workspace = ft.Row(
            [self.rail, ft.VerticalDivider(width=1), self.content], expand=True, spacing=0, visible=False
        )
        self.connect_holder = ft.Container(self.connect_view.root, expand=True, alignment=ft.Alignment.CENTER)
        self.root = ft.Column(
            [top_bar, ft.Divider(height=1), self.workspace, self.connect_holder], expand=True, spacing=0
        )

    # -- connection ------------------------------------------------------------

    async def connect(self, profile: Profile) -> None:
        """Called by the connect view; raises if the session cannot be opened."""
        self._set_status(w.AMBER, "Connecting…", profile.summary())
        self.page.update()
        try:
            await self.bus.connect(profile.to_transport())
        except Exception:
            self._set_status(w.GREY, "Not connected", "")
            self.page.update()
            raise
        self.profile = profile
        self._logs_seen = 0
        self._status_shown = ()
        self._badge_shown = 0
        self.logs_dest.badge = None
        for p in self.pages:
            p.reset()
        self.connect_holder.visible = False
        self.workspace.visible = True
        self.disconnect_btn.visible = True
        self.show(0)
        self.notify(f"Connected ({profile.summary()})", "ok")

    async def _on_disconnect(self, e: Any = None) -> None:
        self.disconnect_btn.disabled = True
        self.page.update()
        await self.bus.disconnect()
        self.store.reset()
        self.disconnect_btn.disabled = False
        self.disconnect_btn.visible = False
        self.workspace.visible = False
        self.connect_holder.visible = True
        self._set_status(w.GREY, "Not connected", "")
        self.page.update()

    def _set_status(self, color: str, text: str, detail: str) -> None:
        self.status_dot.bgcolor = color
        self.status_text.value = text
        self.status_detail.value = detail
        self.status_detail.visible = bool(detail)

    def _refresh_status(self) -> bool:
        """Returns whether the chip changed."""
        if not self.bus.connected or self.profile is None:
            return False
        alive = sum(1 for n in self.store.nodes.values() if n.alive)
        ns = self.profile.namespace or "no namespace"
        text = f"{alive} node{'s' if alive != 1 else ''}" if alive else "Waiting for nodes…"
        shown = (w.GREEN if alive else w.AMBER, "Connected", f"{ns} · {text}")
        if shown == self._status_shown:
            return False
        self._status_shown = shown
        self._set_status(*shown)
        return True

    # -- navigation --------------------------------------------------------------

    @property
    def current(self) -> Any:
        return self.pages[self.index]

    def show(self, index: int) -> None:
        if self.content.content is not None and index != self.index:
            self.current.on_hide()
        self.index = index
        self.rail.selected_index = index
        self.content.content = self.current.root
        self.current.on_show()
        self.current.refresh(None)
        self.page.update()

    def open_node(self, name: str) -> None:
        self.pages[1].select(name)
        self.show(1)

    def open_topic(self, key: str) -> None:
        self.pages[2].select(key)
        self.show(2)

    def open_service(self, key: str) -> None:
        self.pages[3].select(key)
        self.show(3)

    # -- feedback ----------------------------------------------------------------

    def notify(self, text: str, level: str = "info") -> None:
        icon, color = NOTICE_STYLE.get(level, NOTICE_STYLE["info"])
        self.page.show_dialog(
            ft.SnackBar(
                content=ft.Row([ft.Icon(icon, color=color, size=18), ft.Text(text, expand=True, max_lines=3, overflow=ft.TextOverflow.ELLIPSIS)], spacing=10),
                behavior=ft.SnackBarBehavior.FLOATING,
                width=520,
                duration=3000 if level in ("info", "ok") else 6000,
                show_close_icon=True,
            )
        )

    def copy(self, text: str) -> None:
        async def run() -> None:
            await self.clipboard.set(text)
            self.notify("Copied to clipboard")

        self.page.run_task(run)

    def _toggle_theme(self, e: Any = None) -> None:
        dark = self.page.theme_mode != ft.ThemeMode.DARK
        self.page.theme_mode = ft.ThemeMode.DARK if dark else ft.ThemeMode.LIGHT
        self.theme_btn.icon = ft.Icons.LIGHT_MODE_OUTLINED if dark else ft.Icons.DARK_MODE_OUTLINED
        self.theme_btn.tooltip = f"Switch to {'light' if dark else 'dark'} theme"
        self.page.update()

    # -- refresh loop ------------------------------------------------------------

    async def pump(self) -> None:
        """Push model changes to the screen a few times a second, never per message."""
        last_tick = 0.0
        while True:
            await asyncio.sleep(REFRESH_S)
            if not self.bus.connected:
                continue
            try:
                now = time.monotonic()
                if now - last_tick >= 1.0:
                    last_tick = now
                    self.store.tick()
                    self.store.dirty.add("clock")
                dirty = self.store.take_dirty()
                notices = self.store.take_notices()
                if not dirty and not notices:
                    continue
                self._redraw(dirty)
                for notice in notices[-3:]:
                    self.notify(notice.text, notice.level)
            except Exception:
                log.exception("refresh failed")

    def _redraw(self, dirty: set[str]) -> None:
        """Send the client only the controls that changed, not the whole page.

        A page's ``refresh`` returns the controls it touched (``None`` means
        "everything"). Diffing just those is what keeps a 20 Hz tick affordable.
        """
        touched = self.current.refresh(dirty)
        extra: list[ft.Control] = []
        if self._refresh_status():
            extra.append(self.status_chip)
        if self._refresh_log_badge():
            extra.append(self.rail)
        try:
            if touched is None:
                self.page.update(self.content, *extra)
            elif touched or extra:
                self.page.update(*touched, *extra)
        except Exception:
            self.page.update()  # something touched is not mounted yet: redraw it all

    def _refresh_log_badge(self) -> bool:
        if self.current is self.pages[4]:
            self._logs_seen = self.store.logs_appended
        unseen = min(max(self.store.logs_appended - self._logs_seen, 0), 100)
        if unseen == self._badge_shown:
            return False
        self._badge_shown = unseen
        self.logs_dest.badge = None if unseen == 0 else ft.Badge(label="99+" if unseen > 99 else str(unseen))
        return True
