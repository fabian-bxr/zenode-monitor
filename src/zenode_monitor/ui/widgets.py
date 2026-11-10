"""Small building blocks shared by every page."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

import flet as ft
import flet_charts as fch

from .. import state

MONO = "monospace"
MONO_FALLBACK = ["DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", "Consolas", "Menlo", "Courier New"]

GREEN, AMBER, RED, BLUE, GREY = "#3fb950", "#d29922", "#f85149", "#58a6ff", "#8b949e"

STATUS_COLOR = {
    state.RUNNING: GREEN,
    state.STARTING: AMBER,
    state.STOPPING: AMBER,
    state.STOPPED: GREY,
    state.SILENT: RED,
    state.ONLINE: BLUE,
    state.OFFLINE: GREY,
}
STATUS_LABEL = {
    state.RUNNING: "Running",
    state.STARTING: "Starting",
    state.STOPPING: "Stopping",
    state.STOPPED: "Stopped",
    state.SILENT: "Silent",
    state.ONLINE: "Online, no heartbeat",
    state.OFFLINE: "Offline",
}
STATUS_ORDER = [state.SILENT, state.STARTING, state.STOPPING, state.RUNNING, state.ONLINE, state.STOPPED, state.OFFLINE]
LEVEL_COLOR = {"DEBUG": GREY, "INFO": BLUE, "WARNING": AMBER, "ERROR": RED, "CRITICAL": RED}

MUTED = ft.Colors.ON_SURFACE_VARIANT
LINE = ft.Colors.OUTLINE_VARIANT
PANEL = ft.Colors.SURFACE_CONTAINER_LOW
SELECTED = ft.Colors.SECONDARY_CONTAINER


def alpha(color: Any, opacity: float) -> str:
    """A colour with opacity, in Flet's ``"name,opacity"`` string form."""
    return f"{getattr(color, 'value', color)},{opacity}"


def mono(value: str = "", size: float = 12.5, **kw: Any) -> ft.Text:
    return ft.Text(value, font_family=MONO, font_family_fallback=MONO_FALLBACK, size=size, **kw)


def muted(value: str = "", size: float = 12, **kw: Any) -> ft.Text:
    return ft.Text(value, size=size, color=MUTED, **kw)


def dot(color: str = GREY, size: float = 10) -> ft.Container:
    return ft.Container(width=size, height=size, border_radius=size / 2, bgcolor=color)


def pill(text: str, color: str | None = None, tooltip: str | None = None) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, size=11, color=color or MUTED, weight=ft.FontWeight.W_500),
        padding=ft.Padding.symmetric(horizontal=8, vertical=2),
        border_radius=10,
        border=ft.Border.all(1, color or LINE),
        tooltip=tooltip,
    )


def panel(content: ft.Control, padding: float = 16, **kw: Any) -> ft.Container:
    return ft.Container(
        content=content,
        padding=padding,
        bgcolor=PANEL,
        border_radius=12,
        border=ft.Border.all(1, LINE),
        **kw,
    )


def section(title: str, trailing: ft.Control | None = None) -> ft.Row:
    controls: list[ft.Control] = [ft.Text(title, size=13, weight=ft.FontWeight.W_600, color=MUTED)]
    if trailing is not None:
        controls.append(trailing)
    return ft.Row(controls, spacing=8)


def empty_state(icon: Any, title: str, hint: str = "") -> ft.Container:
    return ft.Container(
        content=ft.Column(
            [
                ft.Icon(icon, size=40, color=MUTED),
                ft.Text(title, size=16, weight=ft.FontWeight.W_500, text_align=ft.TextAlign.CENTER),
                ft.Text(hint, size=13, color=MUTED, text_align=ft.TextAlign.CENTER, width=420),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        ),
        alignment=ft.Alignment.CENTER,
        expand=True,
        padding=24,
    )


def search_field(hint: str, on_change: Callable[[str], None]) -> ft.TextField:
    return ft.TextField(
        hint_text=hint,
        prefix_icon=ft.Icons.SEARCH,
        dense=True,
        text_size=13,
        border_radius=8,
        content_padding=ft.Padding.symmetric(horizontal=10, vertical=8),
        on_change=lambda e: on_change(e.control.value or ""),
    )


class ListRow:
    """A clickable two-line row for the master lists, with a selected state."""

    def __init__(self, key: str, on_click: Callable[[str], None], leading: ft.Control | None = None) -> None:
        self.key = key
        self.title = mono("", size=13, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)
        self.subtitle = muted("", no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)
        self.trailing = ft.Row([], spacing=4, tight=True)
        body: list[ft.Control] = []
        if leading is not None:
            body.append(leading)
        body.append(ft.Column([self.title, self.subtitle], spacing=2, expand=True))
        body.append(self.trailing)
        self.root = ft.Container(
            content=ft.Row(body, spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            border_radius=8,
            ink=True,
            on_click=lambda e: on_click(self.key),
        )

    def select(self, selected: bool) -> None:
        self.root.bgcolor = SELECTED if selected else None


class StatTile:
    """A label with one big number."""

    def __init__(self, label: str, tooltip: str | None = None) -> None:
        self.value = ft.Text("–", size=26, weight=ft.FontWeight.W_600)
        self.unit = muted("", size=13)
        self.root = panel(
            ft.Column(
                [
                    muted(label, size=12),
                    ft.Row([self.value, self.unit], spacing=6, vertical_alignment=ft.CrossAxisAlignment.END),
                ],
                spacing=4,
            ),
            padding=14,
            tooltip=tooltip,
        )

    def set(self, value: str, unit: str = "", color: str | None = None) -> None:
        self.value.value, self.unit.value, self.value.color = value, unit, color


SPARK_POINTS = 60


def spark_points(series: Iterable[tuple[float, float]] | None, limit: int = SPARK_POINTS) -> list[tuple[float, float]]:
    """The newest ``limit`` points, with x as seconds before now."""
    if not series:
        return []
    now = time.time()
    return [(t - now, v) for t, v in list(series)[-limit:]]


class Sparkline:
    """A minimal line chart: no axes, tooltip on hover."""

    def __init__(self, height: float = 44, color: str = ft.Colors.PRIMARY, unit: str = "") -> None:
        self.unit = unit
        self.series = fch.LineChartData(
            points=[],
            color=color,
            stroke_width=1.6,
            curved=False,
            point=False,
            below_line_bgcolor=alpha(color, 0.12),
        )
        # animation=False makes the desktop client fail to render the chart; 0 ms is fine.
        self.chart = fch.LineChart(
            data_series=[self.series], animation=0, interactive=True, min_y=0, max_y=1
        )
        self.root = ft.Container(self.chart, height=height)

    def set(self, points: list[tuple[float, float]]) -> None:
        suffix = f" {self.unit}" if self.unit else ""
        self.series.points = [
            fch.LineChartDataPoint(x, y, tooltip=f"{state.fmt_number(y)}{suffix}") for x, y in points
        ]
        ys = [y for _, y in points] or [0.0]
        lo, hi = min(ys), max(ys)
        self.chart.min_y = min(0.0, lo)
        self.chart.max_y = hi * 1.15 if hi > 0 else 1.0
        if points:
            self.chart.min_x, self.chart.max_x = points[0][0], max(points[-1][0], points[0][0] + 1)


class MetricTile:
    """Title, latest value and a sparkline of one metric."""

    def __init__(self, title: str, description: str = "", alert_when_positive: bool = False) -> None:
        self.alert = alert_when_positive
        self.value = ft.Text("–", size=20, weight=ft.FontWeight.W_600)
        self.unit = muted("", size=12)
        self.spark = Sparkline()
        self.root = panel(
            ft.Column(
                [
                    muted(title, size=12, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Row([self.value, self.unit], spacing=5, vertical_alignment=ft.CrossAxisAlignment.END),
                    self.spark.root,
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            padding=12,
            tooltip=description or None,
            col={"xs": 12, "sm": 6, "md": 4, "lg": 3, "xl": 2},
        )

    def set(self, node: state.Node, metric: str) -> None:
        value, unit = state.fmt_metric(node, metric)
        self.value.value, self.unit.value = value, unit
        latest = node.latest(metric)
        bad = self.alert and bool(latest)
        self.value.color = RED if bad else None
        self.spark.series.color = RED if bad else ft.Colors.PRIMARY
        self.spark.unit = unit
        self.spark.set(spark_points(node.history.get(metric)))


def humanize(metric: str) -> str:
    names = {
        "cpu_percent": "CPU",
        "rss_bytes": "Memory",
        "sent": "Sent",
        "received": "Received",
        "dropped": "Dropped",
        "stale": "Stale",
        "handler_errors": "Handler errors",
        "timer_overruns": "Timer overruns",
        "deadline_misses": "Deadline misses",
        "logs_dropped": "Logs dropped",
        "shm_fallbacks": "SHM fallbacks",
        "queue_max_depth": "Queue depth (max)",
        "age_mean_ms": "Message age (mean)",
        "age_max_ms": "Message age (max)",
        "handler_mean_ms": "Handler time (mean)",
        "handler_max_ms": "Handler time (max)",
    }
    return names.get(metric, metric.replace("_", " "))


ALERT_METRICS = {"dropped", "stale", "handler_errors", "timer_overruns", "deadline_misses", "logs_dropped", "shm_fallbacks"}


def clock(ts_ns: int, millis: bool = True) -> str:
    t = time.localtime(ts_ns / 1e9)
    text = time.strftime("%H:%M:%S", t)
    return f"{text}.{ts_ns // 1_000_000 % 1000:03d}" if millis else text
