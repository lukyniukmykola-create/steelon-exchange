import argparse
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    log.warning("zoneinfo not available, falling back to fixed UTC+2/+3 handling")
    KYIV_TZ = timezone(timedelta(hours=2))

DATA_DIR = os.environ.get("DATA_DIR", ".")
HISTORY_FILE = os.path.join(DATA_DIR, "rates_history.json")
SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
CHANNELS_FILE = "channels.json"
CURRENCIES = ["EUR"]
RATE_MIN = 1.0
RATE_MAX = 200.0
RATE_TOLERANCE = 0.005
MAX_RETRIES = 3
RETRY_BACKOFF = 2
CHANNEL_FETCH_PAUSE = 1.0
PHOTO_OCR_MAX_IMAGES = 6
PHOTO_RATE_MAX_DEVIATION = 0.10

# Publishing window in Kyiv time: hourly full-hour slots from FIRST_HOUR to LAST_HOUR.
# GitHub Actions starts scheduled jobs 0-60 min late, so the workflow is scheduled
# ~40 min early and the script itself waits for the exact slot.
FIRST_HOUR = 9
LAST_HOUR = 18
SLOT_GRACE_MIN = 15
MAX_WAIT_MIN = 50

BOT_TOKEN = os.environ.get("BOT_TOKEN")
# CHAT_ID is kept for the existing GitHub Actions configuration.
CHAT_ID = os.environ.get("CHAT_ID") or os.environ.get("STEELON_CHAT_ID")
# Set AUTO_POST_STEELON=0 to keep the hourly scan but publish to the channel
# only via the admin button in the bot.
AUTO_POST_STEELON = os.environ.get("AUTO_POST_STEELON", "1").strip().lower() \
    not in {"0", "false", "no", "off"}

# The bot thread (button presses) and the updater thread (daily delivery) both
# touch the subscribers file, so every read-modify-write goes through this lock.
_SUBSCRIBERS_LOCK = threading.RLock()

NBU_LINK = "https://bank.gov.ua/ua/markets/exchangerates"

PAIR_PATTERNS = {
    code: re.compile(
        rf"\b{code}\b\D{{0,20}}?(\d{{1,4}}[.,]\d{{1,4}})\s*[/\-–—]\s*(\d{{1,4}}[.,]\d{{1,4}})",
        re.DOTALL,
    )
    for code in CURRENCIES
}
SINGLE_CODE_PATTERNS = {
    code: re.compile(rf"\b{code}\b[^0-9]{{0,12}}(\d{{1,4}}[.,]\d{{1,2}})", re.DOTALL)
    for code in CURRENCIES
}
SINGLE_SYMBOL_PATTERNS = {
    "EUR": re.compile(r"[€💸💶]\s*€?\s*[—–-]?\s*(\d{1,4}[.,]\d{1,2})"),
}


def today_str():
    return datetime.now(KYIV_TZ).strftime("%d.%m.%Y")


def wait_for_slot(sent_today):
    """Align this run to a full hour in Kyiv time.

    The cron schedule fires early on purpose, because GitHub delays scheduled
    workflows by tens of minutes. Returns False if this run should just exit.
    """
    now = datetime.now(KYIV_TZ)
    first = now.replace(hour=FIRST_HOUR, minute=0, second=0, microsecond=0)
    last = now.replace(hour=LAST_HOUR, minute=0, second=0, microsecond=0)

    if now < first:
        wait = (first - now).total_seconds()
        if wait > MAX_WAIT_MIN * 60:
            log.info("Too early for %02d:00 Kyiv (%.0f min to wait), skipping run",
                     FIRST_HOUR, wait / 60)
            return False
        log.info("Waiting %.0f min until %02d:00 Kyiv", wait / 60, FIRST_HOUR)
        time.sleep(wait)
        return True

    if now > last + timedelta(minutes=SLOT_GRACE_MIN):
        log.info("Outside the publishing window (%s Kyiv), skipping run",
                 now.strftime("%H:%M"))
        return False

    if not sent_today:
        # Morning table still missing - publish as soon as possible.
        log.info("Morning table not sent yet, running now (%s Kyiv)",
                 now.strftime("%H:%M"))
        return True

    if now.minute <= SLOT_GRACE_MIN:
        log.info("Within grace of the %02d:00 slot, running now", now.hour)
        return True

    nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    if nxt > last:
        log.info("No slot left today, skipping run")
        return False
    wait = (nxt - now).total_seconds()
    if wait > MAX_WAIT_MIN * 60:
        log.info("Next slot %02d:00 too far away, skipping run", nxt.hour)
        return False
    log.info("Waiting %.0f min until %02d:00 Kyiv", wait / 60, nxt.hour)
    time.sleep(wait)
    return True


def _request_with_retry(method, url, **kwargs):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF ** attempt
                log.warning("Attempt %d/%d failed for %s: %s. Retrying in %ds...",
                            attempt, MAX_RETRIES, url, exc, wait)
                time.sleep(wait)
    log.error("All %d attempts failed for %s", MAX_RETRIES, url)
    raise last_exc


def load_history():
    path = HISTORY_FILE
    if not os.path.exists(path):
        # First start with DATA_DIR pointing at an empty volume: fall back to the
        # copy shipped in the repo so the buttons show a table straight away.
        seed = "rates_history.json"
        if os.path.abspath(seed) != os.path.abspath(path) and os.path.exists(seed):
            log.info("No history in DATA_DIR yet, seeding from %s", seed)
            path = seed
        else:
            log.info("No history file found, starting fresh")
            return {"version": 3, "channels": {}, "nbu": {}, "meta": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "channels" not in data:
            log.info("Legacy history format detected, starting fresh")
            return {"version": 3, "channels": {}, "nbu": {}, "meta": {}}
        data.setdefault("nbu", {})
        data.setdefault("channels", {})
        data.setdefault("meta", {})
        data["version"] = 3
        log.info("Loaded history: %d channels", len(data["channels"]))
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read history file, starting fresh: %s", exc)
        return {"version": 3, "channels": {}, "nbu": {}, "meta": {}}


def save_history(history):
    tmp_path = None
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE) or ".", exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(HISTORY_FILE) or ".",
            prefix=".rates_history_", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, HISTORY_FILE)
        log.info("History saved successfully")
    except OSError as exc:
        log.error("Failed to save history: %s", exc)
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def load_subscribers():
    """Return private chats that opted in to the once-a-day rate message."""
    if not os.path.exists(SUBSCRIBERS_FILE):
        return {"version": 1, "subscribers": {}}
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("subscriber data must be an object")
        data.setdefault("version", 1)
        data.setdefault("subscribers", {})
        return data
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log.error("Failed to load subscribers: %s", exc)
        return {"version": 1, "subscribers": {}}


def save_subscribers(data):
    """Atomically persist daily-subscription state."""
    os.makedirs(os.path.dirname(SUBSCRIBERS_FILE) or ".", exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(SUBSCRIBERS_FILE) or ".",
        prefix=".subscribers_", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, SUBSCRIBERS_FILE)
    except OSError:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def set_subscription(chat_id, enabled):
    """Add or remove one private chat from the daily delivery list."""
    with _SUBSCRIBERS_LOCK:
        data = load_subscribers()
        subscribers = data["subscribers"]
        key = str(chat_id)
        if enabled:
            entry = subscribers.setdefault(key, {})
            # Clear the stamp so a fresh subscriber gets today's table.
            entry.pop("last_sent_date", None)
        else:
            subscribers.pop(key, None)
        save_subscribers(data)
        return enabled


def load_channels():
    try:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        channels = config.get("channels", [])
        if not channels:
            log.error("No channels defined in %s", CHANNELS_FILE)
        return channels
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load %s: %s", CHANNELS_FILE, exc)
        return []


def _valid_rate(value):
    return RATE_MIN <= value <= RATE_MAX


def parse_pair(text, code):
    match = PAIR_PATTERNS[code].search(text)
    if not match:
        return None
    sell = float(match.group(2).replace(",", "."))
    if not _valid_rate(sell):
        log.warning("Suspicious %s sell rate %.2f ignored", code, sell)
        return None
    return sell


def parse_single(text, code):
    match = SINGLE_SYMBOL_PATTERNS[code].search(text)
    if match and _valid_rate(float(match.group(1).replace(",", "."))):
        return float(match.group(1).replace(",", "."))
    match = SINGLE_CODE_PATTERNS[code].search(text)
    if match:
        value = float(match.group(1).replace(",", "."))
        if _valid_rate(value):
            return value
        log.warning("Suspicious %s single rate %.2f ignored", code, value)
    return None


def parse_message_rates(text):
    """Return {code: {'sell': x} or {'rate': x}} found in one message."""
    result = {}
    for code in CURRENCIES:
        sell = parse_pair(text, code)
        if sell is not None:
            result[code] = {"sell": sell}
            continue
        single = parse_single(text, code)
        if single is not None:
            result[code] = {"rate": single}
    return result


def _message_date(block, username):
    time_tag = block.find("time")
    if time_tag and time_tag.get("datetime"):
        try:
            posted = datetime.fromisoformat(time_tag["datetime"]).astimezone(KYIV_TZ)
            return posted.strftime("%d.%m.%Y")
        except ValueError:
            log.warning("[%s] Unparseable message timestamp", username)
    return None


def fetch_channel_rates(username):
    """Scrape public preview of a channel; returns ({code: entry}, {code: date_str})."""
    url = f"https://t.me/s/{username}"
    try:
        resp = _request_with_retry(
            "GET",
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
    except requests.RequestException as exc:
        log.error("[%s] Failed to fetch channel: %s", username, exc)
        return {}, {}

    soup = BeautifulSoup(resp.text, "html.parser")
    blocks = soup.find_all("div", class_="tgme_widget_message")

    rates, dates = {}, {}
    for block in reversed(blocks):
        text_div = block.find("div", class_="tgme_widget_message_text")
        if text_div is None:
            continue
        text = text_div.get_text("\n")
        found = parse_message_rates(text)
        date_str = _message_date(block, username)

        for code, entry in found.items():
            if code not in rates:
                rates[code] = entry
                dates[code] = date_str or today_str()

    if rates:
        summary = ", ".join(
            f"{code} " + (
                f"{e['sell']:.2f}" if "sell" in e else f"{e['rate']:.2f}"
            )
            for code, e in sorted(rates.items())
        )
        log.info("[%s] Rates: %s (dates: %s)", username, summary, dates)
    else:
        log.warning("[%s] No EUR rates found in recent messages", username)
    return rates, dates


_OCR_ENGINE = None
_OCR_FAILED = False


def _get_ocr_engine():
    global _OCR_ENGINE, _OCR_FAILED
    if _OCR_ENGINE is None and not _OCR_FAILED:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_ENGINE = RapidOCR()
        except ImportError:
            log.warning("rapidocr-onnxruntime is not installed, photo channels disabled")
            _OCR_FAILED = True
    return _OCR_ENGINE


def _ocr_image(image_bytes):
    """Run OCR on image bytes, return list of recognized text strings."""
    engine = _get_ocr_engine()
    if engine is None:
        return []
    try:
        import cv2
        import numpy as np
        arr = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return []
        result, _ = engine(arr)
        return [item[1] for item in (result or [])]
    except Exception as exc:
        log.warning("OCR failed: %s", exc)
        return []


def fetch_channel_photo_rates(username, nbu_rates):
    """Channel posts rates as images: download newest photos, OCR numbers."""
    url = f"https://t.me/s/{username}"
    try:
        resp = _request_with_retry(
            "GET",
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
    except requests.RequestException as exc:
        log.error("[%s] Failed to fetch channel: %s", username, exc)
        return {}, {}

    soup = BeautifulSoup(resp.text, "html.parser")
    blocks = soup.find_all("div", class_="tgme_widget_message")

    nbu_eur = (nbu_rates.get("EUR") or {}).get("rate")
    rates, dates = {}, {}
    images_done = 0
    for block in reversed(blocks):
        if images_done >= PHOTO_OCR_MAX_IMAGES:
            break
        photos = block.find_all("a", class_="tgme_widget_message_photo_wrap")
        if not photos:
            continue
        date_str = _message_date(block, username)

        for photo in photos:
            match = re.search(r"url\('([^']+)'\)", photo.get("style", ""))
            if not match:
                continue
            try:
                img_resp = requests.get(match.group(1), timeout=30,
                                        headers={"User-Agent": "Mozilla/5.0"})
                img_resp.raise_for_status()
            except requests.RequestException as exc:
                log.warning("[%s] Failed to download photo: %s", username, exc)
                continue

            texts = _ocr_image(img_resp.content)
            images_done += 1
            numbers = []
            for t in texts:
                numbers.extend(re.findall(r"\d{2}[.,]\d{1,2}", t))

            has_usd = "$" in " ".join(texts) or "usd" in " ".join(texts).lower()
            for num in numbers:
                value = float(num.replace(",", "."))
                if not _valid_rate(value):
                    continue
                if nbu_eur and abs(value - nbu_eur) / nbu_eur > PHOTO_RATE_MAX_DEVIATION:
                    log.warning("[%s] OCR value %.2f too far from NBU EUR, skipping",
                                username, value)
                    continue
                if "EUR" in rates:
                    continue
                if has_usd and not any("€" in t or "eur" in t.lower() for t in texts):
                    continue
                rates["EUR"] = {"rate": value}
                dates["EUR"] = date_str or today_str()
                log.info("[%s] OCR EUR: %s=%.2f (posted %s)", username, num, value, date_str)

        if "EUR" in rates:
            break

    if not rates:
        log.warning("[%s] No rates recognized from photos", username)
    return rates, dates


def compute_formula_rates(formula, nbu_rates):
    """Compute channel rates from a formula like 'nbu+0.5%' applied to NBU rates."""
    match = re.fullmatch(r"\s*nbu\s*\+\s*([\d.]+)\s*%\s*", formula)
    if not match:
        log.error("Unsupported formula: %s", formula)
        return {}, {}
    factor = 1.0 + float(match.group(1)) / 100.0
    rates, dates = {}, {}
    for code in CURRENCIES:
        if code in nbu_rates:
            rates[code] = {"rate": round(nbu_rates[code]["rate"] * factor, 2)}
            dates[code] = nbu_rates[code].get("date") or today_str()
            log.info("Formula %s: %s=%.2f", formula, code, rates[code]["rate"])
    return rates, dates


def fetch_nbu_rates():
    """Official NBU reference rate."""
    url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
    try:
        resp = _request_with_retry("GET", url, timeout=15)
        data = resp.json()
    except requests.RequestException as exc:
        log.error("Failed to fetch NBU rates: %s", exc)
        return {}
    except json.JSONDecodeError as exc:
        log.error("Invalid JSON from NBU API: %s", exc)
        return {}

    result = {}
    for item in data:
        if item.get("cc") in CURRENCIES:
            rate = item["rate"]
            if not _valid_rate(rate):
                log.warning("Suspicious NBU %s rate: %.4f", item["cc"], rate)
            result[item["cc"]] = {
                "rate": rate,
                "date": item.get("exchangedate") or today_str(),
            }
            log.info("NBU %s rate: %.4f (%s)", item["cc"], rate, result[item["cc"]]["date"])
    return result


def _entry_value(entry):
    return entry.get("sell", entry.get("rate"))


def fmt_change(curr, prev):
    if prev is None:
        return ""
    diff = curr - prev
    if abs(diff) < RATE_TOLERANCE:
        return ""
    arrow = "📈" if diff > 0 else "📉"
    return f" {arrow}{diff:+.2f}"


def fmt_entry(entry, prev_entry, entry_date):
    """Format one currency entry: value + change marker + laconic update date."""
    value = _entry_value(entry)
    prev_value = _entry_value(prev_entry) if prev_entry else None
    line = f"{value:.2f}{fmt_change(value, prev_value)}"
    if entry_date and entry_date != today_str():
        line += f" (від {entry_date})"
    return line


def build_message(channels, channel_results, nbu, prev_history, report_date=None,
                  notes=None):
    lines = [f"📊 Курс EUR (продаж) на {report_date or today_str()}", ""]

    if "EUR" in nbu:
        nbu_eur = nbu["EUR"]
        date = nbu_eur.get("date") or today_str()
        prev_rate = (prev_history.get("nbu", {}).get("EUR") or {}).get("rate")
        mark = "" if date == today_str() else f" (від {date})"
        lines.append(
            f'🏛 <a href="{NBU_LINK}">НБУ</a>: {nbu_eur["rate"]:.2f}'
            f"{fmt_change(nbu_eur['rate'], prev_rate)}{mark}"
        )
        lines.append("")

    for channel in channels:
        username = channel["username"]
        name = channel.get("name", username)
        link = channel.get("url", f"https://t.me/{username}")
        rates, dates = channel_results.get(username, ({}, {}))
        prev = prev_history.get("channels", {}).get(username, {})

        lines.append(f'<a href="{link}">{name}</a>')
        # notes[username] wins: a formula channel that posted its own rate must
        # not still claim "курс = НБУ +0,5%".
        note = (notes or {}).get(username, channel.get("note"))
        if note and rates:
            lines.append(f"<i>{note}</i>")
        if not rates:
            lines.append("❌ немає даних")
        elif "EUR" in rates:
            lines.append(fmt_entry(rates["EUR"], prev.get("EUR"), dates.get("EUR")))
        lines.append("")

    return "\n".join(lines).rstrip()


def build_cached_message(history=None):
    """Build a table from the latest saved scan without scraping again.

    This is deliberately fast enough for a Telegram button press.  The
    background rate updater remains the single source that fetches channels.
    """
    history = history or load_history()
    channels = load_channels()
    channel_results = {}
    for channel in channels:
        username = channel["username"]
        stored = history.get("channels", {}).get(username, {})
        rates = {}
        dates = {}
        for code, entry in stored.items():
            rates[code] = {key: value for key, value in entry.items() if key != "date"}
            dates[code] = entry.get("date")
        channel_results[username] = (rates, dates)

    meta = history.get("meta", {})
    report_date = meta.get("last_sent_date") or today_str()
    message = build_message(
        channels,
        channel_results,
        history.get("nbu", {}),
        {},
        report_date=report_date,
        notes=meta.get("notes"),
    )
    updated_at = meta.get("last_updated_at")
    if updated_at:
        try:
            updated = datetime.fromisoformat(updated_at).astimezone(KYIV_TZ)
            message += f"\n\n<i>Останнє оновлення: {updated:%d.%m.%Y %H:%M} (Київ)</i>"
        except ValueError:
            pass
    return message


def detect_changes(prev_history, new_history, channels):
    """Compare two history states.

    Returns (changes, refreshed):
      changes   - rate moves worth a separate "Оновлення курсів" post;
      refreshed - the table text needs a refresh even without a rate move,
                  e.g. a rate shown as "(від 25.08)" is now confirmed by
                  today's post and that mark has to disappear.
    """
    changes = []
    refreshed = False
    names = {c["username"]: c.get("name", c["username"]) for c in channels}
    had_history = bool(prev_history.get("channels"))

    for username, stored in new_history.get("channels", {}).items():
        prev = prev_history.get("channels", {}).get(username, {})
        for code, entry in stored.items():
            new_value = _entry_value(entry)
            prev_entry = prev.get(code) or {}
            old_value = _entry_value(prev_entry)
            name = names.get(username, username)
            if old_value is None:
                if had_history:
                    changes.append(f"• {name}: з'явився курс {new_value:.2f}")
                refreshed = True
            elif abs(new_value - old_value) >= RATE_TOLERANCE:
                arrow = "📈" if new_value > old_value else "📉"
                changes.append(
                    f"• {name}: {old_value:.2f} → {new_value:.2f} {arrow}{new_value - old_value:+.2f}"
                )
                refreshed = True
            elif entry.get("date") != prev_entry.get("date"):
                log.info("[%s] %s unchanged at %.2f, freshness %s -> %s",
                         username, code, new_value,
                         prev_entry.get("date"), entry.get("date"))
                refreshed = True

    for code, entry in new_history.get("nbu", {}).items():
        prev_entry = prev_history.get("nbu", {}).get(code) or {}
        old_rate = prev_entry.get("rate")
        if old_rate is None:
            refreshed = True
        elif abs(entry["rate"] - old_rate) >= RATE_TOLERANCE:
            arrow = "📈" if entry["rate"] > old_rate else "📉"
            changes.append(
                f"• НБУ: {old_rate:.2f} → {entry['rate']:.2f} "
                f"{arrow}{entry['rate'] - old_rate:+.2f}"
            )
            refreshed = True
        elif entry.get("date") != prev_entry.get("date"):
            refreshed = True

    return changes, refreshed


def send_telegram_message(text, chat_id=None):
    """Send message, return its message_id (or None)."""
    target_chat_id = chat_id or CHAT_ID
    if not BOT_TOKEN or not target_chat_id:
        raise RuntimeError("BOT_TOKEN and target chat ID must be configured")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = _request_with_retry(
        "POST",
        url,
        data={
            "chat_id": target_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    body = resp.json()
    if not body.get("ok"):
        log.error("Telegram API error: %s", body.get("description", "?"))
        raise RuntimeError(f"Telegram API error: {body.get('description')}")
    log.info("Telegram message sent successfully")
    return (body.get("result") or {}).get("message_id")


def send_daily_subscriptions(text):
    """Deliver the current table once per day to every opted-in private chat.

    A blocked bot (Telegram 403) is removed automatically, so a failed chat
    cannot stop delivery to other subscribers.
    """
    with _SUBSCRIBERS_LOCK:
        data = load_subscribers()
        subscribers = data.get("subscribers", {})
        pending = [cid for cid, sub in subscribers.items()
                   if sub.get("last_sent_date") != today_str()]
    if not pending:
        return
    changed = False
    delivered = 0
    for chat_id, subscriber in list(subscribers.items()):
        if subscriber.get("last_sent_date") == today_str():
            continue
        try:
            send_telegram_message("🔔 Щоденний курс\n\n" + text, chat_id=chat_id)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            message = str(exc).lower()
            if "blocked" in message or "chat not found" in message or "403" in message:
                log.info("Removing unavailable subscriber %s", chat_id)
                subscribers.pop(chat_id, None)
                changed = True
            else:
                log.warning("Could not deliver daily rate to %s: %s", chat_id, exc)
            continue
        subscriber["last_sent_date"] = today_str()
        changed = True
        delivered += 1
    if changed:
        with _SUBSCRIBERS_LOCK:
            save_subscribers(data)
    if delivered:
        log.info("Daily rate delivered to %d subscriber(s)", delivered)


def edit_telegram_message(message_id, text):
    """Edit previously sent message. Returns True on success."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    try:
        resp = _request_with_retry(
            "POST",
            url,
            data={
                "chat_id": CHAT_ID,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        body = resp.json()
        if body.get("ok"):
            log.info("Telegram message %s edited successfully", message_id)
            return True
        log.warning("Edit failed for message %s: %s", message_id,
                    body.get("description", "?"))
    except (requests.RequestException, ValueError) as exc:
        log.warning("Edit failed for message %s: %s", message_id, exc)
    return False


def merge_history(history, channels, channel_results, nbu):
    new_history = {
        "version": 3,
        "channels": dict(history.get("channels", {})),
        "nbu": dict(history.get("nbu", {})),
        "meta": dict(history.get("meta", {})),
    }
    for channel in channels:
        username = channel["username"]
        rates, dates = channel_results.get(username, ({}, {}))
        stored = {
            code: entry
            for code, entry in new_history["channels"].get(username, {}).items()
            if code in CURRENCIES
        }
        if not rates:
            if stored:
                new_history["channels"][username] = stored
            continue
        for code, entry in rates.items():
            stored[code] = dict(entry)
            stored[code]["date"] = dates.get(code) or today_str()
        new_history["channels"][username] = stored
    new_history["nbu"] = {
        code: entry
        for code, entry in new_history["nbu"].items()
        if code in CURRENCIES
    }
    for code, nbu_entry in nbu.items():
        new_history["nbu"][code] = {
            "rate": nbu_entry["rate"],
            "date": nbu_entry.get("date") or today_str(),
        }
    return new_history


def main(dry_run=False, no_wait=False, force=False):
    log.info("Starting currency rates bot")

    channels = load_channels()
    if not channels:
        raise SystemExit(1)
    history = load_history()

    if not dry_run and not no_wait and not force:
        sent_today = history.get("meta", {}).get("last_sent_date") == today_str()
        if not wait_for_slot(sent_today):
            return

    nbu = fetch_nbu_rates()
    channel_results = {}
    notes = {}
    for channel in channels:
        username = channel["username"]
        mode = channel.get("mode", "text")
        notes[username] = channel.get("note")
        if mode == "formula":
            rates, dates = fetch_channel_rates(username)
            if rates:
                notes[username] = None
                log.info("[%s] Own rates found, formula not used", username)
            else:
                rates, dates = compute_formula_rates(channel.get("formula", ""), nbu)
        elif mode == "photo":
            rates, dates = fetch_channel_photo_rates(username, nbu)
        else:
            rates, dates = fetch_channel_rates(username)
        channel_results[username] = (rates, dates)
        time.sleep(CHANNEL_FETCH_PAUSE)

    new_history = merge_history(history, channels, channel_results, nbu)
    changes, refreshed = detect_changes(history, new_history, channels)

    if dry_run:
        print("=" * 50)
        print(build_message(channels, channel_results, nbu, history, notes=notes))
        print("-" * 50)
        print("Changes:", changes if changes else "none")
        print("Table needs refresh:", refreshed)
        print("=" * 50)
        log.info("Dry run finished, message not sent")
        return

    meta = dict(history.get("meta", {}))
    sent_today = meta.get("last_sent_date") == today_str()
    meta["last_updated_at"] = datetime.now(KYIV_TZ).isoformat()
    meta["notes"] = notes
    new_history["meta"] = meta

    table = build_message(channels, channel_results, nbu, history, notes=notes)

    if not AUTO_POST_STEELON and not force:
        log.info("AUTO_POST_STEELON is off: refreshed data only, channel untouched")
        save_history(new_history)
        send_daily_subscriptions(table)
        return

    if sent_today and not changes and not refreshed and not force:
        log.info("Nothing changed since last send, staying silent")
        save_history(new_history)
        send_daily_subscriptions(table)
        return

    can_edit = (
        not force
        and sent_today
        and meta.get("message_id")
        and str(meta.get("chat_id")) == str(CHAT_ID)
    )
    if can_edit:
        if edit_telegram_message(meta["message_id"], table):
            if changes:
                send_telegram_message("🔄 Оновлення курсів:\n" + "\n".join(changes))
        else:
            meta["message_id"] = send_telegram_message(table)
    else:
        if force:
            log.info("Force mode: sending a new table instead of editing")
        meta["message_id"] = send_telegram_message(table)

    meta["chat_id"] = CHAT_ID
    meta["last_sent_date"] = today_str()
    new_history["meta"] = meta
    save_history(new_history)
    send_daily_subscriptions(table)
    log.info("Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EUR rates to Telegram")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and print the message without sending or saving")
    parser.add_argument("--no-wait", action="store_true",
                        help="skip waiting for the next full hour (manual runs)")
    parser.add_argument("--force", action="store_true",
                        help="always post a new table, even without changes")
    args = parser.parse_args()
    try:
        main(dry_run=args.dry_run, no_wait=args.no_wait, force=args.force)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("Unhandled exception: %s", exc)
        raise
