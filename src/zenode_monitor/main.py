"""Entry point: ``uv run zenode-monitor`` or ``uv run flet run src/zenode_monitor/main.py``."""

from __future__ import annotations

import asyncio
import logging

import flet as ft

from zenode_monitor.ui.shell import App


async def start(page: ft.Page) -> App:
    page.title = "Zenode Monitor"
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
    page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.window.width, page.window.height = 1440, 900
    page.window.min_width, page.window.min_height = 960, 600

    app = App(page)
    page.add(app.root)
    page.run_task(app.pump)

    async def on_window_event(e: ft.WindowEvent) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            try:
                await asyncio.wait_for(app.bus.disconnect(), timeout=3)
            finally:
                await page.window.destroy()

    page.window.prevent_close = True
    page.window.on_event = on_window_event
    return app


async def main(page: ft.Page) -> None:
    await start(page)


def run() -> None:
    logging.basicConfig(level=logging.WARNING)
    ft.run(main)


if __name__ == "__main__":
    run()
