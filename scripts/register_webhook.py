"""One-off: register (or re-register) the Telegram webhook.

Run this once after deploying to Vercel, and again whenever the
deployment URL changes. Don't rely on main.py's `lifespan` to do this on
Vercel - its startup event isn't reliably invoked on every cold start
there (see main.py's comment on IS_VERCEL).

Usage:
    python scripts/register_webhook.py https://your-app.vercel.app

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from .env (same file
used by the rest of the project).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def main() -> None:
    if len(sys.argv) != 2:
        print("Қолданылуы: python scripts/register_webhook.py https://your-app.vercel.app")
        raise SystemExit(1)

    base = sys.argv[1].rstrip("/")
    if not base.startswith("https://"):
        raise SystemExit("URL https:// болуы керек")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(".env ішінде TELEGRAM_BOT_TOKEN болуы керек")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

    bot = Bot(token=token)
    await bot.set_webhook(
        url=f"{base}/telegram/webhook",
        secret_token=secret or None,
        drop_pending_updates=True,
    )
    info = await bot.get_webhook_info()
    print(f"[register_webhook] тіркелді: {info.url}")
    if not secret:
        print("[register_webhook] ЕСКЕРТУ: TELEGRAM_WEBHOOK_SECRET бос — webhook қорғалмаған.")


if __name__ == "__main__":
    asyncio.run(main())
