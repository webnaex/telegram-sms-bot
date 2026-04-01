#!/usr/bin/env python3
"""
Telegram Bot → SMS Benachrichtigung (Railway-Version)
Überwacht Telegram-Gruppen und sendet eine SMS bei neuen Nachrichten.

Konfiguration über Umgebungsvariablen in Railway.
"""

import os
import sys
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ── Logging ─────────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── Konfiguration aus Umgebungsvariablen ────────────────────────────────────────────
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

# Kommagetrennte Chat-Namen oder IDs – leer = alle Gruppen/Chats
WATCHED_CHATS_RAW  = get_env("WATCHED_CHATS", required=False)

SMS_TEMPLATE       = os.environ.get("SMS_TEMPLATE", "{chat}: {message}")
MAX_MSG_LENGTH     = int(os.environ.get("MAX_MSG_LENGTH", "120"))


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────────────────────────
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


# ── Bot-Handler ───────────────────────────────────────────────────────────────────────────────────
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

        text = message.text or message.caption or "[Kein Text / Medieninhalt]"
        log.info(f"📩 Neue Nachricht – {sender} in [{chat_name}]")
        send_sms(sender=sender, chat=chat_name, message=text)

    except Exception as e:
        log.error(f"Fehler beim Verarbeiten: {e}")


# ── Hauptprogramm ─────────────────────────────────────────────────────────────────────────────────
def main():
    watched = parse_watched_chats(WATCHED_CHATS_RAW)
    watch_info = ", ".join(str(w) for w in watched) if watched else "alle Chats & Gruppen"
    log.info(f"🤖 Telegram Bot startet | Beobachte: {watch_info}")
    log.info(f"📱 SMS-Ziel: {SMS_TO}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Nachrichten aus Gruppen, Channels und Direktnachrichten
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        handle_message
    ))

    log.info("🟢 Bot läuft. Warte auf Nachrichten…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
