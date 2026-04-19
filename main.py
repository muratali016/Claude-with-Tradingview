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
//@version=5
indicator("MAIN 8/21/200 EMA + VWAP + CFO Dashboard — v2.9", shorttitle="INTRA FUTURES V2", overlay=true, max_labels_count=500)

// ==========================================
// ===== INPUTS
// ==========================================
// --- MA & VWAP Settings ---
fastLen      = input.int(13,  "Fast EMA")
slowLen      = input.int(48, "Slow EMA")
showEma3     = input.bool(true, "Show 3rd EMA")
ema3Len      = input.int(200, "3rd EMA Length")
src          = input.source(close, "Source")
showLabels   = input.bool(true, "Show Signal Labels")
colorBars    = input.bool(true, "Color Bars by Trend")

// --- Extended Hours & Key Levels Settings ---
showExt      = input.bool(true, "Show PM/AH/PD Levels", group="Key Levels")
showORB      = input.bool(true, "Show ORB Lines", group="Key Levels")
orbMinutes   = input.int(15, "ORB Duration (mins)", options=[5, 15, 30, 60], group="Key Levels")
showTests    = input.bool(true, "Show Test 'Cloud' Dots", group="Key Levels", tooltip="Plots a soft dot when price tests a level")
testTolPct   = input.float(0.05, "Test Tolerance (%)", step=0.01, group="Key Levels")
minTouchRvol = input.float(1.2, "Min RVOL for Touch Dot", step=0.1, group="Key Levels", tooltip="Filters out weak tests. 1.0 = Avg Vol, 1.2 = 20% above avg. Only prints dot if RVOL is >= this value.")

// --- Futures & Global Sessions ---
showGlobalSess = input.bool(false, "Show Asia/London Backgrounds", group="Futures & Global Sessions")
asiaSess       = input.session("2000-0300", "Asia Session (EST)", group="Futures & Global Sessions")
londonSess     = input.session("0300-0800", "London Session (EST)", group="Futures & Global Sessions")
asiaColor      = input.color(color.new(color.teal, 90), "Asia Color", group="Futures & Global Sessions")
londonColor    = input.color(color.new(color.orange, 90), "London Color", group="Futures & Global Sessions")

showFutPM      = input.bool(false, "Show Futures PM (Overnight) Levels", group="Futures & Global Sessions")
futPMSess      = input.session("1800-0930", "Futures PM Session (EST)", group="Futures & Global Sessions")

// --- Dashboard Settings ---
showDash       = input.bool(true, "Show CFO Dashboard", group="Dashboard")
showAdxBg      = input.bool(false, "Color Background by ADX", group="Dashboard", tooltip="Colors background Teal if ADX > 25 (Trend) and Gray if ADX < 20 (Chop)")
textSize       = input.string(size.small, "Text Size", options=[size.tiny, size.small, size.normal], group="Dashboard")
vixTf          = input.string("D", "VIX Timeframe", options=["D","60","240","W"], group="Dashboard")

// Session minute boundaries (exchange time)
sessStartMin   = input.int(9*60+30, "Session Start (min from 00:00)", minval=0, maxval=24*60-1, group="Dashboard")
sessEndMin     = input.int(16*60,   "Session End (min from 00:00)",   minval=1, maxval=24*60,   group="Dashboard")
projMethod     = input.string("Average remainder", "Volume Projection", options=["Average remainder","Pace (today)"], group="Dashboard")
zLen           = input.int(20, "VWAP Z-Score length", minval=5, group="Dashboard")

// --- Confirmations ---
confirmBars     = input.int(2, "Confirm bars after cross", minval=1, maxval=5, group="Filters")
confirmVolMult  = input.float(1.0, "Volume × AvgVol for confirmation", group="Filters")

// --- Continuation Signals (NEW) ---
showCont         = input.bool(true,  "Show Continuation Signals",        group="Continuation Signals")
volMult          = input.float(1.0,  "Vol20 Threshold (× median)",       minval=0.1, maxval=3.0, step=0.1, group="Continuation Signals")
spreadMult       = input.float(1.0,  "Spread Threshold (× median)",      minval=0.1, maxval=3.0, step=0.1, group="Continuation Signals")
pullbackLookback = input.int(5,      "Pullback Reclaim Lookback (bars)", minval=2,   maxval=20,  group="Continuation Signals")
expandLookback   = input.int(3,      "Spread Expansion Lookback (bars)", minval=1,   maxval=10,  group="Continuation Signals")
trendBarsThresh  = input.int(15,     "Bars since last cross = TREND",    minval=5,   maxval=100, group="Continuation Signals", tooltip="If more than this many bars have passed since the last EMA cross, indicator switches from reversal mode to continuation mode")

// ==========================================
// ===== CORE CALCULATIONS
// ==========================================
emaFast = ta.ema(src, fastLen)
emaSlow = ta.ema(src, slowLen)
ema3    = ta.ema(src, ema3Len)
vwap    = ta.vwap(close)
testTol = testTolPct / 100.0

// --- Continuation feature calcs ---
ret            = math.log(close / close[1])
vol_20         = ta.stdev(ret, 20) * 100
ema_spread     = emaFast - emaSlow
ema_spread_abs = math.abs(ema_spread)

vol20_median  = ta.percentile_linear_interpolation(vol_20,         50, 50)
spread_median = ta.percentile_linear_interpolation(ema_spread_abs, 50, 50)

high_vol20  = vol_20         >= vol20_median  * volMult
wide_spread = ema_spread_abs >= spread_median * spreadMult

// --- Helper Calcs for Dashboard (ADX, RVOL, Z-Score) ---
[_, _, adxVal] = ta.dmi(14, 14)

vol    = volume
volAvg = ta.sma(vol, 20)
rvol   = volAvg == 0 ? na : vol / volAvg

dev           = close - vwap
stDevVal      = ta.stdev(dev, zLen)
currentZScore = stDevVal == 0 ? na : dev / stDevVal

// ==========================================
// ===== ORIGINAL CROSS SIGNALS
// ==========================================
buy  = ta.crossover(emaFast, emaSlow)
sell = ta.crossunder(emaFast, emaSlow)

greenVolOK = true
redVolOK   = true
for i = 0 to confirmBars - 1
    greenBar = close[i] > open[i]
    redBar   = close[i] < open[i]
    volOK    = volAvg[i] == 0 ? false : volume[i] > volAvg[i] * confirmVolMult
    greenVolOK := greenVolOK and greenBar and volOK
    redVolOK   := redVolOK   and redBar   and volOK

confirmedBuy  = buy[confirmBars]  and greenVolOK
confirmedSell = sell[confirmBars] and redVolOK

// ==========================================
// ===== PM, AH, PD, ORB & FUTURES LOGIC
// ==========================================
var float pmHigh = na
var float pmLow  = na
var float ahHigh = na
var float ahLow  = na
var float orbHigh = na
var float orbLow  = na
var float sessionStartMs = na

// Detect regular session starts
isPreStart  = session.ispremarket and not session.ispremarket[1]
isPostStart = session.ispostmarket and not session.ispostmarket[1]
isRegStart  = session.ismarket and not session.ismarket[1]

// PM Logic
if isPreStart
    pmHigh := high
    pmLow  := low
else if session.ispremarket
    pmHigh := math.max(pmHigh, high)
    pmLow  := math.min(pmLow, low)

// AH Logic
if isPostStart
    ahHigh := high
    ahLow  := low
else if session.ispostmarket
    ahHigh := math.max(ahHigh, high)
    ahLow  := math.min(ahLow, low)

// --- ORB LOGIC (Explicit 9:30 AM EST Cash Open) ---
inCashSess = not na(time(timeframe.period, "0930-1600", "America/New_York"))
isCashStart = inCashSess and not inCashSess[1]

if isCashStart
    sessionStartMs := time
    orbHigh := high
    orbLow  := low

// Check if current bar is within the ORB window
inORB = inCashSess and not na(sessionStartMs) and (time - sessionStartMs) < (orbMinutes * 60 * 1000)

if inORB and not isCashStart
    orbHigh := math.max(orbHigh, high)
    orbLow  := math.min(orbLow, low)

// Fetch Previous Day High and Low securely
pdHigh = request.security(syminfo.tickerid, "D", high[1], lookahead=barmerge.lookahead_on)
pdLow  = request.security(syminfo.tickerid, "D", low[1],  lookahead=barmerge.lookahead_on)

// --- FUTURES PM (OVERNIGHT) LOGIC ---
inFutPM      = not na(time(timeframe.period, futPMSess, "America/New_York"))
isFutPMStart = inFutPM and not inFutPM[1]

var float futPmHigh = na
var float futPmLow  = na

if isFutPMStart
    futPmHigh := high
    futPmLow  := low
else if inFutPM
    futPmHigh := math.max(futPmHigh, high)
    futPmLow  := math.min(futPmLow, low)

// ==========================================
// ===== CONTINUATION SIGNALS (NEW)
// ==========================================
ema_stacked_bull = emaFast > emaSlow
ema_stacked_bear = emaFast < emaSlow

// Regime: are we past the "fresh cross" window?
barsSinceBuy    = nz(ta.barssince(buy),  9999)
barsSinceSell   = nz(ta.barssince(sell), 9999)
barsSinceCross  = math.min(barsSinceBuy, barsSinceSell)
in_trend_mode   = barsSinceCross > trendBarsThresh

// 1) Pullback-and-reclaim
touched_fast_bull = ta.barssince(low  <= emaFast) <= pullbackLookback
touched_fast_bear = ta.barssince(high >= emaFast) <= pullbackLookback

reclaim_bull = ema_stacked_bull and in_trend_mode and touched_fast_bull and close > emaFast and close[1] <= emaFast and high_vol20 and wide_spread
reclaim_bear = ema_stacked_bear and in_trend_mode and touched_fast_bear and close < emaFast and close[1] >= emaFast and high_vol20 and wide_spread

// 2) Spread expansion (ignition bar)
spread_exp_bull_state = ema_spread > 0 and ema_spread > ema_spread[expandLookback] and wide_spread and high_vol20
spread_exp_bear_state = ema_spread < 0 and ema_spread < ema_spread[expandLookback] and wide_spread and high_vol20
exp_bull = in_trend_mode and spread_exp_bull_state and not spread_exp_bull_state[1]
exp_bear = in_trend_mode and spread_exp_bear_state and not spread_exp_bear_state[1]

// 3) ORB break (uses existing orbHigh / orbLow)
orb_break_bull = not na(orbHigh) and close > orbHigh and close[1] <= orbHigh and high_vol20 and ema_stacked_bull
orb_break_bear = not na(orbLow)  and close < orbLow  and close[1] >= orbLow  and high_vol20 and ema_stacked_bear

// ==========================================
// ===== GLOBAL SESSIONS BACKGROUNDS
// ==========================================
inAsia   = not na(time(timeframe.period, asiaSess, "America/New_York"))
inLondon = not na(time(timeframe.period, londonSess, "America/New_York"))

bgcolor(showGlobalSess and inAsia ? asiaColor : na, title="Asia Session Background")
bgcolor(showGlobalSess and inLondon ? londonColor : na, title="London Session Background")

// ==========================================
// ===== TEST DOT LOGIC ("CLOUD DOTS") WITH RVOL FILTER
// ==========================================
isRegSess = session.ismarket
afterORB  = isRegSess and not inORB

isTestReg(lvl) => isRegSess and not na(lvl) and (high >= lvl * (1 - testTol)) and (low <= lvl * (1 + testTol)) and (rvol >= minTouchRvol)
isTestORB(lvl) => afterORB  and not na(lvl) and (high >= lvl * (1 - testTol)) and (low <= lvl * (1 + testTol)) and (rvol >= minTouchRvol)

// ==========================================
// ===== PLOTS & VISUALS
// ==========================================
// EMAs and Cloud
p_emaFast = plot(emaFast, "EMA Fast",  color=color.new(color.yellow, 0), linewidth=2)
p_emaSlow = plot(emaSlow, "EMA Slow", color=color.new(color.purple, 0), linewidth=2)
fill(p_emaFast, p_emaSlow, color=emaFast > emaSlow ? color.new(color.lime, 85) : color.new(color.red, 85), title="EMA Cloud")

// 3rd EMA
plot(showEma3 ? ema3 : na, "EMA 3", color=color.new(color.white, 0), linewidth=2)

// VWAP
plot(vwap,    "VWAP",   color=color.new(color.red, 0),    linewidth=2)

// PM, AH, and PD Lines (Stocks)
plot(showExt and not na(pmHigh) ? pmHigh : na, "PM High", color=color.new(color.blue, 60), style=plot.style_linebr, linewidth=1)
plot(showExt and not na(pmLow)  ? pmLow  : na, "PM Low",  color=color.new(color.blue, 60), style=plot.style_linebr, linewidth=1)
plot(showExt and not na(ahHigh) ? ahHigh : na, "AH High", color=color.new(color.purple, 60), style=plot.style_linebr, linewidth=1)
plot(showExt and not na(ahLow)  ? ahLow  : na, "AH Low",  color=color.new(color.purple, 60), style=plot.style_linebr, linewidth=1)
plot(showExt and not na(pdHigh) ? pdHigh : na, "PD High", color=color.new(color.orange, 50), style=plot.style_linebr, linewidth=1)
plot(showExt and not na(pdLow)  ? pdLow  : na, "PD Low",  color=color.new(color.orange, 50), style=plot.style_linebr, linewidth=1)

// Futures PM Lines
plot(showFutPM and not na(futPmHigh) ? futPmHigh : na, "Fut PM High", color=color.new(color.aqua, 50), style=plot.style_linebr, linewidth=1)
plot(showFutPM and not na(futPmLow)  ? futPmLow  : na, "Fut PM Low",  color=color.new(color.aqua, 50), style=plot.style_linebr, linewidth=1)

// ORB Lines
plot(showORB and not na(orbHigh) ? orbHigh : na, "ORB High", color=color.new(color.green, 50), style=plot.style_linebr, linewidth=1)
plot(showORB and not na(orbLow)  ? orbLow  : na, "ORB Low",  color=color.new(color.red, 50),   style=plot.style_linebr, linewidth=1)

// High-Volume Test Cloud Dots
plot(showTests and isTestReg(pmHigh) ? pmHigh : na, "Test PM High", color=color.new(color.blue, 20),   style=plot.style_circles, linewidth=4)
plot(showTests and isTestReg(pmLow)  ? pmLow  : na, "Test PM Low",  color=color.new(color.blue, 20),   style=plot.style_circles, linewidth=4)
plot(showTests and isTestReg(ahHigh) ? ahHigh : na, "Test AH High", color=color.new(color.purple, 20), style=plot.style_circles, linewidth=4)
plot(showTests and isTestReg(ahLow)  ? ahLow  : na, "Test AH Low",  color=color.new(color.purple, 20), style=plot.style_circles, linewidth=4)
plot(showTests and isTestReg(pdHigh) ? pdHigh : na, "Test PD High", color=color.new(color.orange, 20), style=plot.style_circles, linewidth=4)
plot(showTests and isTestReg(pdLow)  ? pdLow  : na, "Test PD Low",  color=color.new(color.orange, 20), style=plot.style_circles, linewidth=4)
plot(showTests and isTestORB(orbHigh)? orbHigh: na, "Test ORB High",color=color.new(color.green, 20),  style=plot.style_circles, linewidth=4)
plot(showTests and isTestORB(orbLow) ? orbLow : na, "Test ORB Low", color=color.new(color.red, 20),    style=plot.style_circles, linewidth=4)

// Futures PM Test Cloud Dots
plot(showTests and showFutPM and isTestReg(futPmHigh) ? futPmHigh : na, "Test Fut PM High", color=color.new(color.aqua, 20), style=plot.style_circles, linewidth=4)
plot(showTests and showFutPM and isTestReg(futPmLow)  ? futPmLow  : na, "Test Fut PM Low",  color=color.new(color.aqua, 20), style=plot.style_circles, linewidth=4)

// ==========================================
// ===== TEXT LABELS
// ==========================================
var label pmHighLbl  = label.new(na, na, "PM High", color=color.new(color.white, 100), textcolor=color.blue, style=label.style_label_left, size=size.small)
var label pmLowLbl   = label.new(na, na, "PM Low", color=color.new(color.white, 100), textcolor=color.blue, style=label.style_label_left, size=size.small)
var label ahHighLbl  = label.new(na, na, "AH High", color=color.new(color.white, 100), textcolor=color.purple, style=label.style_label_left, size=size.small)
var label ahLowLbl   = label.new(na, na, "AH Low", color=color.new(color.white, 100), textcolor=color.purple, style=label.style_label_left, size=size.small)
var label pdHighLbl  = label.new(na, na, "PD High", color=color.new(color.white, 100), textcolor=color.orange, style=label.style_label_left, size=size.small)
var label pdLowLbl   = label.new(na, na, "PD Low", color=color.new(color.white, 100), textcolor=color.orange, style=label.style_label_left, size=size.small)
var label orbHighLbl = label.new(na, na, "", color=color.new(color.white, 100), textcolor=color.green, style=label.style_label_left, size=size.small)
var label orbLowLbl  = label.new(na, na, "", color=color.new(color.white, 100), textcolor=color.red, style=label.style_label_left, size=size.small)
var label futPmHighLbl= label.new(na, na, "Fut PM High", color=color.new(color.white, 100), textcolor=color.aqua, style=label.style_label_left, size=size.small)
var label futPmLowLbl = label.new(na, na, "Fut PM Low", color=color.new(color.white, 100), textcolor=color.aqua, style=label.style_label_left, size=size.small)

if barstate.islast
    if showExt and not na(pmHigh)
        label.set_xy(pmHighLbl, bar_index + 2, pmHigh)
    if showExt and not na(pmLow)
        label.set_xy(pmLowLbl, bar_index + 2, pmLow)
    if showExt and not na(ahHigh)
        label.set_xy(ahHighLbl, bar_index + 2, ahHigh)
    if showExt and not na(ahLow)
        label.set_xy(ahLowLbl, bar_index + 2, ahLow)
    if showExt and not na(pdHigh)
        label.set_xy(pdHighLbl, bar_index + 2, pdHigh)
    if showExt and not na(pdLow)
        label.set_xy(pdLowLbl, bar_index + 2, pdLow)

    // ORB Labels
    if showORB and not na(orbHigh)
        label.set_xy(orbHighLbl, bar_index + 2, orbHigh)
        label.set_text(orbHighLbl, str.tostring(orbMinutes) + "m ORB High")
    if showORB and not na(orbLow)
        label.set_xy(orbLowLbl, bar_index + 2, orbLow)
        label.set_text(orbLowLbl, str.tostring(orbMinutes) + "m ORB Low")

    // Futures PM Labels
    if showFutPM and not na(futPmHigh)
        label.set_xy(futPmHighLbl, bar_index + 2, futPmHigh)
    if showFutPM and not na(futPmLow)
        label.set_xy(futPmLowLbl, bar_index + 2, futPmLow)

// Signal Shapes — Cross
plotshape(showLabels and buy,  title="Buy",  style=shape.triangleup,   location=location.belowbar, color=color.new(color.lime, 0), size=size.tiny, text="Buy")
plotshape(showLabels and sell, title="Sell", style=shape.triangledown, location=location.abovebar, color=color.new(color.red, 0),  size=size.tiny, text="Sell")

plotshape(showLabels and confirmedBuy,  title="Confirmed Buy",  style=shape.triangleup,   location=location.belowbar, color=color.new(color.green, 0), size=size.small, text="✅ Buy (2)")
plotshape(showLabels and confirmedSell, title="Confirmed Sell", style=shape.triangledown, location=location.abovebar, color=color.new(color.maroon, 0), size=size.small, text="✅ Sell (2)")

// Signal Shapes — Continuation (NEW)
plotshape(showCont and reclaim_bull, title="Bull Pullback Reclaim", location=location.belowbar, style=shape.circle, size=size.small, color=color.new(#00FF88, 0), text="PB▲", textcolor=color.new(#00FF88, 0))
plotshape(showCont and reclaim_bear, title="Bear Pullback Reclaim", location=location.abovebar, style=shape.circle, size=size.small, color=color.new(#FF4444, 0), text="PB▼", textcolor=color.new(#FF4444, 0))

plotshape(showCont and exp_bull, title="Bull Spread Expansion", location=location.belowbar, style=shape.diamond, size=size.small, color=color.new(#00FF88, 0), text="EX▲", textcolor=color.new(#00FF88, 0))
plotshape(showCont and exp_bear, title="Bear Spread Expansion", location=location.abovebar, style=shape.diamond, size=size.small, color=color.new(#FF4444, 0), text="EX▼", textcolor=color.new(#FF4444, 0))

plotshape(showCont and orb_break_bull, title="Bull ORB Break", location=location.belowbar, style=shape.flag, size=size.small, color=color.new(#00BFFF, 0), text="ORB▲", textcolor=color.new(#00BFFF, 0))
plotshape(showCont and orb_break_bear, title="Bear ORB Break", location=location.abovebar, style=shape.flag, size=size.small, color=color.new(#FF4444, 0), text="ORB▼", textcolor=color.new(#FF4444, 0))

// Bar Coloring
barcolor(colorBars ? (emaFast > emaSlow ? color.new(color.lime, 70) : color.new(color.red, 70)) : na)

// Background Coloring (ADX Filter)
adxBg = showAdxBg ? (adxVal > 25 ? color.new(color.teal, 85) : (adxVal < 20 ? color.new(color.gray, 85) : na)) : na
bgcolor(adxBg, title="ADX Background Filter")

// Alerts — Cross
alertcondition(buy,             title="EMA Bullish Cross",      message="Fast>Slow cross — {{ticker}} @ {{close}} ({{interval}})")
alertcondition(sell,            title="EMA Bearish Cross",      message="Fast<Slow cross — {{ticker}} @ {{close}} ({{interval}})")
alertcondition(confirmedBuy,    title="Confirmed Buy (2-bar)",  message="2-bar high-volume green confirm — {{ticker}}")
alertcondition(confirmedSell,   title="Confirmed Sell (2-bar)", message="2-bar high-volume red confirm — {{ticker}}")

// Alerts — Continuation (NEW)
alertcondition(reclaim_bull,    title="Pullback Reclaim — Bull", message="{{ticker}} Bull pullback-reclaim @ {{close}}")
alertcondition(reclaim_bear,    title="Pullback Reclaim — Bear", message="{{ticker}} Bear pullback-reclaim @ {{close}}")
alertcondition(exp_bull,        title="Spread Expansion — Bull", message="{{ticker}} Bull spread-expansion @ {{close}}")
alertcondition(exp_bear,        title="Spread Expansion — Bear", message="{{ticker}} Bear spread-expansion @ {{close}}")
alertcondition(orb_break_bull,  title="ORB Break — Bull",        message="{{ticker}} Bull ORB break @ {{close}}")
alertcondition(orb_break_bear,  title="ORB Break — Bear",        message="{{ticker}} Bear ORB break @ {{close}}")

// ==========================================
// ===== CFO DASHBOARD LOGIC
// ==========================================

vixVal = request.security("CBOE:VIX", vixTf, close, lookahead=barmerge.lookahead_off)
dHigh  = request.security(syminfo.tickerid, "D", high)
dLow   = request.security(syminfo.tickerid, "D", low)
adrVal = request.security(syminfo.tickerid, "D", ta.sma(high - low, 10))
dayRangeCurrent = dHigh - dLow
expectedMovePct = vixVal / 16.0

dVolCur = request.security(syminfo.tickerid, "D", volume)
dVol5   = request.security(syminfo.tickerid, "D", ta.sma(volume, 5))
dVol15  = request.security(syminfo.tickerid, "D", ta.sma(volume, 15))
dVol30  = request.security(syminfo.tickerid, "D", ta.sma(volume, 30))

isIntra   = timeframe.isintraday
curMin    = hour * 60 + minute
totalMin  = math.max(1, sessEndMin - sessStartMin)
inSess    = curMin >= sessStartMin and curMin <= sessEndMin
elapsedPct = isIntra and inSess ? (curMin - sessStartMin) / totalMin : na
remainPct  = isIntra and inSess ? (sessEndMin - curMin) / totalMin   : na

projAvg  = na(elapsedPct) ? na : dVolCur + (dVol30 * nz(remainPct, 0))
projPace = na(elapsedPct) or elapsedPct <= 0 ? na : dVolCur / elapsedPct
dVolProjected = projMethod == "Pace (today)" ? projPace : projAvg

var table dashTable = table.new(position.bottom_right, 2, 15, border_width=1, frame_color=color.gray, border_color=color.gray)

if showDash and barstate.islast
    table.cell(dashTable, 0, 0, "Metric",   bgcolor=color.gray, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 0, "Value",    bgcolor=color.gray, text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 1, "VIX (" + vixTf + ")", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 1, na(vixVal) ? "-" : str.tostring(vixVal, "#.##"), bgcolor=na(vixVal) ? color.black : (vixVal > 20 ? color.maroon : color.black), text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 2, "ADR (10)", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 2, str.tostring(adrVal, "#.##"), bgcolor=dayRangeCurrent > adrVal ? color.orange : color.black, text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 3, "Exp Move", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 3, na(expectedMovePct) ? "-" : str.tostring(expectedMovePct, "#.##") + "%", bgcolor=color.black, text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 4, "ADX (14)", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 4, str.tostring(adxVal, "#.##"), bgcolor=adxVal < 20 ? color.gray : (adxVal > 25 ? color.green : color.black), text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 5, "RVOL",     bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 5, na(rvol) ? "-" : str.tostring(rvol, "#.##") + "x", bgcolor=na(rvol) ? color.black : (rvol > 1.0 ? color.green : color.gray), text_color=color.white, text_size=textSize)

    zScoreColor = na(currentZScore) ? color.black : (math.abs(currentZScore) > 2.0 ? color.orange : color.black)
    table.cell(dashTable, 0, 6, "VWAP Dev", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 6, na(currentZScore) ? "-" : (currentZScore > 0 ? "+" : "") + str.tostring(currentZScore, "#.##") + "σ", bgcolor=zScoreColor, text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 7, "Day Vol (Cur)",  bgcolor=color.new(color.blue, 30), text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 7, str.tostring(dVolCur, format.volume), bgcolor=dVolCur > dVol30 ? color.green : color.gray, text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 8, "Projected (" + projMethod + ")", bgcolor=color.new(color.blue, 30), text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 8, na(dVolProjected) ? "-" : str.tostring(dVolProjected, format.volume), bgcolor=na(dVolProjected) ? color.black : (dVolProjected > dVol30 ? color.green : color.gray), text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 9, "Vol Avg (5)", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 9, str.tostring(dVol5, format.volume), bgcolor=color.black, text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 10, "Vol Avg (15)", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 10, str.tostring(dVol15, format.volume), bgcolor=color.black, text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 11, "Vol Avg (30)", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 11, str.tostring(dVol30, format.volume), bgcolor=color.black, text_color=color.white, text_size=textSize)

    table.cell(dashTable, 0, 12, "In Session?", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 12, timeframe.isintraday ? (inSess ? "Yes" : "No") : "N/A", bgcolor=inSess ? color.new(color.green, 60) : color.new(color.gray, 60), text_color=color.white, text_size=textSize)

    // Mode (Reversal vs Trend) — NEW
    modeTxt   = in_trend_mode ? "TREND" : "REVERSAL"
    modeColor = in_trend_mode ? color.new(color.teal, 30) : color.new(color.orange, 30)
    table.cell(dashTable, 0, 13, "Mode", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 13, modeTxt, bgcolor=modeColor, text_color=color.white, text_size=textSize)

    // Vol20 status — NEW
    table.cell(dashTable, 0, 14, "Vol 20", bgcolor=color.black, text_color=color.white, text_size=textSize)
    table.cell(dashTable, 1, 14, str.tostring(vol_20, "#.###") + (high_vol20 ? "  ✓" : "  ✗"), bgcolor=high_vol20 ? color.new(color.green, 40) : color.new(color.gray, 40), text_color=color.white, text_size=textSize)


--- INDICATOR 2 ---
//@version=5
indicator("EMA Cross ML Features — Vol & Spread", shorttitle="ML Features", overlay=false, max_bars_back=500)

// ══════════════════════════════════════════════════════════════
//  EMA Cross ML Features Indicator
//  Tracks the top 3 predictive features from the ML model:
//    1. vol_20   — 20-bar return volatility  (most important: 0.0822)
//    2. ema_spread — EMA8 minus EMA21       (2nd:             0.0717)
//    3. vol_10   — 10-bar return volatility  (3rd:             0.0698)
//
//  GREEN background on a crossover bar = all 3 features aligned
//  RED background                      = cross but weak setup
// ══════════════════════════════════════════════════════════════

// ── INPUTS ────────────────────────────────────────────────────
ema_fast      = input.int(8,   "EMA Fast",            group="EMA Settings")
ema_slow      = input.int(21,  "EMA Slow",            group="EMA Settings")

vol_mult      = input.float(1.0, "Vol Threshold (×  median)", minval=0.1, maxval=3.0, step=0.1,
   group="Signal Thresholds",
   tooltip="Cross only counts as HIGH-VOL if current vol > this multiple of its 50-bar median")
   
spread_mult   = input.float(1.0, "Spread Threshold (× median)", minval=0.1, maxval=3.0, step=0.1,
   group="Signal Thresholds",
   tooltip="Cross only counts as WIDE-SPREAD if current spread > this multiple of its 50-bar median")

show_vol20    = input.bool(true,  "Show Vol 20",      group="Display")
show_vol10    = input.bool(true,  "Show Vol 10",      group="Display")
show_spread   = input.bool(true,  "Show EMA Spread",  group="Display")
show_crosses  = input.bool(true,  "Show Cross Signals on Price Chart", group="Display")

// ── CALCULATIONS ──────────────────────────────────────────────
ema8  = ta.ema(close, ema_fast)
ema21 = ta.ema(close, ema_slow)

// 1. VOL_20 — rolling std of 20-bar log returns (matches Python feature)
ret        = math.log(close / close[1])
vol_20     = ta.stdev(ret, 20) * 100   // ×100 for readability
vol_10     = ta.stdev(ret, 10) * 100

// 2. EMA_SPREAD — raw point spread
ema_spread      = ema8 - ema21
ema_spread_abs  = math.abs(ema_spread)

// Medians via percentile approximation (50th = median)
vol20_median   = ta.percentile_linear_interpolation(vol_20, 50, 50)
vol10_median   = ta.percentile_linear_interpolation(vol_10, 50, 50)
spread_median  = ta.percentile_linear_interpolation(ema_spread_abs, 50, 50)

// ── THRESHOLD FLAGS ───────────────────────────────────────────
high_vol20    = vol_20         >= vol20_median  * vol_mult
high_vol10    = vol_10         >= vol10_median  * vol_mult
wide_spread   = ema_spread_abs >= spread_median * spread_mult

all_aligned   = high_vol20 and high_vol10 and wide_spread
two_aligned   = (high_vol20 and high_vol10) or (high_vol20 and wide_spread) or (high_vol10 and wide_spread)

// ── CROSSOVER DETECTION ───────────────────────────────────────
bull_cross = ta.crossover(ema8,  ema21)
bear_cross = ta.crossunder(ema8, ema21)
any_cross  = bull_cross or bear_cross

// ── SCORE (0–3) ───────────────────────────────────────────────
score = (high_vol20 ? 1 : 0) + (high_vol10 ? 1 : 0) + (wide_spread ? 1 : 0)

// ══════════════════════════════════════════════════════════════
//  PANEL 1 — Volatility (vol_20 + vol_10)
// ══════════════════════════════════════════════════════════════
vol20_color = high_vol20 ? color.new(#00FF88, 10) : color.new(#00FF88, 70)
vol10_color = high_vol10 ? color.new(#FFD700, 10) : color.new(#FFD700, 70)

plot(show_vol20 ? vol_20 : na, "Vol 20", color=vol20_color, linewidth=2)
plot(show_vol10 ? vol_10 : na, "Vol 10", color=vol10_color, linewidth=1)

// Median reference lines
plot(show_vol20 ? vol20_median * vol_mult : na, "Vol20 Threshold",
   color=color.new(#00FF88, 60), linewidth=1, style=plot.style_linebr)
plot(show_vol10 ? vol10_median * vol_mult : na, "Vol10 Threshold",
   color=color.new(#FFD700, 60), linewidth=1, style=plot.style_linebr)

// Fill above threshold
vol20_thresh_line = vol20_median * vol_mult
vol_bg_color = (show_vol20 and vol_20 >= vol20_thresh_line) ? color.new(#00FF88, 92) : na
bgcolor(vol_bg_color, title="High Vol Zone")

// ══════════════════════════════════════════════════════════════
//  PANEL 2 — EMA Spread
// ══════════════════════════════════════════════════════════════
spread_color = ema_spread > 0 ? (wide_spread ? color.new(#00FF88, 0) : color.new(#00FF88, 60)) : (wide_spread ? color.new(#FF4444, 0) : color.new(#FF4444, 60))

plot(show_spread ? ema_spread : na, "EMA Spread",
   color=spread_color, style=plot.style_histogram, linewidth=2)

// Zero line
hline(0, "Zero", color=color.new(color.white, 60), linestyle=hline.style_solid)

// Spread threshold bands
spread_upper =  spread_median * spread_mult
spread_lower = -spread_median * spread_mult
plot(show_spread ?  spread_upper : na, "Spread Upper Threshold",
   color=color.new(color.white, 70), linewidth=1, style=plot.style_linebr)
plot(show_spread ?  spread_lower : na, "Spread Lower Threshold",
   color=color.new(color.white, 70), linewidth=1, style=plot.style_linebr)

// ══════════════════════════════════════════════════════════════
//  SCORE BAR (0–3 aligned features)
// ══════════════════════════════════════════════════════════════
score_color = score == 3 ? color.new(#00FF88, 0) : score == 2 ? color.new(#FFD700, 0) : score == 1 ? color.new(#FF8C00, 20) : color.new(#FF4444, 40)

plot(score, "ML Score (0–3)", color=score_color,
   style=plot.style_area, linewidth=1, histbase=0)

// ══════════════════════════════════════════════════════════════
//  CROSS SIGNALS ON PRICE CHART
// ══════════════════════════════════════════════════════════════
// Strong cross (score = 3)
plotshape(show_crosses and bull_cross and all_aligned,
   title="Strong Bull Cross",
   location=location.belowbar,
   style=shape.triangleup,
   size=size.normal,
   color=color.new(#00FF88, 0),
   text="▲ STRONG", textcolor=color.new(#00FF88, 0))

plotshape(show_crosses and bear_cross and all_aligned,
   title="Strong Bear Cross",
   location=location.abovebar,
   style=shape.triangledown,
   size=size.normal,
   color=color.new(#FF4444, 0),
   text="▼ STRONG", textcolor=color.new(#FF4444, 0))

// Weak cross (score < 3)
plotshape(show_crosses and bull_cross and not all_aligned,
   title="Weak Bull Cross",
   location=location.belowbar,
   style=shape.triangleup,
   size=size.small,
   color=color.new(#00FF88, 55))

plotshape(show_crosses and bear_cross and not all_aligned,
   title="Weak Bear Cross",
   location=location.abovebar,
   style=shape.triangledown,
   size=size.small,
   color=color.new(#FF4444, 55))

// ══════════════════════════════════════════════════════════════
//  ALERTS
// ══════════════════════════════════════════════════════════════
alertcondition(bull_cross and all_aligned,
   title="Strong Bull Cross — All 3 Features Aligned",
   message="SPY: STRONG Bull EMA cross — Vol20 ✓  Vol10 ✓  Spread ✓")

alertcondition(bear_cross and all_aligned,
   title="Strong Bear Cross — All 3 Features Aligned",
   message="SPY: STRONG Bear EMA cross — Vol20 ✓  Vol10 ✓  Spread ✓")

alertcondition(bull_cross and not all_aligned,
   title="Weak Bull Cross — Features NOT aligned",
   message="SPY: Weak Bull EMA cross — score {{plot(\"ML Score (0–3)\")}}/3 — caution")

alertcondition(bear_cross and not all_aligned,
   title="Weak Bear Cross — Features NOT aligned",
   message="SPY: Weak Bear EMA cross — score {{plot(\"ML Score (0–3)\")}}/3 — caution")

// ══════════════════════════════════════════════════════════════
//  TABLE — live readings in top-right corner
// ══════════════════════════════════════════════════════════════
var table dash = table.new(position.top_right, 2, 5,
   border_width=1,
   border_color=color.new(color.white, 70),
   bgcolor=color.new(#0d1117, 10))

if barstate.islast
    // Header
    table.cell(dash, 0, 0, "Feature",    text_color=color.white,  bgcolor=color.new(#1e2a38, 0), text_size=size.small)
    table.cell(dash, 1, 0, "Status",     text_color=color.white,  bgcolor=color.new(#1e2a38, 0), text_size=size.small)

    // Vol 20
    table.cell(dash, 0, 1, "Vol 20",     text_color=color.silver, text_size=size.small)
    table.cell(dash, 1, 1, high_vol20 ? "✓ HIGH" : "✗ low",
       text_color=high_vol20 ? color.new(#00FF88, 0) : color.new(#FF4444, 0), text_size=size.small)

    // Vol 10
    table.cell(dash, 0, 2, "Vol 10",     text_color=color.silver, text_size=size.small)
    table.cell(dash, 1, 2, high_vol10 ? "✓ HIGH" : "✗ low",
       text_color=high_vol10 ? color.new(#FFD700, 0) : color.new(#FF4444, 0), text_size=size.small)

    // EMA Spread
    table.cell(dash, 0, 3, "EMA Spread", text_color=color.silver, text_size=size.small)
    table.cell(dash, 1, 3, wide_spread ? "✓ WIDE" : "✗ tight",
       text_color=wide_spread ? color.new(#00BFFF, 0) : color.new(#FF4444, 0), text_size=size.small)

    // Score
    score_txt_color = score == 3 ? color.new(#00FF88, 0) : score == 2 ? color.new(#FFD700, 0) : color.new(#FF4444, 0)
    table.cell(dash, 0, 4, "ML Score",   text_color=color.silver, text_size=size.small)
    table.cell(dash, 1, 4, str.tostring(score) + " / 3",
       text_color=score_txt_color, text_size=size.small)

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