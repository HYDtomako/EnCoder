"""Probe keybinding precedence between focused widget normal binding and app priority binding."""

from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class Area(TextArea):
    pass


class ProbeApp(App):
    BINDINGS = [
        Binding("ctrl+c", "app_ctrl_c", "中断/退出", priority=True),
    ]

    def compose(self):
        yield Area()

    def action_app_ctrl_c(self):
        print("APP CTRL+C HANDLED")

    def on_mount(self):
        self.query_one(Area).focus()


async def main():
    app = ProbeApp()
    async with app.run_test() as pilot:
        await pilot.press("x")
        await pilot.press("ctrl+c")
        await pilot.pause()
        area = app.query_one(Area)
        print("copied =", repr(area.selected_text))
        print("app still running:", app.is_running)


import asyncio

asyncio.run(main())