"""Services: browse them, fill in a request, call, read the reply."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import flet as ft

from .. import state
from ..bus import CallResult
from ..schema_form import FieldSpec, build_payload, fields_from_schema, skeleton
from . import widgets as w
from .overview import Page
from .topics import node_chips, short_key

if TYPE_CHECKING:
    from .shell import App

DIAGNOSTIC_HINT = {
    "read": "Read-only: returns state without changing anything",
    "write": "Write: changes a setting on the node",
    "operation": "Operation: triggers an action with side effects",
}
DIAGNOSTIC_COLOR = {"read": w.BLUE, "write": w.AMBER, "operation": w.AMBER}
HISTORY_PER_SERVICE = 10


@dataclass
class CallRecord:
    at_ns: int
    request: str
    result: CallResult


def pretty_json(text: str) -> str:
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


class ServicesPage(Page):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.selected: str | None = None
        self.query = ""
        self.rows: dict[str, w.ListRow] = {}
        self.history: dict[str, list[CallRecord]] = {}
        self.list = ft.ListView([], spacing=2, expand=True)
        self.list_empty = ft.Container(expand=True, visible=False)
        self.detail = ft.Container(expand=True)
        self.root = ft.Row(
            [
                ft.Container(
                    ft.Column([w.search_field("Filter services", self._on_query), self.list, self.list_empty], spacing=8),
                    width=320,
                ),
                ft.VerticalDivider(width=1),
                ft.Container(self.detail, expand=True, padding=ft.Padding.only(left=16)),
            ],
            expand=True,
            spacing=8,
        )
        self._built_for: str | None = None
        self._built_info: Any = None

    def reset(self) -> None:
        self.selected = None
        self._built_for = None
        self.rows.clear()
        self.history.clear()
        self.list.controls = []

    def select(self, key: str) -> None:
        if key in self.store.services and key != self.selected:
            self.selected = key
            self._built_for = None

    def _on_pick(self, key: str) -> None:
        self.select(key)
        self.refresh(None)
        self.root.update()

    def _on_query(self, text: str) -> None:
        self.query = text.strip().lower()
        self._refresh_list()
        self.root.update()

    # -- refresh -----------------------------------------------------------------

    def refresh(self, dirty: set[str] | None) -> list[ft.Control] | None:
        if dirty is not None and "services" not in dirty:
            return []
        self._refresh_list()
        entry = self.store.services.get(self.selected or "")
        if entry is None:
            gone = self.selected
            self.selected = self._built_for = None
            self.detail.content = w.empty_state(
                ft.Icons.BOLT_OUTLINED,
                "Select a service",
                f"{gone} is no longer served. Its node may have stopped." if gone else "Pick a service on the left to see its request fields and call it.",
            )
            return None
        if self._built_for != entry.key or self._built_info != entry.info:
            self._build_detail(entry)
        else:
            self.served_by.content = node_chips(self.app, entry.nodes, "nobody")
        return None

    def _refresh_list(self) -> None:
        ns = self.store.namespace
        entries = [
            s
            for s in sorted(self.store.services.values(), key=lambda s: s.key)
            if not self.store.is_system_service(s.key) and self.query in s.key.lower()
        ]
        rows: dict[str, w.ListRow] = {}
        for s in entries:
            row = self.rows.get(s.key) or w.ListRow(s.key, self._on_pick)
            row.title.value = short_key(s.key, ns)
            types = f"{s.info.request or 'bytes'} → {s.info.reply or 'bytes'}"
            row.subtitle.value = f"{', '.join(sorted(s.nodes))} · {types}"
            d = s.info.diagnostic
            row.trailing.controls = [w.pill(d, DIAGNOSTIC_COLOR.get(d), DIAGNOSTIC_HINT.get(d))] if d else []
            row.select(s.key == self.selected)
            rows[s.key] = row
        self.rows = rows
        self.list.controls = [r.root for r in rows.values()]
        self.list.visible = bool(rows)
        self.list_empty.visible = not rows
        if not rows:
            self.list_empty.content = (
                w.empty_state(ft.Icons.FILTER_ALT_OFF_OUTLINED, "Nothing to show", "No service matches the filter.")
                if self.query
                else w.empty_state(ft.Icons.BOLT_OUTLINED, "No services", "None of the running nodes serves a service.")
            )

    # -- detail ------------------------------------------------------------------

    def _build_detail(self, entry: state.ServiceEntry) -> None:
        self._built_for, self._built_info = entry.key, entry.info
        info = entry.info
        self.fields: list[FieldSpec] = fields_from_schema(info.request_schema)
        self.inputs: dict[str, ft.Control] = {f.name: _input(f) for f in self.fields}
        self.served_by = ft.Container(node_chips(self.app, entry.nodes, "nobody"))
        self.raw = ft.TextField(
            value=skeleton(info.request_schema),
            multiline=True,
            min_lines=4,
            max_lines=12,
            text_style=ft.TextStyle(font_family=w.MONO, font_family_fallback=w.MONO_FALLBACK, size=13),
            label="Request body (JSON)",
        )
        self.form_box = ft.Column(list(self.inputs.values()), spacing=12, tight=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.mode = ft.SegmentedButton(
            segments=[ft.Segment(value="form", label=ft.Text("Form")), ft.Segment(value="json", label=ft.Text("JSON"))],
            selected=["form"],
            show_selected_icon=False,
            on_change=self._on_mode,
            visible=bool(self.fields),
        )
        no_args = not self.fields and info.request in ("Empty", "")
        self.no_args = w.muted("This service takes no arguments.", size=13, visible=no_args)
        self.form_box.visible = bool(self.fields)
        self.raw.visible = not self.fields and not no_args
        self.timeout = ft.TextField(label="Timeout", value="2", suffix="s", width=110, dense=True, text_align=ft.TextAlign.RIGHT)
        self.spinner = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)
        self.call_btn = ft.FilledButton("Call", icon=ft.Icons.PLAY_ARROW, on_click=self._on_call)
        self.result_box = ft.Container(
            w.muted("The reply appears here after you call the service.", size=13)
        )
        self.history_col = ft.Column([], spacing=2)

        pills = []
        if info.diagnostic:
            pills.append(w.pill(info.diagnostic, DIAGNOSTIC_COLOR.get(info.diagnostic), DIAGNOSTIC_HINT.get(info.diagnostic)))
        self.detail.content = ft.Column(
            [
                ft.Row(
                    [
                        w.mono(entry.key, size=20, weight=ft.FontWeight.W_600, selectable=True),
                        ft.IconButton(ft.Icons.CONTENT_COPY, icon_size=16, tooltip="Copy key", on_click=lambda e: self.app.copy(entry.key)),
                        *pills,
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                ),
                ft.Text(info.description, size=14, visible=bool(info.description)),
                ft.Row(
                    [
                        ft.Column([w.muted("Served by", size=11), self.served_by], spacing=4),
                        ft.Column([w.muted("Request", size=11), ft.Text(info.request or "bytes", size=14)], spacing=2),
                        ft.Column([w.muted("Reply", size=11), ft.Text(info.reply or "bytes", size=14)], spacing=2),
                    ],
                    spacing=32,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    wrap=True,
                ),
                ft.ResponsiveRow(
                    [
                        w.panel(
                            ft.Column(
                                [
                                    ft.Row([w.section("Request"), ft.Container(expand=True), self.mode], height=36),
                                    self.no_args,
                                    self.form_box,
                                    self.raw,
                                    ft.Row(
                                        [self.timeout, ft.Container(expand=True), self.spinner, self.call_btn],
                                        spacing=12,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                ],
                                spacing=14,
                                tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                            ),
                            col={"xs": 12, "lg": 6},
                        ),
                        w.panel(
                            ft.Column([ft.Row([w.section("Reply")], height=36), self.result_box], spacing=14, tight=True),
                            col={"xs": 12, "lg": 6},
                        ),
                    ],
                    spacing=12,
                    run_spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                w.section("Calls this session"),
                self.history_col,
            ],
            spacing=14,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self._refresh_history()
        for key, row in self.rows.items():
            row.select(key == entry.key)

    def _on_mode(self, e: Any = None) -> None:
        json_mode = self.mode.selected == ["json"]
        if json_mode:
            payload, _ = build_payload(self.fields, self._values())
            self.raw.value = json.dumps(payload, indent=2)
        self.form_box.visible = not json_mode
        self.raw.visible = json_mode
        self.root.update()

    def _values(self) -> dict[str, Any]:
        return {name: control.value for name, control in self.inputs.items()}

    def _request_body(self) -> bytes | None:
        """The JSON to send, or ``None`` after marking what is wrong."""
        if self.raw.visible:
            self.raw.error = None
            text = (self.raw.value or "").strip() or "{}"
            try:
                json.loads(text)
            except ValueError as exc:
                self.raw.error = f"Not valid JSON: {exc}"
                return None
            return text.encode()
        payload, errors = build_payload(self.fields, self._values())
        for name, control in self.inputs.items():
            if hasattr(control, "error"):
                control.error = errors.get(name)
        if not errors:
            return json.dumps(payload).encode()
        if any(name in errors for name, c in self.inputs.items() if not hasattr(c, "error")):
            self.app.notify("Fill in the required fields", "warn")
        return None

    async def _on_call(self, e: Any = None) -> None:
        key = self.selected
        if key is None:
            return
        self.timeout.error = None
        try:
            timeout = float((self.timeout.value or "").replace(",", "."))
            if not 0 < timeout <= 600:
                raise ValueError
        except ValueError:
            self.timeout.error = "0–600 s"
            self.root.update()
            return
        body = self._request_body()
        if body is None:
            self.root.update()
            return
        self.spinner.visible = True
        self.call_btn.disabled = True
        self.call_btn.content = "Calling…"
        self.root.update()
        result = await self.app.bus.call(key, body, timeout)
        self.history.setdefault(key, []).insert(0, CallRecord(time.time_ns(), body.decode(), result))
        del self.history[key][HISTORY_PER_SERVICE:]
        if self.selected == key and self._built_for == key:
            self.spinner.visible = False
            self.call_btn.disabled = False
            self.call_btn.content = "Call"
            self._show_result(result)
            self._refresh_history()
            self.root.update()
        if not result.ok:
            what = "timed out" if result.kind == "timeout" else "failed"
            self.app.notify(f"{short_key(key, self.store.namespace)} {what}: {result.text.splitlines()[0]}", "error")

    def _show_result(self, r: CallResult) -> None:
        if r.ok:
            icon, color, title = ft.Icons.CHECK_CIRCLE_OUTLINE, w.GREEN, f"Reply in {r.elapsed_ms:.1f} ms"
        elif r.kind == "timeout":
            icon, color, title = ft.Icons.TIMER_OFF_OUTLINED, w.AMBER, "Timed out"
        else:
            icon, color, title = ft.Icons.ERROR_OUTLINE, w.RED, f"Service error after {r.elapsed_ms:.1f} ms"
        hint = "Nobody answered. The serving node may be stopped or wedged." if r.kind == "timeout" else ""
        self.result_box.content = ft.Column(
            [
                ft.Row([ft.Icon(icon, color=color, size=18), ft.Text(title, color=color, weight=ft.FontWeight.W_500)], spacing=8),
                w.mono(pretty_json(r.text), selectable=True, color=None if r.ok else color),
                w.muted(hint, size=12, visible=bool(hint)),
            ],
            spacing=10,
            tight=True,
        )

    def _refresh_history(self) -> None:
        records = self.history.get(self.selected or "", [])
        if not records:
            self.history_col.controls = [w.muted("No calls yet.", size=13)]
            return
        self.history_col.controls = [self._history_row(r) for r in records]
        self._show_result(records[0].result)

    def _history_row(self, record: CallRecord) -> ft.Container:
        r = record.result
        color = w.GREEN if r.ok else (w.AMBER if r.kind == "timeout" else w.RED)
        outcome = r.text.replace("\n", " ")
        return ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.CHECK if r.ok else ft.Icons.CLOSE, size=16, color=color),
                    w.mono(w.clock(record.at_ns, millis=False), color=w.MUTED),
                    w.mono(record.request.replace("\n", " "), expand=1, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Icon(ft.Icons.ARROW_FORWARD, size=14, color=w.MUTED),
                    w.mono(outcome, expand=1, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS, color=None if r.ok else color),
                    w.mono(f"{r.elapsed_ms:.1f} ms", color=w.MUTED),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            border_radius=8,
            ink=True,
            tooltip="Use this request again",
            on_click=lambda e: self._reuse(record),
        )

    def _reuse(self, record: CallRecord) -> None:
        self.raw.value = pretty_json(record.request)
        self.raw.visible = True
        self.form_box.visible = False
        self.no_args.visible = False
        if self.fields:
            self.mode.selected = ["json"]
        self._show_result(record.result)
        self.root.update()


def _input(f: FieldSpec) -> ft.Control:
    label = f"{f.name}{' *' if f.required else ''}"
    hint = f.hint() or None
    if f.kind == "boolean":
        return ft.Switch(label=f.name, value=f.initial(), tooltip=hint)
    if f.kind == "enum":
        return ft.Dropdown(
            label=label,
            value=f.initial() or None,
            options=[ft.DropdownOption(key=str(c), text=str(c)) for c in f.choices or []],
            dense=True,
        )
    mono_style = ft.TextStyle(font_family=w.MONO, font_family_fallback=w.MONO_FALLBACK, size=13)
    return ft.TextField(
        label=label,
        value=f.initial(),
        helper=hint,
        dense=True,
        hint_text={"integer": "whole number", "number": "number", "json": "JSON value, e.g. [1, 2] or {\"a\": 1}"}.get(f.kind),
        text_style=mono_style if f.kind in ("json", "number", "integer") else None,
        keyboard_type=ft.KeyboardType.NUMBER if f.kind in ("number", "integer") else None,
    )
