"""
Trading Copilot (CDP edition) — attaches to an existing Chrome on port 9222.

Workflow:
    1. Launch Chrome with --remote-debugging-port=9222 (use launch_chrome.bat).
    2. Sign into TradingView in that Chrome (Google OAuth works — it's a real browser).
    3. Open your chart.
    4. Run: python trading_copilot_cdp.py

The app attaches via Playwright CDP, auto-snaps Chrome to the right 75% of your screen,
places the chat window on the left 25%, and captures chart screenshots through CDP.
"""
import sys
import json
import logging
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QTextCursor, QFont
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QTextCursor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QCheckBox, QMessageBox,
    QFileDialog, QScrollArea, QFrame, QSizePolicy,
)

from google import genai
from google.genai import types

from config import API_KEY, MODEL


# ============================================================
# CONFIG
# ============================================================
CDP_URL = "http://localhost:9222"
TV_DOMAIN = "tradingview.com"       # substring match for the TradingView tab
GEMINI_MODEL = MODEL

APP_DIR = Path.home() / ".trading_copilot"
SESSION_FILE = APP_DIR / "session_cdp.json"
LOG_FILE = APP_DIR / "trading_copilot_cdp.log"

MAX_IMAGES_IN_CONTEXT = 3

SYSTEM_PROMPT = """You are a trading copilot assisting a futures trader who trades the E-mini S&P 500 (ES=F).

The trader uses TWO custom indicators on their TradingView chart. Here are the indicators:

--- INDICATOR 1 ---
your indicator here
--- INDICATOR 2 ---
your indicator here
When the trader sends you a screenshot, analyze the chart using BOTH indicators' logic.
Reference specific price levels and candle/bar signals you can actually see.
Be concise — 2-5 sentences unless they explicitly ask for depth.

Track the session across turns: entries, exits, stops, P&L, reasoning, mistakes.
When they ask a follow-up like "where did I get in?" or "how am I doing today?",
answer from your memory of prior turns without asking them to repeat.

If a screenshot is attached, prioritize what's visually on it over any assumption.
If no screenshot is attached and one would clearly help, say so briefly.

Traders A+ setup is when the crossover happens and the vol20 is flashing green 
"""

# ============================================================
APP_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
 
 
# ============================================================
# Gemini streaming worker (unchanged pattern)
# ============================================================
class GeminiWorker(QThread):
    chunk_received = Signal(str)
    finished_ok = Signal(str)
    error = Signal(str)
 
    def __init__(self, client, model, system_prompt, history):
        super().__init__()
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.history = history
 
    def run(self):
        try:
            image_msgs = [i for i, m in enumerate(self.history) if m.get("image_bytes")]
            keep = set(image_msgs[-MAX_IMAGES_IN_CONTEXT:])
 
            contents = []
            for i, msg in enumerate(self.history):
                parts = []
                if msg.get("text"):
                    parts.append(types.Part.from_text(text=msg["text"]))
                img = msg.get("image_bytes")
                if img and i in keep:
                    parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))
                if parts:
                    contents.append(types.Content(role=msg["role"], parts=parts))
 
            full_text = ""
            stream = self.client.models.generate_content_stream(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=self.system_prompt,
                    temperature=0.4,
                ),
            )
            for chunk in stream:
                if chunk.text:
                    full_text += chunk.text
                    self.chunk_received.emit(chunk.text)
            self.finished_ok.emit(full_text)
        except Exception as e:
            logger.exception("Gemini call failed")
            self.error.emit(str(e))
 
 
# ============================================================
# Markdown → HTML (tiny subset) for chat bubbles
# ============================================================
import re as _re
 
 
def md_to_html(text: str) -> str:
    """Minimal markdown: **bold**, *italic*, `code`, bullets, and line breaks."""
    s = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Bold **...**
    s = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=_re.DOTALL)
    # Italic *...*  (skip leftovers from bold)
    s = _re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", s)
    # Inline `code`
    s = _re.sub(
        r"`([^`\n]+?)`",
        r'<code style="background:#2a2a2a;padding:1px 4px;border-radius:3px;'
        r'font-family:Consolas,monospace;">\1</code>', s,
    )
    # Bullet lines (- item  or  * item)  →  • item
    s = _re.sub(r"(?m)^\s*[-*]\s+", "• ", s)
    # Paragraph breaks and single newlines
    s = s.replace("\n\n", "<br><br>").replace("\n", "<br>")
    return s
 
 
# ============================================================
# Chat bubble widget — one per turn
# ============================================================
class MessageBubble(QFrame):
    STYLES = {
        "user":   {"bg": "#2962ff", "fg": "#ffffff", "name": "You",     "accent": "#bbdefb"},
        "model":  {"bg": "#2a2a2a", "fg": "#e8e8e8", "name": "Copilot", "accent": "#81c784"},
        "system": {"bg": "transparent", "fg": "#ffb74d", "name": "",    "accent": "#ffb74d"},
    }
 
    def __init__(self, role: str, text: str = "", has_image: bool = False):
        super().__init__()
        self.role = role
        self.has_image = has_image
        self._raw_text = text
 
        style = self.STYLES.get(role, self.STYLES["model"])
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"MessageBubble {{ background: {style['bg']}; border-radius: 10px; }}"
            if role != "system" else "MessageBubble { background: transparent; }"
        )
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
 
        lay = QVBoxLayout(self)
        lay.setContentsMargins(11, 8, 11, 9) if role != "system" else lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)
 
        # Header (name + optional attachment pill)
        if role != "system":
            hdr = QHBoxLayout()
            hdr.setSpacing(6)
            name = QLabel(style["name"])
            name.setStyleSheet(f"color:{style['accent']}; font-weight:600; font-size:11px;")
            hdr.addWidget(name)
            if has_image:
                pill = QLabel("📎 chart")
                pill.setStyleSheet(
                    "color:#ffffff; background:rgba(255,255,255,0.15); "
                    "padding:1px 6px; border-radius:8px; font-size:10px;"
                )
                hdr.addWidget(pill)
            hdr.addStretch()
            lay.addLayout(hdr)
 
        # Body
        self.body = QLabel()
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        italic = "font-style:italic;" if role == "system" else ""
        size = "12px" if role != "system" else "11px"
        self.body.setStyleSheet(
            f"color:{style['fg']}; font-family:'Segoe UI','SF Pro',Arial; "
            f"font-size:{size}; {italic} background:transparent;"
        )
        self.body.setText(md_to_html(text) if text else "")
        lay.addWidget(self.body)
 
    def append_stream(self, chunk: str):
        """For streaming — re-render full HTML on each chunk."""
        self._raw_text += chunk
        self.body.setText(md_to_html(self._raw_text))
 
    def set_text(self, text: str):
        self._raw_text = text
        self.body.setText(md_to_html(text))
 
 
 
class TradingCopilotCDP(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Trading Copilot — Chat (CDP mode)")
        self.history: list[dict] = []
        self.worker: GeminiWorker | None = None
 
        # --- Validate key ---
        if not API_KEY or API_KEY == "PASTE_YOUR_NEW_KEY_HERE":
            QMessageBox.critical(self, "Missing API Key",
                "Open config.py and paste your Gemini API key into API_KEY.")
            sys.exit(1)
        self.client = genai.Client(api_key=API_KEY)
 
        # --- Playwright CDP connection (kept alive for app lifetime) ---
        self._pw = None
        self._browser = None
        self._tv_page = None
        self._connect_to_chrome()  # raises/exits on failure
 
        # --- UI ---
        self._build_ui()
        self._position_windows()
 
        # --- Load prior session ---
        self._load_session()
 
    # ---------- Chrome CDP ----------
    def _connect_to_chrome(self):
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(CDP_URL)
            logger.info(f"Connected to Chrome at {CDP_URL}")
        except Exception as e:
            QMessageBox.critical(self, "Cannot connect to Chrome",
                f"Failed to connect to Chrome at {CDP_URL}.\n\n"
                f"Launch Chrome first with:\n"
                f'  chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\\ChromeCDP"\n\n'
                f"Or run launch_chrome.bat.\n\nDetails: {e}")
            sys.exit(1)
 
        self._tv_page = self._find_tv_page()
        if not self._tv_page:
            QMessageBox.warning(self, "No TradingView tab",
                f"Connected to Chrome, but no tab matching '{TV_DOMAIN}' was found.\n\n"
                f"Open tradingview.com in your Chrome window, then click 'Reconnect'.")
 
    def _find_tv_page(self):
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                try:
                    if TV_DOMAIN in p.url:
                        logger.info(f"Found TradingView tab: {p.url}")
                        return p
                except Exception:
                    continue
        return None
 
    def _position_chrome_window(self):
        """Use CDP's Browser.setWindowBounds to snap Chrome to the right 75%."""
        if not self._tv_page:
            return
        try:
            screen = QApplication.primaryScreen().availableGeometry()
            sw, sh = screen.width(), screen.height()
            sx, sy = screen.x(), screen.y()
            chat_w = int(sw * 0.25)
 
            cdp = self._tv_page.context.new_cdp_session(self._tv_page)
            info = cdp.send("Browser.getWindowForTarget")
            window_id = info["windowId"]
            cdp.send("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {
                    "left": sx + chat_w,
                    "top": sy,
                    "width": sw - chat_w,
                    "height": sh,
                    "windowState": "normal",
                },
            })
            logger.info("Positioned Chrome to right 75% of screen")
        except Exception as e:
            logger.warning(f"Could not auto-position Chrome: {e}")
 
    def _position_windows(self):
        """Qt window → left 25%. Chrome → right 75% (via CDP)."""
        screen = QApplication.primaryScreen().availableGeometry()
        sw, sh = screen.width(), screen.height()
        chat_w = int(sw * 0.25)
        self.setGeometry(screen.x(), screen.y(), chat_w, sh)
        self._position_chrome_window()
 
    # ---------- Screenshot ----------
    def capture_chart_png(self) -> bytes | None:
        """Capture via CDP — no screen-region math, works even if Chrome is occluded."""
        if not self._tv_page:
            self._tv_page = self._find_tv_page()
            if not self._tv_page:
                self._append_system("⚠ No TradingView tab found. Click Reconnect.")
                return None
        try:
            return self._tv_page.screenshot(type="png", full_page=False, timeout=10000)
        except PlaywrightTimeoutError:
            self._append_system("⚠ Screenshot timed out. Is the chart still loaded?")
            return None
        except Exception as e:
            logger.exception("Screenshot failed")
            self._append_system(f"⚠ Screenshot error: {e}")
            # Page probably closed — try re-finding
            self._tv_page = self._find_tv_page()
            return None
 
    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        rl = QVBoxLayout(central)
        rl.setContentsMargins(8, 8, 8, 8)
        rl.setSpacing(6)
 
        header = QLabel(f"📈 Chart Copilot · {GEMINI_MODEL} · CDP")
        header.setFont(QFont("", 11, QFont.Bold))
        rl.addWidget(header)
 
        self.tv_status = QLabel()
        self.tv_status.setStyleSheet("color:#888; font-size:10px;")
        self._refresh_tv_status()
        rl.addWidget(self.tv_status)
 
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_scroll.setStyleSheet(
            "QScrollArea { background:#151515; border:1px solid #2a2a2a; border-radius:8px; }"
            "QScrollBar:vertical { background:#151515; width:8px; }"
            "QScrollBar::handle:vertical { background:#3a3a3a; border-radius:4px; min-height:20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )
        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background:#151515;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(10, 10, 10, 10)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch(1)  # keeps bubbles packed to the top
        self.chat_scroll.setWidget(self.chat_container)
        rl.addWidget(self.chat_scroll, stretch=1)
 
        # Track the currently-streaming assistant bubble so chunks can append to it
        self._current_assistant_bubble: MessageBubble | None = None
 
        opts_row = QHBoxLayout()
        self.attach_screenshot_cb = QCheckBox("Attach chart screenshot")
        self.attach_screenshot_cb.setChecked(True)
        opts_row.addWidget(self.attach_screenshot_cb)
        opts_row.addStretch()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#888; font-size:11px;")
        opts_row.addWidget(self.status_label)
        rl.addLayout(opts_row)
 
        input_row = QHBoxLayout()
        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("Ask about the chart… (Enter to send)")
        self.input_line.setStyleSheet(
            "QLineEdit { background:#2a2a2a; color:#f0f0f0; border:1px solid #444; "
            "border-radius:6px; padding:6px; font-size:12px; }"
        )
        self.input_line.returnPressed.connect(self.on_send)
        input_row.addWidget(self.input_line, stretch=1)
 
        self.send_btn = QPushButton("Send")
        self.send_btn.setStyleSheet(
            "QPushButton { background:#2962ff; color:white; border:none; "
            "border-radius:6px; padding:6px 14px; font-weight:bold; } "
            "QPushButton:disabled { background:#555; }"
        )
        self.send_btn.clicked.connect(self.on_send)
        input_row.addWidget(self.send_btn)
        rl.addLayout(input_row)
 
        footer = QHBoxLayout()
        reconnect_btn = QPushButton("Reconnect")
        reconnect_btn.setToolTip("Re-scan Chrome for the TradingView tab")
        reconnect_btn.clicked.connect(self.on_reconnect)
        footer.addWidget(reconnect_btn)
 
        snap_btn = QPushButton("Re-snap windows")
        snap_btn.clicked.connect(self._position_windows)
        footer.addWidget(snap_btn)
 
        new_btn = QPushButton("New Session")
        new_btn.clicked.connect(self.on_new_session)
        footer.addWidget(new_btn)
 
        export_btn = QPushButton("Export")
        export_btn.clicked.connect(self.on_export)
        footer.addWidget(export_btn)
 
        footer.addStretch()
        rl.addLayout(footer)
 
        self.setCentralWidget(central)
 
    def _refresh_tv_status(self):
        if self._tv_page:
            url = self._tv_page.url[:60] + ("…" if len(self._tv_page.url) > 60 else "")
            self.tv_status.setText(f"✓ Attached: {url}")
            self.tv_status.setStyleSheet("color:#81c784; font-size:10px;")
        else:
            self.tv_status.setText("✗ No TradingView tab — click Reconnect after opening one")
            self.tv_status.setStyleSheet("color:#ef5350; font-size:10px;")
 
    def on_reconnect(self):
        self._tv_page = self._find_tv_page()
        self._refresh_tv_status()
        if self._tv_page:
            self._position_chrome_window()
            self._append_system(f"Reconnected to: {self._tv_page.url}")
        else:
            self._append_system("Still no TradingView tab found.")
 
    # ---------- Send / receive ----------
    def on_send(self):
        text = self.input_line.text().strip()
        if not text or self.worker is not None:
            return
 
        img = self.capture_chart_png() if self.attach_screenshot_cb.isChecked() else None
        msg = {"role": "user", "text": text, "image_bytes": img,
               "ts": datetime.utcnow().isoformat()}
        self.history.append(msg)
        self._render_user_turn(msg)
        self.input_line.clear()
 
        self._set_busy(True)
        self.worker = GeminiWorker(self.client, GEMINI_MODEL, SYSTEM_PROMPT, self.history)
        self._begin_assistant_turn()
        self.worker.chunk_received.connect(self._on_chunk)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.start()
 
    def _on_chunk(self, chunk): self._append_to_last_assistant(chunk)
 
    def _on_finished(self, full_text):
        self.history.append({"role": "model", "text": full_text, "image_bytes": None,
                             "ts": datetime.utcnow().isoformat()})
        self._save_session()
        self._set_busy(False)
        self.worker = None
 
    def _on_error(self, err):
        self._append_system(f"⚠ Gemini error: {err}")
        self._set_busy(False)
        self.worker = None
 
    def _set_busy(self, busy):
        self.send_btn.setEnabled(not busy)
        self.input_line.setEnabled(not busy)
        self.status_label.setText("Thinking…" if busy else "")
 
    # ---------- Rendering (bubble-based) ----------
    def _add_bubble_row(self, bubble: MessageBubble):
        """Wrap bubble in a row with left/right/center alignment based on role."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        bubble.setMaximumWidth(max(220, int(self.chat_scroll.viewport().width() * 0.88)))
 
        if bubble.role == "user":
            row.addStretch(1)
            row.addWidget(bubble, 0)
        elif bubble.role == "system":
            row.addStretch(1)
            row.addWidget(bubble, 0)
            row.addStretch(1)
        else:  # model
            row.addWidget(bubble, 0)
            row.addStretch(1)
 
        row_w = QWidget()
        row_w.setLayout(row)
        # Insert before the trailing stretch so bubbles stack top-down
        insert_at = self.chat_layout.count() - 1
        self.chat_layout.insertWidget(insert_at, row_w)
        QTimer.singleShot(10, self._scroll_to_end)
 
    def _render_user_turn(self, msg):
        bubble = MessageBubble("user", msg["text"], has_image=bool(msg.get("image_bytes")))
        self._add_bubble_row(bubble)
 
    def _begin_assistant_turn(self):
        bubble = MessageBubble("model", "")
        self._current_assistant_bubble = bubble
        self._add_bubble_row(bubble)
 
    def _append_to_last_assistant(self, text: str):
        if self._current_assistant_bubble is not None:
            self._current_assistant_bubble.append_stream(text)
            QTimer.singleShot(5, self._scroll_to_end)
 
    def _append_system(self, text: str):
        bubble = MessageBubble("system", text)
        self._add_bubble_row(bubble)
 
    def _scroll_to_end(self):
        sb = self.chat_scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
 
    def _clear_chat_ui(self):
        """Remove all bubble rows from the layout (keep trailing stretch)."""
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._current_assistant_bubble = None
 
    # ---------- Session persistence ----------
    def _save_session(self):
        try:
            slim = [{"role": m["role"], "text": m["text"],
                     "had_image": bool(m.get("image_bytes")), "ts": m.get("ts")}
                    for m in self.history]
            SESSION_FILE.write_text(json.dumps(slim, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Save failed")
 
    def _load_session(self):
        if not SESSION_FILE.exists():
            return
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        for m in data:
            self.history.append({"role": m["role"], "text": m["text"],
                                 "image_bytes": None, "ts": m.get("ts")})
            if m["role"] == "user":
                self._render_user_turn({"text": m["text"], "image_bytes": m.get("had_image")})
            else:
                self._begin_assistant_turn()
                self._append_to_last_assistant(m["text"])
        if data:
            self._append_system(f"— Loaded {len(data)} prior messages —")
 
    def on_new_session(self):
        r = QMessageBox.question(self, "New Session", "Clear current chat and memory?",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes: return
        self.history.clear()
        self._clear_chat_ui()
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        self._append_system("— New session started —")
 
    def on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export chat",
            str(Path.home() / f"chat_{datetime.now():%Y%m%d_%H%M%S}.md"),
            "Markdown (*.md);;JSON (*.json)")
        if not path: return
        try:
            if path.endswith(".json"):
                slim = [{"role": m["role"], "text": m["text"],
                         "had_image": bool(m.get("image_bytes")), "ts": m.get("ts")}
                        for m in self.history]
                Path(path).write_text(json.dumps(slim, indent=2), encoding="utf-8")
            else:
                lines = []
                for m in self.history:
                    who = "**You**" if m["role"] == "user" else "**Copilot**"
                    tag = " 📎" if m.get("image_bytes") else ""
                    lines.append(f"{who}{tag} — _{m.get('ts','')}_\n\n{m['text']}\n")
                Path(path).write_text("\n---\n\n".join(lines), encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))
 
    # ---------- Cleanup ----------
    def closeEvent(self, event):
        try:
            if self._browser:
                self._browser.close()  # disconnects CDP; Chrome keeps running
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        super().closeEvent(event)
 
 
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Trading Copilot CDP")
    win = TradingCopilotCDP()
    win.show()
    sys.exit(app.exec())
 
 
if __name__ == "__main__":
    main()
 
