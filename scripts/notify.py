"""Fetch DTEK outage schedule for one address and send it to a Telegram chat.

All configuration comes from environment variables (GitHub Actions secrets).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from curl_cffi import requests as creq
from curl_cffi.requests import AsyncSession

from dtek_client import DtekClient
from dtek_client.const import DTEK_SITES
from dtek_client.models import HomeNumResponse, SlotStatus

KYIV = ZoneInfo("Europe/Kyiv")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def flag(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


def tg_send(token: str, chat_id: str, text: str) -> None:
    resp = creq.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    # Do not print resp.url/request: it contains the bot token.
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram API HTTP {resp.status_code}: {resp.text[:200]}")


def _slot_sort_key(key: str) -> tuple[int, str]:
    return (int(key) if key.isdigit() else 10**6, key)


_TIME_RE = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*$")


def _to_minutes(part: str) -> int | None:
    m = _TIME_RE.match(part)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2) or 0)


def _slot_bounds(key: str, labels: dict[str, str], total: int) -> tuple[int, int]:
    """Return (start, end) in minutes for a slot.

    Real labels are like "00-01" (hourly sites) or "00:00–00:30" (30-min slots).
    Falls back to key arithmetic when the label cannot be parsed.
    """
    label = labels.get(key, "")
    parts = re.split(r"\s*[–—-]\s*", label, maxsplit=1)
    if len(parts) == 2:
        start, end = _to_minutes(parts[0]), _to_minutes(parts[1])
        if start is not None and end is not None:
            if end <= start:  # e.g. "23-00"
                end += 24 * 60
            return start, end
    size = 30 if total > 24 else 60
    idx = int(key) - 1 if key.isdigit() else 0
    return idx * size, (idx + 1) * size


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_slots(slots: dict[str, SlotStatus], labels: dict[str, str]) -> str:
    """Convert slot statuses into merged time ranges (🔴 outage, 🟡 possible).

    FIRST/MFIRST = first half of the slot, SECOND/MSECOND = second half.
    """
    intervals: list[tuple[str, int, int]] = []
    total = len(slots)
    for key in sorted(slots, key=_slot_sort_key):
        st = slots[key]
        if st.has_outage:
            kind = "outage"
        elif st.may_have_outage:
            kind = "maybe"
        else:
            continue
        start, end = _slot_bounds(key, labels, total)
        mid = (start + end) // 2
        if st in (SlotStatus.FIRST, SlotStatus.MFIRST):
            end = mid
        elif st in (SlotStatus.SECOND, SlotStatus.MSECOND):
            start = mid
        intervals.append((kind, start, end))

    merged: list[list[Any]] = []
    for kind, start, end in sorted(intervals, key=lambda x: (x[1], x[2])):
        if merged and merged[-1][0] == kind and start <= merged[-1][2]:
            merged[-1][2] = max(merged[-1][2], end)
        else:
            merged.append([kind, start, end])
    if not merged:
        return "Відключень не заплановано ✅"
    icons = {"outage": "🔴", "maybe": "🟡"}
    return "\n".join(
        f"{icons[k]} {_fmt(a)}–{_fmt(b)}" + (" (можливо)" if k == "maybe" else "")
        for k, a, b in merged
    )


async def make_client(site: str) -> Any:
    if flag("DTEK_STUB"):
        from dtek_client.stub_client import StubDtekClient

        return StubDtekClient(site)

    from dtek_client.browser_auth import get_cleared_cookies

    base_url, schedule_path = DTEK_SITES[site]  # tuple[str, str]
    schedule_url = f"{base_url}{schedule_path}"
    cookies, csrf = await get_cleared_cookies(schedule_url, show_browser=flag("DTEK_SHOW_BROWSER"))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "uk,en;q=0.9",
        "Origin": base_url,
        "Referer": schedule_url,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf
    session = AsyncSession(timeout=30.0, headers=headers, cookies=cookies, impersonate="chrome120")
    return DtekClient(site, ajax_url=f"{base_url}/ua/ajax", session=session)


# ── State (change detection) ──────────────────────────────────────────────────
# The state file holds only schedule text and the last error notice — no secrets,
# no address. In GitHub Actions it is persisted between runs with actions/cache.
STATE_FILE = Path(os.environ.get("STATE_FILE", ".state/last.json"))
ERROR_COOLDOWN_SEC = 6 * 3600  # repeat the same error notice at most every 6 hours


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def schedule_changed(old: dict[str, str], new: dict[str, str]) -> bool:
    """True if any currently published day is new or differs from the stored one.

    Days that merely disappeared (yesterday after midnight) do not count as a change.
    """
    return any(old.get(key) != value for key, value in new.items())


def build_message(
    resp: HomeNumResponse, city: str, street: str, house: str
) -> tuple[str, dict[str, str]]:
    """Return (message_text, signature). The signature has no timestamps in it."""
    entry = resp.houses.get(house)
    if entry is None:
        raise RuntimeError(
            f"House {house!r} not found. Available: {', '.join(sorted(resp.houses)[:30])}"
        )
    now = datetime.datetime.now(KYIV).strftime("%d.%m.%Y %H:%M")
    head = [f"<b>Графік відключень</b> ({now})", f"{city}, {street}, {house}"]

    if entry.is_excluded:
        body = "Адреса не входить до графіків відключень."
        return "\n".join(head + [body]), {"_status": body}
    if entry.is_multi_group:
        body = (
            f"Складна схема підключення, групи: {', '.join(entry.group_ids)}. "
            "Точний прогноз неможливий."
        )
        return "\n".join(head + [body]), {"_status": body}

    group = entry.primary_group or "?"
    names = resp.preset.sch_names if resp.preset else {}
    head.append(f"Черга: {names.get(group, group)}")
    if resp.update_timestamp:
        head.append(f"Оновлено ДТЕК: {resp.update_timestamp}")

    labels = resp.preset.time_zone if resp.preset else {}
    if resp.fact is None:
        return "\n".join(head + ["\nФактичний графік не опубліковано."]), {"_status": "no-fact"}

    today_ts = resp.fact.today_ts
    sig: dict[str, str] = {"_group": group}
    sections: list[str] = []
    have_tomorrow = False
    for ts_str in sorted(resp.fact.days, key=int):
        slots = resp.fact.days[ts_str].get(group)
        if not slots:
            continue
        ts = int(ts_str)
        date = datetime.datetime.fromtimestamp(ts, KYIV).date()
        if ts == today_ts:
            name = "Сьогодні"
        elif ts == today_ts + 86400:
            name, have_tomorrow = "Завтра", True
        else:
            name = "Графік"
        text = format_slots(slots, labels)
        sections.append(f"\n<b>{name}</b> ({date:%d.%m}):\n{text}")
        sig[date.isoformat()] = text
    if not have_tomorrow:
        sections.append("\n<b>Завтра:</b>\nще не опубліковано")
    if len(sig) == 1:
        sig["_status"] = "empty"
    return "\n".join(head + sections), sig


async def main() -> int:
    logging.basicConfig(level=logging.DEBUG if flag("DTEK_DEBUG") else logging.INFO)
    site, city, street, house = (
        env("DTEK_SITE"),
        env("DTEK_CITY"),
        env("DTEK_STREET"),
        env("DTEK_HOUSE"),
    )
    dry_run = flag("DRY_RUN")

    client = await make_client(site)
    async with client:
        resp = await client.get_home_num(city, street)  # ONE request: houses + preset + fact

    if flag("DTEK_DEBUG"):
        print("update_timestamp:", resp.update_timestamp)
        if resp.fact:
            print("fact today_ts:", resp.fact.today_ts, "days:", list(resp.fact.days))
        if resp.preset:
            print("time_zone sample:", dict(list(resp.preset.time_zone.items())[:4]))

    text, sig = build_message(resp, city, street, house)
    state = load_state()
    old_sig = state.get("sig") or {}
    needs_send = flag("FORCE_SEND") or not old_sig or schedule_changed(old_sig, sig)

    if dry_run:
        print(text)
        print(f"[dry-run] would send: {needs_send} (state untouched)")
        return 0

    if not needs_send:
        print("No changes in the schedule - nothing to send.")
        if state.pop("error", None) is not None:
            save_state(state)
        return 0

    tg_send(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"), text)
    state["sig"] = sig
    state.pop("error", None)
    save_state(state)  # saved only after a successful send
    print("Message sent.")
    return 0


def _report_failure(text: str) -> None:
    """Send a short error notice (at most once per cooldown for the same error)."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if flag("DRY_RUN") or not token or not chat:
        return
    text = text[:300]
    state = load_state()
    last = state.get("error") or {}
    if last.get("text") == text and time.time() - float(last.get("ts", 0)) < ERROR_COOLDOWN_SEC:
        return
    try:
        tg_send(token, chat, f"⚠️ dtek-notify: {text}")
        state["error"] = {"text": text, "ts": time.time()}
        save_state(state)
    except Exception:  # noqa: BLE001
        pass


def run() -> int:
    try:
        return asyncio.run(main())
    except SystemExit as exc:
        # SystemExit(0)/None is a normal exit; only real failures are reported.
        if exc.code not in (0, None):
            _report_failure(str(exc.code))
        raise
    except Exception as exc:  # noqa: BLE001
        _report_failure(f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    sys.exit(run())
