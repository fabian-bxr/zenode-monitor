"""The start screen: where to connect, and which namespace to watch."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import flet as ft

from ..profiles import Profile, ProfileBook
from . import widgets as w

if TYPE_CHECKING:
    from .shell import App

_LOCATOR = re.compile(r"^(tcp|udp|tls|quic|ws|wss|unixsock-stream|unixpipe|serial|vsock)/.+")
NEW = "__new__"


def parse_endpoints(text: str) -> list[str]:
    """Split on commas/whitespace; a bare ``host:port`` is taken as TCP."""
    out = []
    for part in re.split(r"[,\s]+", text.strip()):
        if part:
            out.append(part if _LOCATOR.match(part) else f"tcp/{part}")
    return out


def endpoint_error(endpoint: str) -> str | None:
    rest = endpoint.split("/", 1)[1]
    if endpoint.startswith(("tcp/", "udp/", "tls/", "quic/", "ws/", "wss/")) and not re.search(r":\d+$", rest.split("#")[0]):
        return f"{endpoint} needs a port, e.g. tcp/192.168.1.10:7447"
    return None


class ConnectView:
    def __init__(self, app: App) -> None:
        self.app = app
        self.book = ProfileBook.load()

        self.profile_dd = ft.Dropdown(
            label="Profile", expand=True, dense=True, text_size=14, on_select=lambda e: self._load_selected()
        )
        self.delete_btn = ft.IconButton(
            ft.Icons.DELETE_OUTLINE, tooltip="Delete this profile", on_click=self._delete_profile
        )
        self.name = ft.TextField(label="Profile name", dense=True, hint_text="e.g. Rover lab", helper="Optional. Named connections are saved when you connect.")
        self.mode = ft.SegmentedButton(
            segments=[
                ft.Segment(value="peer", label=ft.Text("Peer"), icon=ft.Icon(ft.Icons.DEVICE_HUB)),
                ft.Segment(value="client", label=ft.Text("Client"), icon=ft.Icon(ft.Icons.ROUTER_OUTLINED)),
            ],
            selected=["peer"],
            show_selected_icon=False,
            on_change=lambda e: self._sync_mode(),
        )
        self.mode_hint = w.muted("")
        self.endpoints = ft.TextField(label="Endpoints", dense=True, hint_text="tcp/192.168.1.10:7447", helper_max_lines=2, error_max_lines=2)
        self.namespace = ft.TextField(
            label="Namespace",
            dense=True,
            hint_text="rover",
            helper_max_lines=2,
            helper="The key prefix the nodes run under ([transport] namespace). Empty if they use none.",
            on_submit=self._on_connect,
        )
        self.multicast = ft.Switch(label="Multicast scouting", value=True)
        self.shm = ft.Switch(
            label="Shared memory", value=False, tooltip="Needed to receive topics published over shared memory"
        )
        self.error = ft.Container(
            content=ft.Row(
                [ft.Icon(ft.Icons.ERROR_OUTLINE, color=w.RED, size=18), ft.Text("", color=w.RED, size=13, expand=True, selectable=True)],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            visible=False,
        )
        self.spinner = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)
        self.connect_btn = ft.FilledButton("Connect", icon=ft.Icons.LINK, on_click=self._on_connect)

        self.root = ft.Container(
            width=560,
            content=w.panel(
                ft.Column(
                    [
                        ft.Text("Connect to a zenode system", size=22, weight=ft.FontWeight.W_600),
                        w.muted("Join the zenoh network your nodes run on. The monitor only observes; it does not register as a node.", size=13),
                        ft.Container(height=4),
                        ft.Row(
                            [
                                self.profile_dd,
                                self.delete_btn,
                                ft.OutlinedButton("Load zenode.toml", icon=ft.Icons.FILE_OPEN_OUTLINED, on_click=self._load_toml),
                            ],
                            spacing=8,
                        ),
                        self.name,
                        ft.Column([ft.Row([self.mode]), self.mode_hint], spacing=6, tight=True),
                        self.endpoints,
                        self.namespace,
                        ft.Row([self.multicast, self.shm], spacing=24),
                        self.error,
                        ft.Row([self.spinner, self.connect_btn], alignment=ft.MainAxisAlignment.END, spacing=12),
                    ],
                    spacing=14,
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                padding=28,
            ),
        )
        self._fill_profiles(self.book.last)
        self._load_selected()

    # -- profiles ----------------------------------------------------------------

    def _fill_profiles(self, select: str = "") -> None:
        self.profile_dd.options = [ft.DropdownOption(key=NEW, text="New connection")] + [
            ft.DropdownOption(key=p.name, text=p.name) for p in self.book.profiles
        ]
        self.profile_dd.value = select if self.book.get(select) else NEW

    def _load_selected(self) -> None:
        profile = self.book.get(self.profile_dd.value or "")
        self.delete_btn.disabled = profile is None
        self._show(profile or Profile(name=""))
        self._safe_update()

    def _show(self, p: Profile) -> None:
        self.name.value = p.name
        self.mode.selected = [p.mode]
        self.endpoints.value = ", ".join(p.connect)
        self.namespace.value = p.namespace
        self.multicast.value = p.multicast_scouting
        self.shm.value = p.shared_memory
        self._set_error(None)
        self._sync_mode()

    def _sync_mode(self) -> None:
        client = self.mode.selected == ["client"]
        self.mode_hint.value = (
            "Connects to a zenoh router. An endpoint is required."
            if client
            else "Finds other peers on the local network, or connects to the endpoints below."
        )
        self.endpoints.helper = (
            "Router locator(s), comma-separated"
            if client
            else "Optional peer or router locator(s), comma-separated. Leave empty to rely on multicast."
        )
        self._safe_update()

    def _delete_profile(self, e: Any = None) -> None:
        name = self.profile_dd.value or ""
        self.book.remove(name)
        self.book.save()
        self._fill_profiles()
        self._load_selected()
        self.app.notify(f"Deleted profile “{name}”")

    async def _load_toml(self, e: Any = None) -> None:
        files = await self.app.file_picker.pick_files(
            dialog_title="Open zenode.toml", allowed_extensions=["toml"], file_type=ft.FilePickerFileType.CUSTOM
        )
        if not files or not files[0].path:
            return
        try:
            profile = Profile.from_toml(files[0].path)
        except Exception as exc:
            self._set_error(f"Could not read {files[0].name}: {exc}")
            self._safe_update()
            return
        self.profile_dd.value = NEW
        self.delete_btn.disabled = True
        self._show(profile)
        self._safe_update()
        self.app.notify(f"Loaded transport settings from {files[0].name}", "ok")

    # -- connecting --------------------------------------------------------------

    def _read(self) -> Profile | None:
        """Validate the form. Returns ``None`` after showing what to fix."""
        self.endpoints.error = None
        mode = self.mode.selected[0] if self.mode.selected else "peer"
        endpoints = parse_endpoints(self.endpoints.value or "")
        for ep in endpoints:
            if problem := endpoint_error(ep):
                self.endpoints.error = problem
                return None
        if mode == "client" and not endpoints:
            self.endpoints.error = "Client mode needs a router endpoint, e.g. tcp/192.168.1.10:7447"
            return None
        if mode == "peer" and not endpoints and not self.multicast.value:
            self.endpoints.error = "Enter an endpoint or enable multicast scouting, otherwise no node can be found"
            return None
        return Profile(
            name=(self.name.value or "").strip(),
            mode=mode,
            connect=endpoints,
            namespace=(self.namespace.value or "").strip().strip("/"),
            multicast_scouting=bool(self.multicast.value),
            shared_memory=bool(self.shm.value),
        )

    async def _on_connect(self, e: Any = None) -> None:
        self._set_error(None)
        profile = self._read()
        if profile is None:
            self._safe_update()
            return
        self._busy(True)
        try:
            await self.app.connect(profile)
        except Exception as exc:
            self._set_error(_explain(exc))
        else:
            if profile.name:
                self.book.put(profile)
                self.book.last = profile.name
                self.book.save()
                self._fill_profiles(profile.name)
                self.delete_btn.disabled = False
        finally:
            self._busy(False)

    def _busy(self, busy: bool) -> None:
        self.spinner.visible = busy
        self.connect_btn.disabled = busy
        self.connect_btn.content = "Connecting…" if busy else "Connect"
        self._safe_update()

    def _set_error(self, text: str | None) -> None:
        self.error.visible = bool(text)
        self.error.content.controls[1].value = text or ""

    def _safe_update(self) -> None:
        try:
            self.root.update()
        except Exception:
            pass  # not on the page yet


def _explain(exc: Exception) -> str:
    text = str(exc)
    text = re.sub(r"\s+at /\S+\.rs:\d+\.?$", "", text)  # drop the Rust source location
    if "Unable to connect" in text:
        return f"{text} Check the address, that the router is running, and that a firewall is not in the way."
    return text or type(exc).__name__
