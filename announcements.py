"""SDU academic news discovery and Telegram subscriptions."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
from telegram.error import Forbidden
from google import genai
from google.genai import types

import core
import i18n

log = logging.getLogger(__name__)
FEEDS = {"kk": "https://sdu.edu.kz/feed/", "ru": "https://sdu.edu.kz/ru/feed/",
         "en": "https://sdu.edu.kz/en/feed/"}
ACCOUNTS = {
    "Instagram": os.getenv("SDU_INSTAGRAM_HANDLE", "sdukz").lower(),
    "Facebook": os.getenv("SDU_FACEBOOK_HANDLE", "sdukz").lower(),
    "X": os.getenv("SDU_X_HANDLE", "").lower(),
}
LOCAL_TZ = timezone(timedelta(hours=5))
ACADEMICS = "academics"
ACADEMIC_TERMS = re.compile(
    r"оқу|білім|емтихан|сабақ|зертте|ғылым|аккредит|бағдарлама|грант|шәкіртақы|"
    r"уч[её]б|обучени|образован|экзамен|исследован|научн|академ|стипенди|"
    r"academ|research|curricul|course|education|exam|accredit|scholarship|"
    r"degree program|research grant|student exchange|exchange program|admission",
    re.I,
)
SOCIAL_EVENT_TITLES = re.compile(
    r"welcome part|freshers.? part|sdu fest|\bparty\b|фестиваль|вечеринк|сауық кеш|қарсы алу кеш",
    re.I,
)
NEWS_WORDS = {
    "kk": r"жаңалық|не жаң[ао]|не болып жатыр|не өтіп жатыр|хабарландыру|іс.?шара|қандай (жаңа |соңғы )?(грант|мүмкіндік)",
    "ru": r"новост|что нового|что происходит|что проходит|объявлени|мероприяти|событи[яй]|какие (новые )?(гранты|возможности)",
    "en": r"\bnews\b|what.?s (new|happening|going on)|what is (new|happening|going on)|\bannouncements?\b|\bevents?\b|(?:any|what|new|latest) (?:grants|opportunities|scholarships)",
}
TEXTS = i18n.by_lang("announcements")


def label(key: str, lang: str) -> str:
    return TEXTS[lang][key]


def news_intent(question: str, default_lang: str) -> str | None:
    question = question.casefold()
    languages = [lang for lang, pattern in NEWS_WORDS.items() if re.search(pattern, question)]
    if not languages:
        return None
    has_context = re.search(r"\b(sdu|сду|универ\w*|university|campus|кампус)\b", question)
    general_news = re.search(r"жаңалық|хабарландыру|новост|объявлени|\bnews\b|\bannouncements?\b", question)
    if not general_news and re.search(r"\bevents?\b|мероприяти|іс.?шара", question):
        return None
    if not (has_context or general_news):
        return None
    lang = default_lang if default_lang in languages else languages[0]
    return lang


class FeedText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(html: str) -> str:
    parser = FeedText()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


@dataclass(frozen=True)
class News:
    title: str
    url: str
    published: datetime
    summary: str
    source: str

    def is_academic(self) -> bool:
        return not SOCIAL_EVENT_TITLES.search(self.title) and bool(
            ACADEMIC_TERMS.search(f"{self.title} {self.summary}")
        )

    def render(self) -> str:
        date = self.published.astimezone(LOCAL_TZ).date()
        return f"**{self.title}** · {date} · {self.source}\n{self.summary}\n{self.url}"


def source_for(url: str, instagram_verified: bool = False) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        return None
    host = parts.hostname
    path = parts.path.strip("/").lower().split("/")
    if host in {"sdu.edu.kz", "www.sdu.edu.kz"} and path[0]:
        return "SDU website"
    if host in {"www.instagram.com", "instagram.com"} and len(path) == 2 and path[0] in {"p", "reel"}:
        if ACCOUNTS["Instagram"] and instagram_verified:
            return "Instagram"
    if host in {"www.facebook.com", "facebook.com", "m.facebook.com"} and len(path) >= 3:
        if path[0] == ACCOUNTS["Facebook"] and path[1] in {"posts", "videos"}:
            return "Facebook"
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} and len(path) == 3:
        if path[0] == ACCOUNTS["X"] and path[1] == "status" and path[2].isdigit():
            return "X"
    return None


def parse_feed(xml: bytes) -> list[News]:
    entries = []
    for item in ET.fromstring(xml).findall("./channel/item"):
        title = plain(item.findtext("title", ""))
        url = item.findtext("link", "").strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
        except (TypeError, ValueError):
            continue
        if title and source_for(url) == "SDU website" and published.tzinfo:
            entries.append(News(title[:250], url, published, plain(item.findtext("description", ""))[:450], "SDU website"))
    return entries


async def website_news(client: httpx.AsyncClient, lang: str) -> list[News]:
    response = await client.get(FEEDS[lang])
    response.raise_for_status()
    return parse_feed(response.content)


def canonical(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.username or parts.password:
        return ""
    host = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
    if host == "twitter.com":
        host = "x.com"
    return f"https://{host}{parts.path.rstrip('/')}"


async def grounding_url(client: httpx.AsyncClient, url: str) -> str:
    parts = urlsplit(url)
    if parts.netloc == "vertexaisearch.cloud.google.com" and parts.scheme == "https":
        if parts.path.startswith("/grounding-api-redirect/"):
            response = await client.get(url, follow_redirects=False)
            return response.headers.get("location", "")
    return url


async def instagram_author(client: httpx.AsyncClient, url: str) -> bool:
    """Use the public embed's author link to distinguish accounts sharing a post."""
    try:
        response = await client.get(f"{url}/embed/", follow_redirects=False)
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    handle = re.escape(ACCOUNTS["Instagram"])
    return bool(re.search(
        rf'''<a\b(?=[^>]*\bclass=["'][^"']*\bUsername\b)(?=[^>]*\bhref=["'](?:https://www\.instagram\.com)?/{handle}/(?:\?[^"']*)?["'])''',
        response.text,
        re.I,
    ))


def social_item(row: dict, url: str, source: str) -> News | None:
    if any(not isinstance(row.get(key), str) for key in ("date", "title", "summary")):
        return None
    try:
        published = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None
    today = datetime.now(LOCAL_TZ).date()
    title = plain(row["title"])[:250]
    if title and today - timedelta(days=30) <= published.date() <= today:
        return News(title, url, published, plain(row["summary"])[:450], source)
    return None


def curated_news(lang: str) -> list[News]:
    """Allow a maintainer to supply posts verified outside public search."""
    records = json.loads(os.getenv("SDU_CURATED_POSTS", "[]"))
    items = []
    for row in records:
        if not isinstance(row, dict) or row.get("lang") != lang or not isinstance(row.get("url"), str):
            continue
        url = canonical(row["url"])
        source = source_for(url, instagram_verified=True)
        if source in ACCOUNTS and (item := social_item(row, url, source)):
            items.append(item)
    return items


async def social_news(client: httpx.AsyncClient, lang: str) -> list[News]:
    domains = {"Instagram": "instagram.com", "Facebook": "facebook.com", "X": "x.com or twitter.com"}
    targets = ", ".join(f"@{handle} on {domains[site]}" for site, handle in ACCOUNTS.items() if handle)
    if not targets:
        return []
    model = os.getenv("GEMINI_MODEL") or "gemini-flash-latest"
    if core.client is not None:
        return await search_social(client, core.client.aio, model, lang, targets)
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Social search needs GEMINI_API_KEY")
    async with genai.Client(api_key=key).aio as gemini:
        return await search_social(client, gemini, model, lang, targets)


async def search_social(client: httpx.AsyncClient, gemini, model: str, lang: str, targets: str) -> list[News]:
    response = await gemini.models.generate_content(
        model=model,
        contents=(
            f"Today is {datetime.now(timezone.utc).date()}. Search the public web for dated announcements "
            f"from SDU University in Kaskelen, Kazakhstan. Search each account separately: {targets}. "
            "Use only posts published by those accounts in the last 30 days about study programmes, "
            "exams, teaching, research, accreditation or academic scholarships. "
            "Skip parties, festivals and general campus event listings. "
            f"Return a JSON array of up to eight objects with title and summary in {core.LANGS[lang]}, "
            "date as YYYY-MM-DD, and url as the exact cited public post permalink. "
            "Do not infer a date or URL. Omit undated posts, profile pages, and inaccessible results. "
            "Cite the actual indexed posts through Google Search grounding. Return [] if no posts qualify."
        ),
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
    )
    if not response.candidates:
        return []
    metadata = response.candidates[0].grounding_metadata
    if not metadata or not metadata.grounding_supports:
        return []
    used = {index for support in metadata.grounding_supports for index in support.grounding_chunk_indices or []}
    web = [chunk.web for i, chunk in enumerate(metadata.grounding_chunks or [])
           if i in used and chunk.web and chunk.web.uri][:20]
    resolved = await asyncio.gather(*(grounding_url(client, chunk.uri) for chunk in web), return_exceptions=True)
    evidence = {canonical(url) for url in resolved if isinstance(url, str)}
    raw = (response.text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", raw, flags=re.I)
    records = json.loads(raw)
    if not isinstance(records, list):
        return []
    verified = []
    for row in records[:8]:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        url = canonical(row["url"])
        source = source_for(url, instagram_verified=True)
        if url in evidence and source in ACCOUNTS:
            verified.append((row, url, source))
    authors = await asyncio.gather(*(instagram_author(client, url) for _, url, site in verified if site == "Instagram"))
    instagram_authors = iter(authors)
    items = []
    for row, url, source in verified:
        if source == "Instagram" and not next(instagram_authors):
            continue
        if item := social_item(row, url, source):
            items.append(item)
    return items


async def collect(lang: str) -> tuple[list[News], bool]:
    async with httpx.AsyncClient(timeout=5) as client:
        results = await asyncio.gather(
            website_news(client, lang), asyncio.wait_for(social_news(client, lang), timeout=12),
            return_exceptions=True,
        )
    failed = False
    try:
        curated = curated_news(lang)
    except (TypeError, ValueError):
        log.warning("SDU_CURATED_POSTS is invalid")
        curated = []
        failed = True
    items = {item.url: item for item in curated if item.is_academic()}
    for result in results:
        if isinstance(result, Exception):
            log.warning("SDU announcement source failed: %s", type(result).__name__)
            failed = True
        else:
            items.update((canonical(item.url), item) for item in result if item.is_academic())
    return sorted(items.values(), key=lambda item: item.published, reverse=True), failed


async def answer(lang: str) -> str:
    items, partial = await collect(lang)
    lines = [f"**{label('heading', lang)}**"]
    lines.extend(item.render() for item in items[:5])
    if not items:
        lines.append(label("empty", lang))
    if partial:
        lines.append(label("limited", lang))
    return "\n\n".join(lines)


REDIS_URL = os.getenv("ANNOUNCEMENTS_REDIS_URL", "").rstrip("/")
REDIS_TOKEN = os.getenv("ANNOUNCEMENTS_REDIS_TOKEN", "")
SUBSCRIPTIONS = "sdu:announcements:subscriptions"


async def redis(*command):
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(REDIS_URL, json=list(command),
                                     headers={"Authorization": f"Bearer {REDIS_TOKEN}"})
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError("Announcement storage command failed")
        return payload["result"]


def subscriptions_enabled() -> bool:
    return bool(REDIS_URL and REDIS_TOKEN)


async def subscribe(chat_id: int, lang: str, enabled: bool) -> None:
    field = f"{chat_id}:{ACADEMICS}"
    if enabled:
        await redis("HSETNX", SUBSCRIPTIONS, field, json.dumps([lang, time.time()]))
    else:
        await redis("HDEL", SUBSCRIPTIONS, field)


async def notify(bot) -> int:
    entries = await redis("HGETALL", SUBSCRIPTIONS)
    subscriptions = {
        field: value for field, value in zip(entries[::2], entries[1::2])
        if field.endswith(f":{ACADEMICS}")
    }
    languages = {json.loads(value)[0] for value in subscriptions.values()}
    news = dict(zip(languages, await asyncio.gather(*(collect(lang) for lang in languages))))
    sent = 0
    for field, value in subscriptions.items():
        chat, _ = field.split(":")
        lang, since = json.loads(value)
        for item in reversed(news[lang][0]):
            # A date-only post is considered new from the next local calendar day.
            if item.published.timestamp() <= since:
                continue
            if await redis("HGET", SUBSCRIPTIONS, field) != value:
                break
            digest = sha256(canonical(item.url).encode()).hexdigest()
            key = f"sdu:announcements:sent:{chat}:{digest}"
            if not await redis("SET", key, "1", "NX", "EX", "3888000"):
                continue
            try:
                await bot.send_message(
                    chat_id=int(chat), text=f"{label('notification', lang)}\n\n{item.render().replace('**', '')}",
                    disable_web_page_preview=True,
                )
            except Forbidden:
                await redis("HDEL", SUBSCRIPTIONS, field)
                break
            except Exception as exc:
                await redis("DEL", key)
                log.warning("SDU announcement delivery failed: %s", type(exc).__name__)
            else:
                sent += 1
    return sent
