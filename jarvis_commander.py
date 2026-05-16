#!/usr/bin/env python3
"""
Jarvis Commander — Central orchestrator for all trading agents.
Polls Shadow Bot, Forex, and NEXUS every 60s.
Detects anomalies. Routes /ask commands via Telegram.
"""

import json, logging, os, requests, threading, time
from datetime import datetime
from flask import Flask, jsonify
import pytz

ET = pytz.timezone("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8620250094")
POLL_INTERVAL    = 60    # seconds between full poll cycles
ALERT_COOLDOWN   = 3600  # 1 hour between same alert per bot

BOTS = {
    "shadow": {
        "name":    "Shadow Bot",
        "emoji":   "📈",
        "url":     os.environ.get("SHADOW_URL", "https://shadowtrading-bot-production.up.railway.app"),
        "status":  "/api/status",
    },
    "forex": {
        "name":    "Jarvis Forex",
        "emoji":   "💱",
        "url":     os.environ.get("JARVIS_URL", "https://devoted-success-production-f3f9.up.railway.app"),
        "status":  "/api/status",
    },
    "nexus": {
        "name":    "NEXUS",
        "emoji":   "⚡",
        "url":     os.environ.get("NEXUS_URL", "https://nadex-stream2-production.up.railway.app"),
        "status":  "/api/status",
    },
}

# ══════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════
_bot_snapshots  = {}  # key -> latest status dict (+ _polled_at)
_alert_sent_at  = {}  # "key:type" -> datetime (cooldown tracker)
_last_update_id = 0
app             = Flask(__name__)

# ══════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — skipping")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")

# ══════════════════════════════════════════════════════════
# POLLING
# ══════════════════════════════════════════════════════════
def fetch_status(key: str) -> dict | None:
    bot = BOTS[key]
    try:
        r = requests.get(f"{bot['url']}{bot['status']}", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Poll {key}: {e}")
        return None

# ══════════════════════════════════════════════════════════
# ANOMALY DETECTION
# ══════════════════════════════════════════════════════════
def _cooldown_ok(key: str, alert_type: str) -> bool:
    tag  = f"{key}:{alert_type}"
    last = _alert_sent_at.get(tag)
    if last and (datetime.now(ET) - last).total_seconds() < ALERT_COOLDOWN:
        return False
    _alert_sent_at[tag] = datetime.now(ET)
    return True

def check_anomalies(key: str, status: dict):
    name = BOTS[key]["name"]
    em   = BOTS[key]["emoji"]

    # Loss streak ≥ 3
    losses = int(status.get("loss_today", 0))
    if losses >= 3 and _cooldown_ok(key, "loss_streak"):
        send_telegram(
            f"🚨 *COMMANDER — {em} {name}*\n"
            f"Loss streak: *{losses} losses* today\n"
            f"Consider pausing. Type `/ask {key}` for full status."
        )

    # Drawdown ≥ 2%
    balance = float(status.get("balance", 0) or 0)
    day_pnl = float(status.get("day_pnl", 0) or 0)
    if balance > 0 and day_pnl < 0:
        dd_pct = abs(day_pnl) / balance * 100
        if dd_pct >= 2.0 and _cooldown_ok(key, "drawdown"):
            send_telegram(
                f"⛔ *COMMANDER — {em} {name}*\n"
                f"Drawdown: *-{dd_pct:.1f}%* (${abs(day_pnl):.2f})\n"
                f"Approaching daily loss limit — monitoring."
            )

    # No trades by 2:00-2:30pm ET window
    now = datetime.now(ET)
    t   = now.hour * 60 + now.minute
    if 840 <= t <= 870:
        wins   = int(status.get("win_today", 0))
        losses = int(status.get("loss_today", 0))
        if wins + losses == 0 and _cooldown_ok(key, "no_trades"):
            send_telegram(
                f"💤 *COMMANDER — {em} {name}*\n"
                f"No trades executed all session (2pm check).\n"
                f"Score threshold or regime filter blocking everything — check Railway logs."
            )

    # Bot returned an error field
    if status.get("error") and _cooldown_ok(key, "error"):
        send_telegram(
            f"❌ *COMMANDER — {em} {name}*\n"
            f"Bot error: `{status['error']}`\n"
            f"May need redeploy."
        )

def poll_loop():
    while True:
        for key in BOTS:
            status = fetch_status(key)
            if status:
                _bot_snapshots[key] = {
                    **status,
                    "_polled_at": datetime.now(ET).isoformat()
                }
                log.info(f"Polled {key}: P&L={status.get('day_pnl', 'N/A')} pos={status.get('positions', 'N/A')}")
                check_anomalies(key, status)
            else:
                if _bot_snapshots.get(key) and _cooldown_ok(key, "unreachable"):
                    send_telegram(
                        f"❌ *COMMANDER — {BOTS[key]['emoji']} {BOTS[key]['name']}*\n"
                        f"Bot not responding. Check Railway dashboard."
                    )
        time.sleep(POLL_INTERVAL)

# ══════════════════════════════════════════════════════════
# STATUS FORMATTERS
# ══════════════════════════════════════════════════════════
def _fmt_shadow(s: dict) -> str:
    regime = s.get("regime", "?")
    r_em   = "🟢" if regime == "BULL" else ("🔴" if regime == "BEAR" else "🟡")
    pos    = s.get("positions_detail", [])
    lines  = ""
    for p in pos:
        lines += f"\n  • {p['symbol']} {p.get('side','?')} @ ${p.get('entry',0):.2f} | P&L: ${p.get('pnl',0):+.2f}"
    return (
        f"📈 *Shadow Bot*\n"
        f"Balance: `${s.get('balance',0):,.2f}` | Day P&L: `${s.get('day_pnl',0):+.2f}`\n"
        f"W/L: {s.get('win_today',0)}W / {s.get('loss_today',0)}L | WR: {s.get('win_rate',0)}%\n"
        f"Positions: {s.get('positions',0)}/2 | Session: {s.get('session','?')}\n"
        f"{r_em} Regime: {regime} | Paused: {'Yes ⏸' if s.get('paused') else 'No ▶️'}"
        + (f"\n\nOpen positions:{lines}" if lines else "")
    )

def _fmt_forex(s: dict) -> str:
    pnl  = float(s.get("total_pl", s.get("day_pnl", 0)) or 0)
    pos  = s.get("positions", [])
    npos = len(pos) if isinstance(pos, list) else int(pos or 0)
    return (
        f"💱 *Jarvis Forex*\n"
        f"Balance: `${float(s.get('balance',0)):,.2f}` | P&L: `${pnl:+.2f}`\n"
        f"Open positions: {npos}\n"
        f"Paused: {'Yes ⏸' if s.get('paused') else 'No ▶️'}"
    )

def _fmt_nexus(s: dict) -> str:
    up = "✅ Live" if s.get("connected") else "❌ Offline"
    return (
        f"⚡ *NEXUS*\n"
        f"Status: {up} | Session: {s.get('session','?')}\n"
        f"Signals today: {s.get('signals_today',0)} | Wins: {s.get('wins_today',0)}\n"
        f"Deposit: {'✅ Funded' if s.get('funded') else '⚠️ Needs $250 deposit'}"
    )

FORMATTERS = {"shadow": _fmt_shadow, "forex": _fmt_forex, "nexus": _fmt_nexus}

# ══════════════════════════════════════════════════════════
# COMMAND ROUTING
# ══════════════════════════════════════════════════════════
def handle_command(text: str) -> str | None:
    low   = text.lower().strip()
    parts = low.split()

    # /status — summary of all bots from cache
    if low == "/status":
        lines = ["🎯 *Jarvis Commander — All Agents*\n"]
        for key, bot in BOTS.items():
            snap = _bot_snapshots.get(key)
            if snap:
                pnl  = float(snap.get("day_pnl", 0) or 0)
                pos  = snap.get("positions", 0)
                ts   = snap.get("_polled_at", "?")[:16].replace("T", " ")
                lines.append(
                    f"{bot['emoji']} *{bot['name']}* | P&L: `${pnl:+.2f}` | "
                    f"Pos: {pos} | Updated: {ts}"
                )
            else:
                lines.append(f"{bot['emoji']} *{bot['name']}* — No data yet")
        return "\n".join(lines)

    # /ask <bot> — live fetch from that bot
    if parts[0] == "/ask" and len(parts) >= 2:
        target = parts[1]
        if target not in BOTS:
            return f"Unknown agent `{target}`. Available: {', '.join(BOTS.keys())}"
        status = fetch_status(target)
        if not status:
            return f"❌ {BOTS[target]['name']} not responding — check Railway"
        return FORMATTERS[target](status)

    if low in ("/help", "/start"):
        return (
            "🎯 *JARVIS COMMANDER ONLINE*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Shadow Bot — monitoring\n"
            "✅ Jarvis Forex — monitoring\n"
            "✅ NEXUS — monitoring\n"
            "✅ Anomaly detection active\n\n"
            "*Commands:*\n"
            "/status — all agents snapshot\n"
            "/ask shadow — Shadow Bot live status\n"
            "/ask forex — Jarvis Forex live status\n"
            "/ask nexus — NEXUS live status\n"
            "/help — this menu"
        )

    return None

# ══════════════════════════════════════════════════════════
# TELEGRAM POLLING
# ══════════════════════════════════════════════════════════
def telegram_poll_loop():
    global _last_update_id
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _last_update_id + 1, "timeout": 30},
                timeout=35
            )
            for u in r.json().get("result", []):
                _last_update_id = u["update_id"]
                text = u.get("message", {}).get("text", "")
                if text:
                    reply = handle_command(text)
                    if reply:
                        send_telegram(reply)
        except Exception as e:
            log.warning(f"Telegram poll: {e}")
        time.sleep(1)

# ══════════════════════════════════════════════════════════
# API ENDPOINTS
# ══════════════════════════════════════════════════════════
@app.route("/health")
def health():
    return jsonify({
        "status":       "ok",
        "bots_tracked": list(_bot_snapshots.keys()),
        "bots_total":   len(BOTS)
    }), 200

@app.route("/api/all_status")
def all_status():
    return jsonify(_bot_snapshots), 200

@app.route("/api/bot/<key>")
def bot_status_endpoint(key):
    if key not in BOTS:
        return jsonify({"error": "unknown bot"}), 404
    snap = _bot_snapshots.get(key)
    if not snap:
        return jsonify({"error": "no data yet — poll pending"}), 404
    return jsonify(snap), 200

@app.route("/")
def root():
    return jsonify({
        "service":   "Jarvis Commander",
        "version":   "1.0",
        "agents":    {k: v["name"] for k, v in BOTS.items()},
        "endpoints": ["/health", "/api/all_status", "/api/bot/<key>"]
    })

# ══════════════════════════════════════════════════════════
# BOOT
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    log.info(f"Jarvis Commander starting on port {port}")

    threading.Thread(target=poll_loop,          daemon=True).start()
    threading.Thread(target=telegram_poll_loop, daemon=True).start()

    send_telegram(
        "🎯 *JARVIS COMMANDER ONLINE*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Shadow Bot — monitoring\n"
        "✅ Jarvis Forex — monitoring\n"
        "✅ NEXUS — monitoring\n"
        "✅ Anomaly detection: loss streak, drawdown, no-trades\n"
        "✅ Command routing: /ask shadow | /ask forex | /ask nexus\n\n"
        "Type /help for full command list"
    )

    app.run(host="0.0.0.0", port=port, debug=False)
