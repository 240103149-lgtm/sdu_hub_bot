"""US11 canteen menu: the 🍽 keyboard button shows the price list.

The prices live in knowledge/sdu_canteen_menu.md, so the AI answers "how much
is tea?" from the same table this card shows. To add a dish, add one row:

    | Item | Қазақша | Русский | Price |
    | Tea  | Шай     | Чай     | 150 ₸ |
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import core
import i18n

MENU_FILE = core.KNOWLEDGE_DIR / "sdu_canteen_menu.md"
TEXTS = i18n.by_lang("canteen")  # locales/<lang>.json, section "canteen"
COLUMN = {"en": 0, "kk": 1, "ru": 2}


Names = tuple[str, str, str]  # en, kk, ru


def _names(heading: str) -> Names:
    parts = [part.strip() for part in heading.split("|")]
    return tuple((parts * 3)[:3])


def load_places() -> list[tuple[Names, list[tuple[Names, list[tuple[str, ...]]]]]] | None:
    """[(place names, [(section names, rows (en, kk, ru, price))])]; None when
    the file is missing or unreadable. "## Place: English | Қазақша | Русский"
    starts a counter, "### English | Қазақша | Русский" a section in it."""
    try:
        source = MENU_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    places: list = []
    for line in source.splitlines():
        line = line.strip()
        if line.startswith("## Place:"):
            places.append((_names(line[len("## Place:"):]), []))
            continue
        if line.startswith("### "):
            if not places:
                places.append((("", "", ""), []))
            places[-1][1].append((_names(line[4:]), []))
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not line.startswith("|") or len(cells) != 4 or cells[0] == "Item" or cells[0].startswith(":"):
            continue
        if not places:
            places.append((("", "", ""), []))
        if not places[-1][1]:
            places[-1][1].append((("", "", ""), []))
        places[-1][1][-1][1].append(tuple(cells))
    places = [(names, [sec for sec in sections if sec[1]]) for names, sections in places]
    return [place for place in places if place[1]]


def load_menu() -> list[tuple[Names, list[tuple[str, ...]]]] | None:
    """Every section of every counter, in order."""
    places = load_places()
    if places is None:
        return None
    return [section for _, sections in places for section in sections]


def render_places(lang: str) -> list[str]:
    """One card per counter (each fits in one Telegram message)."""
    lang = core.normalize_lang(lang)
    labels = TEXTS[lang]
    places = load_places()
    if not places:
        return [labels["missing"]]
    column = COLUMN[lang]
    cards = []
    for place, sections in places:
        lines = [f"**{labels['title']}**"]
        if place[column]:
            lines.append(f"📍 **{place[column]}**")
        # "(Belka Food)" keeps names unique for the AI; under its own heading it is not needed.
        brand = f" ({place[0].split(' (')[0]})" if place[0] else None
        for names, rows in sections:
            lines.append("")
            if names[column]:
                lines.append(f"**{names[column]}**")
            lines += [f"• {row[column].replace(brand, '') if brand else row[column]} — **{row[3]}**" for row in rows]
        cards.append("\n".join(lines))
    cards[-1] += "\n\n" + labels["note"]
    return cards


def render(lang: str) -> str:
    return "\n\n".join(render_places(lang))


# ------------------------------------------------------------ an order ----

MAX_QUANTITY = 50


@dataclass
class Line:
    names: tuple[str, str, str]  # en, kk, ru
    quantity: int
    price: int                   # one item, tenge

    @property
    def subtotal(self) -> int:
        return self.price * self.quantity


@dataclass
class Order:
    lines: list[Line] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)  # names not in the menu

    @property
    def total(self) -> int:
        return sum(line.subtotal for line in self.lines)


def _key(text: str) -> str:
    return re.sub(r"[^\w%]+", " ", (text or "").lower()).strip()


def _price(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def find(name: str, rows: list[tuple[str, ...]]) -> tuple[str, ...] | None:
    """The menu row for a name in any of the three languages: exact, then
    the start of a name ("Combo 2" -> "Combo 2: chicken doner ..."), then close spelling."""
    wanted = _key(name)
    if not wanted:
        return None
    by_name = {_key(row[i]): row for row in rows for i in range(3)}
    if wanted in by_name:
        return by_name[wanted]
    starts = [row for key, row in by_name.items() if key.startswith(wanted + " ") or key.split(" (")[0] == wanted]
    if len({row for row in starts}) == 1:
        return starts[0]
    close = difflib.get_close_matches(wanted, list(by_name), n=1, cutoff=0.8)
    return by_name[close[0]] if close else None


def order_total(items: list[dict]) -> Order | None:
    """Adds up an order such as [{"name": "Chicken shawarma", "quantity": 2}]
    from the menu table. None when the menu is not available."""
    menu = load_menu()
    if not menu:
        return None
    rows = [row for _, section in menu for row in section]
    order = Order()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        try:
            quantity = int(item.get("quantity") or 1)
        except (TypeError, ValueError):
            quantity = 1
        quantity = min(max(quantity, 1), MAX_QUANTITY)
        row = find(name, rows)
        if row is None:
            if name:
                order.unknown.append(name)
            continue
        for line in order.lines:  # the same dish twice: one line
            if line.names == row[:3]:
                line.quantity = min(line.quantity + quantity, MAX_QUANTITY)
                break
        else:
            order.lines.append(Line(names=tuple(row[:3]), quantity=quantity, price=_price(row[3])))
    return order


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " ₸"


def render_order(order: Order, lang: str) -> str:
    lang = core.normalize_lang(lang)
    labels = TEXTS[lang]
    lines = [f"**{labels['order_title']}**", ""]
    for line in order.lines:
        lines.append(f"• {line.names[COLUMN[lang]]} × {line.quantity} — {money(line.subtotal)}")
    if order.lines:
        lines += ["", f"**{labels['order_total'].format(total=money(order.total))}**"]
    if order.unknown:
        lines += ["", labels["order_unknown"].format(names=", ".join(order.unknown))]
    lines += ["", labels["order_note"]]
    return "\n".join(lines)
