import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

KYIV_TZ = timezone(timedelta(hours=3))
HISTORY_FILE = "rates_history.json"
CURRENCIES = ["USD", "EUR"]

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# Public preview of the exchange's Telegram channel - no login needed,
# plain HTML, no JavaScript rendering required.
TRUEEXCHANGE_CHANNEL_URL = "https://t.me/s/TrueExchange_IFUA"


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def parse_rate_pair(text, code):
    """Find 'USD 44.65/44.80' style pairs for a given currency code."""
    pattern = rf"{code}\D{{0,20}}?([\d]+[.,]\d+)\s*/\s*([\d]+[.,]\d+)"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        buy = float(match.group(1).replace(",", "."))
        sell = float(match.group(2).replace(",", "."))
        return buy, sell
    return None


def fetch_trueexchange_rates():
    """Read the latest rates post from the TrueExchange Telegram channel."""
    resp = requests.get(
        TRUEEXCHANGE_CHANNEL_URL,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.find_all("div", class_="tgme_widget_message_text")

    # Walk from the newest message backwards until we find one that has
    # both USD and EUR rates in it.
    for msg in reversed(messages):
        text = msg.get_text("\n")
        usd = parse_rate_pair(text, "USD")
        eur = parse_rate_pair(text, "EUR")
        if usd and eur:
            return {
                "USD": {"buy": usd[0], "sell": usd[1]},
                "EUR": {"buy": eur[0], "sell": eur[1]},
            }
    return {}


def fetch_nbu_rates():
    """Official NBU reference rate for the same currencies."""
    url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
    data = requests.get(url, timeout=15).json()
    result = {}
    for item in data:
        if item.get("cc") in CURRENCIES:
            result[item["cc"]] = item["rate"]
    return result


def fmt_delta(curr, prev):
    if prev is None:
        return ""
    diff = curr - prev
    if abs(diff) < 0.005:
        return " (без змін)"
    arrow = "🔺" if diff > 0 else "🔻"
    return f" ({arrow}{diff:+.2f})"


def build_message(trueexchange, nbu, history):
    today = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
    names = {"USD": "💵 USD/UAH", "EUR": "💶 EUR/UAH"}
    lines = [f"📊 Курс валют на {today}", ""]

    for code in CURRENCIES:
        prev = history.get(code, {})
        lines.append(names[code])

        if code in trueexchange:
            buy, sell = trueexchange[code]["buy"], trueexchange[code]["sell"]
            lines.append(
                f"TrueExchange (Ів.-Франківськ): купівля {buy:.2f}{fmt_delta(buy, prev.get('buy'))}, "
                f"продаж {sell:.2f}{fmt_delta(sell, prev.get('sell'))}"
            )
        else:
            lines.append("TrueExchange: не вдалося отримати курс")

        if code in nbu:
            rate = nbu[code]
            lines.append(f"НБУ (офіційний): {rate:.4f}{fmt_delta(rate, prev.get('nbu'))}")

            if code in trueexchange:
                spread_buy = trueexchange[code]["buy"] - rate
                spread_sell = trueexchange[code]["sell"] - rate
                lines.append(
                    f"Різниця з НБУ: купівля {spread_buy:+.2f}, продаж {spread_sell:+.2f}"
                )

        lines.append("")

    lines.append("Курси оновлюються щодня автоматично.")
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()


def main():
    history = load_history()
    trueexchange = fetch_trueexchange_rates()
    nbu = fetch_nbu_rates()

    message = build_message(trueexchange, nbu, history)
    send_telegram_message(message)

    new_history = {}
    for code in CURRENCIES:
        entry = {}
        if code in trueexchange:
            entry["buy"] = trueexchange[code]["buy"]
            entry["sell"] = trueexchange[code]["sell"]
        if code in nbu:
            entry["nbu"] = nbu[code]
        new_history[code] = entry
    save_history(new_history)


if __name__ == "__main__":
    main()
