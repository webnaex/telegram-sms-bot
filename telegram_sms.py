#!/usr/bin/env python3
"""
Telegram Bot → SMS Benachrichtigung (Railway-Version)
Überwacht Telegram-Gruppen und sendet eine SMS bei neuen Nachrichten.

Konfiguration øber Umgebungsvariablen in Railway.
"""

import os
import sys
import logging
import hashlib
import time
import re
import requests
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── Konfiguration aus Umgebungsvariablen ──────────────────────────────────────
def get_env(key: str, required: bool = True) -> str:
    val = os.environ.get(key, "").strip()
    if required and not val:
        log.error(f"Umgebungsvariable '{key}' fehlt! Bitte in Railway setzen.")
        sys.exit(1)
    return val

TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")   # Von @BotFather
SEVEN_API_KEY      = get_env("SEVEN_API_KEY")          # Von seven.io Dashboard
SMS_FROM           = get_env("SMS_FROM")               # Absendername, z.B. "TelegramBot"
SMS_TO             = get_env("SMS_TO")                 # Deine Handynummer, z.B. +4912345678

# Deine eigene Telegram-User-ID (Zahlen-ID, nicht Username) – nur du kannst pausieren
# Leer lassen = jeder kann den Bot steuern (nicht empfohlen!)
ADMIN_USER_ID_RAW  = get_env("ADMIN_USER_ID", required=False)
ADMIN_USER_ID      = int(ADMIN_USER_ID_RAW) if ADMIN_USER_ID_RAW else None

# Kommagetrennte Chat-Namen oder IDs – leer = alle Gruppen/Chats
WATCHED_CHATS_RAW  = get_env("WATCHED_CHATS", required=False)

SMS_TEMPLATE       = os.environ.get("SMS_TEMPLATE", "{chat}: {message}")
MAX_MSG_LENGTH     = int(os.environ.get("MAX_MSG_LENGTH", "120"))

# ── Pause-Status ──────────────────────────────────────────────────────────────
# 0 = nicht pausiert, float timestamp = pausiert bis zu diesem Zeitpunkt
# -1 = dauerhaft pausiert (bis /resume)
PAUSE_UNTIL: float = 0.0

# ── Duplikat-Schutz ───────────────────────────────────────────────────────────
DEDUP_CACHE: dict = {}     # key → timestamp
DEDUP_TTL        = 3600    # exakt gleicher Text: 1 Stunde
DEDUP_TTL_RESULT = 14400   # Ergebnis-Gruppe: 4 Stunden (bis neues Signal)

RESULT_GROUP_KEY = "XAUUSD_RESULT_GROUP"

# Keywords die eine Ergebnis-Nachricht kennzeichnen (TP getroffen, Ziel erreicht)
RESULT_KEYWORDS = [
    "SMASHED",
    "PIPS PROFIT",
    "XAUUSD TP",
    "XAUUSD TARGET",
]

# ── Filter: SMS NUR bei diesen Keywords ───────────────────────────────────────
TRIGGER_KEYWORDS = [
    "$XAUUSD",
    "#XAUUSD",
]

# ── Filter: Diese Texte NIEMALS per SMS senden ────────────────────────────────
BLACKLIST_PHRASES = [
    "Trading is not for everyone.",
    "Lot Sizing Guidelines for Effective Money Management",
    "VIP INVESTMENT PLANS",
    "Good day Admin,  please I want to know more about your investment trading.",
    "Nice, I would love to start with 5000 USDT",
    "Success rarely comes from waiting—it comes from taking calculated risks.",
]


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────
def parse_watched_chats(raw: str) -> list:
    if not raw:
        return []
    result = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            result.append(int(entry))
        except ValueError:
            result.append(entry.lower())
    return result


def should_notify(chat_id: int, chat_name: str, watched: list) -> bool:
    if not watched:
        return True
    for entry in watched:
        if isinstance(entry, int) and entry == chat_id:
            return True
        if isinstance(entry, str) and entry == chat_name.lower():
            return True
    return False


def is_admin(user_id: int) -> bool:
    if ADMIN_USER_ID is None:
        return True  # kein Admin gesetzt → alle erlaubt
    return user_id == ADMIN_USER_ID


def is_sms_paused() -> bool:
    """Gibt True zurück wenn SMS-Versand gerade pausiert ist."""
    global PAUSE_UNTIL
    if PAUSE_UNTIL == 0.0:
        return False
    if PAUSE_UNTIL == -1.0:
        return True  # dauerhaft pausiert
    if time.time() < PAUSE_UNTIL:
        return True
    # Pause abgelaufen → automatisch zurücksetzen
    PAUSE_UNTIL = 0.0
    log.info("⏰ Pause abgelaufen – SMS-Versand wieder aktiv.")
    return False


def parse_pause_arg(arg: str) -> tuple[float, str]:
    """
    Parst das Argument für /pause und gibt (timestamp_until, beschreibung) zurück.
    timestamp_until = -1 bedeutet dauerhaft.

    Unterstützte Formate:
      (leer)              → dauerhaft
      30m                 → 30 Minuten
      2h                  → 2 Stunden
      3d                  → 3 Tage
      23:00               → heute 23:00 Uhr (morgen falls schon vorbei)
      24.04.2026 11:00    → exaktes Datum + Uhrzeit
      24.04.2026          → exaktes Datum, 00:00 Uhr
    """
    arg = arg.strip()

    if not arg:
        return -1.0, "dauerhaft"

    # Minuten: 30m
    m = re.fullmatch(r"(\d+)m", arg, re.IGNORECASE)
    if m:
        secs = int(m.group(1)) * 60
        until = time.time() + secs
        desc = f"{m.group(1)} Minuten (bis {datetime.fromtimestamp(until).strftime('%d.%m.%Y %H:%M')})"
        return until, desc

    # Stunden: 2h
    m = re.fullmatch(r"(\d+)h", arg, re.IGNORECASE)
    if m:
        secs = int(m.group(1)) * 3600
        until = time.time() + secs
        desc = f"{m.group(1)} Stunden (bis {datetime.fromtimestamp(until).strftime('%d.%m.%Y %H:%M')})"
        return until, desc

    # Tage: 3d
    m = re.fullmatch(r"(\d+)d", arg, re.IGNORECASE)
    if m:
        secs = int(m.group(1)) * 86400
        until = time.time() + secs
        desc = f"{m.group(1)} Tage (bis {datetime.fromtimestamp(until).strftime('%d.%m.%Y %H:%M')})"
        return until, desc

    # Uhrzeit: 23:00
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", arg)
    if m:
        now = datetime.now()
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        until = target.timestamp()
        desc = f"bis {target.strftime('%d.%m.%Y %H:%M')} Uhr"
        return until, desc

    # Datum + Uhrzeit: 24.04.2026 11:00
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})", arg)
    if m:
        target = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                          int(m.group(4)), int(m.group(5)))
        until = target.timestamp()
        desc = f"bis {target.strftime('%d.%m.%Y %H:%M')} Uhr"
        return until, desc

    # Nur Datum: 24.04.2026
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", arg)
    if m:
        target = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 0, 0)
        until = target.timestamp()
        desc = f"bis {target.strftime('%d.%m.%Y')} 00:00 Uhr"
        return until, desc

    return None, None  # ungültiges Format


def send_sms(sender: str, chat: str, message: str):
    try:
        if len(message) > MAX_MSG_LENGTH:
            message = message[:MAX_MSG_LENGTH] + "…"

        body = SMS_TEMPLATE.format(sender=sender, chat=chat, message=message)

        response = requests.post(
            "https://gateway.seven.io/api/sms",
            headers={"X-Api-Key": SEVEN_API_KEY},
            data={"to": SMS_TO, "from": SMS_FROM, "text": body},
            timeout=10,
        )
        response.raise_for_status()
        log.info(f"✅ SMS gesendet → {SMS_TO}")
    except Exception as e:
        log.error(f"SMS-Fehler: {e}")


# ── Befehle ───────────────────────────────────────────────────────────────────
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PAUSE_UNTIL
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Nicht autorisiert.")
        return

    arg = " ".join(context.args) if context.args else ""
    until, desc = parse_pause_arg(arg)

    if until is None:
        await update.message.reply_text(
            "❌ Ungültiges Format. Beispiele:\n"
            "/pause → dauerhaft\n"
            "/pause 30m → 30 Minuten\n"
            "/pause 2h → 2 Stunden\n"
            "/pause 3d → 3 Tage\n"
            "/pause 23:00 → bis 23:00 Uhr\n"
            "/pause 24.04.2026 11:00 → bis Datum & Uhrzeit"
        )
        return

    PAUSE_UNTIL = until
    log.info(f"⏸ SMS pausiert {desc} (von User {user.id})")
    await update.message.reply_text(f"⏸ SMS-Versand pausiert {desc}.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PAUSE_UNTIL
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Nicht autorisiert.")
        return

    PAUSE_UNTIL = 0.0
    log.info(f"▶️ SMS wieder aktiviert (von User {user.id})")
    await update.message.reply_text("▶️ SMS-Versand wieder aktiv.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Nicht autorisiert.")
        return

    if is_sms_paused():
        if PAUSE_UNTIL == -1.0:
            msg = "⏸ SMS-Versand ist dauerhaft pausiert.\n/resume zum Aktivieren."
        else:
            until_str = datetime.fromtimestamp(PAUSE_UNTIL).strftime("%d.%m.%Y %H:%M")
            msg = f"⏸ SMS-Versand pausiert bis {until_str} Uhr.\n/resume zum sofortigen Aktivieren."
    else:
        msg = "✅ SMS-Versand ist aktiv."
    await update.message.reply_text(msg)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Nicht autorisiert.")
        return

    msg = (
        "📋 *Verfügbare Befehle:*\n\n"
        "/status — Aktuellen SMS-Status anzeigen\n\n"
        "/pause — SMS dauerhaft pausieren\n"
        "/pause 30m — Für 30 Minuten pausieren\n"
        "/pause 2h — Für 2 Stunden pausieren\n"
        "/pause 3d — Für 3 Tage pausieren\n"
        "/pause 23:00 — Bis heute 23:00 Uhr pausieren\n"
        "/pause 24.04.2026 11:00 — Bis Datum & Uhrzeit pausieren\n\n"
        "/resume — SMS-Versand sofort wieder aktivieren\n\n"
        "/help — Diese Hilfe anzeigen"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Bot-Handler ───────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        message = update.message or update.channel_post
        if not message:
            return

        # Absender
        sender = "Unbekannt"
        if message.from_user:
            u = message.from_user
            sender = " ".join(filter(None, [u.first_name, u.last_name])) or u.username or str(u.id)

        # Chat
        chat_id   = message.chat.id
        chat_name = message.chat.title or message.chat.username or str(chat_id)

        watched = parse_watched_chats(WATCHED_CHATS_RAW)
        if not should_notify(chat_id, chat_name, watched):
            return

        text = message.text or message.caption or ""
        if not text:
            return

        # Blacklist: bestimmte Standardtexte ignorieren
        for phrase in BLACKLIST_PHRASES:
            if phrase in text:
                log.info(f"🚫 Nachricht gefiltert (Blacklist): [{chat_name}]")
                return

        # Whitelist: nur SMS wenn $XAUUSD oder #XAUUSD enthalten
        if not any(kw in text for kw in TRIGGER_KEYWORDS):
            log.info(f"⏭️ Nachricht übersprungen (kein Trigger): [{chat_name}]")
            return

        # Pause-Check
        if is_sms_paused():
            if PAUSE_UNTIL == -1.0:
                log.info(f"⏸ SMS pausiert (dauerhaft) – øbersprungen: [{chat_name}]")
            else:
                until_str = datetime.fromtimestamp(PAUSE_UNTIL).strftime("%d.%m.%Y %H:%M")
                log.info(f"⏸ SMS pausiert bis {until_str} – øbersprungen: [{chat_name}]")
            return

        # Duplikat-Check
        now = time.time()
        is_result = any(kw in text for kw in RESULT_KEYWORDS)

        if is_result:
            dedup_key = RESULT_GROUP_KEY
            ttl = DEDUP_TTL_RESULT
        else:
            # Signal → Ergebnis-Cooldown zurücksetzen
            DEDUP_CACHE.pop(RESULT_GROUP_KEY, None)
            dedup_key = hashlib.md5(text.encode()).hexdigest()
            ttl = DEDUP_TTL

        # Abgelaufene Einträge aufräumen
        expired = [h for h, t in DEDUP_CACHE.items() if now - t > DEDUP_TTL_RESULT]
        for h in expired:
            del DEDUP_CACHE[h]
        if dedup_key in DEDUP_CACHE and now - DEDUP_CACHE[dedup_key] < ttl:
            log.info(f"Duplikat ignoriert (Cooldown aktiv): [{chat_name}]")
            return
        DEDUP_CACHE[dedup_key] = now

        log.info(f"📩 Trigger erkannt – SMS wird gesendet [{chat_name}]")
        send_sms(sender=sender, chat=chat_name, message=text)

    except Exception as e:
        log.error(f"Fehler beim Verarbeiten: {e}")


# ── Hauptprogramm ─────────────────────────────────────────────────────────────
def main():
    watched = parse_watched_chats(WATCHED_CHATS_RAW)
    watch_info = ", ".join(str(w) for w in watched) if watched else "alle Chats & Gruppen"
    log.info(f"🤖 Telegram Bot startet | Beobachte: {watch_info}")
    log.info(f"📱 SMS-Ziel: {SMS_TO}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Steuerungsbefehle (nur per Direktnachricht an den Bot)
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help",   cmd_help))

    # Nachrichten aus Gruppen, Channels und Direktnachrichten
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        handle_message
    ))

    log.info("🟢 Bot läuft. Warte auf Nachrichten…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
