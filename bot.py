"""Run the Telegram bot with long polling.

    python bot.py

No domain, HTTPS certificate or hosting needed - this is the mode to use on
your own laptop, for demos and for the defence. For a deployed server see
TELEGRAM_MODE=webhook in main.py.
"""
from telegram import Update

from telegram_bot import build_application


def main() -> None:
    app = build_application()
    print("University AI Knowledge Hub bot is running (polling). Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
