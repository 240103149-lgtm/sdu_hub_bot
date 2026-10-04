"""University AI Knowledge Hub - website / REST API.

    uvicorn main:app --reload

All of the thinking lives in core.py, which the Telegram bot imports as well,
so the website and the bot always answer from the same knowledge base with the
same prompt and the same error messages.

Setting TELEGRAM_MODE=webhook additionally serves the Telegram bot from this
same process - useful once the project is deployed behind an HTTPS domain.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai
import core
import rooms

TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "off").strip().lower()  # off | webhook
WEBHOOK_BASE = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/telegram/webhook"

# Vercel sets this automatically in both the build and the function runtime.
IS_VERCEL = bool(os.getenv("VERCEL"))

# Built lazily (once per warm instance) instead of only in `lifespan`,
# because ASGI startup/shutdown events are not reliably invoked between
# cold starts on Vercel's Python runtime - so the webhook handler below
# builds it itself on the first update if `lifespan` never ran.
_telegram_app = None
_telegram_app_lock = asyncio.Lock()


async def _get_telegram_app():
    global _telegram_app
    if _telegram_app is not None:
        return _telegram_app
    async with _telegram_app_lock:
        if _telegram_app is None:
            from telegram_bot import build_application

            telegram_app = build_application()
            await telegram_app.initialize()
            _telegram_app = telegram_app
    return _telegram_app


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On a persistent host (Railway, Render, a VPS), build the bot once at
    startup and register the webhook URL with Telegram automatically.

    Skipped on Vercel: lifespan events aren't guaranteed to run on every
    cold start there, so registering the webhook here would be unreliable
    (and, since a new instance may spin up per request, wasteful). On
    Vercel, register the webhook once with `scripts/register_webhook.py`
    after deploying - the Application itself is still built lazily, on the
    first incoming update, by _get_telegram_app() above.
    """
    if TELEGRAM_MODE == "webhook" and not IS_VERCEL:
        if not WEBHOOK_BASE.startswith("https://"):
            raise RuntimeError(
                "TELEGRAM_MODE=webhook үшін TELEGRAM_WEBHOOK_URL https:// мекенжайы болуы керек "
                "(мысалы https://my-app.up.railway.app)."
            )
        from telegram import Update as TelegramUpdate

        telegram_app = await _get_telegram_app()
        await telegram_app.start()
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_BASE}{WEBHOOK_PATH}",
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=TelegramUpdate.ALL_TYPES,
            drop_pending_updates=True,
        )
        print(f"[main] Telegram webhook registered at {WEBHOOK_BASE}{WEBHOOK_PATH}")
        if not WEBHOOK_SECRET:
            print("[main] WARNING: TELEGRAM_WEBHOOK_SECRET is empty - the endpoint is unprotected.")

    try:
        yield
    finally:
        global _telegram_app
        if _telegram_app is not None:
            await _telegram_app.stop()
            await _telegram_app.shutdown()
            _telegram_app = None


app = FastAPI(title="University AI Knowledge Hub", lifespan=lifespan)


# ------------------------------------------------------------------ API ----


class Message(BaseModel):
    role: str  # "user" or "assistant"
    text: str


class ChatRequest(BaseModel):
    message: str
    history: list[Message] = []
    lang: str = "en"  # interface language: kk, ru or en


@app.post("/api/chat")
def chat(req: ChatRequest):
    """US5: answer one free-text question from the approved knowledge base."""
    lang = core.normalize_lang(req.lang)
    try:
        reply = core.answer(req.message, [m.model_dump() for m in req.history], lang)
    except core.AssistantError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.localized(lang))
    return {"reply": reply}


@app.get("/api/guide", response_class=PlainTextResponse)
def guide(lang: str = "kk"):
    """US3: the registration guide for the requested language."""
    lang = core.normalize_lang(lang, default="kk")
    text = core.load_guide(lang)
    if text is None:
        raise HTTPException(status_code=404, detail=core.msg("guide_missing", lang))
    return text


@app.get("/api/rooms")
def free_rooms(day: str | None = None, time: str | None = None, lang: str = "en"):
    """Free classrooms for one class period: the current one, or ?day=Mo&time=10:30."""
    lang = core.normalize_lang(lang)
    try:
        view = rooms.pick_view(day, time)
        free = rooms.free_rooms(view)
        schedule = rooms.load_schedule()
    except rooms.ScheduleError as exc:
        print(f"[main] schedule: {exc}")
        raise HTTPException(status_code=503, detail=core.msg("schedule_missing", lang))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "day": view.day,
        "start": rooms.hhmm(view.slot.start),
        "end": rooms.hhmm(view.slot.end),
        "status": view.status,  # now, next, day_over, day_off - or None if the student chose
        "days": schedule.days,
        "slots": [{"start": rooms.hhmm(s.start), "end": rooms.hhmm(s.end)} for s in schedule.slots],
        "total": len(schedule.rooms),
        "free": [
            {
                "room": item.room.name,
                "block": item.room.block,
                "building": item.room.building,
                "until": rooms.hhmm(item.until) if item.until is not None else None,
            }
            for item in free
        ],
    }


@app.get("/api/health")
def health():
    """Quick check that the server is up and can see its documents."""
    try:
        room_count = len(rooms.load_schedule().rooms)
    except rooms.ScheduleError:
        room_count = None
    return {
        "status": "ok",
        "ai": ai.describe(),  # the models that may answer, in the order they are tried
        "documents": [path.name for path in core.knowledge_files()],
        "rooms": room_count,
        "telegram": TELEGRAM_MODE,
    }


# ------------------------------------------------------ Telegram webhook ----


@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Receive updates from Telegram when TELEGRAM_MODE=webhook."""
    if TELEGRAM_MODE != "webhook":
        raise HTTPException(status_code=404, detail="Telegram webhook is disabled")
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret token")

    from telegram import Update as TelegramUpdate

    telegram_app = await _get_telegram_app()
    update = TelegramUpdate.de_json(await request.json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


# --------------------------------------------------------------- website ----

if core.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=core.STATIC_DIR), name="static")


@app.get("/")
def index():
    page = core.STATIC_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found")
    return FileResponse(page)
