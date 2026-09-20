"""Демо алдында бәрі дұрыс екенін тексеру.

    python selftest.py

Әр қадам бөлек тексеріледі, сондықтан біреуі құласа да қалғандары жүреді.
Соңында қысқа қорытынды шығады.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

OK, BAD = "  OK  ", " FAIL "
results: list[tuple[bool, str]] = []


def report(passed: bool, title: str, detail: str = "") -> bool:
    results.append((passed, title))
    print(f"[{OK if passed else BAD}] {title}")
    if detail:
        for line in detail.strip().split("\n"):
            print(f"         {line}")
    return passed


def note(title: str, detail: str = "") -> None:
    """Міндетті емес нәрсе: қорытындыға қате болып жазылмайды."""
    print(f"[ ---- ] {title}")
    if detail:
        print(f"         {detail}")


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 60 - len(title)))


# 1. -------------------------------------------------------- dependencies --
section("1. Кітапханалар")
missing = []
for module, package in [
    ("dotenv", "python-dotenv"),
    ("google.genai", "google-genai"),
    ("telegram", "python-telegram-bot"),
    ("fastapi", "fastapi"),
    ("docx", "python-docx"),
]:
    try:
        __import__(module)
    except ImportError:
        missing.append(package)
report(
    not missing,
    "барлық кітапхана орнатылған",
    "" if not missing else f"жоқ: {', '.join(missing)}\nістеу керек: pip install -r requirements.txt",
)
if missing:
    print("\nКітапханаларды орнатпай тексеруді жалғастыра алмаймыз.")
    sys.exit(1)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# 2. ---------------------------------------------------------------- .env --
section("2. .env файлы")
env_path = Path(__file__).parent / ".env"
report(env_path.exists(), ".env файлы бар", "" if env_path.exists() else "істеу керек: cp env.example .env")
report(bool(os.getenv("GEMINI_API_KEY")), "GEMINI_API_KEY толтырылған")
report(bool(os.getenv("GEMINI_MODEL")), "GEMINI_MODEL толтырылған",
       f"модель: {os.getenv('GEMINI_MODEL')}" if os.getenv("GEMINI_MODEL") else "")
report(bool(os.getenv("TELEGRAM_BOT_TOKEN")), "TELEGRAM_BOT_TOKEN толтырылған",
       "" if os.getenv("TELEGRAM_BOT_TOKEN") else "@BotFather → /newbot")

if not (os.getenv("GEMINI_API_KEY") and os.getenv("GEMINI_MODEL")):
    print("\nGemini кілтінсіз әрі қарай тексере алмаймыз.")
    sys.exit(1)

import core  # noqa: E402

# 3. ------------------------------------------------------ knowledge base --
section("3. Білім қоры (knowledge/)")
documents = core.knowledge_files()
report(
    bool(documents),
    f"{len(documents)} құжат табылды",
    "\n".join(f"- {p.relative_to(core.KNOWLEDGE_DIR)}" for p in documents)
    or "knowledge/ папкасы бос — бот жауап бере алмайды",
)

unreadable = []
for path in documents:
    try:
        core.read_document(path)
    except Exception as exc:  # noqa: BLE001
        unreadable.append(f"{path.name}: {exc}")
report(not unreadable, "барлық құжат оқылды", "\n".join(unreadable))

# 4. ------------------------------------------------------------ US3 guide --
section("4. Нұсқаулық (US3)")
for lang in ("kk", "ru", "en"):
    guide = core.load_guide(lang)
    if guide and guide.strip():
        report(True, f"{lang}: нұсқаулық жүктелді ({len(guide)} таңба)")
    else:
        report(
            False,
            f"{lang}: нұсқаулық табылмады",
            f"knowledge/registration_guide.{lang}.md немесе registration_guide.md керек",
        )

portal = os.getenv("REGISTRATION_PORTAL_URL", "").strip()
if portal:
    note(f"портал сілтемесі: {portal}")
else:
    note("портал сілтемесі жоқ", "REGISTRATION_PORTAL_URL бос (міндетті емес)")

# 5. ---------------------------------------------------------- Gemini (US5) --
section("5. Gemini жауабы (US5: 5 секунд)")
question = "Тест: бір қысқа сөйлеммен жауап бер."
started = time.monotonic()
try:
    answer = core.answer(question, [], "kk")
    elapsed = time.monotonic() - started
    report(True, f"Gemini жауап берді ({elapsed:.1f}s)", answer[:200])
    report(
        elapsed <= core.SLOW_RESPONSE,
        f"5 секунд шегіне сыйды ({elapsed:.1f}s ≤ {core.SLOW_RESPONSE:.0f}s)",
        "" if elapsed <= core.SLOW_RESPONSE else "жылдамырақ модель немесе кішірек knowledge/ көмектеседі",
    )
except core.AssistantError as exc:
    report(False, "Gemini жауап берді", f"{exc.key}: {exc.localized('kk')}")
except Exception as exc:  # noqa: BLE001
    report(False, "Gemini жауап берді", repr(exc))

# 6. --------------------------------------------------------------- Telegram --
section("6. Telegram боты")
token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    report(False, "бот токені тексерілді", "TELEGRAM_BOT_TOKEN бос")
else:
    import asyncio

    from telegram import Bot
    from telegram.error import TelegramError

    async def check_bot() -> None:
        try:
            me = await Bot(token).get_me()
            report(True, f"бот қосылды: @{me.username}")
        except TelegramError as exc:
            report(False, "бот қосылды", f"{exc}\nтокенді @BotFather-ден тексеріңіз")
        except Exception as exc:  # noqa: BLE001
            report(False, "бот қосылды", repr(exc))

    asyncio.run(check_bot())

mode = os.getenv("TELEGRAM_MODE", "off").strip().lower()
if mode == "webhook":
    url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    report(url.startswith("https://"), "webhook URL дұрыс", "" if url else "TELEGRAM_WEBHOOK_URL бос")
    report(bool(os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()),
           "webhook құпия сөзі қойылған", "TELEGRAM_WEBHOOK_SECRET бос — қауіпсіз емес")
else:
    note("режим: polling", "ботты қосу:  python bot.py")

# ------------------------------------------------------------------- total --
failed = [title for passed, title in results if not passed]
print("\n" + "=" * 66)
print(f"{len(results) - len(failed)} сәтті, {len(failed)} қате")
for title in failed:
    print(f"  - {title}")
if not failed:
    print("Бәрі дайын. Ботты қосуға болады:  python bot.py")
sys.exit(1 if failed else 0)
