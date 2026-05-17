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
        "url":     os.environ.get("SHADOW_URL", "https://shadowtrading-bot-production.up.railway.app").strip(),
        "status":  "/api/status",
    },
    "forex": {
        "name":    "Jarvis Forex",
        "emoji":   "💱",
        "url":     os.environ.get("JARVIS_URL", "https://devoted-success-production-f3f9.up.railway.app").strip(),
        "status":  "/api/data",
        "auth":    ("jarvis", os.environ.get("JARVIS_PASSWORD", "Destin123")),
    },
    "nexus": {
        "name":    "NEXUS",
        "emoji":   "⚡",
        "url":     os.environ.get("NEXUS_URL", "https://nadex-stream2-production.up.railway.app").strip(),
        "status":  "/api/data",
    },
    "dropship": {
        "name":    "Dropship Bot",
        "emoji":   "🛒",
        "url":     os.environ.get("DROPSHIP_URL", "https://worker-production-197f.up.railway.app").strip(),
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
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=8
        )
        result = r.json()
        if not result.get("ok"):
            log.error(f"Telegram sendMessage failed: {result}")
    except Exception as e:
        log.warning(f"Telegram error: {e}")

# ══════════════════════════════════════════════════════════
# POLLING
# ══════════════════════════════════════════════════════════
def fetch_status(key: str) -> dict | None:
    bot  = BOTS[key]
    auth = bot.get("auth")
    try:
        r = requests.get(f"{bot['url']}{bot['status']}", auth=auth, timeout=8)
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
                # Forex/NEXUS nest their P&L — extract for logging
                pnl = (status.get("day_pnl")
                       or status.get("state", {}).get("day_pnl")
                       or status.get("allTime", {}).get("pnl", "N/A"))
                pos = (status.get("positions")
                       or len(status.get("state", {}).get("assets", {}))
                       or "N/A")
                log.info(f"Polled {key}: P&L={pnl} pos={pos}")
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
        lines += f"\n  {p['symbol']} {p.get('side','?')} @ ${p.get('entry',0):.2f} | P&L: ${p.get('pnl',0):+.2f}"
    return (
        f"📈 Shadow Bot\n"
        f"Balance: ${s.get('balance',0):,.2f} | Day P&L: ${s.get('day_pnl',0):+.2f}\n"
        f"W/L: {s.get('win_today',0)}W / {s.get('loss_today',0)}L | WR: {s.get('win_rate',0)}%\n"
        f"Positions: {s.get('positions',0)}/2 | Session: {s.get('session','?')}\n"
        f"{r_em} Regime: {regime} | Paused: {'Yes' if s.get('paused') else 'No'}"
        + (f"\n\nOpen:{lines}" if lines else "")
    )

def _fmt_forex(s: dict) -> str:
    state  = s.get("state", {})
    bal    = float(state.get("balance", 0))
    trade  = state.get("active_trade")
    all_t  = s.get("allTime", {})
    pnl    = float(all_t.get("pnl", 0))
    wins   = all_t.get("wins", 0)
    losses = all_t.get("losses", 0)
    paused = state.get("paused", False)
    trade_line = f"\n  {trade['instrument']} {trade['direction']} | P&L: ${float(trade.get('unrealized_pl', 0)):+.2f}" if trade else "\n  No open trade"
    return (
        f"💱 Jarvis Forex\n"
        f"Balance: ${bal:,.2f} | All-time P&L: ${pnl:+.2f}\n"
        f"W/L: {wins}W / {losses}L | Paused: {'Yes' if paused else 'No'}"
        + trade_line
    )

def _fmt_nexus(s: dict) -> str:
    state   = s.get("state", {})
    assets  = state.get("assets", {})
    session = state.get("session", "?")
    paused  = state.get("paused", False)
    lines   = []
    for asset, data in assets.items():
        price = data.get("price", 0)
        mom   = data.get("momentum", 0)
        cci   = data.get("cci", 0)
        dots  = data.get("sar_dots", 0)
        lines.append(f"  {asset}: ${price:,.0f} | Mom: {mom:.0f}% | CCI: {cci:.0f} | SAR dots: {dots}")
    return (
        f"⚡ NEXUS\n"
        f"Session: {session} | Paused: {'Yes' if paused else 'No'}\n"
        + "\n".join(lines) +
        f"\nDeposit: Needs $250 to go live"
    )

def _fmt_dropship(s: dict) -> str:
    pnl      = float(s.get("day_pnl", 0) or 0)
    orders   = s.get("orders_today", 0)
    open_o   = s.get("open_orders", 0)
    seller   = s.get("best_seller", "N/A")
    goal_pct = s.get("goal_pct", 0)
    vs_yest  = float(s.get("vs_yesterday", 0) or 0)
    trend    = "up" if vs_yest >= 0 else "down"
    return (
        f"🛒 Dropship Bot\n"
        f"Today: ${pnl:.2f} profit | {orders} orders fulfilled\n"
        f"Open orders: {open_o} pending\n"
        f"Best seller: {seller}\n"
        f"Goal: {goal_pct}% | {trend} ${abs(vs_yest):.2f} vs yesterday"
    )

FORMATTERS = {"shadow": _fmt_shadow, "forex": _fmt_forex, "nexus": _fmt_nexus, "dropship": _fmt_dropship}

# ══════════════════════════════════════════════════════════
# COMMAND ROUTING
# ══════════════════════════════════════════════════════════
def handle_command(text: str) -> str | None:
    low   = text.lower().strip()
    parts = low.split()

    # /status — summary of all bots from cache
    if low == "/status":
        lines = ["🎯 Jarvis Commander — All Agents\n"]
        for key, bot in BOTS.items():
            snap = _bot_snapshots.get(key)
            if snap:
                pnl  = float(snap.get("day_pnl", 0) or 0)
                pos  = snap.get("positions", 0)
                ts   = snap.get("_polled_at", "?")[:16].replace("T", " ")
                lines.append(
                    f"{bot['emoji']} {bot['name']} | P&L: ${pnl:+.2f} | "
                    f"Pos: {pos} | {ts}"
                )
            else:
                lines.append(f"{bot['emoji']} {bot['name']} — polling...")
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

    # /review — pull Shadow Bot weekly stats from Sheets
    if low == "/review":
        shadow_url = BOTS["shadow"]["url"]
        try:
            r = requests.get(f"{shadow_url}/api/weekly_review", timeout=15)
            d = r.json()
        except Exception as e:
            return f"Could not fetch weekly review: {e}"

        if d.get("error"):
            return f"Review error: {d['error']}"
        if d.get("total_trades", 0) == 0:
            return f"No closed trades this week yet.\n{d.get('message','')}"

        by_sym = d.get("by_symbol", {})
        sym_lines = ""
        for sym, s in by_sym.items():
            if s["total"] > 0:
                sym_lines += f"  {sym}: {s['wins']}W/{s['total']-s['wins']}L ({s['wr']}% WR) P&L: ${s['pnl']:+.2f}\n"

        best  = d.get("best_trade", {})
        worst = d.get("worst_trade", {})

        return (
            f"SHADOW BOT — WEEK REVIEW ({d.get('week_start','')})\n"
            f"--------------------\n"
            f"Trades: {d['total_trades']} | {d['wins']}W / {d['losses']}L | WR: {d['win_rate']}%\n"
            f"Net P&L: ${d['total_pnl']:+.2f}\n"
            f"Avg win: ${d['avg_win']:+.2f} | Avg loss: ${d['avg_loss']:+.2f}\n"
            f"Regime: {d.get('regime','?')}\n\n"
            f"By symbol:\n{sym_lines}"
            f"Best: {best.get('symbol','?')} ${best.get('pnl',0):+.2f} ({best.get('reason','')})\n"
            f"Worst: {worst.get('symbol','?')} ${worst.get('pnl',0):+.2f} ({worst.get('reason','')})"
        )

    if low in ("/help", "/start"):
        return (
            "JARVIS COMMANDER ONLINE\n"
            "--------------------\n"
            "Shadow Bot: monitoring\n"
            "Jarvis Forex: monitoring\n"
            "NEXUS: monitoring\n"
            "Dropship Bot: monitoring\n"
            "Anomaly detection: active\n\n"
            "Commands:\n"
            "/status - all agents snapshot\n"
            "/ask shadow - Shadow Bot live status\n"
            "/ask forex - Jarvis Forex live status\n"
            "/ask nexus - NEXUS live status\n"
            "/ask dropship - Dropship store status\n"
            "/review - Shadow Bot week review from Sheets\n"
            "/help - this menu"
        )

    return None

# ══════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK (replaces long-polling — no conflicts)
# ══════════════════════════════════════════════════════════
def register_webhook(public_url: str):
    """Tell Telegram to POST updates to our /telegram_webhook endpoint."""
    webhook_url = f"{public_url}/telegram_webhook"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
            json={"url": webhook_url},
            timeout=10
        )
        result = r.json()
        if result.get("ok"):
            log.info(f"Webhook registered: {webhook_url}")
        else:
            log.error(f"Webhook registration failed: {result}")
    except Exception as e:
        log.error(f"Webhook registration error: {e}")

# ══════════════════════════════════════════════════════════
# API ENDPOINTS
# ══════════════════════════════════════════════════════════
@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    from flask import request as freq
    update = freq.get_json(force=True, silent=True) or {}
    text   = update.get("message", {}).get("text", "")
    name   = update.get("message", {}).get("from", {}).get("first_name", "?")
    chat_id = str(update.get("message", {}).get("chat", {}).get("id", TELEGRAM_CHAT_ID))
    log.info(f"Webhook: text='{text}' from={name} chat_id={chat_id}")
    if text:
        reply = handle_command(text)
        log.info(f"handle_command returned: {repr(reply)[:80] if reply else 'None'}")
        if reply:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": reply},
                    timeout=8
                )
                result = r.json()
                log.info(f"sendMessage result: ok={result.get('ok')} err={result.get('description','')}")
            except Exception as e:
                log.error(f"sendMessage exception: {e}")
    return jsonify({"ok": True}), 200

@app.route("/test")
def test_endpoint():
    """Hit this to confirm the service can send Telegram messages."""
    reply = handle_command("/start")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": reply or "test ok"},
            timeout=8
        )
        result = r.json()
        return jsonify({"sent": result.get("ok"), "detail": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/morning_briefing", methods=["POST"])
def api_morning_briefing():
    """n8n calls this at 9:25am ET — compiles all agent data and sends Telegram briefing."""
    try:
        from flask import request as freq
        body = freq.get_json(force=True, silent=True) or {}
        # Allow n8n to inject extra context (news, fear/greed etc)
        extra = body.get("extra", "")

        lines = ["MORNING BRIEFING\n--------------------"]
        live_agents = 0

        for key, snap in _bot_snapshots.items():
            if not snap:
                continue
            live_agents += 1
            name = BOTS[key]["name"]
            em   = BOTS[key]["emoji"]
            pnl  = float(snap.get("day_pnl", 0) or
                         snap.get("state", {}).get("day_pnl", 0) or 0)
            if key == "shadow":
                regime = snap.get("regime", "?")
                pos    = snap.get("positions", 0)
                lines.append(f"{em} {name}: P&L ${pnl:+.2f} | {pos} positions | Regime {regime}")
            elif key == "forex":
                bal = float(snap.get("state", {}).get("balance", 0) or
                             snap.get("balance", 0) or 0)
                lines.append(f"{em} {name}: Balance ${bal:,.0f} | P&L ${pnl:+.2f}")
            elif key == "nexus":
                lines.append(f"{em} {name}: Session ready | Deposit needed to go live")
            elif key == "dropship":
                orders = snap.get("orders_today", 0)
                lines.append(f"{em} {name}: P&L ${pnl:+.2f} | {orders} orders today")

        lines.append(f"\n{live_agents}/{len(BOTS)} agents online")
        if extra:
            lines.append(f"\n{extra}")
        lines.append("\nMarket opens 9:30am ET. Good luck today.")

        msg = "\n".join(lines)
        send_telegram(msg)
        log.info("Morning briefing sent via n8n trigger")
        return jsonify({"ok": True, "message": msg}), 200
    except Exception as e:
        log.error(f"Morning briefing error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/n8n_alert", methods=["POST"])
def api_n8n_alert():
    """Generic n8n → Telegram alert. POST {message: '...', level: 'info|warn|alert'}"""
    try:
        from flask import request as freq
        body  = freq.get_json(force=True, silent=True) or {}
        msg   = body.get("message", "")
        level = body.get("level", "info")
        icons = {"info": "ℹ️", "warn": "⚠️", "alert": "🚨"}
        icon  = icons.get(level, "ℹ️")
        if msg:
            send_telegram(f"{icon} n8n: {msg}")
            log.info(f"n8n alert sent: {msg[:60]}")
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({
        "status":         "ok",
        "telegram_token": "SET" if TELEGRAM_TOKEN else "MISSING",
        "telegram_chat":  TELEGRAM_CHAT_ID,
        "webhook_mode":   True,
        "bots_tracked":   list(_bot_snapshots.keys()),
        "bots_total":     len(BOTS)
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

    # Use env var if set, fall back to known Railway URL
    public_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "web-production-32238.up.railway.app")
    if public_url and not public_url.startswith("http"):
        public_url = f"https://{public_url}"

    log.info(f"Jarvis Commander starting on port {port} | public_url={public_url}")

    threading.Thread(target=poll_loop, daemon=True).start()

    if TELEGRAM_TOKEN:
        register_webhook(public_url)
        send_telegram(
            "🎯 JARVIS COMMANDER ONLINE\n"
            "--------------------\n"
            "Shadow Bot: monitoring\n"
            "Jarvis Forex: monitoring\n"
            "NEXUS: monitoring\n"
            "Anomaly detection: active\n\n"
            "Commands: /status /ask shadow /ask forex /ask nexus /help"
        )

    app.run(host="0.0.0.0", port=port, debug=False)
