import argparse
import json
import logging
import os
import re
import sys
import tempfile
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

HISTORY_FILE = "rates_history.json"
CHANNELS_FILE = "channels.json"
CURRENCIES = ["USD", "EUR"]
RATE_MIN = 1.0
RATE_MAX = 200.0
MAX_RETRIES = 3
RETRY_BACKOFF = 2
CHANNEL_FETCH_PAUSE = 1.0

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

TRUEEXCHANGE_CHANNEL_URL = "https://t.me/s/TrueExchange_IFUA"
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
    "USD": re.compile(r"[$💵]\s*\$?\s*[—–-]?\s*(\d{1,4}[.,]\d{1,2})"),
    "EUR": re.compile(r"[€💸💶]\s*€?\s*[—–-]?\s*(\d{1,4}[.,]\d{1,2})"),
}


def today_str():
    return datetime.now(KYIV_TZ).strftime("%d.%m.%Y")


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
    if not os.path.exists(HISTORY_FILE):
        log.info("No history file found, starting fresh")
        return {"version": 2, "channels": {}, "nbu": {}}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "channels" not in data:
            log.info("Legacy history format detected, starting fresh")
            return {"version": 2, "channels": {}, "nbu": {}}
        data.setdefault("nbu", {})
        data.setdefault("channels", {})
        data["version"] = 2
        log.info("Loaded history: %d channels", len(data["channels"]))
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to read history file, starting fresh: %s", exc)
        return {"version": 2, "channels": {}, "nbu": {}}


def save_history(history):
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=".", prefix=".rates_history_", suffix=".tmp"
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
    buy = float(match.group(1).replace(",", "."))
    sell = float(match.group(2).replace(",", "."))
    if not (_valid_rate(buy) and _valid_rate(sell)):
        log.warning("Suspicious %s pair %.2f/%.2f ignored", code, buy, sell)
        return None
    return buy, sell


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
    """Return {code: {'buy':..,'sell':..} or {'rate':..}} found in one message."""
    result = {}
    for code in CURRENCIES:
        pair = parse_pair(text, code)
        if pair:
            result[code] = {"buy": pair[0], "sell": pair[1]}
            continue
        single = parse_single(text, code)
        if single is not None:
            result[code] = {"rate": single}
    return result


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

        time_tag = block.find("time")
        posted = None
        if time_tag and time_tag.get("datetime"):
            try:
                posted = datetime.fromisoformat(time_tag["datetime"]).astimezone(KYIV_TZ)
            except ValueError:
                log.warning("[%s] Unparseable message timestamp", username)
        date_str = posted.strftime("%d.%m.%Y") if posted else None

        for code, entry in found.items():
            if code not in rates:
                rates[code] = entry
                dates[code] = date_str or today_str()

    if rates:
        summary = ", ".join(
            f"{code} " + (
                f"{e['buy']:.2f}/{e['sell']:.2f}" if "buy" in e else f"{e['rate']:.2f}"
            )
            for code, e in sorted(rates.items())
        )
        log.info("[%s] Rates: %s (dates: %s)", username, summary, dates)
    else:
        log.warning("[%s] No USD/EUR rates found in recent messages", username)
    return rates, dates


_OCR_ENGINE = None
_OCR_FAILED = False
PHOTO_OCR_MAX_IMAGES = 6


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

    rates, dates = {}, {}
    images_done = 0
    for block in reversed(blocks):
        if images_done >= PHOTO_OCR_MAX_IMAGES:
            break
        photos = block.find_all("a", class_="tgme_widget_message_photo_wrap")
        if not photos:
            continue
        time_tag = block.find("time")
        date_str = None
        if time_tag and time_tag.get("datetime"):
            try:
                posted = datetime.fromisoformat(time_tag["datetime"]).astimezone(KYIV_TZ)
                date_str = posted.strftime("%d.%m.%Y")
            except ValueError:
                pass

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

            symbol_codes = set()
            joined = " ".join(texts).lower()
            if "€" in joined or "eur" in joined or "євро" in joined:
                symbol_codes.add("EUR")
            if "$" in joined or "usd" in joined or "дол" in joined:
                symbol_codes.add("USD")

            for num in numbers:
                value = float(num.replace(",", "."))
                if not _valid_rate(value):
                    continue
                code = next(iter(symbol_codes), None)
                if code is None:
                    code = min(
                        CURRENCIES,
                        key=lambda c: abs(value - nbu_rates.get(c, {}).get("rate", value)),
                    )
                if code in rates:
                    continue
                rates[code] = {"rate": value}
                dates[code] = date_str or today_str()
                log.info("[%s] OCR %s: %s=%.2f (posted %s)", username, code, num, value, date_str)

        if len(rates) == len(CURRENCIES):
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
    """Official NBU reference rate for the same currencies."""
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


def fmt_change(curr, prev):
    if prev is None:
        return ""
    diff = curr - prev
    if abs(diff) < 0.005:
        return " (без змін)"
    arrow = "🔺" if diff > 0 else "🔻"
    return f" {arrow}{diff:+.2f}"


def fmt_entry(entry, prev_entry, entry_date):
    """Format one currency entry with change markers and staleness warning."""
    stale = bool(entry_date) and entry_date != today_str()
    if "buy" in entry:
        delta_buy = fmt_change(entry["buy"], prev_entry.get("buy") if prev_entry else None)
        delta_sell = fmt_change(entry["sell"], prev_entry.get("sell") if prev_entry else None)
        line = f"куп {entry['buy']:.2f}{delta_buy} / прод {entry['sell']:.2f}{delta_sell}"
    else:
        delta = fmt_change(entry["rate"], prev_entry.get("rate") if prev_entry else None)
        line = f"{entry['rate']:.2f}{delta}"
    if stale:
        line += f" ⚠️старий курс (від {entry_date})"
    return line


def build_message(channels, channel_results, nbu, history):
    names = {"USD": "💵 USD", "EUR": "💶 EUR"}
    lines = [f"📊 Курси обмінників на {today_str()}", ""]

    prev_nbu = history.get("nbu", {})
    nbu_parts = []
    for code in CURRENCIES:
        if code in nbu:
            rate = nbu[code]["rate"]
            date = nbu[code].get("date") or today_str()
            prev_rate = (prev_nbu.get(code) or {}).get("rate")
            mark = "" if date == today_str() else f" ⚠️старий курс (від {date})"
            nbu_parts.append(f"{names[code]} {rate:.4f}{fmt_change(rate, prev_rate)}{mark}")
    lines.append(f'🏛 <a href="{NBU_LINK}">НБУ</a>: ' + " | ".join(nbu_parts))
    lines.append("")

    any_stale = False
    for channel in channels:
        username = channel["username"]
        name = channel.get("name", username)
        link = channel.get("url", f"https://t.me/{username}")
        rates, dates = channel_results.get(username, ({}, {}))
        prev = history.get("channels", {}).get(username, {})

        lines.append(f'<a href="{link}">{name}</a>')
        note = channel.get("note")
        if note and rates:
            lines.append(f"<i>{note}</i>")
        if not rates:
            lines.append("❌ не вдалося отримати курс")
        else:
            lines.append("")
        for code in CURRENCIES:
            if code not in rates:
                continue
            entry_line = fmt_entry(rates[code], prev.get(code), dates.get(code))
            if "⚠️" in entry_line:
                any_stale = True
            lines.append(f"{names[code]}: {entry_line}")
            if "buy" in rates[code] and code in nbu:
                spread_buy = rates[code]["buy"] - nbu[code]["rate"]
                spread_sell = rates[code]["sell"] - nbu[code]["rate"]
                lines.append(
                    f"↳ різниця з НБУ: {spread_buy:+.2f} / {spread_sell:+.2f}"
                )
        lines.append("")

    if any_stale:
        lines.append("⚠️ «старий курс» — обмінник не публікував новий курс сьогодні.")
    lines.append("Курси оновлюються щодня автоматично.")
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = _request_with_retry(
            "POST",
            url,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        log.error("Failed to send Telegram message: %s", exc)
        raise

    try:
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError(f"Unexpected response type: {type(body).__name__}")
        if not body.get("ok"):
            error_code = body.get("error_code", "?")
            description = body.get("description", "unknown")
            log.error("Telegram API error %s: %s", error_code, description)
            raise RuntimeError(f"Telegram API error {error_code}: {description}")
    except (ValueError, KeyError) as exc:
        log.warning("Could not parse Telegram response: %s", exc)

    log.info("Telegram message sent successfully")


def merge_history(history, channels, channel_results, nbu):
    new_history = {
        "version": 2,
        "channels": dict(history.get("channels", {})),
        "nbu": dict(history.get("nbu", {})),
    }
    for channel in channels:
        username = channel["username"]
        rates, dates = channel_results.get(username, ({}, {}))
        if not rates:
            continue
        stored = dict(new_history["channels"].get(username, {}))
        for code, entry in rates.items():
            stored[code] = dict(entry)
            stored[code]["date"] = dates.get(code) or today_str()
        new_history["channels"][username] = stored
    for code, nbu_entry in nbu.items():
        new_history["nbu"][code] = {
            "rate": nbu_entry["rate"],
            "date": nbu_entry.get("date") or today_str(),
        }
    return new_history


def main(dry_run=False):
    log.info("Starting currency rates bot")

    channels = load_channels()
    if not channels:
        raise SystemExit(1)
    history = load_history()

    nbu = fetch_nbu_rates()
    channel_results = {}
    for channel in channels:
        username = channel["username"]
        mode = channel.get("mode", "text")
        if mode == "formula":
            rates, dates = compute_formula_rates(channel.get("formula", ""), nbu)
        elif mode == "photo":
            rates, dates = fetch_channel_photo_rates(username, nbu)
        else:
            rates, dates = fetch_channel_rates(username)
        channel_results[username] = (rates, dates)
        time.sleep(CHANNEL_FETCH_PAUSE)

    got_any = any(rates for rates, _ in channel_results.values())
    if not got_any and not nbu:
        log.error("No data at all from any source. Aborting.")
        raise SystemExit(1)

    message = build_message(channels, channel_results, nbu, history)

    if dry_run:
        print("=" * 50)
        print(message)
        print("=" * 50)
        log.info("Dry run finished, message not sent")
        return

    send_telegram_message(message)
    save_history(merge_history(history, channels, channel_results, nbu))
    log.info("Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily currency rates to Telegram")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and print the message without sending or saving")
    args = parser.parse_args()
    try:
        main(dry_run=args.dry_run)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("Unhandled exception: %s", exc)
        raise
