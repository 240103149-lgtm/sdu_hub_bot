"""Durable Telegram bot state for serverless hosts such as Vercel.

Vercel runs the webhook in short-lived instances. Their memory and /tmp are
not shared and are wiped on a cold start, so the bot forgot languages, linked
accounts and reminder settings, and no timer was left to send a reminder.

With a Redis REST database (Upstash: Vercel -> Storage -> Upstash for Redis
sets KV_REST_API_URL and KV_REST_API_TOKEN by itself) each student's
user_data lives there instead:

    webhook update   load the student's record -> handlers -> save it if changed
    /api/cron/...    notifications.durable_tick reads every subscribed record

Without such a database nothing changes: the bot keeps its in-memory state
and PicklePersistence exactly as before.

Records are Fernet-encrypted JSON. Portal cookies and the Moodle token are as
sensitive as passwords, so nothing readable is ever written to Redis.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import weakref
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable

import httpx
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

log = logging.getLogger(__name__)

# The first pair that is set wins. Vercel's Upstash integration creates the
# KV_* pair; Upstash itself documents UPSTASH_*; SDU_OAUTH_* is the name the
# website's SDU Platform sign-in used first (docs/SDU_OAUTH.md).
ENV_PAIRS = (
    ("BOT_STATE_REDIS_URL", "BOT_STATE_REDIS_TOKEN"),
    ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
    ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
    ("SDU_OAUTH_REDIS_URL", "SDU_OAUTH_REDIS_TOKEN"),
)
PREFIX = "sduhub:bot:"
TIMEOUT = 8            # seconds per Redis request
SENT_TTL = 3 * 86400   # a delivered reminder is remembered this long
TICK_INTERVAL = 50     # seconds; a second trigger within this is ignored
HEARTBEAT_FRESH = 20 * 60  # no finished check for this long = delivery paused
MGET_BATCH = 50


class StoreError(Exception):
    """Redis could not be reached or answered with an error."""


def redis_config() -> tuple[str, str] | None:
    """(REST URL, token) of the configured Redis database, or None."""
    for url_name, token_name in ENV_PAIRS:
        url = os.getenv(url_name, "").strip().rstrip("/")
        token = os.getenv(token_name, "").strip()
        if url and token and url.startswith("https://"):
            return url, token
    return None


def _cipher() -> MultiFernet | None:
    # Encrypt with WEB_SESSION_SECRET when it is set; records written before
    # it was added (keyed by the bot token) stay readable.
    secrets = [value for value in ((os.getenv("WEB_SESSION_SECRET") or "").strip(),
                                   (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()) if value]
    if not secrets:
        return None
    keys = [Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"SDU-Hub-bot-state-v1\0" + value.encode()).digest()))
            for value in dict.fromkeys(secrets)]
    return MultiFernet(keys)


def _plain(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(type(value).__name__)


def dump(data: dict) -> str:
    """user_data as JSON. A value JSON cannot hold stays in memory only."""
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_plain)
    except (TypeError, ValueError):
        clean = {}
        for key, value in data.items():
            try:
                json.dumps({str(key): value}, default=_plain)
            except (TypeError, ValueError):
                log.warning("Bot state: %r is not JSON and is kept in memory only", key)
                continue
            clean[str(key)] = value
        return json.dumps(clean, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_plain)


def subscribed(data: dict) -> bool:
    """True if the scheduled check has anything to do for this student."""
    return bool((data.get("class_notifications") or data.get("deadline_notifications"))
                and not data.get("reminders_blocked"))


class Store:
    """The Upstash-compatible Redis REST API: POST [command, args...]."""

    def __init__(self, url: str, token: str, cipher: MultiFernet,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url, self.token, self.cipher, self.transport = url, token, cipher, transport
        self._clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    # ------------------------------------------------------------ transport ----

    def _http(self) -> httpx.AsyncClient:
        # One connection pool per event loop: no new TLS handshake per command.
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport,
                                       headers={"Authorization": f"Bearer {self.token}"})
            self._clients[loop] = client
        return client

    async def _post(self, path: str, body: Any) -> Any:
        for attempt in range(2):
            try:
                response = await self._http().post(self.url + path, json=body)
            except httpx.HTTPError as exc:
                if attempt:
                    raise StoreError(type(exc).__name__) from None
                continue
            if response.status_code >= 500 and not attempt:
                continue
            try:
                payload = response.json()
            except ValueError:
                raise StoreError(f"HTTP {response.status_code}") from None
            if response.status_code != 200:
                raise StoreError(f"HTTP {response.status_code}")
            return payload
        raise StoreError("unreachable")

    async def command(self, *args: Any) -> Any:
        payload = await self._post("", [str(arg) for arg in args])
        if not isinstance(payload, dict) or payload.get("error"):
            raise StoreError("command failed")
        return payload.get("result")

    async def pipeline(self, commands: list[list[Any]]) -> list[Any]:
        if not commands:
            return []
        payload = await self._post("/pipeline", [[str(arg) for arg in command] for command in commands])
        if not isinstance(payload, list) or len(payload) != len(commands):
            raise StoreError("pipeline failed")
        if any(not isinstance(item, dict) or item.get("error") for item in payload):
            raise StoreError("pipeline command failed")
        return [item.get("result") for item in payload]

    # ---------------------------------------------------------------- users ----

    @staticmethod
    def _user_key(user_id: int) -> str:
        return f"{PREFIX}user:{int(user_id)}"

    def _open(self, sealed: Any) -> dict | None:
        if not sealed:
            return None
        try:
            data = json.loads(self.cipher.decrypt(str(sealed).encode()))
        except (InvalidToken, ValueError):
            # Another secret wrote it (TELEGRAM_BOT_TOKEN rotated): unreadable.
            log.warning("Bot state: a stored record could not be decrypted and is ignored")
            return None
        return data if isinstance(data, dict) else None

    def seal(self, data: dict) -> str:
        return self.cipher.encrypt(dump(data).encode()).decode()

    async def load_user(self, user_id: int) -> dict | None:
        return self._open(await self.command("GET", self._user_key(user_id)))

    async def load_users(self, user_ids: Iterable[int]) -> dict[int, dict | None]:
        ids = list(dict.fromkeys(int(uid) for uid in user_ids))
        result: dict[int, dict | None] = {}
        for offset in range(0, len(ids), MGET_BATCH):
            batch = ids[offset:offset + MGET_BATCH]
            values = await self.command("MGET", *(self._user_key(uid) for uid in batch))
            for uid, sealed in zip(batch, values or []):
                result[uid] = self._open(sealed)
        return result

    async def save_user(self, user_id: int, data: dict, *, was_subscribed: bool = False) -> None:
        commands = [["SET", self._user_key(user_id), self.seal(data)]]
        if subscribed(data):
            commands.append(["SADD", PREFIX + "reminders", int(user_id)])
        elif was_subscribed:
            commands.append(["SREM", PREFIX + "reminders", int(user_id)])
        await self.pipeline(commands)

    async def reminder_users(self) -> list[int]:
        members = await self.command("SMEMBERS", PREFIX + "reminders")
        return sorted(int(uid) for uid in members or [] if str(uid).lstrip("-").isdigit())

    async def unsubscribe(self, user_ids: Iterable[int]) -> None:
        ids = [int(uid) for uid in user_ids]
        if ids:
            await self.command("SREM", PREFIX + "reminders", *ids)

    # ------------------------------------------------------------ reminders ----

    @staticmethod
    def _claim_key(name: str) -> str:
        return PREFIX + "sent:" + hashlib.sha256(name.encode()).hexdigest()

    async def claim(self, name: str, ttl: int = SENT_TTL) -> bool:
        """Atomically reserve one reminder. False: it was already sent."""
        return await self.command("SET", self._claim_key(name), "1", "NX", "EX", int(ttl)) == "OK"

    async def release(self, name: str) -> None:
        """Undo a claim whose message could not be delivered (it is retried)."""
        await self.command("DEL", self._claim_key(name))

    async def begin_tick(self) -> bool:
        """One scheduled check at a time, at most one per TICK_INTERVAL."""
        return await self.command("SET", PREFIX + "tick", int(time.time()), "NX", "EX", TICK_INTERVAL) == "OK"

    async def heartbeat(self) -> None:
        await self.command("SET", PREFIX + "heartbeat", int(time.time()), "EX", 86400)

    async def last_heartbeat(self) -> float | None:
        value = await self.command("GET", PREFIX + "heartbeat")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None


_current: tuple[tuple, Store | None] | None = None


def current() -> Store | None:
    """The configured store, or None when no Redis database is set up."""
    global _current
    config = redis_config()
    cipher_inputs = ((os.getenv("WEB_SESSION_SECRET") or "").strip(), (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip())
    signature = (config, cipher_inputs)
    if _current is None or _current[0] != signature:
        cipher = _cipher() if config else None
        if config and cipher is None:
            log.warning("Bot state: Redis is set but no secret to encrypt it; state stays in memory")
        _current = (signature, Store(*config, cipher) if config and cipher else None)
    return _current[1]


@asynccontextmanager
async def synced(app, update) -> AsyncIterator[None]:
    """Around one webhook update: the student's durable record in, changes out.

    If Redis cannot be read, the update still runs on the in-memory state but
    nothing is written back, so a stale copy never overwrites the real one.
    """
    store = current()
    user = getattr(update, "effective_user", None)
    if store is None or user is None:
        yield
        return
    data = app.user_data[user.id]  # created when missing
    try:
        record = await store.load_user(user.id)
        loaded = True
    except StoreError as exc:
        log.warning("Bot state: could not load a student's record (%s)", exc)
        record, loaded = None, False
    if record is not None:
        data.clear()
        data.update(record)
    before = dump(data) if record is not None else None
    was_subscribed = subscribed(data)
    try:
        yield
    finally:
        if loaded and dump(data) != before:
            try:
                await store.save_user(user.id, data, was_subscribed=was_subscribed)
            except StoreError as exc:
                log.warning("Bot state: could not save a student's record (%s)", exc)
