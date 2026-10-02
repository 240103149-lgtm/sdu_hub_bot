"""Client for the student portal (https://my.sdu.edu.kz) used by /grade.

Nothing here knows about Telegram or Gemini, so the same code can later back a
website endpoint too. The portal has no public API, so we behave like a
browser: open the login page, submit the form, then submit the e-mail 2FA code.

The forms are read from the portal's own HTML (field names, hidden tokens,
form action), so we do not hard-code any of them. If the portal changes its
markup, the worst case is a PortalError("unexpected_page") - never a crash.

Flow used by the bot:

    login = PortalLogin()
    needs_code = await login.submit_credentials(student_id, password)
    if needs_code:
        await login.submit_code(code_from_email)
    cookies = login.cookies()      # keep these, NOT the password
    await login.close()
    ...
    rows = await fetch_grades(cookies)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

PORTAL_BASE = os.getenv("PORTAL_BASE_URL", "https://my.sdu.edu.kz").strip().rstrip("/")
LOGIN_URL = os.getenv("PORTAL_LOGIN_URL", "").strip() or PORTAL_BASE + "/"
# The Transcript page: every semester with its courses, grades and GPA.
GRADES_URL = os.getenv("PORTAL_GRADES_URL", "").strip() or PORTAL_BASE + "/index.php?mod=transkript"
TIMEOUT = float(os.getenv("PORTAL_TIMEOUT_SECONDS", "15"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UniversityHubBot/1.0)",
    "Accept-Language": "en",
}

TEXT_INPUT_TYPES = {"", "text", "number", "tel", "email", "search"}
CODE_HINTS = ("code", "otp", "token", "verif", "2fa", "pin", "код", "растау")


class PortalError(Exception):
    """A failure the bot can explain to the student.

    `key` is one of: bad_credentials, bad_code, unavailable, unexpected_page,
    session_expired, session_lost, grades_not_configured.
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


# ------------------------------------------------------------ HTML helpers --


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _has_password(form: Tag) -> bool:
    return form.find("input", attrs={"type": "password"}) is not None


def _text_inputs(form: Tag) -> list[Tag]:
    return [
        i
        for i in form.find_all("input")
        if i.get("name") and (i.get("type") or "text").lower() in TEXT_INPUT_TYPES
    ]


def _is_code_input(field: Tag) -> bool:
    blob = " ".join(
        str(field.get(a) or "") for a in ("name", "id", "placeholder", "autocomplete", "aria-label")
    ).lower()
    return any(hint in blob for hint in CODE_HINTS)


def _find_login_form(soup: BeautifulSoup) -> Tag | None:
    for form in soup.find_all("form"):
        if _has_password(form) and _text_inputs(form):
            return form
    return None


def _find_code_form(soup: BeautifulSoup) -> Tag | None:
    """The 'enter the code we e-mailed you' form, or None.

    A form counts when one of its inputs looks like a code field, or when it
    has a single text input and the page itself talks about a code - so an
    ordinary search box on a dashboard is not mistaken for the 2FA step.
    """
    page_text = soup.get_text(" ").lower()
    mentions_code = any(word in page_text for word in ("code", "код"))
    for form in soup.find_all("form"):
        if _has_password(form):
            continue
        inputs = _text_inputs(form)
        if not inputs:
            continue
        if any(_is_code_input(i) for i in inputs) or (len(inputs) == 1 and mentions_code):
            return form
    return None


def _payload(form: Tag) -> dict[str, str]:
    """Everything the browser would send, e.g. hidden CSRF tokens included."""
    data: dict[str, str] = {}
    for field in form.find_all("input"):
        name = field.get("name")
        if not name:
            continue
        kind = (field.get("type") or "text").lower()
        if kind in ("button", "image", "file", "reset", "submit"):
            continue
        if kind in ("checkbox", "radio") and not field.has_attr("checked"):
            continue
        data[name] = field.get("value") or ""
    submit = form.find(["input", "button"], attrs={"type": "submit"})
    if submit is not None and submit.get("name"):
        data[submit["name"]] = submit.get("value") or ""
    return data


# ------------------------------------------------------------------- login --


class PortalLogin:
    """One sign-in in progress. Holds the HTTP session between the password
    step and the 2FA step, so the cookies the portal sets stay together."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.client = httpx.AsyncClient(
            follow_redirects=True, timeout=TIMEOUT, headers=HEADERS, transport=transport
        )
        self._code_form: Tag | None = None
        self._code_url = ""

    async def _submit(self, form: Tag, page_url: str, data: dict[str, str]) -> httpx.Response:
        action = urljoin(str(page_url), form.get("action") or "")
        # A form with a password must never be sent as GET (it would end up in the URL).
        method = "post" if _has_password(form) else (form.get("method") or "get").lower()
        try:
            if method == "post":
                response = await self.client.post(action, data=data)
            else:
                response = await self.client.get(action, params=data)
        except httpx.HTTPError as exc:
            raise PortalError("unavailable") from exc
        if response.status_code >= 500:
            raise PortalError("unavailable")
        return response

    async def submit_credentials(self, student_id: str, password: str) -> bool:
        """Sign in. Returns True if the portal now wants the e-mail 2FA code."""
        try:
            page = await self.client.get(LOGIN_URL)
        except httpx.HTTPError as exc:
            raise PortalError("unavailable") from exc
        if page.status_code >= 500:
            raise PortalError("unavailable")

        form = _find_login_form(_soup(page.text))
        if form is None:
            print(f"[grade] no login form found at {page.url} (HTTP {page.status_code})")
            raise PortalError("unexpected_page")

        password_field = form.find("input", attrs={"type": "password"})
        id_field = _text_inputs(form)[0]
        data = _payload(form)
        data[id_field["name"]] = student_id
        data[password_field["name"]] = password

        response = await self._submit(form, str(page.url), data)
        if response.status_code in (401, 403):
            raise PortalError("bad_credentials")

        soup = _soup(response.text)
        if _find_login_form(soup) is not None:
            # Bounced back to the login page: wrong student number or password.
            raise PortalError("bad_credentials")

        code_form = _find_code_form(soup)
        if code_form is not None:
            self._code_form = code_form
            self._code_url = str(response.url)
            return True
        return False

    async def submit_code(self, code: str) -> None:
        """Send the 2FA code from the e-mail. Raises PortalError("bad_code")
        if the portal rejects it; the same PortalLogin can then try again."""
        form = self._code_form
        if form is None:
            raise PortalError("session_lost")

        inputs = _text_inputs(form)
        field = next((i for i in inputs if _is_code_input(i)), inputs[0])
        data = _payload(form)
        data[field["name"]] = code

        response = await self._submit(form, self._code_url, data)
        if response.status_code in (400, 401, 403, 422):
            raise PortalError("bad_code")

        soup = _soup(response.text)
        if _find_login_form(soup) is not None:
            raise PortalError("bad_code")
        again = _find_code_form(soup)
        if again is not None:  # the portal is asking for the code once more
            self._code_form = again
            self._code_url = str(response.url)
            raise PortalError("bad_code")
        self._code_form = None

    def cookies(self) -> dict[str, str]:
        """The portal session. Treat it like a password: it is what proves the
        student is signed in, so the bot keeps it instead of the real password."""
        return {cookie.name: cookie.value for cookie in self.client.cookies.jar}

    async def close(self) -> None:
        await self.client.aclose()


# ------------------------------------------------------------------ grades --


def parse_grades(soup: BeautifulSoup) -> list[list[str]]:
    """Rows of the biggest table on the page, header first.

    Generic on purpose: it works for any plain HTML table. Once we have the
    real grades page we can replace this with exact, per-column parsing.
    """
    tables = soup.find_all("table")
    if not tables:
        return []
    biggest = max(tables, key=lambda table: len(table.find_all("tr")))
    rows: list[list[str]] = []
    for tr in biggest.find_all("tr"):
        cells = [" ".join(cell.get_text(" ").split()) for cell in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    return rows


async def fetch_grades(
    cookies: dict[str, str], transport: httpx.AsyncBaseTransport | None = None
) -> list[list[str]]:
    """Open the grades page with a saved portal session and read it."""
    if not GRADES_URL:
        raise PortalError("grades_not_configured")
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=TIMEOUT, headers=HEADERS, transport=transport
    ) as client:
        try:
            response = await client.get(GRADES_URL)
        except httpx.HTTPError as exc:
            raise PortalError("unavailable") from exc

    if response.status_code in (401, 403):
        raise PortalError("session_expired")
    if response.status_code >= 500:
        raise PortalError("unavailable")

    soup = _soup(response.text)
    if _find_login_form(soup) is not None:  # sent back to the login page
        raise PortalError("session_expired")
    return parse_grades(soup)


# -------------------------------------------------------------- transcript --

SEMESTER_RE = re.compile(r"^\d{4}\s*-\s*\d{4}\.\s*\d+$")  # "2024 - 2025. 1"
COURSE_CODE_RE = re.compile(r"^[A-Z]{2,5}\s*\d{3}[A-Za-z]?$")  # "CSS 105"
NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")


@dataclass
class Course:
    code: str
    name: str
    credits: str
    ects: str
    score: str
    letter: str
    points: str
    status: str


@dataclass
class Semester:
    title: str  # "2024 - 2025. 1"
    courses: list[Course] = field(default_factory=list)
    credits: str = ""
    ects: str = ""
    spa: str = ""  # this semester's GPA
    gpa: str = ""  # cumulative GPA after this semester


def _cells(tr: Tag) -> list[str]:
    return [" ".join(cell.get_text(" ").split()) for cell in tr.find_all(["td", "th"])]


def _labelled(text: str, label: str) -> str:
    match = re.search(rf"\b{label}\s*:\s*([\d.]+)", text)
    return match.group(1) if match else ""


def parse_transcript(soup: BeautifulSoup) -> list[Semester]:
    """Semesters of the Transcript page, oldest first.

    Works on the text of each table row, so it does not depend on CSS classes
    or column positions beyond: a semester title row ("2024 - 2025. 1"),
    course rows that start with a course code, and a totals row with SPA/GPA.
    Semesters without any course are dropped.
    """
    semesters: list[Semester] = []
    current: Semester | None = None
    for tr in soup.find_all("tr"):
        if tr.find("tr") is not None:  # a layout table that wraps other tables
            continue
        cells = _cells(tr)
        text = " ".join(c for c in cells if c)
        if not text:
            continue

        if SEMESTER_RE.match(text):
            current = Semester(title=text)
            semesters.append(current)
        elif current is None:
            continue
        elif "GPA" in text and "SPA" in text:
            numbers = [c for c in cells if NUMBER_RE.match(c)]
            current.credits = numbers[0] if len(numbers) > 0 else ""
            current.ects = numbers[1] if len(numbers) > 1 else ""
            current.spa = _labelled(text, "SPA")
            current.gpa = _labelled(text, "GPA")
        elif len(cells) >= 6 and COURSE_CODE_RE.match(cells[0]):
            cells = (cells + [""] * 8)[:8]
            current.courses.append(Course(*cells))
    return [s for s in semesters if s.courses]


async def fetch_transcript(
    cookies: dict[str, str], transport: httpx.AsyncBaseTransport | None = None
) -> list[Semester]:
    """Download the Transcript page with a saved portal session and parse it."""
    async with httpx.AsyncClient(
        cookies=cookies, follow_redirects=True, timeout=TIMEOUT, headers=HEADERS, transport=transport
    ) as client:
        try:
            response = await client.get(GRADES_URL)
        except httpx.HTTPError as exc:
            raise PortalError("unavailable") from exc

    if response.status_code in (401, 403):
        raise PortalError("session_expired")
    if response.status_code >= 500:
        raise PortalError("unavailable")

    soup = _soup(response.text)
    if _find_login_form(soup) is not None:  # sent back to the login page
        raise PortalError("session_expired")
    semesters = parse_transcript(soup)
    if not semesters:
        print(f"[grade] transcript page at {response.url} had no recognisable semesters")
    return semesters
