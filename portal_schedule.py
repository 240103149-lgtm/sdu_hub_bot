"""Read the signed-in student's Course Schedule, never the campus export."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

import grade
import rooms

COURSE = re.compile(r"\b([A-Z]{2,5})\s*(\d{3}[A-Za-z]?)\b")
CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")
DAY_NAMES = (
    ("mo", "mon", "monday", "понедельник", "пн", "дүйсенбі"),
    ("tu", "tue", "tuesday", "вторник", "вт", "сейсенбі"),
    ("we", "wed", "wednesday", "среда", "ср", "сәрсенбі"),
    ("th", "thu", "thursday", "четверг", "чт", "бейсенбі"),
    ("fr", "fri", "friday", "пятница", "пт", "жұма"),
    ("sa", "sat", "saturday", "суббота", "сб", "сенбі"),
    ("su", "sun", "sunday", "воскресенье", "вс", "жексенбі"),
)


def _normal(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold().strip(".: ")


def _day(value):
    text = _normal(value)
    return next((rooms.DAYS[i] for i, names in enumerate(DAY_NAMES) if text in names), None)


def _clock(value):
    return [f"{int(m[0]):02d}:{m[1]}" for m in CLOCK.findall(str(value))]


def _course(value):
    match = COURSE.search(value)
    return f"{match[1]} {match[2]}" if match else value.strip()


def _lesson(cell, day, start, end=None):
    text = cell.get_text("\n", strip=True)
    match = COURSE.search(text)
    if not match:
        return None
    course = f"{match[1]} {match[2]}"
    room = cell.get("data-room", "")
    room_match = re.search(r"\b([A-Z]\s*\d{1,3}[A-Za-z]?)\b", text)
    if not room and room_match:
        room = room_match[1]
    if not room and re.search(r"\bonline\b|\bvirtual\b", text, re.I):
        room = "Online"
    section = re.search(r"\b\d{1,3}-[NPL]\b", text)
    title = cell.get("data-title") or cell.get("title") or ""
    if not title:
        titled = cell.find(attrs={"title": True})
        title = titled["title"] if titled else course
    return {"course": course, "title": title, "day": day, "start": start, "end": end,
            "room": room, "section": section[0] if section else "", "source": "mysdu"}


def parse_schedule(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    result = []
    recognized = False
    unparsed_class = False
    fields = {
        "day": {"day", "weekday", "день", "күн"},
        "start": {"start", "start time", "time", "уақыт", "время", "начало"},
        "end": {"end", "end time", "окончание", "аяқталуы"},
        "course": {"course", "subject", "course code", "пән", "предмет", "код пәні"},
        "title": {"course title", "course name", "subject name", "название", "пән атауы"},
        "room": {"room", "classroom", "аудитория", "кабинет"},
        "section": {"section", "секция", "топ"},
    }
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        columns = None
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            names = [_normal(cell.get_text(" ", strip=True)) for cell in cells]
            mapped = {key: next((i for i, name in enumerate(names) if name in aliases), -1)
                      for key, aliases in fields.items()}
            if all(mapped[key] >= 0 for key in ("day", "start", "course")):
                columns = mapped
                recognized = True
                continue
            if columns:
                def value(key):
                    index = columns[key]
                    return cells[index].get_text(" ", strip=True) if 0 <= index < len(cells) else ""

                day, times, course = _day(value("day")), _clock(value("start")), value("course")
                if not day or not times or not course:
                    continue
                end = _clock(value("end")) or times[1:]
                result.append({"course": _course(course), "title": value("title") or course,
                               "day": day, "start": times[0], "end": end[0] if end else None,
                               "room": value("room"), "section": value("section"), "source": "mysdu"})
        if columns:
            continue

        # Weekly grids use day columns and can span rows/columns. Keep their
        # actual time labels; a missing end time is not invented.
        grid = []
        spans = {}
        for row in rows:
            expanded = []
            col = 0
            for cell in row.find_all(["th", "td"], recursive=False):
                while (len(grid), col) in spans:
                    expanded.append(spans[(len(grid), col)])
                    col += 1
                width = max(1, min(20, int(cell.get("colspan", 1))))
                height = max(1, min(50, int(cell.get("rowspan", 1))))
                for offset in range(width):
                    expanded.append(cell)
                    for below in range(1, height):
                        spans[(len(grid) + below, col + offset)] = cell
                col += width
            while (len(grid), col) in spans:
                expanded.append(spans[(len(grid), col)])
                col += 1
            grid.append(expanded)
        day_columns = {}
        header_index = -1
        for index, row in enumerate(grid):
            matches = {i: _day(cell.get_text(" ", strip=True)) for i, cell in enumerate(row)}
            matches = {i: day for i, day in matches.items() if day}
            if len(matches) >= 3:
                day_columns, header_index = matches, index
                recognized = True
                break
        seen_cells = {}
        for row in grid[header_index + 1:] if day_columns else []:
            time_cells = [cell for i, cell in enumerate(row) if i not in day_columns]
            times = next((values for cell in time_cells if (values := _clock(cell.get_text(" ", strip=True)))), [])
            if not times:
                continue
            for column, day in day_columns.items():
                if column >= len(row):
                    continue
                cell = row[column]
                key = (id(cell), day)
                if key in seen_cells:
                    if len(times) > 1:
                        seen_cells[key]["end"] = times[-1]
                    continue
                lesson = _lesson(cell, day, times[0], times[1] if len(times) > 1 else None)
                if lesson:
                    seen_cells[key] = lesson
                    result.append(lesson)
                elif cell.get_text(" ", strip=True) not in ("", "-", "—"):
                    unparsed_class = True
    if unparsed_class:
        raise grade.PortalError("unexpected_page")
    if not recognized and not re.search(
            r"no (?:registered courses|classes|scheduled classes)|schedule is empty|нет занятий|кесте бос", soup.get_text(" "), re.I):
        raise grade.PortalError("unexpected_page")
    unique = {tuple(str(item.get(key, "")) for key in ("course", "day", "start", "room", "section")): item for item in result}
    return sorted(unique.values(), key=lambda item: (rooms.DAYS.index(item["day"]), item["start"], item["course"]))


def _same_portal(url: str) -> bool:
    target, base = urlsplit(url), urlsplit(grade.PORTAL_BASE)
    return target.scheme == base.scheme and target.netloc == base.netloc


def schedule_link(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a"):
        name = re.sub(r"[^\w]", "", anchor.get_text(" ", strip=True).casefold())
        if name not in {"courseschedule", "mytimetable", "mycourseschedule", "classschedule", "сабақкестесі", "расписаниезанятий", "меніңкестем"}:
            continue
        href = anchor.get("href", "")
        if not href or href.startswith(("#", "javascript:")):
            match = re.search(r"(?:location(?:\.href)?\s*=|(?:GoTo|load)\s*\()\s*['\"]([^'\"]+)['\"]", anchor.get("onclick", ""), re.I)
            href = match[1] if match else ""
        url = urljoin(page_url, href)
        if href and _same_portal(url):
            return url
    return None


async def fetch_schedule(cookies: dict, transport=None) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=grade.TIMEOUT,
                                 headers=grade.HEADERS, transport=transport) as client:
        for name, value in cookies.items():
            client.cookies.set(name, value, domain=urlsplit(grade.PORTAL_BASE).hostname, path="/")
        async def get(url):
            try:
                response = await client.get(url)
            except httpx.HTTPError:
                raise grade.PortalError("unavailable") from None
            soup = grade._soup(response.text)
            if not _same_portal(str(response.url)):
                raise grade.PortalError("unexpected_page")
            if response.status_code in (401, 403) or grade._find_login_form(soup) or response.text.startswith("#!"):
                raise grade.PortalError("session_expired")
            if response.status_code >= 400:
                raise grade.PortalError("unavailable")
            return response

        home = await get(grade.PORTAL_BASE + "/")
        url = schedule_link(home.text, str(home.url))
        page = await get(url) if url else home
        try:
            return parse_schedule(page.text)
        except (ValueError, TypeError, KeyError):
            raise grade.PortalError("unexpected_page") from None
