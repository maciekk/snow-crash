"""Snow Crash — TUI chat client for Ollama."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

import ollama
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer
from textual.reactive import reactive
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Footer, Input, Markdown, OptionList, Static


# ── Message bubbles ───────────────────────────────────────────────────────────


class UserBubble(Static):
    """A user message bubble."""

    DEFAULT_CSS = """
    UserBubble {
        background: $primary-darken-3;
        border: tall $primary;
        border-title-color: $primary;
        color: $text;
        margin: 1 2;
        padding: 0 1;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__(text, markup=False)
        self.border_title = "You"


class AssistantBubble(Widget):
    """An assistant message bubble that renders Markdown and supports streaming."""

    DEFAULT_CSS = """
    AssistantBubble {
        background: $surface;
        border: tall $accent;
        border-title-color: $accent;
        color: $text;
        margin: 1 2;
        padding: 0 1;
        height: auto;
    }
    AssistantBubble Markdown {
        background: transparent;
        padding: 0;
        margin: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.border_title = "Assistant"
        self._content = ""

    def compose(self) -> ComposeResult:
        yield Markdown("")

    def append(self, chunk: str) -> None:
        self._content += chunk
        self.query_one(Markdown).update(self._content)

    def finish(self) -> None:
        """Called when streaming is complete."""
        pass


# ── Model selector ────────────────────────────────────────────────────────────


class ModelDropdown(OptionList):
    """Floating model list spawned by ModelPicker."""

    DEFAULT_CSS = """
    ModelDropdown {
        layer: overlay;
        width: 40;
        height: auto;
        max-height: 12;
        background: $surface;
        border: solid $accent;
    }
    """

    def __init__(self, models: list[str], on_pick) -> None:
        super().__init__(*models)
        self._models = models
        self._on_pick = on_pick

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._on_pick(self._models[event.option_index])
        self.remove()

    def on_blur(self) -> None:
        self.remove()

    def key_escape(self) -> None:
        self.remove()


class ModelPicker(Static, can_focus=True):
    """Single-row label showing the active model; click to open a dropdown."""

    DEFAULT_CSS = """
    ModelPicker {
        width: auto;
        height: 1;
        padding: 0 1;
        background: $primary-darken-1;
        color: $text;
        content-align: center middle;
    }
    ModelPicker:focus {
        background: $primary-darken-2;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._models: list[str] = []
        self._index = 0

    def on_mount(self) -> None:
        try:
            self._models = [m.model for m in ollama.list().models]
        except Exception:
            self._models = []
        self._refresh()

    def _refresh(self) -> None:
        label = self._models[self._index] if self._models else "(no models)"
        self.update(f"{label} \u25be")

    def on_click(self) -> None:
        # Toggle: close if already open
        try:
            self.app.screen.query_one(ModelDropdown).remove()
            return
        except NoMatches:
            pass
        if not self._models:
            return
        region = self.region
        dropdown = ModelDropdown(self._models, self._pick)
        self.app.screen.mount(dropdown)
        # Right-align with picker, flush below the top bar
        dropdown.styles.offset = (region.right - 40, region.bottom)
        dropdown.focus()

    def _pick(self, model: str) -> None:
        if model in self._models:
            self._index = self._models.index(model)
        self._refresh()
        self.focus()

    @property
    def selected_model(self) -> str:
        return self._models[self._index] if self._models else ""


class TopBar(Horizontal):
    """Single-row title bar with model picker in the upper-right corner."""

    DEFAULT_CSS = """
    TopBar {
        height: 1;
        dock: top;
        background: $primary;
        align: left middle;
        padding: 0 1;
    }
    TopBar #app-title {
        width: 1fr;
        color: $text;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("Snow Crash", id="app-title")
        yield ModelPicker()

    @property
    def selected_model(self) -> str:
        return self.query_one(ModelPicker).selected_model


# ── Input bar ─────────────────────────────────────────────────────────────────


class InputBar(Horizontal):
    """Bottom input bar."""

    DEFAULT_CSS = """
    InputBar {
        height: 3;
        align: left middle;
        padding: 0 1;
        background: $panel;
        border-top: solid $primary;
    }
    InputBar Input {
        width: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type a message and press Enter…", id="chat-input")


# ── Main app ──────────────────────────────────────────────────────────────────


@dataclass
class Message:
    role: str
    content: str


class SnowCrashApp(App):
    """Ollama TUI chat application."""

    CSS = """
    Screen {
        layers: base overlay;
    }
    #chat-log {
        height: 1fr;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear"),
    ]

    busy: reactive[bool] = reactive(False)

    def __init__(self) -> None:
        super().__init__()
        self._history: list[Message] = []

    def compose(self) -> ComposeResult:
        yield TopBar()
        yield ScrollableContainer(id="chat-log")
        yield InputBar()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()

    # ── Reactive ──────────────────────────────────────────────────────────────

    def watch_busy(self, busy: bool) -> None:
        self.query_one("#chat-input", Input).disabled = busy
        if not busy:
            self.query_one("#chat-input", Input).focus()

    # ── Events ────────────────────────────────────────────────────────────────

    @on(Input.Submitted, "#chat-input")
    def handle_send(self) -> None:
        if self.busy:
            return
        inp = self.query_one("#chat-input", Input)
        text = inp.value.strip()
        if not text:
            return
        inp.clear()
        self._send_message(text)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_clear_chat(self) -> None:
        self._history.clear()
        log = self.query_one("#chat-log", ScrollableContainer)
        log.remove_children()

    # ── Worker ────────────────────────────────────────────────────────────────

    def _send_message(self, text: str) -> None:
        model = self.query_one(TopBar).selected_model
        if not model:
            self.notify("No model selected.", severity="error")
            return

        # Show user bubble
        log = self.query_one("#chat-log", ScrollableContainer)
        log.mount(UserBubble(text))

        # Create assistant bubble
        bubble = AssistantBubble()
        log.mount(bubble)
        log.scroll_end(animate=False)

        self._history.append(Message("user", text))
        self.busy = True
        self._stream_response(model, bubble)

    @work(exclusive=False, thread=False)
    async def _stream_response(self, model: str, bubble: AssistantBubble) -> None:
        full = ""
        try:
            messages = [{"role": m.role, "content": m.content} for m in self._history]
            async_client = ollama.AsyncClient()
            stream: AsyncIterator = await async_client.chat(
                model=model,
                messages=messages,
                stream=True,
            )
            async for chunk in stream:
                token = chunk.message.content or ""
                full += token
                bubble.append(token)
                self.query_one("#chat-log", ScrollableContainer).scroll_end(animate=False)
        except Exception as exc:
            bubble.append(f"\n\n**Error:** {exc}")
        finally:
            bubble.finish()
            self._history.append(Message("assistant", full))
            self.busy = False


def main() -> None:
    SnowCrashApp().run()


if __name__ == "__main__":
    main()
