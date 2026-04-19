"""
Trading Copilot — split-screen TradingView + Gemini chat.

Left pane (75%): TradingView in embedded Chromium (QWebEngineView, persistent profile).
Right pane (25%): Gemini chat panel with on-demand chart screenshots and rolling memory.

Setup:
    pip install -r requirements.txt
    set GEMINI_API_KEY=your_key_here     (Windows)
    python trading_copilot.py

First run: log into TradingView in the left pane. Session persists across launches.
"""
import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from io import BytesIO

import mss
from PIL import Image

from PySide6.QtCore import Qt, QThread, Signal, QUrl, QTimer
from PySide6.QtGui import QTextCursor, QPixmap, QImage, QIcon, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QCheckBox, QMessageBox,
    QFileDialog, QFrame
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage

from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================
TRADINGVIEW_URL = "https://www.tradingview.com/chart/"
from config import API_KEY, MODEL
GEMINI_MODEL = MODEL

APP_DIR = Path.home() / ".trading_copilot"
PROFILE_DIR = APP_DIR / "browser_profile"
SESSION_FILE = APP_DIR / "session.json"
LOG_FILE = APP_DIR / "trading_copilot.log"

# How many recent screenshots to keep in the context we send to Gemini.
# Older screenshots are dropped from the prompt (text is always kept).
MAX_IMAGES_IN_CONTEXT = 3

# ============================================================
# SYSTEM PROMPT — paste your 2 indicators' code/logic below
# ============================================================
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
# Logging
# ============================================================
APP_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ============================================================
# Gemini worker (runs on background QThread so UI stays responsive)
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
        self.history = history  # list of {"role": "user"|"model", "text": str, "image_bytes": Optional[bytes]}

    def run(self):
        try:
            # Build Content list, pruning old images to save tokens
            image_msgs = [i for i, m in enumerate(self.history) if m.get("image_bytes")]
            keep_image_indices = set(image_msgs[-MAX_IMAGES_IN_CONTEXT:])

            contents = []
            for i, msg in enumerate(self.history):
                parts = []
                text = msg.get("text", "")
                if text:
                    parts.append(types.Part.from_text(text=text))
                img = msg.get("image_bytes")
                if img and i in keep_image_indices:
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
# Main Window
# ============================================================
class TradingCopilot(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Trading Copilot — TradingView + Gemini")
        self.resize(1800, 1000)

        # History of messages for Gemini + for on-screen display
        #   { "role": "user"|"model", "text": str, "image_bytes": Optional[bytes], "ts": iso }
        self.history: list[dict] = []
        self.worker: GeminiWorker | None = None
        self._streaming_buffer = ""  # accumulates streamed chunks for current assistant turn

        # --- Gemini client ---
        if not API_KEY or API_KEY == "PASTE_YOUR_NEW_KEY_HERE":
            QMessageBox.critical(
                self, "Missing API Key",
                "Open config.py and paste your Gemini API key into API_KEY.\n\n"
                "Get one at https://aistudio.google.com/apikey",
            )
            sys.exit(1)
        self.client = genai.Client(api_key=API_KEY)

        # --- Build UI ---
        self._build_ui()

        # --- Load prior session if it exists ---
        self._load_session()

    # ---------- UI ----------
    def _build_ui(self):
        splitter = QSplitter(Qt.Horizontal)

        # ===== Left: Chat panel =====
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 8, 8, 8)
        rl.setSpacing(6)

        header = QLabel(f"📈 Chart Copilot  ·  {GEMINI_MODEL}")
        header.setFont(QFont("", 11, QFont.Bold))
        rl.addWidget(header)

        # Conversation display
        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        self.chat_view.setStyleSheet(
            "QTextEdit { background:#1e1e1e; color:#e0e0e0; "
            "font-family:'Segoe UI','SF Pro',Arial; font-size:12px; "
            "border:1px solid #333; border-radius:6px; padding:6px; }"
        )
        rl.addWidget(self.chat_view, stretch=1)

        # Screenshot toggle
        opts_row = QHBoxLayout()
        self.attach_screenshot_cb = QCheckBox("Attach chart screenshot")
        self.attach_screenshot_cb.setChecked(True)
        opts_row.addWidget(self.attach_screenshot_cb)
        opts_row.addStretch()
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#888; font-size:11px;")
        opts_row.addWidget(self.status_label)
        rl.addLayout(opts_row)

        # Input
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

        # Footer buttons
        footer = QHBoxLayout()
        clear_btn = QPushButton("New Session")
        clear_btn.clicked.connect(self.on_new_session)
        footer.addWidget(clear_btn)

        save_btn = QPushButton("Export Chat")
        save_btn.clicked.connect(self.on_export)
        footer.addWidget(save_btn)

        footer.addStretch()
        rl.addLayout(footer)

        splitter.addWidget(right)

        # ===== Right: TradingView =====
        self.profile = QWebEngineProfile("trading_copilot_profile", self)
        self.profile.setPersistentStoragePath(str(PROFILE_DIR))
        self.profile.setCachePath(str(PROFILE_DIR / "cache"))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        # Spoof a real Chrome UA so Google OAuth doesn't block the embedded browser.
        self.profile.setHttpUserAgent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

        self.webview = QWebEngineView()
        page = QWebEnginePage(self.profile, self.webview)
        self.webview.setPage(page)
        self.webview.setUrl(QUrl(TRADINGVIEW_URL))
        splitter.addWidget(self.webview)

        # 25/75 split — chat left, chart right
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([450, 1350])
        self.setCentralWidget(splitter)

    # ---------- Screenshot ----------
    def capture_chart_png(self) -> bytes | None:
        """Grab the exact screen region of the TradingView pane as PNG bytes."""
        try:
            top_left = self.webview.mapToGlobal(self.webview.rect().topLeft())
            w, h = self.webview.width(), self.webview.height()
            if w <= 0 or h <= 0:
                return None
            with mss.mss() as sct:
                monitor = {"top": top_left.y(), "left": top_left.x(), "width": w, "height": h}
                shot = sct.grab(monitor)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                buf = BytesIO()
                img.save(buf, format="PNG", optimize=True)
                return buf.getvalue()
        except Exception as e:
            logger.exception("Screenshot failed")
            self._append_system(f"[screenshot failed: {e}]")
            return None

    # ---------- Send / receive ----------
    def on_send(self):
        text = self.input_line.text().strip()
        if not text or self.worker is not None:
            return

        img_bytes = self.capture_chart_png() if self.attach_screenshot_cb.isChecked() else None

        msg = {
            "role": "user",
            "text": text,
            "image_bytes": img_bytes,
            "ts": datetime.utcnow().isoformat(),
        }
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

    def _on_chunk(self, chunk: str):
        self._streaming_buffer += chunk
        self._append_to_last_assistant(chunk)

    def _on_finished(self, full_text: str):
        self.history.append({
            "role": "model",
            "text": full_text,
            "image_bytes": None,
            "ts": datetime.utcnow().isoformat(),
        })
        self._save_session()
        self._set_busy(False)
        self.worker = None
        self._streaming_buffer = ""

    def _on_error(self, err: str):
        self._append_system(f"⚠ Gemini error: {err}")
        self._set_busy(False)
        self.worker = None
        self._streaming_buffer = ""

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.input_line.setEnabled(not busy)
        self.status_label.setText("Thinking…" if busy else "")

    # ---------- Rendering ----------
    def _render_user_turn(self, msg):
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        attached = "  📎" if msg.get("image_bytes") else ""
        html = (
            f'<div style="margin-top:10px;"><b style="color:#4fc3f7;">You{attached}</b><br>'
            f'{self._escape_html(msg["text"])}</div>'
        )
        cursor.insertHtml(html)
        self._scroll_to_end()

    def _begin_assistant_turn(self):
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(
            '<div style="margin-top:10px;"><b style="color:#81c784;">Copilot</b><br></div>'
        )
        self._scroll_to_end()

    def _append_to_last_assistant(self, text: str):
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self._scroll_to_end()

    def _append_system(self, text: str):
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(f'<div style="color:#ffb74d; margin-top:8px;">{self._escape_html(text)}</div>')
        self._scroll_to_end()

    def _scroll_to_end(self):
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _escape_html(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace("\n", "<br>"))

    # ---------- Session persistence ----------
    def _save_session(self):
        try:
            # Don't persist full images to disk — just a flag. Keeps session.json small.
            slim = [
                {"role": m["role"], "text": m["text"],
                 "had_image": bool(m.get("image_bytes")), "ts": m.get("ts")}
                for m in self.history
            ]
            SESSION_FILE.write_text(json.dumps(slim, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Failed to save session")

    def _load_session(self):
        if not SESSION_FILE.exists():
            return
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to load session")
            return
        for m in data:
            self.history.append({
                "role": m["role"], "text": m["text"],
                "image_bytes": None, "ts": m.get("ts"),
            })
            if m["role"] == "user":
                self._render_user_turn({"text": m["text"], "image_bytes": m.get("had_image")})
            else:
                self._begin_assistant_turn()
                self._append_to_last_assistant(m["text"])
        if data:
            self._append_system(f"— Loaded {len(data)} prior messages —")

    def on_new_session(self):
        reply = QMessageBox.question(
            self, "New Session", "Clear the current chat and memory?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.history.clear()
        self.chat_view.clear()
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
        self._append_system("— New session started —")

    def on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export chat",
            str(Path.home() / f"chat_{datetime.now():%Y%m%d_%H%M%S}.md"),
            "Markdown (*.md);;JSON (*.json)",
        )
        if not path:
            return
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


def main():
    # High-DPI awareness on Windows
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("Trading Copilot")
    win = TradingCopilot()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
