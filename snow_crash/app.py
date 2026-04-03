"""Snow Crash — TUI chat client for Ollama."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

import math
import time

import ollama
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Footer, Input, Markdown, OptionList, Static


# ── Cyberpunk theme ───────────────────────────────────────────────────────────

CYBERPUNK = Theme(
    name="cyberpunk",
    dark=True,
    primary="#00e5ff",       # neon cyan  — user bubbles, borders, input
    secondary="#ff0080",     # hot magenta — assistant bubbles
    accent="#f0006e",        # hot pink — dropdown, footer keys, focused widgets
    background="#080810",    # near-black with blue cast
    surface="#0d0d1a",       # slightly lifted surface
    panel="#0a0a14",         # panel / bars
    warning="#ffe600",       # neon yellow
    error="#ff0040",         # neon red
    success="#00ff41",       # matrix green
    foreground="#c8f0ff",    # ice-blue text
)


# ── Message bubbles ───────────────────────────────────────────────────────────


class UserBubble(Widget):
    """A user message bubble — right-aligned with a cyan right-edge bar."""

    DEFAULT_CSS = """
    UserBubble {
        background: $background;
        border-right: heavy $primary;
        margin: 0 0 1 0;
        height: auto;
    }
    UserBubble .heading {
        color: $primary;
        text-style: bold;
        text-align: right;
        padding: 0 1;
        background: #1c1c28;
    }
    UserBubble .body {
        color: $text;
        text-align: right;
        padding: 0 1;
        background: #1c1c28;
    }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        yield Static("you", classes="heading")
        yield Static(self._text, markup=False, classes="body")


class AssistantBubble(Widget):
    """An assistant message bubble — left-aligned with a magenta left-edge bar."""

    DEFAULT_CSS = """
    AssistantBubble {
        background: $background;
        border-left: heavy $secondary;
        margin: 0 0 1 0;
        height: auto;
    }
    AssistantBubble .heading {
        color: $secondary;
        text-style: bold;
        padding: 0 1;
        background: $background;
    }
    AssistantBubble Markdown {
        background: $background;
        padding: 0 1;
        margin: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._content = ""

    def compose(self) -> ComposeResult:
        yield Static("AI", classes="heading")
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
        background: $panel;
        border: heavy $accent;
    }
    ModelDropdown > .option-list--option-highlighted {
        color: $accent;
        text-style: bold;
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
        background: $surface;
        color: $primary;
        content-align: center middle;
        text-style: bold;
    }
    ModelPicker:focus {
        background: $panel;
        color: $accent;
    }
    ModelPicker:hover {
        background: $surface;
        color: $accent;
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
        background: #002830;
        align: left middle;
        padding: 0 1;
    }
    TopBar #app-title {
        width: 1fr;
        color: $primary;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("// SNOW CRASH //", id="app-title")
        yield ModelPicker()

    @property
    def selected_model(self) -> str:
        return self.query_one(ModelPicker).selected_model


# ── Input bar ─────────────────────────────────────────────────────────────────


class StrobingPrompt(Static):
    """'>>' prompt: snaps to white, fast burn to cyan, lingers, then fades to near-black.

    Phase A  [top 18%] — white → full cyan, fast linear
    Phase B  [next 22%] — hold at full cyan
    Phase C  [bottom 60%] — cyan → near-black, quadratic ease-in
                            (slow departure from cyan, accelerates into dark)
    """

    DEFAULT_CSS = """
    StrobingPrompt {
        width: auto;
        text-style: bold;
        margin: 0 1 0 0;
    }
    """

    _PERIOD  = 1.875         # seconds per cycle
    _FPS     = 30

    _A_EDGE  = 0.82          # raw_t boundary: above = Phase A (white→cyan)
    _B_EDGE  = 0.60          # raw_t boundary: above = Phase B (linger), below = Phase C

    _DARK  = (0,   5,   8)   # near-black with faint cyan ghost
    _CYAN  = (0, 229, 255)   # full neon cyan  #00e5ff
    _WHITE = (255, 255, 255)

    def on_mount(self) -> None:
        self.set_interval(1 / self._FPS, self._strobe)

    def _strobe(self) -> None:
        raw_t = 1.0 - (time.monotonic() % self._PERIOD) / self._PERIOD

        if raw_t >= self._A_EDGE:
            # Phase A: white → cyan (fast, linear)
            local = (raw_t - self._A_EDGE) / (1.0 - self._A_EDGE)
            lo, hi = self._CYAN, self._WHITE
            eased = local
        elif raw_t >= self._B_EDGE:
            # Phase B: hold at full cyan
            self.styles.color = "rgb(0,229,255)"
            return
        else:
            # Phase C: cyan → near-black, ease-in (slow at cyan, fast at dark)
            local = raw_t / self._B_EDGE   # 1.0 at cyan end, 0.0 at dark end
            eased = 1.0 - (1.0 - local) ** 2
            lo, hi = self._DARK, self._CYAN

        r = int(lo[0] + (hi[0] - lo[0]) * eased)
        g = int(lo[1] + (hi[1] - lo[1]) * eased)
        b = int(lo[2] + (hi[2] - lo[2]) * eased)
        self.styles.color = f"rgb({r},{g},{b})"


class InputBar(Horizontal):
    """Bottom input bar."""

    DEFAULT_CSS = """
    InputBar {
        height: 3;
        align: left middle;
        padding: 0 1;
        background: $panel;
        border-top: heavy #00ff41;
    }
    InputBar Input {
        width: 1fr;
        border: none;
        background: $panel;
        color: $text;
        padding: 0;
    }
    InputBar Input .input--placeholder {
        color: #303840;
    }
    InputBar Input:hover {
        border: none;
    }
    InputBar Input:focus {
        border: none;
    }
    """

    def compose(self) -> ComposeResult:
        yield StrobingPrompt(">>")
        yield Input(placeholder="Jack in, interrogate the entity…", id="chat-input")


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
        background: $background;
        border-top: solid #007a94;
    }
    Footer {
        background: $panel;
        color: $primary;
    }
    MarkdownHorizontalRule, Rule {
        color: #00ff41;
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
        self.register_theme(CYBERPUNK)
        self.theme = "cyberpunk"
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
