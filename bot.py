"""
Forex News Telegram Bot — powered by JBlanked News API (MQL5 source)
"""

import os
import logging
import asyncio
import schedule
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from telegram import Bot
from telegram.constants import ParseMode
from dotenv import load_dotenv
load_dotenv()

# ──────────────────────────────────────────────────────────────
#  CONFIG — paste your values directly or use env vars
# ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
JBLANKED_API_KEY = os.getenv("JBLANKED_API_KEY")

# Impact levels to track: "High", "Medium", "Low" (JBlanked capitalises these)
IMPACT_FILTER = {"High", "Medium"}

# Currencies to watch (filtered locally — avoids hitting rate limit with multiple requests)
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "NZD"}

# Alert X minutes before each event
ALERT_MINUTES_BEFORE = [60, 15]

# Timezone for display
DISPLAY_TZ = ZoneInfo("Europe/Berlin")

# ──────────────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
#  STATE
# ──────────────────────────────────────────────────────────────

daily_events: list[dict] = []
alerted_keys: set[str]   = set()

IMPACT_EMOJI = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}

# ──────────────────────────────────────────────────────────────
#  FETCH FROM JBLANKED  (1 request, filter locally)
# ──────────────────────────────────────────────────────────────

def fetch_events() -> list[dict]:
    """
    Single GET to today's MQL5 calendar — no currency param to avoid rate limit.
    Auth: "Api-Key <key>"  (NOT Bearer)
    Docs: https://www.jblanked.com/news/api/docs/calendar/
    """
    url     = "https://www.jblanked.com/news/api/mql5/calendar/today/"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {JBLANKED_API_KEY}",
    }

    try:
        r = requests.get(url, headers=headers, timeout=15)

        if r.status_code == 401:
            log.error("❌ JBlanked 401 — get your API key at https://www.jblanked.com/profile/")
            return []
        if r.status_code == 429:
            log.warning("⚠️ JBlanked rate limit hit (free tier: 1 req / 5 min). Using cached events.")
            return []

        r.raise_for_status()
        data = r.json()

        # Response is either a list directly or wrapped in a key
        if isinstance(data, list):
            events = data
        elif isinstance(data, dict):
            events = data.get("results", data.get("events", data.get("data", [])))
        else:
            events = []

        log.info(f"JBlanked returned {len(events)} raw events")
        return events

    except Exception as e:
        log.error(f"JBlanked fetch error: {e}")
        return []


def filter_events(events: list[dict]) -> list[dict]:
    """Keep only events matching our impact + currency filters."""
    filtered = []
    for e in events:
        # JBlanked MQL5 fields: Currency, Impact (capitalised), Name, Date, Time, Forecast, Previous, Actual
        impact   = e.get("Impact") or e.get("impact") or e.get("Strength") or ""
        currency = e.get("Currency") or e.get("currency") or ""

        # Normalise impact — some versions use "High"/"Medium", others use numeric strength
        impact_str = str(impact).capitalize()

        if impact_str in IMPACT_FILTER and currency in CURRENCIES:
            filtered.append(e)

    log.info(f"After filter: {len(filtered)} events (impact={IMPACT_FILTER}, currencies={CURRENCIES})")
    return filtered


def parse_event_time(event: dict) -> datetime | None:
    """
    JBlanked MQL5 calendar returns Date + Time as separate fields.
    Date: "2025-09-17"  Time: "13:30" or "13:30:00"
    """
    date_str = event.get("Date") or event.get("date") or ""
    time_str = event.get("Time") or event.get("time") or ""

    if not date_str:
        return None

    # Sometimes time is empty for all-day events
    if not time_str or time_str.lower() in ("all day", "tentative", ""):
        time_str = "00:00"

    # Strip seconds if present
    time_part = time_str[:5]  # "HH:MM"

    try:
        dt = datetime.strptime(f"{date_str} {time_part}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        # Fallback: try ISO format in case API changes
        try:
            raw = event.get("time") or event.get("Time") or ""
            if "T" in raw:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            pass
        return None

# ──────────────────────────────────────────────────────────────
#  MESSAGE FORMATTING
# ──────────────────────────────────────────────────────────────

def get_field(event: dict, *keys: str, default="—") -> str:
    """Try multiple key variants (capitalised + lowercase)."""
    for k in keys:
        v = event.get(k) or event.get(k.lower()) or event.get(k.capitalize())
        if v:
            return str(v)
    return default


def fmt_digest(events: list[dict]) -> str:
    if not events:
        return "📭 *No significant forex events today.* Easy session ahead."

    now_local = datetime.now(DISPLAY_TZ)
    lines = [
        f"📅 *ECONOMIC CALENDAR — {now_local.strftime('%A %d %B %Y').upper()}*",
        f"_{now_local.strftime('%Z (UTC%z)')}_\n",
    ]

    for level in ["High", "Medium", "Low"]:
        group = [
            e for e in events
            if (e.get("Impact") or e.get("impact") or e.get("Strength") or "").capitalize() == level
        ]
        if not group:
            continue

        emoji = IMPACT_EMOJI.get(level, "⚪")
        lines.append(f"{emoji} *{level} Impact*")
        for e in group:
            ev_dt = parse_event_time(e)
            t_str = ev_dt.astimezone(DISPLAY_TZ).strftime("%H:%M") if ev_dt else "?"
            cur   = get_field(e, "Currency", default="?")
            name  = get_field(e, "Name", "event", "title", default="Unknown")
            est   = get_field(e, "Forecast", "forecast", "Estimate", default="—")
            prev  = get_field(e, "Previous", "previous", "Prev", default="—")
            lines.append(f"  • `{t_str}` `{cur}` {name}")
            lines.append(f"    _Fcst: {est} · Prev: {prev}_")
        lines.append("")

    lines.append("_Good luck out there Vendetta Folks!_ 🎯")
    return "\n".join(lines)


def fmt_alert(event: dict, minutes_before: int) -> str:
    impact  = (get_field(event, "Impact", "Strength", default="low")).capitalize()
    emoji   = IMPACT_EMOJI.get(impact, "⚪")
    name    = get_field(event, "Name", "event", "title", default="Unknown Event")
    cur     = get_field(event, "Currency", default="?")
    ev_dt   = parse_event_time(event)
    est     = get_field(event, "Forecast", "forecast", default="N/A")
    prev    = get_field(event, "Previous", "previous", default="N/A")
    t_str   = ev_dt.astimezone(DISPLAY_TZ).strftime("%H:%M %Z") if ev_dt else "?"
    timing  = "🚨 *HAPPENING NOW*" if minutes_before == 0 else f"⏰ *In {minutes_before} minutes*"

    return (
        f"{emoji} *{impact.upper()} IMPACT — {cur}*\n"
        f"{timing}\n\n"
        f"📌 *{name}*\n"
        f"🕐 `{t_str}`\n"
        f"📈 Forecast: `{est}`\n"
        f"📉 Previous: `{prev}`\n\n"
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
#  SCHEDULER JOBS
# ──────────────────────────────────────────────────────────────

def job_refresh():
    """Fetch fresh data + post daily digest. Called at startup, 6 AM and 12 PM UTC."""
    global daily_events, alerted_keys
    alerted_keys.clear()

    raw = fetch_events()
    if raw:  # only update cache if we got data (avoid clearing on rate-limit)
        daily_events = filter_events(raw)

    send(fmt_digest(daily_events))


def job_check_alerts():
    """Run every minute. Fire pre-event alerts when threshold window is hit."""
    now = datetime.now(timezone.utc)
    for event in daily_events:
        ev_dt = parse_event_time(event)
        if ev_dt is None:
            continue
        minutes_until = (ev_dt - now).total_seconds() / 60
        for threshold in ALERT_MINUTES_BEFORE:
            if abs(minutes_until - threshold) < 0.75:
                name = get_field(event, "Name", "event", default="event")
                key  = f"{name}_{ev_dt.isoformat()}_{threshold}"
                if key not in alerted_keys:
                    alerted_keys.add(key)
                    log.info(f"🔔 Alert: {name} in {threshold}min")
                    send(fmt_alert(event, threshold))

# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────

def main():
    log.info("🤖 Forex News Bot starting (JBlanked MQL5 edition)...")
    log.info(f"  Chat ID      : {TELEGRAM_CHAT_ID}")
    log.info(f"  Impact filter: {IMPACT_FILTER}")
    log.info(f"  Currencies   : {CURRENCIES}")
    log.info(f"  Alert windows: {ALERT_MINUTES_BEFORE} min before")

    job_refresh()

    # Refresh at 6 AM UTC (pre-London) and 12 PM UTC (NY open)
    schedule.every().day.at("06:00").do(job_refresh)
    schedule.every().day.at("12:00").do(job_refresh)

    # Check for upcoming events every minute
    schedule.every(1).minutes.do(job_check_alerts)

    log.info("Scheduler running. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()