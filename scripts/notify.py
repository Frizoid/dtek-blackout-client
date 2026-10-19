"""Fetch DTEK outage schedule for one address and send it to a Telegram chat.

All configuration comes from environment variables (GitHub Actions secrets).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sys
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


def format_slots(slots: dict[str, SlotStatus], labels: dict[str, str]) -> str:
    """Merge consecutive slots with the same status into time ranges.

    Labels like "00:00–00:30" come from preset.time_zone (real site data).
    """
    icons = {"outage": "🔴", "maybe": "🟡"}
    runs: list[list[Any]] = []  # [kind, first_key, last_key]
    for key in sorted(slots, key=_slot_sort_key):
        st = slots[key]
        if st.has_outage:
            kind = "outage"
        elif st.may_have_outage:
            kind = "maybe"
        else:
            kind = None
        if kind is None:
            continue
        if runs and runs[-1][0] == kind and _slot_sort_key(key)[0] == _slot_sort_key(runs[-1][2])[0] + 1:
            runs[-1][2] = key
        else:
            runs.append([kind, key, key])
    if not runs:
        return "Відключень не заплановано ✅"

    def start(k: str) -> str:
        return labels.get(k, k).split("–")[0]

    def end(k: str) -> str:
        parts = labels.get(k, k).split("–")
        return parts[-1]

    lines = []
    for kind, a, b in runs:
        suffix = " (можливо)" if kind == "maybe" else ""
        lines.append(f"{icons[kind]} {start(a)}–{end(b)}{suffix}")
    return "\n".join(lines)


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


def build_message(resp: HomeNumResponse, city: str, street: str, house: str) -> str:
    entry = resp.houses.get(house)
    if entry is None:
        raise RuntimeError(
            f"House {house!r} not found. Available: {', '.join(sorted(resp.houses)[:30])}"
        )
    now = datetime.datetime.now(KYIV).strftime("%d.%m.%Y %H:%M")
    head = [f"<b>Графік відключень</b> ({now})", f"{city}, {street}, {house}"]

    if entry.is_excluded:
        return "\n".join(head + ["Адреса не входить до графіків відключень."])
    if entry.is_multi_group:
        return "\n".join(head + [f"Складна схема підключення, групи: {', '.join(entry.group_ids)}. Точний прогноз неможливий."])

    group = entry.primary_group or "?"
    names = resp.preset.sch_names if resp.preset else {}
    head.append(f"Черга: {names.get(group, group)}")
    if resp.update_timestamp:
        head.append(f"Оновлено ДТЕК: {resp.update_timestamp}")

    labels = resp.preset.time_zone if resp.preset else {}
    parts = head
    if resp.fact is None:
        parts.append("\nФактичний графік не опубліковано.")
        return "\n".join(parts)

    today = resp.fact.get_group_today(group)
    tomorrow = resp.fact.get_group_day(resp.fact.today_ts + 86400, group)
    parts.append("\n<b>Сьогодні:</b>\n" + (format_slots(today, labels) if today else "не опубліковано"))
    parts.append("\n<b>Завтра:</b>\n" + (format_slots(tomorrow, labels) if tomorrow else "ще не опубліковано"))
    return "\n".join(parts)


async def main() -> int:
    logging.basicConfig(level=logging.DEBUG if flag("DTEK_DEBUG") else logging.INFO)
    site, city, street, house = env("DTEK_SITE"), env("DTEK_CITY"), env("DTEK_STREET"), env("DTEK_HOUSE")
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

    text = build_message(resp, city, street, house)
    if dry_run:
        print(text)
    else:
        tg_send(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"), text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except BaseException as exc:  # noqa: BLE001
        if not flag("DRY_RUN") and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
            try:
                tg_send(
                    os.environ["TELEGRAM_BOT_TOKEN"],
                    os.environ["TELEGRAM_CHAT_ID"],
                    f"⚠️ dtek-notify: {type(exc).__name__}: {str(exc)[:300]}",
                )
            except Exception:  # noqa: BLE001
                pass
        raise
