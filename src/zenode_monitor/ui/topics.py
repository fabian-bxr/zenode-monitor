"""Topics: what is published, by whom, and the live messages of the one you open."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import flet as ft

from .. import state
from . import widgets as w
from .overview import Page

if TYPE_CHECKING:
    from .shell import App

ROW_HEIGHT = 26
LIST_INTERVAL_S = 0.2
"""How often rows are added to the message list. Redrawing a 500-row list is the
expensive part of this page, and nobody reads a list that scrolls faster than this."""
FRAME_INTERVAL_S = 1 / 30
"""How often the latest-message panel is redrawn while a topic streams."""
MAX_PRETTY_CHARS = 20_000


_ROW = "{:<12}  {:<12.12}  {:>7}  {}"


def short_key(key: str, namespace: str) -> str:
    return key[len(namespace) + 1 :] if namespace and key.startswith(namespace + "/") else key


def node_chips(app: App, names: set[str], none: str) -> ft.Control:
    if not names:
        return w.muted(none, size=13)
    return ft.Row(
        [
            ft.Container(
                content=w.mono(n, size=12),
                padding=ft.Padding.symmetric(horizontal=8, vertical=3),
                border_radius=6,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                ink=True,
                tooltip=f"Open node {n}",
                on_click=lambda e, n=n: app.open_node(n),
            )
            for n in sorted(names)
        ],
        spacing=6,
        wrap=True,
        run_spacing=6,
    )


class TopicsPage(Page):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.selected: str | None = None
        self.query = ""
        self.show_system = False
        self.visible = False
        self.rows: dict[str, w.ListRow] = {}
        self.list = ft.ListView([], spacing=2, expand=True)
        self.list_empty = ft.Container(expand=True, visible=False)
        self.detail = ft.Container(expand=True)
        self.root = ft.Row(
            [
                ft.Container(
                    ft.Column(
                        [
                            w.search_field("Filter topics", self._on_query),
                            ft.Switch(
                                label="Show zenode system topics",
                                value=False,
                                label_text_style=ft.TextStyle(size=12),
                                on_change=self._on_system,
                            ),
                            self.list,
                            self.list_empty,
                        ],
                        spacing=8,
                    ),
                    width=320,
                ),
                ft.VerticalDivider(width=1),
                ft.Container(self.detail, expand=True, padding=ft.Padding.only(left=16)),
            ],
            expand=True,
            spacing=8,
        )
        self._built_for: str | None = None
        self._shown = 0
        self._pinned: state.TopicSample | None = None
        self._pinned_row: ft.Container | None = None
        self._detail_sample: state.TopicSample | None = None
        self.image: ft.Image | None = None
        self._image_sample: state.TopicSample | None = None
        self._video_task: Any = None
        self.frames_shown = 0
        self._list_at = 0.0

    # -- lifecycle ---------------------------------------------------------------

    def reset(self) -> None:
        self.selected = None
        self._built_for = None
        self.rows.clear()
        self.list.controls = []

    def on_show(self) -> None:
        self.visible = True
        self._watch()
        if self._video_task is None:
            self._video_task = self.app.page.run_task(self._latest_loop)

    def on_hide(self) -> None:
        self.visible = False
        if self._video_task is not None:
            self._video_task.cancel()
            self._video_task = None
        self.app.bus.unwatch_topic()
        self._built_for = None  # the stream restarts when the page is shown again

    def select(self, key: str) -> None:
        if key not in self.store.topics:
            return
        entry = self.store.topics[key]
        if entry.system and not self.show_system:
            self.show_system = True
        self.selected = key
        self._built_for = None
        if self.visible:
            self._watch()

    def _watch(self) -> None:
        entry = self.store.topics.get(self.selected or "")
        if entry is None:
            self.app.bus.unwatch_topic()
            return
        self.app.bus.watch_topic(entry.key, latched=entry.latched)
        self._built_for = None

    def _on_pick(self, key: str) -> None:
        self.select(key)
        self.refresh(None)
        self.root.update()

    def _on_query(self, text: str) -> None:
        self.query = text.strip().lower()
        self._refresh_list()
        self.root.update()

    def _on_system(self, e: Any) -> None:
        self.show_system = bool(e.control.value)
        self._refresh_list()
        self.root.update()

    # -- refresh -----------------------------------------------------------------

    def refresh(self, dirty: set[str] | None) -> list[ft.Control] | None:
        whole = dirty is None or "topics" in dirty
        if whole:
            self._refresh_list()
        entry = self.store.topics.get(self.selected or "")
        if entry is None:
            if self.selected is not None:
                self.selected = None
                self.app.bus.unwatch_topic()
            self._built_for = None
            self.detail.content = w.empty_state(
                ft.Icons.SENSORS_OUTLINED,
                "Select a topic",
                "Pick a topic on the left to see who publishes it and watch its messages live.",
            )
            return None
        if self._built_for != entry.key:
            self._build_detail(entry)
            whole, dirty = True, None
        elif dirty is not None and "topics" in dirty:
            self._refresh_peers(entry)
        if dirty is None:
            self._refresh_stream(force_detail=True)
            return None
        if not dirty & {"stream", "clock"}:
            return None if whole else []
        list_changed = self._refresh_stream()
        # The latest-message panel is redrawn by its own loop, not here.
        touched = [self.rate.root, self.bandwidth.root, self.count.root]
        return None if whole else touched + ([self.stream_panel] if list_changed else [])

    def _refresh_list(self) -> None:
        ns = self.store.namespace
        entries = [
            t
            for t in sorted(self.store.topics.values(), key=lambda t: t.key)
            if (self.show_system or not t.system) and self.query in t.key.lower()
        ]
        rows: dict[str, w.ListRow] = {}
        for t in entries:
            row = self.rows.get(t.key) or w.ListRow(t.key, self._on_pick)
            row.title.value = short_key(t.key, ns)
            flow = f"{', '.join(sorted(t.publishers)) or '—'} → {', '.join(sorted(t.subscribers)) or '—'}"
            row.subtitle.value = " · ".join(x for x in (t.schema_name, flow) if x)
            trailing: list[ft.Control] = []
            if t.latched:
                trailing.append(w.pill("latched"))
            if not t.matched:
                trailing.append(ft.Icon(ft.Icons.WARNING_AMBER, size=16, color=w.AMBER, tooltip="Nodes subscribe to this topic, but no running node publishes it"))
            row.trailing.controls = trailing
            row.select(t.key == self.selected)
            rows[t.key] = row
        self.rows = rows
        self.list.controls = [r.root for r in rows.values()]
        self.list.visible = bool(rows)
        self.list_empty.visible = not rows
        if not rows:
            if self.store.topics:
                hint = "No topic matches the filter." if self.query else "Only zenode system topics exist. Turn on the switch above to see them."
                self.list_empty.content = w.empty_state(ft.Icons.FILTER_ALT_OFF_OUTLINED, "Nothing to show", hint)
            else:
                self.list_empty.content = w.empty_state(
                    ft.Icons.SENSORS_OFF_OUTLINED,
                    "No topics yet",
                    "Topics appear as soon as a running node publishes its descriptor.",
                )

    # -- detail ------------------------------------------------------------------

    def _build_detail(self, entry: state.TopicEntry) -> None:
        self._built_for = entry.key
        self._shown = self.store.stream.appended - len(self.store.stream.samples)
        self._pinned = self._pinned_row = self._detail_sample = None
        self.pubs_holder = ft.Container()
        self.subs_holder = ft.Container()
        self.rate = w.StatTile("Rate")
        self.bandwidth = w.StatTile("Bandwidth")
        self.count = w.StatTile("Received")
        for t in (self.rate, self.bandwidth, self.count):
            t.root.width = 150
            t.root.padding = 10
            t.value.size = 20
        self.pause_btn = ft.OutlinedButton("Pause", icon=ft.Icons.PAUSE, on_click=self._toggle_pause)
        self.stream_list = ft.ListView([], item_extent=ROW_HEIGHT, auto_scroll=True, auto_scroll_animation=0, expand=True)
        self.stream_empty = ft.Container(expand=True)
        self.msg_title = ft.Text("Latest message", size=13, weight=ft.FontWeight.W_600, color=w.MUTED)
        self.follow_btn = ft.TextButton("Follow latest", icon=ft.Icons.VERTICAL_ALIGN_BOTTOM, visible=False, on_click=self._unpin)
        self.msg_facts = ft.Row([], spacing=20, wrap=True, run_spacing=6)
        self.msg_body = ft.Container(expand=True)
        self.image = self._image_sample = None
        self.msg_text = self.msg_scroll = None

        header = w.mono(_ROW.format("Received", "From", "Seq", "Payload"), color=w.MUTED, no_wrap=True)
        self.detail.content = ft.Column(
            [
                ft.Row(
                    [
                        w.mono(entry.key, size=20, weight=ft.FontWeight.W_600, selectable=True),
                        ft.IconButton(ft.Icons.CONTENT_COPY, icon_size=16, tooltip="Copy key", on_click=lambda e: self.app.copy(entry.key)),
                        *[w.pill(f) for f in entry.flags],
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                ),
                ft.Row(
                    [
                        ft.Column([w.muted("Type", size=11), ft.Text(entry.schema_name or "unknown", size=14)], spacing=2),
                        ft.Column([w.muted("Published by", size=11), self.pubs_holder], spacing=4),
                        ft.Column([w.muted("Subscribed by", size=11), self.subs_holder], spacing=4),
                    ],
                    spacing=32,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    wrap=True,
                ),
                ft.Row(
                    [
                        self.rate.root,
                        self.bandwidth.root,
                        self.count.root,
                        ft.Container(expand=True),
                        self.pause_btn,
                        ft.OutlinedButton("Clear", icon=ft.Icons.CLEAR_ALL, on_click=self._clear),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [
                        stream_panel := w.panel(
                            ft.Column([ft.Container(header, padding=ft.Padding.symmetric(horizontal=4)), ft.Divider(height=1), self.stream_list, self.stream_empty], spacing=6),
                            padding=12,
                            expand=3,
                        ),
                        message_panel := w.panel(
                            ft.Column(
                                [
                                    ft.Row([self.msg_title, ft.Container(expand=True), self.follow_btn], height=30),
                                    self.msg_facts,
                                    ft.Divider(height=1),
                                    self.msg_body,
                                ],
                                spacing=8,
                            ),
                            padding=12,
                            expand=2,
                        ),
                    ],
                    expand=True,
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ],
            spacing=14,
            expand=True,
        )
        self.stream_panel, self.message_panel = stream_panel, message_panel
        self._refresh_peers(entry)
        for key, row in self.rows.items():
            row.select(key == entry.key)

    def _refresh_peers(self, entry: state.TopicEntry) -> None:
        self.pubs_holder.content = node_chips(self.app, entry.publishers, "nobody" if entry.matched else "nobody — no running node publishes this")
        self.subs_holder.content = node_chips(self.app, entry.subscribers, "nobody")

    def _toggle_pause(self, e: Any = None) -> None:
        stream = self.store.stream
        stream.paused = not stream.paused
        self.pause_btn.content = "Resume" if stream.paused else "Pause"
        self.pause_btn.icon = ft.Icons.PLAY_ARROW if stream.paused else ft.Icons.PAUSE
        # auto_scroll stays on: nothing is appended while paused, and switching it off
        # made the list jump away from the newest rows.
        self.root.update()

    def _clear(self, e: Any = None) -> None:
        self.store.stream.clear()
        self.stream_list.controls = []
        self._shown = self.store.stream.appended
        self._pinned = self._pinned_row = self._detail_sample = None
        self._refresh_stream(force_detail=True)
        self.root.update()

    def _pin(self, e: Any) -> None:
        if self._pinned_row is not None:
            self._pinned_row.bgcolor = None
        self._pinned_row = e.control
        self._pinned = e.control.data
        e.control.bgcolor = w.SELECTED
        self._refresh_stream(force_detail=True)
        self.root.update()

    def _unpin(self, e: Any = None) -> None:
        if self._pinned_row is not None:
            self._pinned_row.bgcolor = None
        self._pinned = self._pinned_row = None
        self._refresh_stream(force_detail=True)
        self.root.update()

    def _row(self, s: state.TopicSample, wildcard: bool) -> ft.Container:
        env = s.envelope
        preview = s.preview()
        if wildcard:
            preview = f"{short_key(s.key, self.store.namespace)}  {preview}"
        # One Text per row, columns aligned by the monospace font: a row of several
        # controls made the 500-row list three times as expensive to redraw.
        line = _ROW.format(w.clock(s.recv_ns), env.node or "–", env.seq if env.seq is not None else "–", preview)
        return ft.Container(
            content=w.mono(line, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
            alignment=ft.Alignment.CENTER_LEFT,
            height=ROW_HEIGHT,
            padding=ft.Padding.symmetric(horizontal=4),
            border_radius=4,
            data=s,
            on_click=self._pin,
        )

    def _refresh_stream(self, force_detail: bool = False) -> bool:
        """Update the numbers every time, the row list a few times a second.

        Returns whether the list was touched.
        """
        stream = self.store.stream
        entry = self.store.topics.get(self.selected or "")
        hz, bps = stream.rate()
        self.rate.set(state.fmt_number(hz), "Hz")
        number, _, unit = state.fmt_bytes(bps).partition(" ")
        self.bandwidth.set(number, f"{unit}/s")
        self.count.set(f"{stream.count:,}", "msgs")

        now = time.monotonic()
        if not force_detail and now - self._list_at < LIST_INTERVAL_S:
            if stream.appended != self._shown:
                self.store.dirty.add("stream")  # rows are waiting: come back next tick
            return False
        self._list_at = now
        new = min(stream.appended - self._shown, len(stream.samples))
        self._shown = stream.appended
        if new > 0:
            wildcard = bool(entry and entry.wildcard)
            fresh = list(stream.samples)[-new:]
            controls = self.stream_list.controls + [self._row(s, wildcard) for s in fresh]
            self.stream_list.controls = controls[-state.STREAM_CAPACITY :]
        has_rows = bool(self.stream_list.controls)
        self.stream_list.visible = has_rows
        self.stream_empty.visible = not has_rows
        if not has_rows:
            if stream.paused:
                text, hint = "Paused", "Resume to see new messages."
            elif entry is not None and not entry.matched:
                text, hint = "No publisher", "Nodes subscribe to this topic, but no running node publishes it."
            else:
                text, hint = "Listening…", "Subscribed. Messages appear here as soon as one is published."
            self.stream_empty.content = w.empty_state(ft.Icons.HEARING_OUTLINED, text, hint)

        if force_detail:
            sample = self._pinned or (stream.samples[-1] if stream.samples else None)
            self._detail_sample = sample
            self._show_message(sample)
        return True

    def _show_message(self, s: state.TopicSample | None) -> None:
        pinned = self._pinned is not None
        self.msg_title.value = "Selected message" if pinned else "Latest message"
        self.follow_btn.visible = pinned
        if s is None:
            self.msg_facts.controls = []
            self.msg_body.content = w.muted("No message received yet.", size=13)
            return
        env = s.envelope
        facts = [
            ("From", env.node or "unknown"),
            ("Seq", str(env.seq) if env.seq is not None else "–"),
            ("Sent", w.clock(env.ts_ns) if env.ts_ns else "–"),
            ("Age", f"{(s.recv_ns - env.ts_ns) / 1e6:.1f} ms" if env.ts_ns else "–"),
            ("Size", state.fmt_bytes(s.size)),
            ("Encoding", s.encoding or "–"),
        ]
        if s.key != self.selected:
            facts.insert(0, ("Key", s.key))
        if env.traceparent:
            facts.append(("Trace", env.traceparent.split("-")[1] if "-" in env.traceparent else env.traceparent))
        self.msg_facts.controls = [
            ft.Column([w.muted(label, size=11), w.mono(value, selectable=True)], spacing=0) for label, value in facts
        ]
        if s.is_image:
            self._set_image(s)
            return
        self.stream_panel.expand, self.message_panel.expand = 3, 2
        text = s.pretty()
        if len(text) > MAX_PRETTY_CHARS:
            text = text[:MAX_PRETTY_CHARS] + f"\n… truncated, {len(text) - MAX_PRETTY_CHARS:,} more characters"
        if self.msg_text is None or self.msg_body.content is not self.msg_scroll:
            self.msg_text = w.mono(text, selectable=True)
            self.msg_scroll = ft.Column([self.msg_text], scroll=ft.ScrollMode.AUTO, expand=True)
            self.msg_body.content = self.msg_scroll
        else:
            self.msg_text.value = text  # same control: the scroll position and selection survive

    # -- images ------------------------------------------------------------------

    def _set_image(self, s: state.TopicSample) -> None:
        """Show a frame in the one Image control, creating it on the first frame.

        Reusing the control (with gapless playback) keeps the previous frame on
        screen until the next is decoded, so a stream does not flicker.
        """
        self._image_sample = s
        if self.image is None or self.msg_body.content is not self.image:
            self.image = ft.Image(src=s.payload, fit=ft.BoxFit.CONTAIN, gapless_playback=True, expand=True)
            self.msg_body.content = self.image
            self.stream_panel.expand, self.message_panel.expand = 2, 3  # give the picture room
        else:
            self.image.src = s.payload

    async def _latest_loop(self) -> None:
        """Keep the "Latest message" panel current at up to 30 fps.

        Runs beside the general refresh loop and updates only that panel, so a
        fast-changing value or a video stream is shown as it arrives. Messages
        that come in between two redraws are skipped here, never queued; every
        one of them is still in the message list.
        """
        loop = asyncio.get_running_loop()
        due = loop.time()
        while True:
            # Sleep to a fixed schedule so the time spent drawing does not lower the frame rate.
            due = max(due + FRAME_INTERVAL_S, loop.time())
            await asyncio.sleep(due - loop.time())
            stream = self.store.stream
            if self._built_for is None or self._pinned is not None or stream.paused or not stream.samples:
                continue
            s = stream.samples[-1]
            if s is self._detail_sample:
                continue
            self._detail_sample = s
            try:
                self._show_message(s)
                self.message_panel.update()
            except Exception:
                continue  # panel not on the page yet; the general refresh will draw it
            self.frames_shown += 1
