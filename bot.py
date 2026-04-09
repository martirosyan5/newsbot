"""
Forex News Telegram Bot — scrapes ForexFactory via cloudscraper
"""

import os
import re
import html
import json
import logging
import asyncio
import schedule
import time
import cloudscraper
from collections import Counter
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode

load_dotenv()

# ──────────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

IMPACT_FILTER = {"High", "Medium"}
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "NZD"}
ALERT_MINUTES_BEFORE = [60, 15]
DISPLAY_TZ = ZoneInfo("Europe/Berlin")

# ──────────────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
#  STATE
# ──────────────────────────────────────────────────────────────

daily_events: list = []
alerted_keys: set = set()
IMPACT_EMOJI = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}

# ──────────────────────────────────────────────────────────────
#  SCRAPE FOREXFACTORY
# ──────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_events() -> list:
    try:
        scraper = cloudscraper.create_scraper()
        today_str = datetime.now(DISPLAY_TZ).strftime("%b%d.%Y").lower()
        url = f"https://www.forexfactory.com/calendar?day={today_str}"
        log.info(f"Scraping: {url}")

        r = scraper.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()

        with open("ff_debug.html", "w", encoding="utf-8") as f:
            f.write(r.text)

        events = parse_calendar_html(r.text)
        log.info(f"Scraped {len(events)} raw events")
        return events

    except Exception as e:
        log.error(f"Scrape error: {e}")
        return []


def parse_calendar_html(html_text: str) -> list:
    soup = BeautifulSoup(html_text, "lxml")

    table = soup.find("table", class_=re.compile("calendar__table"))
    if not table:
        log.error("❌ calendar__table not found")
        return []

    return parse_calendar_table(table)


def parse_calendar_table(table) -> list:
    events = []
    last_time = None
    today_str = datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d")

    for row in table.find_all("tr", class_=re.compile("calendar__row")):
        if "calendar__row--day-breaker" in row.get("class", []):
            continue


        time_cell = row.find("td", class_="calendar__time")
        if time_cell:
            t = time_cell.get_text(strip=True)
            if t and t.lower() not in ("", "all day", "tentative"):
                last_time = t


        cur_cell = row.find("td", class_="calendar__currency")
        currency = cur_cell.get_text(strip=True) if cur_cell else ""


        impact_cell = row.find("td", class_="calendar__impact")
        impact = detect_impact(impact_cell)


        name_cell = row.find("td", class_="calendar__event")
        name = name_cell.get_text(strip=True) if name_cell else ""
        if not name:
            continue


        def cell_text(cls):
            cell = row.find("td", class_=cls)
            return cell.get_text(strip=True) if cell else ""


        event_dt = parse_time(last_time, today_str)

        log.info(f"Parsed raw time: {last_time} -> {event_dt}")

        events.append({
            "name": name,
            "currency": currency,
            "impact": impact,
            "time_str": last_time or "",
            "datetime": event_dt,
            "forecast": cell_text("calendar__forecast"),
            "previous": cell_text("calendar__previous"),
            "actual": cell_text("calendar__actual"),
        })

    return events


def parse_calendar_state_from_js(html_text: str) -> list:
    match = re.search(
        r"window\.calendarComponentStates\[\d+\]\s*=\s*(\{.*?\})\s*;</script>",
        html_text,
        re.DOTALL,
    )
    if not match:
        return []

    raw_state = match.group(1)

    try:
        state = json.loads(raw_state)
    except json.JSONDecodeError as e:
        log.warning(f"Failed to parse JS calendar state: {e}")
        return []

    events = []
    today_str = datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d")

    for day in state.get("days", []):
        for item in day.get("events", []):
            time_label = (item.get("timeLabel") or "").strip()
            if time_label.lower() in {"tentative", "all day"}:
                time_label = ""

            impact_name = (item.get("impactName") or "").strip().lower()
            impact = impact_name.capitalize() if impact_name in {"high", "medium", "low"} else "Low"

            event_dt = parse_time(time_label, today_str)

            events.append(
                {
                    "name": item.get("name", "").strip(),
                    "currency": item.get("currency", "").strip(),
                    "impact": impact,
                    "time_str": time_label,
                    "datetime": event_dt,
                    "forecast": (item.get("forecast") or "").strip(),
                    "previous": (item.get("previous") or "").strip(),
                    "actual": (item.get("actual") or "").strip(),
                }
            )

    return [e for e in events if e["name"]]


def detect_impact(impact_cell) -> str:
    if not impact_cell:
        return "Low"

    span = impact_cell.find("span")
    if not span:
        return "Low"

    classes = " ".join(span.get("class", [])).lower()
    title = (span.get("title") or "").lower()
    combined = f"{classes} {title}"

    if "impact-red" in combined or "high" in combined:
        return "High"
    if "impact-ora" in combined or "medium" in combined:
        return "Medium"
    if "impact-yel" in combined or "low" in combined:
        return "Low"

    return "Low"


def parse_time(time_str: Optional[str], date_str: str) -> Optional[datetime]:
    if not time_str:
        return None

    try:
        dt_str = f"{date_str} {time_str.upper()}"

        for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%d %I%p"):
            try:
                naive = datetime.strptime(dt_str, fmt)

                # 👉 ВАЖНО: ForexFactory уже даёт Berlin time
                local_dt = naive.replace(tzinfo=DISPLAY_TZ)

                return local_dt.astimezone(timezone.utc)

            except ValueError:
                continue

    except Exception as e:
        log.debug(f"Time parse failed '{time_str}': {e}")

    return None


def filter_events(events: list) -> list:
    filtered = [
        e for e in events if e["impact"] in IMPACT_FILTER and e["currency"] in CURRENCIES
    ]

    return sorted(
        filtered,
        key=lambda e: e["datetime"] if e["datetime"] is not None else datetime.max.replace(tzinfo=timezone.utc),
    )


# ──────────────────────────────────────────────────────────────
#  FORMATTING
# ──────────────────────────────────────────────────────────────

def fmt_digest(events: list) -> str:
    if not events:
        return "📭 <b>No significant forex events today.</b> Easy session ahead."

    now_local = datetime.now(DISPLAY_TZ)
    lines = [
        f"📅 <b>ECONOMIC CALENDAR — {html.escape(now_local.strftime('%A %d %B %Y').upper())}</b>",
        f"<i>{html.escape(now_local.strftime('%Z (UTC%z)'))}</i>",
        "",
    ]

    for level in ["High", "Medium", "Low"]:
        group = [e for e in events if e["impact"] == level]
        if not group:
            continue

        lines.append(f"{IMPACT_EMOJI.get(level, '⚪')} <b>{level} Impact</b>")
        for e in group:
            ev_dt = e["datetime"]
            t_str = ev_dt.astimezone(DISPLAY_TZ).strftime("%H:%M") if ev_dt else "?"
            lines.append(f"  • <code>{html.escape(t_str)}</code> <code>{html.escape(e['currency'])}</code> {html.escape(e['name'])}")
            lines.append(
                f"    <i>Fcst: {html.escape(e['forecast'] or '—')} · Prev: {html.escape(e['previous'] or '—')}</i>"
            )
        lines.append("")

    lines.append("<i>Good luck out there Vendetta Folks!</i> 🎯")
    return "\n".join(lines)


def fmt_alert(event: dict, minutes_before: int) -> str:
    impact = event["impact"]
    ev_dt = event["datetime"]
    t_str = ev_dt.astimezone(DISPLAY_TZ).strftime("%H:%M %Z") if ev_dt else "?"
    timing = "🚨 <b>HAPPENING NOW</b>" if minutes_before == 0 else f"⏰ <b>In {minutes_before} minutes</b>"

    return (
        f"{IMPACT_EMOJI.get(impact, '⚪')} <b>{html.escape(impact.upper())} IMPACT — {html.escape(event['currency'])}</b>\n"
        f"{timing}\n\n"
        f"📌 <b>{html.escape(event['name'])}</b>\n"
        f"🕐 <code>{html.escape(t_str)}</code>\n"
        f"📈 Forecast: <code>{html.escape(event['forecast'] or 'N/A')}</code>\n"
        f"📉 Previous: <code>{html.escape(event['previous'] or 'N/A')}</code>\n\n"
        f"<i>Volatility incoming — manage your risk!</i> 📊"
    )


# ──────────────────────────────────────────────────────────────
#  TELEGRAM
# ──────────────────────────────────────────────────────────────

async def _send(text: str):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.HTML)


def send(text: str):
    try:
        asyncio.run(_send(text))
        log.info("✅ Message sent to Telegram")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ──────────────────────────────────────────────────────────────
#  SCHEDULER
# ──────────────────────────────────────────────────────────────

def job_refresh():
    global daily_events, alerted_keys

    alerted_keys.clear()
    raw_events = fetch_events()
    daily_events = filter_events(raw_events)

    impact_counts = Counter(e["impact"] for e in raw_events)
    log.info(f"Impact distribution: {dict(impact_counts)}")
    log.info(f"Filtered events count: {len(daily_events)}")

    for e in daily_events[:5]:
        t_str = e["datetime"].astimezone(DISPLAY_TZ).strftime("%H:%M") if e["datetime"] else "?"
        log.info(f"Kept: {t_str} {e['currency']} {e['impact']} | {e['name']}")

    send(fmt_digest(daily_events))


def job_check_alerts():
    now = datetime.now(timezone.utc)

    for event in daily_events:
        ev_dt = event.get("datetime")
        if not ev_dt:
            continue

        minutes_until = (ev_dt - now).total_seconds() / 60

        for threshold in ALERT_MINUTES_BEFORE:
            if abs(minutes_until - threshold) < 0.75:
                key = f"{event['name']}_{ev_dt.isoformat()}_{threshold}"
                if key not in alerted_keys:
                    alerted_keys.add(key)
                    log.info(f"🔔 Alert: {event['name']} in {threshold}min")
                    send(fmt_alert(event, threshold))


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────

def main():
    log.info("🤖 Forex News Bot starting (ForexFactory + cloudscraper)...")
    log.info(f"  Chat ID      : {TELEGRAM_CHAT_ID}")
    log.info(f"  Impact filter: {IMPACT_FILTER}")
    log.info(f"  Currencies   : {CURRENCIES}")
    log.info(f"  Alert windows: {ALERT_MINUTES_BEFORE} min before")
    log.info(f"  Display TZ   : {DISPLAY_TZ}")

    job_refresh()

    schedule.every().day.at("06:00").do(job_refresh)
    schedule.every().day.at("12:00").do(job_refresh)
    schedule.every(1).minutes.do(job_check_alerts)

    log.info("Scheduler running. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
