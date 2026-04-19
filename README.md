# Trading Copilot

A split-screen desktop companion for TradingView. On the left, a chat panel powered by Google's Gemini / Gemma models. On the right, your live TradingView chart in a real Chrome window. Ask questions, attach the current chart as a screenshot, and the copilot remembers your session across restarts.

Built for discretionary futures traders who want a second set of eyes on their setups without context-switching to a separate chat tab.

---

## Features

- **Real Chrome for charts, Qt for chat** — no embedded-browser OAuth issues. You sign into TradingView once with Google and stay signed in.
- **One-click screenshot of the chart** — captured via Chrome DevTools Protocol, sent to the model alongside your question.
- **Persistent memory** — every turn is saved to disk; close the app and reopen tomorrow, the conversation resumes.
- **Rolling image window** — all text history is sent every turn, but only the last 3 screenshots, keeping image-token costs bounded.
- **Your indicators in the system prompt** — paste your Pine Script (or a plain-English description) and the copilot reasons about every screenshot using your own logic.
- **Model switching** — works with `gemma-4-26b-a4b-it` (cheap and fast) or `gemini-2.5-pro` (stronger vision) with a single-line config change.

---

## Architecture

```
┌─────────────────────┐          ┌──────────────────────────────┐
│   Qt chat window    │          │   Chrome (headful)           │
│   (PySide6)         │          │   --remote-debugging-port    │
│                     │          │   =9222                      │
│   ┌─────────────┐   │  CDP     │                              │
│   │ bubbles UI  │◄──┼──────────┤   https://tradingview.com    │
│   └─────────────┘   │  screen- │   your logged-in session     │
│         │           │  shot    │                              │
│         ▼           │          └──────────────────────────────┘
│   Gemini API        │
│   (google-genai)    │
└─────────────────────┘
```

The Python app attaches to an already-running Chrome over CDP using Playwright. It doesn't drive Chrome or scrape TradingView — it only reads window geometry and captures PNG screenshots of the page. Your Chrome session is untouched.

---

## Requirements

- Python 3.10+
- Windows 10/11 (tested). Linux/macOS should work with minor path tweaks.
- A Chrome or Chromium build you can launch with the `--remote-debugging-port` flag.
- A Google AI Studio API key — get one at https://aistudio.google.com/apikey

---

## Installation

```powershell
git clone <your-repo-url>
cd trading-copilot

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements_cdp.txt
```

---

## Configuration

Create `config.py` in the project root (already ignored by `.gitignore`):

```python
# config.py
API_KEY = "your_gemini_key_here"
MODEL = "gemma-4-26b-a4b-it"   # or "gemini-2.5-pro" for stronger vision
```

Open `trading_copilot_cdp.py` and find the `SYSTEM_PROMPT` block. Paste your two indicators' Pine Script (or a plain-English description of their signals) into the two marked slots. That's the only code you need to edit.

---

## Usage

### 1. Launch Chrome on port 9222

Edit `launch_chrome.bat` and update `CHROME_EXE` to point at your Chrome/Chromium binary. Then double-click it.

The launcher:
- Kills any existing Chrome/Chromium processes (so the debug port is free).
- Uses `%LOCALAPPDATA%\ChromeCDP` as a dedicated profile directory — **don't put this in OneDrive**, sync conflicts will cause Chrome to close unexpectedly.
- Opens `tradingview.com/chart/` in the new Chrome window.

First launch: sign into TradingView (Google OAuth works — this is a real browser). Cookies persist in the profile dir across launches.

### 2. Run the copilot

```powershell
python trading_copilot_cdp.py
```

The app auto-positions Chrome to the right 75% of your primary screen and the Qt chat window to the left 25%. If you drag Chrome around, hit the **Re-snap windows** button to reset the split.

### 3. Chat

Type a question and hit Enter. By default a screenshot of the current chart is attached to every message (toggle via the checkbox if you want a text-only follow-up like "what's my P&L today?").

---

## How memory works

Two layers:

1. **In-memory conversation list.** Every turn is appended to `self.history` in the app. On each API call, the full text history is rebuilt into a `Content[]` array and sent to the model. Gemini is stateless; "memory" is just resending the transcript.

2. **Disk persistence.** After every assistant response, `session_cdp.json` is written to `%USERPROFILE%\.trading_copilot\`. On next launch, it's rehydrated into the UI.

**Important caveats:**

- Screenshot bytes are **not** persisted to disk — only a `had_image: true/false` flag. On restart, the copilot remembers that you showed it a chart and what it said about it, but can't re-examine the pixels.
- Only the **last 3 screenshots** are included in the model request (`MAX_IMAGES_IN_CONTEXT` at the top of the file). Tune if you want more visual history at the cost of more tokens.
- Hit **New Session** to wipe both the UI and `session_cdp.json`.

---

## Project structure

```
trading-copilot/
├── trading_copilot_cdp.py    # Main app (CDP mode, recommended)
├── trading_copilot.py        # Alternate: embedded Chromium via QWebEngineView
│                             #   (Google OAuth blocked in embedded browsers)
├── launch_chrome.bat         # Windows launcher for Chrome on port 9222
├── config.py                 # API key + model choice (gitignored)
├── requirements_cdp.txt      # Deps for CDP version (recommended)
├── requirements.txt          # Deps for embedded version
├── .gitignore
└── README.md
```

---

## Customizing the system prompt

The `SYSTEM_PROMPT` constant at the top of `trading_copilot_cdp.py` controls how the copilot reasons about your charts. Two slots are marked for your indicators:

```
--- INDICATOR 1 ---
[PASTE YOUR FIRST INDICATOR CODE OR DESCRIPTION HERE]

--- INDICATOR 2 ---
[PASTE YOUR SECOND INDICATOR CODE OR DESCRIPTION HERE]
```

Either paste the Pine Script directly or write a plain-English description of what the indicator shows and how you use it. Plain English often works better — the model doesn't need to execute Pine, it needs to recognize the indicator's output on the chart.

---

## Known limitations

- **Windows-first.** The batch launcher and `%LOCALAPPDATA%` path are Windows conventions. On macOS/Linux, launch Chrome manually with `--remote-debugging-port=9222 --user-data-dir=~/ChromeCDP` and the Python script works.
- **Gemma 4 26B A4B is weaker at fine chart detail than Gemini 2.5 Pro.** If the model misreads small price labels or candle wicks, switch `MODEL` in `config.py` to `gemini-2.5-pro`.
- **No structured trade log.** "You went long at 5890" lives as free text in the conversation, not as a typed record. An end-of-day P&L summary is only as reliable as the model's recall.
- **No conversation compaction.** After long sessions, the transcript grows. You've got 256K tokens of context with Gemma 4, but eventually you'll want a "summarize older turns" step.

---

## Security

- `config.py` contains your API key. It's in `.gitignore` — **do not commit it**.
- Screenshots are sent to Google's API. Don't share charts with information you don't want Google to have.
- The app only reads from your Chrome session. It doesn't send keystrokes or automate clicks.

---

## License

MIT
