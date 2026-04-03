"""Snow Crash — TUI chat client for Ollama."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

import asyncio
import colorsys
import math
import os
import re
import time
from pathlib import Path

import ollama
from pylatexenc.latex2text import LatexNodes2Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.theme import Theme
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Footer, Input, Markdown, OptionList, Rule, Static


_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "snow-crash"
_SYS_PROMPT_FILE = _DATA_DIR / "system_prompt.txt"


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


# ── LaTeX → Unicode conversion ───────────────────────────────────────────────

_latex_converter = LatexNodes2Text()
_CODE_BLOCK_RE = re.compile(r'```[\s\S]*?```|`[^`\n]+`')
_DISPLAY_MATH_RE = re.compile(r'\$\$(.+?)\$\$', re.DOTALL)
_INLINE_MATH_RE = re.compile(r'\$([^\$\n]+?)\$')


def _safe_latex(expr: str) -> str:
    try:
        return _latex_converter.latex_to_text(expr)
    except Exception:
        return f"${expr}$"


def _convert_latex(text: str) -> str:
    """Replace LaTeX math expressions with Unicode, leaving code blocks intact."""
    parts: list[str] = []
    last = 0
    for m in _CODE_BLOCK_RE.finditer(text):
        seg = text[last:m.start()]
        seg = _DISPLAY_MATH_RE.sub(lambda x: _safe_latex(x.group(1)), seg)
        seg = _INLINE_MATH_RE.sub(lambda x: _safe_latex(x.group(1)), seg)
        parts.append(seg)
        parts.append(m.group(0))
        last = m.end()
    seg = text[last:]
    seg = _DISPLAY_MATH_RE.sub(lambda x: _safe_latex(x.group(1)), seg)
    seg = _INLINE_MATH_RE.sub(lambda x: _safe_latex(x.group(1)), seg)
    parts.append(seg)
    return "".join(parts)


# ── Collapsible Markdown sections ────────────────────────────────────────────

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)


def _parse_sections(text: str) -> list[tuple[int, str, str]]:
    """Split Markdown at the minimum heading level found.

    Each section's body extends to the next heading of the *same* level,
    so sub-headings (###, ####, …) are included in the body and handled
    recursively when CollapsibleSection composes itself.
    level=0 means preamble content before the first heading.
    """
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(0, "", text)]

    min_level = min(len(m.group(1)) for m in matches)
    top = [m for m in matches if len(m.group(1)) == min_level]

    sections: list[tuple[int, str, str]] = []
    if top[0].start() > 0:
        preamble = text[:top[0].start()].strip()
        if preamble:
            sections.append((0, "", preamble))
    for i, m in enumerate(top):
        body_start = m.end()
        body_end = top[i + 1].start() if i + 1 < len(top) else len(text)
        sections.append((min_level, m.group(2).strip(), text[body_start:body_end].strip()))
    return sections


class SectionHeading(Static):
    """Clickable heading that signals its parent CollapsibleSection to toggle."""

    class Clicked(Message):
        pass

    DEFAULT_CSS = """
    SectionHeading {
        color: $primary;
        text-style: bold;
        padding: 0 1;
        background: $background;
    }
    SectionHeading:hover {
        color: $accent;
    }
    """

    def __init__(self, level: int, text: str) -> None:
        super().__init__("")
        self._level = level
        self._text = text

    def on_mount(self) -> None:
        self.render_state(collapsed=False)

    def render_state(self, collapsed: bool) -> None:
        arrow = "\u25b8" if collapsed else "\u25be"
        self.update(f"{'#' * self._level} {self._text} {arrow}")

    def on_click(self) -> None:
        self.post_message(self.Clicked())


class CollapsibleSection(Widget):
    """A Markdown section whose body can be hidden by clicking the heading."""

    DEFAULT_CSS = """
    CollapsibleSection {
        height: auto;
        margin: 0;
    }
    CollapsibleSection Markdown {
        background: $background;
        padding: 0 1;
        margin: 0;
    }
    CollapsibleSection .ellipsis {
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, level: int, heading: str, body: str) -> None:
        super().__init__()
        self._level = level
        self._heading = heading
        self._body = body
        self._collapsed = False

    def compose(self) -> ComposeResult:
        yield SectionHeading(self._level, self._heading)
        # Recursively render sub-sections if the body contains headings
        if self._body:
            sub = _parse_sections(self._body)
            if any(lv > 0 for lv, _, _ in sub):
                for lv, hd, bd in sub:
                    if lv == 0:
                        yield Markdown(bd)
                    else:
                        yield CollapsibleSection(lv, hd, bd)
            else:
                yield Markdown(self._body)
        yield Static("…", classes="ellipsis")

    def on_mount(self) -> None:
        self.query_one(".ellipsis").display = False

    def on_section_heading_clicked(self, event: SectionHeading.Clicked) -> None:
        event.stop()
        self._collapsed = not self._collapsed
        self.query_one(SectionHeading).render_state(self._collapsed)
        # Toggle every child except the heading and the ellipsis placeholder
        for child in self.children:
            if not isinstance(child, SectionHeading) and not child.has_class("ellipsis"):
                child.display = not self._collapsed
        self.query_one(".ellipsis").display = self._collapsed


class AssistantBubble(Widget):
    """An assistant message bubble — left-aligned with a magenta left-edge bar."""

    _FPS = 12
    _HUE_PERIOD = 1.5  # seconds per full rainbow cycle
    _FLUSH_FPS = 15    # max Markdown re-renders per second while streaming

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
        self._spinner_timer = None
        self._flush_timer = None
        self._dirty = False

    def compose(self) -> ComposeResult:
        yield Static("AI", classes="heading")
        yield Markdown("")

    def on_mount(self) -> None:
        self._spinner_timer = self.set_interval(1 / self._FPS, self._tick)

    def _tick(self) -> None:
        hue = (time.monotonic() % self._HUE_PERIOD) / self._HUE_PERIOD
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        self.query_one(".heading", Static).styles.color = (
            f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})"
        )

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self.query_one(".heading", Static).styles.color = ""

    def _flush(self) -> None:
        if self._dirty:
            try:
                chat_log = self.app.query_one("#chat-log", ScrollableContainer)
                # Capture scroll position BEFORE layout changes so we know
                # whether the user was at the bottom prior to new content arriving.
                was_at_bottom = chat_log.max_scroll_y - chat_log.scroll_y <= 3
                self.query_one(Markdown).update(_convert_latex(self._content))
                self._dirty = False
                if was_at_bottom:
                    self.call_after_refresh(lambda: chat_log.scroll_end(animate=False))
            except Exception:
                pass

    def append(self, chunk: str) -> None:
        if chunk and not self._content:
            self._stop_spinner()
            self._flush_timer = self.set_interval(1 / self._FLUSH_FPS, self._flush)
        self._content += chunk
        self._dirty = True

    def finish(self) -> None:
        """Replace the streaming Markdown with collapsible sections if headings exist."""
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None
        self._flush()  # render any buffered content before restructuring
        self._stop_spinner()
        sections = _parse_sections(_convert_latex(self._content))
        if not any(level > 0 for level, _, _ in sections):
            return  # No headings — keep the plain Markdown as-is
        self.query_one(Markdown).remove()
        for level, heading, body in sections:
            if level == 0:
                self.mount(Markdown(body))
            else:
                self.mount(CollapsibleSection(level, heading, body))


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


class SystemPromptBar(Horizontal):
    """Collapsible bar for editing the system prompt (toggled with Ctrl+Y)."""

    DEFAULT_CSS = """
    SystemPromptBar {
        height: 3;
        dock: top;
        align: left middle;
        padding: 0 1;
        background: #0a0a1e;
        border-top: solid #3a007a;
        border-bottom: solid #3a007a;
    }
    SystemPromptBar #sys-label {
        width: auto;
        color: $accent;
        text-style: bold;
        margin: 0 1 0 0;
    }
    SystemPromptBar Input {
        width: 1fr;
        border: none;
        background: #0a0a1e;
        color: $foreground;
        padding: 0;
    }
    SystemPromptBar Input:focus {
        border: none;
    }
    SystemPromptBar Input .input--placeholder {
        color: #443060;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("SYS:", id="sys-label")
        yield Input(placeholder="System prompt (applied to all messages)…", id="sys-input")

    @property
    def value(self) -> str:
        return self.query_one("#sys-input", Input).value

    def set_value(self, text: str) -> None:
        self.query_one("#sys-input", Input).value = text

    @on(Input.Changed, "#sys-input")
    def _save_on_change(self, event: Input.Changed) -> None:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _SYS_PROMPT_FILE.write_text(event.value)
        except OSError:
            pass


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
        dock: bottom;
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
    #content-wrapper {
        width: 100%;
        height: 1fr;
        align: center top;
    }
    #content-col {
        width: 100%;
        max-width: 120;
        height: 1fr;
    }
    #header-rule {
        color: #007a94;
        margin: 0;
    }
    #chat-log {
        height: 1fr;
        overflow-y: auto;
        background: $background;
    }
    Footer {
        background: $panel;
        color: $primary;
    }
    MarkdownHorizontalRule, Rule {
        color: #00ff41;
    }
    MarkdownBlock .strong {
        color: #ffd700;
        text-style: bold;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear"),
        ("ctrl+y", "toggle_system_prompt", "System prompt"),
    ]

    busy: reactive[bool] = reactive(False)

    def __init__(self) -> None:
        super().__init__()
        self._history: list[Message] = []

    def compose(self) -> ComposeResult:
        yield TopBar()
        bar = SystemPromptBar()
        bar.display = False
        yield bar
        yield Rule(id="header-rule")
        with Vertical(id="content-wrapper"):
            with Vertical(id="content-col"):
                yield ScrollableContainer(id="chat-log")
        yield InputBar()
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(CYBERPUNK)
        self.theme = "cyberpunk"
        if _SYS_PROMPT_FILE.exists():
            saved = _SYS_PROMPT_FILE.read_text()
            if saved:
                self.query_one(SystemPromptBar).set_value(saved)
        self.query_one("#chat-input", Input).focus()

    # ── Reactive ──────────────────────────────────────────────────────────────

    def watch_busy(self, busy: bool) -> None:
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

    def action_toggle_system_prompt(self) -> None:
        bar = self.query_one(SystemPromptBar)
        bar.display = not bar.display
        if bar.display:
            bar.query_one("#sys-input", Input).focus()
        else:
            self.query_one("#chat-input", Input).focus()

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
            sys_prompt = self.query_one(SystemPromptBar).value.strip()
            messages = []
            if sys_prompt:
                messages.append({"role": "system", "content": sys_prompt})
            messages += [{"role": m.role, "content": m.content} for m in self._history]
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
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            bubble.append(f"\n\n**Error:** {exc}")
        finally:
            try:
                bubble.finish()
                self._history.append(Message("assistant", full))
                self.busy = False
            except Exception:
                pass


def main() -> None:
    SnowCrashApp().run()


if __name__ == "__main__":
    main()
