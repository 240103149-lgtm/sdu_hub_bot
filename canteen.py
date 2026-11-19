"""US11 canteen menu: the 🍽 keyboard button shows the price list.

The prices live in knowledge/sdu_canteen_menu.md, so the AI answers "how much
is tea?" from the same table this card shows. To add a dish, add one row:

    | Item | Қазақша | Русский | Price |
    | Tea  | Шай     | Чай     | 150 ₸ |
"""
from __future__ import annotations

import core
import i18n

MENU_FILE = core.KNOWLEDGE_DIR / "sdu_canteen_menu.md"
TEXTS = i18n.by_lang("canteen")  # locales/<lang>.json, section "canteen"
COLUMN = {"en": 0, "kk": 1, "ru": 2}


def load_items() -> list[tuple[str, str, str, str]] | None:
    """(English, Kazakh, Russian, price) rows; None when the file is missing or unreadable."""
    try:
        source = MENU_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    rows = []
    for line in source.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not line.strip().startswith("|") or len(cells) != 4 or cells[0] == "Item" or cells[0].startswith(":"):
            continue
        rows.append(tuple(cells))
    return rows


def render(lang: str) -> str:
    lang = core.normalize_lang(lang)
    labels = TEXTS[lang]
    items = load_items()
    if not items:
        return labels["missing"]
    lines = [f"**{labels['title']}**", ""]
    lines += [f"• {row[COLUMN[lang]]} — **{row[3]}**" for row in items]
    lines += ["", labels["note"]]
    return "\n".join(lines)
