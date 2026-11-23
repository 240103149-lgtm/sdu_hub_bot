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
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
import i18n
import campus_events
import rooms
import student_schedule
import web_accounts
import sdu_oauth

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
_telegram_update_lock = asyncio.Lock()


async def _get_telegram_app():
    global _telegram_app
    if _telegram_app is not None:
        return _telegram_app
    async with _telegram_app_lock:
        if _telegram_app is None:
            from telegram_bot import build_application

            telegram_app = build_application()
            await telegram_app.initialize()
            if telegram_app.post_init:
                await telegram_app.post_init(telegram_app)
            # A persistent webhook process must start the job queue even when
            # initialized lazily rather than through ASGI lifespan.
            if not IS_VERCEL:
                await telegram_app.start()
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
        if not telegram_app.running:
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
            if _telegram_app.running:
                await _telegram_app.stop()
            await _telegram_app.shutdown()
            _telegram_app = None


app = FastAPI(title="University AI Knowledge Hub", lifespan=lifespan)
app.include_router(web_accounts.router)
app.include_router(sdu_oauth.router)


@app.exception_handler(RequestValidationError)
async def account_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/me/"):
        return JSONResponse(status_code=422, content={"detail": "invalid_input"},
                            headers={"Cache-Control": "private, no-store"})
    return await request_validation_exception_handler(request, exc)


@app.middleware("http")
async def private_account_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/me/"):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ------------------------------------------------------------------ API ----


@app.get("/api/config")
def public_config():
    from urllib.parse import urlparse

    bot_url = os.getenv("TELEGRAM_PUBLIC_URL", "https://t.me/sdu_hub_bot").strip()
    parsed = urlparse(bot_url)
    if parsed.scheme != "https" or parsed.hostname != "t.me":
        bot_url = "https://t.me/sdu_hub_bot"
    return {"telegram_url": bot_url, "timezone": "Asia/Almaty"}


@app.get("/api/events")
async def public_events():
    try:
        return await campus_events.fetch_events()
    except campus_events.FeedError:
        raise HTTPException(status_code=503, detail="SDU Life is temporarily unavailable") from None


@app.get("/api/schedule")
def public_schedule(course: str = "", offset: int = 0):
    if len(course) > 100 or not 0 <= offset <= 10000:
        raise HTTPException(status_code=422, detail="Invalid search")
    try:
        # A search is required: don't send the university's entire timetable.
        groups = student_schedule.groups(course) if len(course.strip()) >= 2 else []
        return {"groups": groups[offset:offset + 30], "total": len(groups),
                "next_offset": offset + 30 if offset + 30 < len(groups) else None,
                **student_schedule.term()}
    except (rooms.ScheduleError, OSError, ValueError):
        raise HTTPException(status_code=503, detail="The timetable is temporarily unavailable") from None


class Message(BaseModel):
    role: str  # "user" or "assistant"
    text: str


class ChatRequest(BaseModel):
    message: str
    history: list[Message] = []
    lang: str = "en"  # interface language: kk, ru or en


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Public service tools and approved knowledge, without personal sessions."""
    import web_assistant
    try:
        return await web_assistant.answer(req.message, [m.model_dump() for m in req.history], core.normalize_lang(req.lang))
    except core.AssistantError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.localized(req.lang)) from None


@app.post("/api/me/chat")
async def personal_chat(req: ChatRequest, request: Request, response: Response):
    """Same-origin chat with access only to this visitor's own linked accounts."""
    import web_assistant
    web_accounts.same_origin(request)
    try:
        return await web_assistant.answer(req.message, [m.model_dump() for m in req.history],
                                          core.normalize_lang(req.lang), request, response)
    except core.AssistantError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.localized(req.lang)) from None


@app.get("/api/guide", response_class=PlainTextResponse)
def guide(lang: str = "kk"):
    """US2: the registration guide for the requested language.

    404: not published for this semester; 503: temporarily unreadable (the
    page then offers Retry).
    """
    lang = core.normalize_lang(lang, default="kk")
    try:
        text = core.load_guide(lang)
    except core.GuideError as exc:
        print(f"[main] registration guide: {exc}")
        raise HTTPException(status_code=503, detail=core.msg("guide_error", lang))
    if text is None or not text.strip():
        raise HTTPException(status_code=404, detail=core.msg("guide_missing", lang))
    return text


@app.get("/api/health")
def health():
    """Quick check that the server is up and can see its documents."""
    return {
        "status": "ok",
        "provider": core.AI_PROVIDER,
        "model": core.MODEL,
        "documents": [path.name for path in core.knowledge_files()],
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
    # ConversationHandler requires sequential updates, including webhook requests.
    async with _telegram_update_lock:
        await telegram_app.process_update(update)
    return {"ok": True}


# --------------------------------------------------------------- website ----

if core.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=core.STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    """The website, with its texts from locales/<lang>.json put into the page (US6)."""
    page = core.STATIC_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found")
    texts = {lang: i18n.section(lang, "web") for lang in i18n.LANGS}
    # "<" is escaped so no text can ever close the <script> element early.
    payload = json.dumps(texts, ensure_ascii=False).replace("<", "\\u003c")
    return page.read_text(encoding="utf-8").replace("/*I18N*/null", payload, 1)
