"""
Forex News Telegram Bot — scrapes ForexFactory directly
"""

import os
import re
import logging
import asyncio
import schedule
import time
import requests
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode

load_dotenv()

# ──────────────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

IMPACT_FILTER = {"High", "Medium"}
CURRENCIES    = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "NZD"}
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
alerted_keys: set  = set()
IMPACT_EMOJI = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}

# ──────────────────────────────────────────────────────────────
#  SCRAPE FOREXFACTORY
# ──────────────────────────────────────────────────────────────

FF_URL = "https://www.forexfactory.com/calendar"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.forexfactory.com/",
}


def fetch_events() -> list:
    """Scrape today's ForexFactory calendar page."""
    try:
        session = requests.Session()
        # First hit the homepage to get cookies (FF requires this)
        session.get("https://www.forexfactory.com/", headers=HEADERS, timeout=15)

        today_str = date.today().strftime("%b%d.%Y").lower()  # e.g. "mar26.2026"
        url = f"{FF_URL}?day={today_str}"
        log.info(f"Scraping: {url}")

        r = session.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()

        events = parse_calendar_html(r.text)
        log.info(f"Scraped {len(events)} raw events")
        return events

    except Exception as e:
        log.error(f"Scrape error: {e}")
        return []


def parse_calendar_html(html: str) -> list:
    """Parse ForexFactory calendar HTML into a list of event dicts."""
    soup   = BeautifulSoup(html, "lxml")
    table  = soup.find("table", class_=re.compile("calendar"))
    if not table:
        log.warning("Could not find calendar table in HTML — FF may have changed layout")
        return []

    events    = []
    last_time = None
    today_str = date.today().strftime("%Y-%m-%d")

    for row in table.find_all("tr", class_=re.compile("calendar__row")):
        # Skip header/spacer rows
        if "calendar__row--day-breaker" in row.get("class", []):
            continue

        # Time cell — sometimes empty (inherits from previous row)
        time_cell = row.find("td", class_=re.compile("calendar__time"))
        if time_cell:
            t = time_cell.get_text(strip=True)
            if t and t.lower() not in ("", "all day", "tentative"):
                last_time = t  # e.g. "8:30am"

        # Currency
        cur_cell = row.find("td", class_=re.compile("calendar__currency"))
        currency = cur_cell.get_text(strip=True) if cur_cell else ""

        # Impact — read from the span title or class
        impact_cell = row.find("td", class_=re.compile("calendar__impact"))
        impact = "Low"
        if impact_cell:
            span = impact_cell.find("span")
            if span:
                cls = " ".join(span.get("class", []))
                title = span.get("title", "")
                if "high" in cls.lower() or "high" in title.lower():
                    impact = "High"
                elif "medium" in cls.lower() or "medium" in title.lower():
                    impact = "Medium"
                elif "low" in cls.lower() or "low" in title.lower():
                    impact = "Low"

        # Event name
        name_cell = row.find("td", class_=re.compile("calendar__event"))
        name = name_cell.get_text(strip=True) if name_cell else ""
        if not name:
            continue

        # Forecast / Previous / Actual
        def cell_text(cls_pattern):
            cell = row.find("td", class_=re.compile(cls_pattern))
            return cell.get_text(strip=True) if cell else ""

        forecast = cell_text("calendar__forecast")
        previous = cell_text("calendar__previous")
        actual   = cell_text("calendar__actual")

        # Parse time to UTC datetime
        ev_dt = parse_time(last_time, today_str)

        events.append({
            "name":     name,
            "currency": currency,
            "impact":   impact,
            "time_str": last_time or "",
            "datetime": ev_dt,
            "forecast": forecast,
            "previous": previous,
            "actual":   actual,
        })

    return events


def parse_time(time_str: str, date_str: str) -> datetime:
    """Convert '8:30am' + '2026-03-26' to UTC datetime."""
    if not time_str:
        return None
    try:
        # FF times are US Eastern (ET)
        dt_str = f"{date_str} {time_str.upper()}"
        # Try 12h format
        for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%d %I%p"):
            try:
                dt_et = datetime.strptime(dt_str, fmt)
                # ET = UTC-5 (EST) or UTC-4 (EDT)
                # Use a simple heuristic: Mar–Nov = EDT (UTC-4), else EST (UTC-5)
                month = dt_et.month
                offset = -4 if 3 <= month <= 11 else -5
                dt_utc = dt_et.replace(tzinfo=timezone.utc) - timezone(
                    __import__("datetime").timedelta(hours=offset)
                ).utcoffset(None) + __import__("datetime").timedelta(hours=abs(offset))
                # Simpler: just subtract offset
                from datetime import timedelta
                dt_utc = dt_et.replace(tzinfo=timezone.utc) + timedelta(hours=-offset)
                return dt_utc
            except ValueError:
                continue
    except Exception as e:
        log.debug(f"Time parse failed for '{time_str}': {e}")
    return None


def filter_events(events: list) -> list:
    return [
        e for e in events
        if e["impact"] in IMPACT_FILTER and e["currency"] in CURRENCIES
    ]

# ──────────────────────────────────────────────────────────────
#  FORMATTING
# ──────────────────────────────────────────────────────────────

def fmt_digest(events: list) -> str:
    if not events:
        return "📭 *No significant forex events today.* Easy session ahead."

    now_local = datetime.now(DISPLAY_TZ)
    lines = [
        f"📅 *ECONOMIC CALENDAR — {now_local.strftime('%A %d %B %Y').upper()}*",
        f"_{now_local.strftime('%Z (UTC%z)')}_\n",
    ]

    for level in ["High", "Medium", "Low"]:
        group = [e for e in events if e["impact"] == level]
        if not group:
            continue
        lines.append(f"{IMPACT_EMOJI.get(level, '⚪')} *{level} Impact*")
        for e in group:
            ev_dt = e["datetime"]
            t_str = ev_dt.astimezone(DISPLAY_TZ).strftime("%H:%M") if ev_dt else "?"
            est   = e["forecast"] or "—"
            prev  = e["previous"] or "—"
            lines.append(f"  • `{t_str}` `{e['currency']}` {e['name']}")
            lines.append(f"    _Fcst: {est} · Prev: {prev}_")
        lines.append("")

    lines.append("_Good luck out there Vendetta Folks!_ 🎯")
    return "\n".join(lines)


def fmt_alert(event: dict, minutes_before: int) -> str:
    impact = event["impact"]
    emoji  = IMPACT_EMOJI.get(impact, "⚪")
    ev_dt  = event["datetime"]
    t_str  = ev_dt.astimezone(DISPLAY_TZ).strftime("%H:%M %Z") if ev_dt else "?"
    timing = "🚨 *HAPPENING NOW*" if minutes_before == 0 else f"⏰ *In {minutes_before} minutes*"

    return (
        f"{emoji} *{impact.upper()} IMPACT — {event['currency']}*\n"
        f"{timing}\n\n"
        f"📌 *{event['name']}*\n"
        f"🕐 `{t_str}`\n"
        f"📈 Forecast: `{event['forecast'] or 'N/A'}`\n"
        f"📉 Previous: `{event['previous'] or 'N/A'}`\n\n"
        f"_Volatility incoming — manage your risk!_ 📊"
    )

# ──────────────────────────────────────────────────────────────
#  TELEGRAM
# ──────────────────────────────────────────────────────────────

async def _send(text: str):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)

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
    raw = fetch_events()
    if raw:
        daily_events = filter_events(raw)
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
    log.info("🤖 Forex News Bot starting (ForexFactory scraper)...")
    log.info(f"  Chat ID      : {TELEGRAM_CHAT_ID}")
    log.info(f"  Impact filter: {IMPACT_FILTER}")
    log.info(f"  Currencies   : {CURRENCIES}")
    log.info(f"  Alert windows: {ALERT_MINUTES_BEFORE} min before")

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