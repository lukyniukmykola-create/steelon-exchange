"""Interactive Telegram interface for Steelon exchange rates.

Run this continuously on a server.  GitHub Actions is useful for a scheduled
post, but it cannot keep a Telegram long-poll connection open for buttons.
"""
import argparse
import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

import fetch_and_post as rates

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
STEELON_CHAT_ID = os.environ.get("STEELON_CHAT_ID") or os.environ.get("CHAT_ID")
ADMIN_IDS = {
    value.strip()
    for value in os.environ.get("TELEGRAM_ADMIN_IDS", "").split(",")
    if value.strip()
}
POLL_TIMEOUT = 45

MENU = {
    "inline_keyboard": [
        [{"text": "📩 Надіслати мені курс", "callback_data": "my_rate"}],
        [{"text": "📣 Надіслати курс у Steelon", "callback_data": "send_steelon"}],
        [{"text": "🕒 Останні оновлення", "callback_data": "latest"}],
        [{"text": "🔔 Отримувати щодня", "callback_data": "subscribe"}],
        [{"text": "🔕 Вимкнути щоденні", "callback_data": "unsubscribe"}],
    ]
}


def api(method, data=None, timeout=20):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=data or {},
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", f"Telegram {method} failed"))
    return body.get("result")


def send_message(chat_id, text, menu=False):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if menu:
        data["reply_markup"] = json.dumps(MENU, ensure_ascii=False)
    return api("sendMessage", data)


def show_menu(chat_id):
    return send_message(
        chat_id,
        "Вітаю! Оберіть дію. Дані беруться з останнього оновлення курсу.",
        menu=True,
    )


def current_table():
    history = rates.load_history()
    if not history.get("channels") and not history.get("nbu"):
        return None
    return rates.build_cached_message(history)


def change_subscription(chat_id, enabled):
    """Delegate to the locked helper so the updater thread cannot clobber us."""
    rates.set_subscription(chat_id, enabled)


def handle_callback(callback):
    action = callback.get("data")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    chat_type = message.get("chat", {}).get("type")
    user_id = callback.get("from", {}).get("id")
    if not chat_id:
        return
    api("answerCallbackQuery", {"callback_query_id": callback["id"]})
    if chat_type != "private":
        send_message(chat_id, "Відкрийте бота в особистих повідомленнях і натисніть /start.")
        return

    if action == "my_rate":
        table = current_table()
        send_message(chat_id, table or "Ще немає збереженого курсу. Спробуйте трохи пізніше.", menu=True)
    elif action == "latest":
        table = current_table()
        prefix = "🕒 Останні оновлення\n\n"
        send_message(chat_id, prefix + (table or "Оновлень ще немає."), menu=True)
    elif action == "send_steelon":
        if not ADMIN_IDS:
            send_message(chat_id, "Публікація в Steelon ще не налаштована: додайте TELEGRAM_ADMIN_IDS.", menu=True)
        elif str(user_id) not in ADMIN_IDS:
            send_message(chat_id, "Лише адміністратор може надсилати курс у групу Steelon.", menu=True)
        elif not STEELON_CHAT_ID:
            send_message(chat_id, "Не задано STEELON_CHAT_ID для групи Steelon.", menu=True)
        else:
            table = current_table()
            if not table:
                send_message(chat_id, "Ще немає збереженого курсу.", menu=True)
                return
            rates.send_telegram_message(table, chat_id=STEELON_CHAT_ID)
            send_message(chat_id, "Курс надіслано в Steelon.", menu=True)
    elif action == "subscribe":
        change_subscription(chat_id, enabled=True)
        send_message(chat_id, "Готово — надсилатиму курс один раз на день.", menu=True)
    elif action == "unsubscribe":
        change_subscription(chat_id, enabled=False)
        send_message(chat_id, "Щоденну розсилку вимкнено.", menu=True)


COMMANDS = {"/start", "/menu", "/help"}


def parse_command(text):
    """Return the bare command, or None. Handles '/start@SteelonBot' and empty text."""
    if not text:
        return None
    word = text.split(maxsplit=1)[0].lower()
    if not word.startswith("/"):
        return None
    command = word.split("@", 1)[0]
    return command if command in COMMANDS else None


def handle_update(update):
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return
    # Stickers, photos and service messages carry no text at all.
    text = (message.get("text") or message.get("caption") or "").strip()
    is_private = chat.get("type") == "private"

    if parse_command(text):
        if is_private:
            show_menu(chat_id)
        else:
            send_message(chat_id, "Відкрийте бота в особистих повідомленнях і натисніть /start.")
        return

    # Any other private message: show the menu instead of staying silent.
    if is_private:
        show_menu(chat_id)


def poll_forever():
    offset = None
    while True:
        try:
            data = {"timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset is not None:
                data["offset"] = offset
            updates = api("getUpdates", data, timeout=POLL_TIMEOUT + 15) or []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception:
                    log.exception("Failed to handle Telegram update %s", update.get("update_id"))
        except requests.RequestException as exc:
            log.warning("Telegram polling failed: %s", exc)
            time.sleep(5)
        except Exception:
            log.exception("Telegram polling error")
            time.sleep(5)


def run_rate_updater():
    """Refresh the data at each full Kyiv hour during the existing work window."""
    last_slot = None
    while True:
        now = datetime.now(rates.KYIV_TZ)
        slot = now.strftime("%Y-%m-%d-%H")
        if rates.FIRST_HOUR <= now.hour <= rates.LAST_HOUR and now.minute < 5 and slot != last_slot:
            last_slot = slot
            try:
                rates.main(no_wait=True)
            except Exception:
                log.exception("Rate update failed")
        time.sleep(20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Steelon interactive Telegram bot")
    parser.add_argument(
        "--with-rate-updater",
        action="store_true",
        help="also refresh rates hourly (09:00–18:00 Kyiv) and send daily subscriptions",
    )
    args = parser.parse_args()
    if args.with_rate_updater:
        threading.Thread(target=run_rate_updater, daemon=True).start()
    log.info("Interactive Steelon bot started")
    poll_forever()
