"""Probe Textual 8.2.8 TextArea key handling for enter-submit design."""

from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class Area(TextArea):
    BINDINGS = [
        Binding("enter", "submit", "发送", priority=True),
        Binding("ctrl+j", "insert_newline", "换行", priority=True),
    ]

    class Submitted(Message):
        def __init__(self, text: str):
            self.text = text
            super().__init__()

    def action_submit(self):
        self.post_message(self.Submitted(self.text))
        self.text = ""

    def action_insert_newline(self):
        self.insert("\n")


class ProbeApp(App):
    def compose(self):
        yield Area()

    def on_area_submitted(self, message: Area.Submitted):
        print("SUBMITTED:", repr(message.text))

    def on_mount(self):
        self.query_one(Area).focus()


async def main():
    app = ProbeApp()
    async with app.run_test() as pilot:
        area = app.query_one(Area)
        await pilot.press("x")
        await pilot.press("enter")
        print("after enter:", repr(area.text))
        await pilot.press("y")
        await pilot.press("ctrl+j")
        await pilot.press("z")
        await pilot.pause()
        print("after ctrl+j:", repr(area.text))
        # does TextArea copy binding intercept ctrl+c?
        await pilot.press("ctrl+c")
        await pilot.pause()
        print("status: selected_text =", repr(area.selected_text if hasattr(area, 'selected_text') else 'n/a'))
        print("ctrl+c did NOT quit" if app.is_running else "ctrl+c quit")
        # test ctrl+enter as well
        await pilot.press("a")
        await pilot.press("ctrl+enter")
        await pilot.pause()
        print("after ctrl+enter text:", repr(area.text))


import asyncio

asyncio.run(main())