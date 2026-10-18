"""US7: sign-in protection shared by the portal (/grade, /documents, /login)
and Moodle (/deadline) sign-ins.

    - After MAX_FAILURES wrong passwords in a row for one account, signing in
      to that account through the bot is locked for LOCK_MINUTES; the bot then
      says "Account temporarily locked" and does not contact the portal.
    - Every sign-in is timed against the 3-second target and logged, like the
      chatbot's answers (US1).

Failures are counted per account (student number / Moodle username), not per
Telegram user, so trying one account from several chats does not help. They
live in bot_data, which the bot's persistence keeps across restarts.
"""
from __future__ import annotations

import math
import os
import time

import core
import i18n

MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOCK_SECONDS = float(os.getenv("LOGIN_LOCK_MINUTES", "15")) * 60
TARGET_SECONDS = float(os.getenv("LOGIN_TARGET_SECONDS", "3"))

TEXTS = i18n.by_key("accounts")  # locales/<lang>.json, section "accounts"


def _table(bot_data: dict) -> dict:
    return bot_data.setdefault("login_failures", {})


def _key(system: str, account: str) -> str:
    return f"{system}:{account.strip().lower()}"


def locked_for(bot_data: dict, system: str, account: str, now: float | None = None) -> float:
    """Seconds the account's sign-in stays locked; 0 when it is not locked."""
    now = time.time() if now is None else now
    entry = _table(bot_data).get(_key(system, account))
    return max(0.0, entry.get("until", 0) - now) if entry else 0.0


def record_failure(bot_data: dict, system: str, account: str, now: float | None = None) -> int:
    """One wrong password. Returns the attempts left; 0 means it is locked now."""
    now = time.time() if now is None else now
    entry = _table(bot_data).setdefault(_key(system, account), {"count": 0, "until": 0})
    if entry["until"] and entry["until"] <= now:  # an old lock has ended: start again
        entry.update(count=0, until=0)
    entry["count"] += 1
    if entry["count"] >= MAX_FAILURES:
        entry.update(count=0, until=now + LOCK_SECONDS)
        print(f"[auth] {system} sign-in for {account!r} locked for {LOCK_SECONDS / 60:g} min")
        return 0
    return MAX_FAILURES - entry["count"]


def record_success(bot_data: dict, system: str, account: str) -> None:
    _table(bot_data).pop(_key(system, account), None)


def locked_text(lang: str, seconds: float) -> str:
    text = TEXTS["account_locked"].get(core.normalize_lang(lang), TEXTS["account_locked"]["en"])
    return text.format(max=MAX_FAILURES, minutes=max(1, math.ceil(seconds / 60)))


def attempts_text(lang: str, left: int) -> str:
    text = TEXTS["attempts_left"].get(core.normalize_lang(lang), TEXTS["attempts_left"]["en"])
    return text.format(left=left)


def log_timing(system: str, step: str, seconds: float) -> None:
    """[auth] portal sign-in: 1.4s (target 3s)"""
    flag = "  <-- slower than the US7 target" if seconds > TARGET_SECONDS else ""
    print(f"[auth] {system} {step}: {seconds:.1f}s (target {TARGET_SECONDS:g}s){flag}")
