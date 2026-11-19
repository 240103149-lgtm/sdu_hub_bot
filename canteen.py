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


def load_menu() -> list[tuple[tuple[str, str, str], list[tuple[str, ...]]]] | None:
    """[(section names (en, kk, ru), rows (en, kk, ru, price))]; None when the
    file is missing or unreadable. A section starts with "## English | Қазақша | Русский"."""
    try:
        source = MENU_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    sections: list = []
    for line in source.splitlines():
        line = line.strip()
        if line.startswith("## "):
            names = [part.strip() for part in line[3:].split("|")]
            sections.append((tuple((names * 3)[:3]), []))
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not line.startswith("|") or len(cells) != 4 or cells[0] == "Item" or cells[0].startswith(":"):
            continue
        if not sections:
            sections.append((("", "", ""), []))
        sections[-1][1].append(tuple(cells))
    return [section for section in sections if section[1]]


def render(lang: str) -> str:
    lang = core.normalize_lang(lang)
    labels = TEXTS[lang]
    menu = load_menu()
    if not menu:
        return labels["missing"]
    column = COLUMN[lang]
    lines = [f"**{labels['title']}**"]
    for names, rows in menu:
        lines.append("")
        if names[column]:
            lines.append(f"**{names[column]}**")
        lines += [f"• {row[column]} — **{row[3]}**" for row in rows]
    lines += ["", labels["note"]]
    return "\n".join(lines)
