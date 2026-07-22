import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from playwright.async_api import async_playwright

KYIV_TZ = timezone(timedelta(hours=3))
HISTORY_FILE = "rates_history.json"
CURRENCIES = ["USD", "EUR"]

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


async def fetch_trueobmin_rates():
    """Render trueobmin.com with a headless browser (rates are loaded via JS)
    and parse buy/sell rates for USD and EUR from the visible text."""
    result = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto("https://trueobmin.com/", wait_until="networkidle", timeout=60000)
        # give the Vue widget extra time to populate the rates table
        await page.wait_for_timeout(4000)
        body_text = await page.inner_text("body")
        await browser.close()

    for code in CURRENCIES:
        pattern = rf"{code}[^\n]*?Куп[іi]вля\s*([\d]+[.,]\d+)[^\n]*?Продаж\s*([\d]+[.,]\d+)"
        match = re.search(pattern, body_text, re.DOTALL | re.IGNORECASE)
        if match:
            buy = float(match.group(1).replace(",", "."))
            sell = float(match.group(2).replace(",", "."))
            result[code] = {"buy": buy, "sell": sell}
    return result


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


def build_message(trueobmin, nbu, history):
    today = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
    names = {"USD": "💵 USD/UAH", "EUR": "💶 EUR/UAH"}
    lines = [f"📊 Курс валют на {today}", ""]

    for code in CURRENCIES:
        prev = history.get(code, {})
        lines.append(names[code])

        if code in trueobmin:
            buy, sell = trueobmin[code]["buy"], trueobmin[code]["sell"]
            lines.append(
                f"TrueExchange (Ів.-Франківськ): купівля {buy:.2f}{fmt_delta(buy, prev.get('buy'))}, "
                f"продаж {sell:.2f}{fmt_delta(sell, prev.get('sell'))}"
            )
        else:
            lines.append("TrueExchange: не вдалося отримати курс")

        if code in nbu:
            rate = nbu[code]
            lines.append(f"НБУ (офіційний): {rate:.4f}{fmt_delta(rate, prev.get('nbu'))}")

            if code in trueobmin:
                spread_buy = trueobmin[code]["buy"] - rate
                spread_sell = trueobmin[code]["sell"] - rate
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


async def main():
    history = load_history()
    trueobmin = await fetch_trueobmin_rates()
    nbu = fetch_nbu_rates()

    message = build_message(trueobmin, nbu, history)
    send_telegram_message(message)

    new_history = {}
    for code in CURRENCIES:
        entry = {}
        if code in trueobmin:
            entry["buy"] = trueobmin[code]["buy"]
            entry["sell"] = trueobmin[code]["sell"]
        if code in nbu:
            entry["nbu"] = nbu[code]
        new_history[code] = entry
    save_history(new_history)


if __name__ == "__main__":
    asyncio.run(main())
