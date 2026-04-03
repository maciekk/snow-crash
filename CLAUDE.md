# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies and run
uv sync
uv run snow-crash

# Run directly without installing the script
uv run python -m snow_crash.app

# Add a dependency
uv add <package>
```

There are no tests or linter configured. The entire application lives in `snow_crash/app.py`.

## Architecture

Single-file Textual TUI app (`snow_crash/app.py`) built around `SnowCrashApp`, which is the top-level `App` subclass. The Ollama Python SDK provides async streaming AI responses.

**Widget hierarchy:**
```
SnowCrashApp
├── TopBar → ModelPicker (queries Ollama on mount for available models)
├── SystemPromptBar (toggleable via Ctrl+Y, persists to disk)
├── ScrollableContainer #chat-log
│   ├── UserBubble
│   └── AssistantBubble (streaming target, holds collapsible sections)
└── InputBar → StrobingPrompt + TextArea
```

**Streaming flow:** `handle_send()` → `_stream_response()` (a `@work` async worker) → iterates `ollama.AsyncClient().chat(stream=True)` → calls `bubble.append_chunk()` per token → `bubble.finish()` on completion → `_save_chat()`.

**State:** `busy: reactive[bool]` gates input. `self._history: list[Message]` is the in-memory conversation, cleared on Ctrl+L, saved on quit, restorable via the Ctrl+H history browser.

**Persistence:** Chat sessions are saved as YAML front-matter Markdown files in `~/.local/share/snow-crash/chats/`. System prompt persists to `~/.local/share/snow-crash/system_prompt.txt`.

## UI Peculiarities

### Strobing Prompt (`StrobingPrompt`, ~line 552)
The `>>` prompt in the input bar runs a continuous 3-phase color animation at 30 FPS (1.875 s period):
- **Phase A** (0–18%): white → full cyan, linear — fast "ignition"
- **Phase B** (18–40%): holds at full cyan
- **Phase C** (40–100%): cyan → near-black, quadratic ease-in — slow fade

This stops and restarts on each cycle independently of any other app state.

### Rainbow Heading Animation (`AssistantBubble._tick()`, ~line 290)
While waiting for the first token from Ollama, any Markdown headings in the bubble cycle through rainbow hues at 12 FPS (1.5 s period). The animation stops as soon as the first chunk arrives. This gives visual feedback that a response is in progress even before any text appears.

### Streaming Responsiveness (`AssistantBubble.append_chunk()`, ~line 317)
To prevent the UI from lagging on large or fast responses, Markdown re-renders are throttled to 15 FPS via a dirty-flag pattern: incoming tokens accumulate in a buffer and the `Markdown` widget is only updated at the next tick if the dirty flag is set. The scroll position is also managed here — auto-scroll only fires if the user was already at the bottom before the chunk arrived, so reading earlier history is not interrupted.

### Collapsible Sections (`CollapsibleSection`, ~line 191)
`AssistantBubble` recursively parses its content for Markdown heading hierarchies and wraps each section in a `CollapsibleSection` widget. Clicking a heading toggles the body. Collapsed sections show a `…` indicator. This restructuring happens on `finish()`, not during streaming.

### LaTeX → Unicode (`latex_to_unicode()`, ~line 92)
Before rendering, assistant content is scanned for `$...$` and `$$...$$` math expressions. These are converted to Unicode via `pylatexenc`, with code fences and inline code spans explicitly skipped. Conversion failures fall back to the original expression silently.

### Model Dropdown (`ModelDropdown`, ~line 345)
The model picker renders as a floating overlay anchored below `TopBar`. It queries `ollama.list()` on mount. The dropdown is a separate widget mounted into the app root so it can overlap other widgets without clipping.

### Cyberpunk Theme
Defined as a named Textual `Theme` registered at startup. Key colors: neon cyan `#00e5ff` (user), hot magenta `#ff0080` (assistant), matrix green `#00ff41` (active input border), near-black `#080810` (background).
