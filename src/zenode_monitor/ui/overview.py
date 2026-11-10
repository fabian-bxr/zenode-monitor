"""The landing page: system totals and one card per node."""

from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from .. import state
from . import widgets as w

if TYPE_CHECKING:
    from .shell import App


class Page:
    """What the shell expects of a page."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.store = app.store

    def reset(self) -> None: ...

    def on_show(self) -> None: ...

    def on_hide(self) -> None: ...

    def refresh(self, dirty: set[str] | None) -> list[ft.Control] | None:
        """Bring the page up to date. ``dirty`` is ``None`` for "everything".

        Returns the controls that changed so the shell can redraw just those,
        or ``None`` when the whole page should be redrawn.
        """
        return None


def waiting_for_nodes(app: App) -> ft.Control:
    ns = app.store.namespace
    where = f"namespace “{ns}”" if ns else "the empty namespace"
    return w.empty_state(
        ft.Icons.SENSORS_OFF_OUTLINED,
        "Waiting for nodes…",
        f"Connected, but no node has announced itself in {where} yet. "
        "Check that the namespace matches the nodes' [transport] namespace and that they are reachable from this machine.",
    )


class NodeCard:
    def __init__(self, app: App, name: str) -> None:
        self.dot = w.dot()
        self.status = w.muted("")
        self.meta = w.muted("", no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)
        self.cpu, self.mem, self.out = (ft.Text("–", size=14, weight=ft.FontWeight.W_500) for _ in range(3))
        self.spark = w.Sparkline(height=36, unit="msg/s")
        self.root = w.panel(
            ft.Column(
                [
                    ft.Row(
                        [self.dot, w.mono(name, size=14, weight=ft.FontWeight.W_600, expand=True, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS), self.status],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.meta,
                    ft.Row(
                        [_fact("CPU", self.cpu), _fact("Memory", self.mem), _fact("Sent", self.out)],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    self.spark.root,
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            padding=14,
            ink=True,
            on_click=lambda e: app.open_node(name),
            col={"xs": 12, "sm": 6, "lg": 4, "xl": 3},
        )

    def set(self, node: state.Node) -> None:
        status = node.status
        self.dot.bgcolor = w.STATUS_COLOR[status]
        self.status.value = w.STATUS_LABEL[status]
        self.root.opacity = 0.55 if status == state.OFFLINE else 1.0
        uptime = state.fmt_duration(node.health.uptime_s) if node.health and node.alive else ""
        self.meta.value = " · ".join(x for x in (node.host, f"up {uptime}" if uptime else "") if x) or " "
        cpu = node.latest("cpu_percent")
        self.cpu.value = f"{cpu:.0f} %" if cpu is not None else "–"
        rss = node.latest("rss_bytes")
        self.mem.value = state.fmt_bytes(rss) if rss is not None else "–"
        sent = node.latest("sent")
        self.out.value = f"{state.fmt_number(sent)} /s" if sent is not None else "–"
        self.spark.set(w.spark_points(node.history.get("sent")))


def _fact(label: str, value: ft.Text) -> ft.Column:
    return ft.Column([w.muted(label, size=11), value], spacing=0)


class OverviewPage(Page):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.t_nodes = w.StatTile("Nodes running")
        self.t_problem = w.StatTile("Need attention", "Nodes that are silent, starting, stopping or offline")
        self.t_topics = w.StatTile("Topics")
        self.t_services = w.StatTile("Services")
        self.t_rate = w.StatTile("Messages sent", "Sum of all nodes' publish rates")
        self.t_errors = w.StatTile("Drops and errors", "Dropped and stale messages, handler errors, timer overruns and deadline misses per second")
        tiles = [self.t_nodes, self.t_problem, self.t_topics, self.t_services, self.t_rate, self.t_errors]
        for t in tiles:
            t.root.col = {"xs": 6, "md": 4, "xl": 2}
        self.cards: dict[str, NodeCard] = {}
        self.grid = ft.ResponsiveRow([], spacing=12, run_spacing=12)
        self.body = ft.Column(
            [
                ft.ResponsiveRow([t.root for t in tiles], spacing=12, run_spacing=12),
                w.section("Nodes"),
                self.grid,
            ],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self.root = ft.Container(expand=True)

    def reset(self) -> None:
        self.cards.clear()
        self.grid.controls = []

    def refresh(self, dirty: set[str] | None) -> list[ft.Control] | None:
        if dirty is not None and not dirty & {"nodes", "topics", "services", "status"}:
            return []
        store = self.store
        if not store.nodes:
            self.root.content = waiting_for_nodes(self.app)
            return None
        swapped = self.root.content is not self.body
        self.root.content = self.body

        counts = store.status_counts()
        running = counts.get(state.RUNNING, 0)
        self.t_nodes.set(str(running), f"of {len(store.nodes)}")
        problems = len(store.nodes) - running - counts.get(state.ONLINE, 0)
        self.t_problem.set(str(problems), color=w.RED if problems else None)
        self.t_topics.set(str(sum(1 for t in store.topics.values() if not t.system)))
        self.t_services.set(str(sum(1 for k in store.services if not store.is_system_service(k))))
        self.t_rate.set(state.fmt_number(store.total("sent")), "msg/s")
        errors = sum(store.total(m) for m in ("dropped", "stale", "handler_errors", "timer_overruns", "deadline_misses"))
        self.t_errors.set(state.fmt_number(errors), "/s", color=w.RED if errors > 0 else None)

        names = sorted(store.nodes)
        if names != list(self.cards):
            self.cards = {n: self.cards.get(n) or NodeCard(self.app, n) for n in names}
            self.grid.controls = [c.root for c in self.cards.values()]
        for name, card in self.cards.items():
            card.set(store.nodes[name])
        return None if swapped or dirty is None else [self.body]
