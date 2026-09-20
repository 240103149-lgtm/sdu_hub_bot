"""University AI Knowledge Hub - website / REST API.

    uvicorn main:app --reload

All of the thinking lives in core.py, which the Telegram bot imports as well,
so the website and the bot always answer from the same knowledge base with the
same prompt and the same error messages.

Setting TELEGRAM_MODE=webhook additionally serves the Telegram bot from this
same process - useful once the project is deployed behind an HTTPS domain.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core

TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "off").strip().lower()  # off | webhook
WEBHOOK_BASE = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/telegram/webhook"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Telegram application alongside the website in webhook mode."""
    telegram_app = None
    if TELEGRAM_MODE == "webhook":
        if not WEBHOOK_BASE.startswith("https://"):
            raise RuntimeError(
                "TELEGRAM_MODE=webhook үшін TELEGRAM_WEBHOOK_URL https:// мекенжайы болуы керек "
                "(мысалы https://my-app.up.railway.app)."
            )
        from telegram import Update as TelegramUpdate

        from telegram_bot import build_application

        telegram_app = build_application()
        await telegram_app.initialize()
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

    app.state.telegram = telegram_app
    try:
        yield
    finally:
        if telegram_app is not None:
            await telegram_app.stop()
            await telegram_app.shutdown()


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


@app.get("/api/health")
def health():
    """Quick check that the server is up and can see its documents."""
    return {
        "status": "ok",
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
    telegram_app = getattr(app.state, "telegram", None)
    if telegram_app is None:
        raise HTTPException(status_code=404, detail="Telegram webhook is disabled")
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret token")

    from telegram import Update as TelegramUpdate

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
