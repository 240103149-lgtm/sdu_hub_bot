"""US8 Document Submission: required enrollment documents, file checks and
the database of submitted documents.

Like rooms.py it knows nothing about Telegram, so the website could use the
same rules later:

    catalog = load_catalog()      # data/required_documents.json
    problem = check_file(doc_type, file_name, mime_type, size, catalog.max_bytes)
    db = store()                  # data/documents.db (SQLite)
    db.start(...)  db.save(...)  db.submit(...)  db.review(...)

The checklist is copied from the official SDU admission page (see "source"
and "checked" in the JSON file). When the admissions office changes the list,
only that file is edited; it is re-read automatically.

Document Management System
--------------------------
The user story asks for the university's Document Management stored
procedures. They are not available to this project yet, so DocumentStore keeps
the same operations in a local SQLite database. Each public method runs as one
transaction and stands for one procedure:

    start()            choose / change the enrollment track
    save()             register an uploaded file
    submit()           check completeness, send the files for review
    review()           record the admissions office's decision
    get()              the submission and its current files
    pending_reviews()  files waiting for a decision

Connecting the real DMS means replacing the bodies of these methods with
calls to its procedures; the bot does not change.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

BASE_DIR = Path(__file__).parent
CATALOG_FILE = BASE_DIR / "data" / "required_documents.json"

# Vercel can write only to /tmp, which is wiped between cold starts - the same
# limitation as bot_state.pickle. Use a persistent host (or the real DMS) there.
DB_PATH = Path(
    os.getenv("DOCUMENTS_DB_PATH", "").strip()
    or (
        Path(tempfile.gettempdir()) / "documents.db"
        if os.getenv("VERCEL")
        else BASE_DIR / "data" / "documents.db"
    )
)

# Status of one uploaded file...
UPLOADED = "uploaded"          # received, not sent for review yet
UNDER_REVIEW = "under_review"  # submitted, waiting for the admissions office
VERIFIED = "verified"
REJECTED = "rejected"          # the student has to upload it again
REPLACED = "replaced"          # a newer upload of the same document exists
MISSING = "missing"            # never stored: no file of that document yet

# ...and of the whole submission (UNDER_REVIEW and VERIFIED are shared).
DRAFT = "draft"
NEEDS_CORRECTION = "needs_correction"

REJECT_REASONS = ("unreadable", "wrong_document", "invalid")

# File formats. The extension and the MIME type the client reports must agree.
EXTENSIONS = {"pdf": "pdf", "jpg": "jpg", "jpeg": "jpg", "png": "png"}
MIME_TYPES = {
    "pdf": {"application/pdf", "application/x-pdf"},
    "jpg": {"image/jpeg", "image/jpg", "image/pjpeg"},
    "png": {"image/png"},
}

LANG_FALLBACK = "en"
ID_PATTERN = re.compile(r"^[a-z0-9_]{1,24}$")  # short: it travels in button data


def _pick(texts: dict[str, str], lang: str) -> str:
    return texts.get(lang) or texts.get(LANG_FALLBACK) or next(iter(texts.values()), "")


# ---------------------------------------------------------------- catalog ----


class CatalogError(Exception):
    """data/required_documents.json is missing or malformed."""


@dataclass(frozen=True, eq=False)
class DocType:
    id: str
    tracks: tuple[str, ...]
    required: bool
    formats: tuple[str, ...]
    names: dict[str, str]
    shorts: dict[str, str]
    when: dict[str, str]  # optional documents: when they apply

    def label(self, lang: str, short: bool = False) -> str:
        return _pick(self.shorts, lang) if short and self.shorts else _pick(self.names, lang)

    def condition(self, lang: str) -> str:
        return _pick(self.when, lang) if self.when else ""


@dataclass(frozen=True, eq=False)
class Catalog:
    source: str
    checked: str          # YYYY-MM-DD: when the list was compared with the source
    max_bytes: int
    tracks: dict[str, dict[str, str]]  # track id -> name per language, file order
    documents: tuple[DocType, ...]

    def get(self, doc_id: str) -> DocType | None:
        return next((doc for doc in self.documents if doc.id == doc_id), None)

    def for_track(self, track: str) -> list[DocType]:
        return [doc for doc in self.documents if track in doc.tracks]

    def required(self, track: str) -> list[DocType]:
        return [doc for doc in self.for_track(track) if doc.required]

    def track_name(self, track: str, lang: str) -> str:
        return _pick(self.tracks.get(track) or {track: track}, lang)


def parse_catalog(raw: Any) -> Catalog:
    """Validate the JSON so a typo fails loudly instead of confusing students."""
    if not isinstance(raw, dict):
        raise ValueError("the file must contain a JSON object")

    tracks: dict[str, dict[str, str]] = {}
    for item in raw["tracks"]:
        track_id = str(item["id"])
        if not ID_PATTERN.match(track_id) or track_id in tracks:
            raise ValueError(f"bad or repeated track id {track_id!r}")
        tracks[track_id] = dict(item["name"])
    if not tracks:
        raise ValueError("no tracks")

    docs: list[DocType] = []
    for item in raw["documents"]:
        doc_id = str(item["id"])
        if not ID_PATTERN.match(doc_id) or any(d.id == doc_id for d in docs):
            raise ValueError(f"bad or repeated document id {doc_id!r}")
        doc_tracks = tuple(item["tracks"])
        formats = tuple(item["formats"])
        if not doc_tracks or not set(doc_tracks) <= set(tracks):
            raise ValueError(f"{doc_id}: unknown track in {doc_tracks}")
        if not formats or not set(formats) <= set(MIME_TYPES):
            raise ValueError(f"{doc_id}: formats must be among {sorted(MIME_TYPES)}")
        names = dict(item["name"])
        if LANG_FALLBACK not in names:
            raise ValueError(f"{doc_id}: an English name is required")
        docs.append(
            DocType(
                id=doc_id,
                tracks=doc_tracks,
                required=bool(item.get("required", True)),
                formats=formats,
                names=names,
                shorts=dict(item.get("short") or {}),
                when=dict(item.get("when") or {}),
            )
        )
    if not docs:
        raise ValueError("no documents")

    max_mb = float(raw.get("max_file_mb", 10))
    if max_mb <= 0:
        raise ValueError("max_file_mb must be positive")
    return Catalog(
        source=str(raw.get("source", "")),
        checked=str(raw.get("checked", "")),
        max_bytes=int(max_mb * 1024 * 1024),
        tracks=tracks,
        documents=tuple(docs),
    )


_catalog_cache: tuple[Path, float, Catalog] | None = None


def load_catalog() -> Catalog:
    """The checklist, re-read only when the file has changed on disk."""
    global _catalog_cache
    path = CATALOG_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise CatalogError(f"{path} not found") from exc
    if _catalog_cache is None or _catalog_cache[:2] != (path, mtime):
        try:
            catalog = parse_catalog(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CatalogError(f"{path.name} could not be read: {exc}") from exc
        _catalog_cache = (path, mtime, catalog)
    return _catalog_cache[2]


# ------------------------------------------------------- validation rules ----


def _extension(file_name: str | None) -> str:
    name = (file_name or "").strip()
    return name.rsplit(".", 1)[1].strip().lower() if "." in name else ""


def file_format(file_name: str | None, mime_type: str | None) -> str | None:
    """'pdf', 'jpg' or 'png' - or None for anything else.

    A known extension and a known MIME type must agree, so a picture renamed
    to .pdf is not accepted as a PDF. A generic MIME type (octet-stream) is
    left to the extension.
    """
    extension = _extension(file_name)
    by_name = EXTENSIONS.get(extension) if extension else None
    if extension and by_name is None:
        return None  # .docx, .heic, .zip ...
    mime = (mime_type or "").split(";")[0].strip().lower()
    by_mime = next((fmt for fmt, types in MIME_TYPES.items() if mime in types), None)
    if by_name and by_mime and by_name != by_mime:
        return None
    return by_name or by_mime


def describe_format(file_name: str | None, mime_type: str | None) -> str:
    """What the student actually sent, for the error message: 'DOCX', 'image/heic'."""
    extension = _extension(file_name)
    return extension.upper() if extension else (mime_type or "?")


def check_file(
    doc: DocType, file_name: str | None, mime_type: str | None, size: int | None, max_bytes: int
) -> str | None:
    """None if the file is acceptable for `doc`, else 'bad_format' or 'too_large'."""
    fmt = file_format(file_name, mime_type)
    if fmt is None or fmt not in doc.formats:
        return "bad_format"
    if size is not None and size > max_bytes:
        return "too_large"
    return None


# ----------------------------------------------------------------- status ----


@dataclass(frozen=True)
class StoredDocument:
    id: int
    student_id: str
    doc_type: str
    kind: str              # "document" or "photo": how Telegram sends it back
    file_id: str           # the file itself stays on Telegram's servers
    file_name: str
    mime_type: str
    file_size: int | None
    status: str
    reason: str            # why it was rejected
    reviewer: str
    uploaded_at: str       # ISO 8601, UTC
    reviewed_at: str


@dataclass(frozen=True)
class Submission:
    student_id: str
    chat_id: int           # where real-time status updates are sent
    track: str
    status: str
    submitted_at: str
    updated_at: str
    documents: dict[str, StoredDocument]  # the current file of each document


@dataclass(frozen=True)
class SubmitResult:
    missing: list[DocType]       # non-empty: submission blocked
    sent: list[StoredDocument]   # files that went to review now
    submission: Submission


@dataclass(frozen=True)
class ReviewResult:
    document: StoredDocument
    submission: Submission
    completed: bool  # this decision made the whole submission verified


def missing_required(catalog: Catalog, track: str, docs: dict[str, StoredDocument]) -> list[DocType]:
    """Required documents with no file yet, or whose file was rejected."""
    return [
        doc for doc in catalog.required(track)
        if doc.id not in docs or docs[doc.id].status == REJECTED
    ]


def derive_status(
    catalog: Catalog, track: str, submitted_at: str | None, docs: dict[str, StoredDocument]
) -> str:
    """The submission status, recomputed after every change."""
    relevant = {doc.id: docs[doc.id] for doc in catalog.for_track(track) if doc.id in docs}
    if not submitted_at:
        return DRAFT
    if any(d.status == REJECTED for d in relevant.values()):
        return NEEDS_CORRECTION
    if any(d.status == UPLOADED for d in relevant.values()) or missing_required(catalog, track, docs):
        return DRAFT  # something new (or a changed track) still has to be submitted
    if not any(d.status == UNDER_REVIEW for d in relevant.values()):
        return VERIFIED
    return UNDER_REVIEW


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- database ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    student_id    TEXT PRIMARY KEY,      -- university student number
    chat_id       INTEGER NOT NULL,
    track         TEXT NOT NULL,         -- grant | paid
    status        TEXT NOT NULL,         -- draft | under_review | needs_correction | verified
    submitted_at  TEXT,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submitted_documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id    TEXT NOT NULL REFERENCES submissions(student_id),
    doc_type      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    file_id       TEXT NOT NULL,
    file_name     TEXT,
    mime_type     TEXT,
    file_size     INTEGER,
    status        TEXT NOT NULL,         -- uploaded | under_review | verified | rejected | replaced
    reason        TEXT,
    reviewer      TEXT,
    uploaded_at   TEXT NOT NULL,
    reviewed_at   TEXT
);
CREATE INDEX IF NOT EXISTS submitted_documents_by_student
    ON submitted_documents (student_id, doc_type);
CREATE INDEX IF NOT EXISTS submitted_documents_by_status
    ON submitted_documents (status);
"""


def _document(row: sqlite3.Row) -> StoredDocument:
    return StoredDocument(
        id=row["id"],
        student_id=row["student_id"],
        doc_type=row["doc_type"],
        kind=row["kind"],
        file_id=row["file_id"],
        file_name=row["file_name"] or "",
        mime_type=row["mime_type"] or "",
        file_size=row["file_size"],
        status=row["status"],
        reason=row["reason"] or "",
        reviewer=row["reviewer"] or "",
        uploaded_at=row["uploaded_at"],
        reviewed_at=row["reviewed_at"] or "",
    )


class DocumentStore:
    """The table of submitted documents. Every upload is a new row, so the
    history is kept; the newest row of each document is the current file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit on success, roll back on any error
                yield conn
        finally:
            conn.close()

    # -------------------------------------------------------- helpers ----

    @staticmethod
    def _current(conn: sqlite3.Connection, student_id: str) -> dict[str, StoredDocument]:
        rows = conn.execute(
            "SELECT * FROM submitted_documents WHERE id IN ("
            " SELECT MAX(id) FROM submitted_documents WHERE student_id = ? GROUP BY doc_type"
            ") ORDER BY id",
            (student_id,),
        ).fetchall()
        return {row["doc_type"]: _document(row) for row in rows}

    def _submission(self, conn: sqlite3.Connection, student_id: str) -> Submission | None:
        row = conn.execute("SELECT * FROM submissions WHERE student_id = ?", (student_id,)).fetchone()
        if row is None:
            return None
        return Submission(
            student_id=row["student_id"],
            chat_id=row["chat_id"],
            track=row["track"],
            status=row["status"],
            submitted_at=row["submitted_at"] or "",
            updated_at=row["updated_at"],
            documents=self._current(conn, student_id),
        )

    def _refresh(self, conn: sqlite3.Connection, catalog: Catalog, student_id: str) -> str:
        """Recompute and store the submission status inside the same transaction."""
        row = conn.execute(
            "SELECT track, submitted_at FROM submissions WHERE student_id = ?", (student_id,)
        ).fetchone()
        status = derive_status(catalog, row["track"], row["submitted_at"], self._current(conn, student_id))
        conn.execute(
            "UPDATE submissions SET status = ?, updated_at = ? WHERE student_id = ?",
            (status, now_iso(), student_id),
        )
        return status

    @staticmethod
    def _require(conn: sqlite3.Connection, student_id: str) -> None:
        if conn.execute("SELECT 1 FROM submissions WHERE student_id = ?", (student_id,)).fetchone() is None:
            raise LookupError(f"no submission for {student_id}: choose a track first")

    # ---------------------------------------------- "stored procedures" ----

    def get(self, student_id: str) -> Submission | None:
        with self._tx() as conn:
            return self._submission(conn, student_id)

    def start(self, catalog: Catalog, student_id: str, chat_id: int, track: str) -> Submission:
        """Create the submission, or switch its track (grant / paid)."""
        if track not in catalog.tracks:
            raise ValueError(f"unknown track {track!r}")
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO submissions (student_id, chat_id, track, status, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (student_id) DO UPDATE SET chat_id = excluded.chat_id, track = excluded.track",
                (student_id, chat_id, track, DRAFT, now_iso()),
            )
            self._refresh(conn, catalog, student_id)
            return self._submission(conn, student_id)

    def save(
        self,
        catalog: Catalog,
        student_id: str,
        chat_id: int,
        doc_type: str,
        *,
        kind: str,
        file_id: str,
        file_name: str | None,
        mime_type: str | None,
        file_size: int | None,
    ) -> Submission:
        """Store a checked file; an earlier file of the same document is replaced."""
        if catalog.get(doc_type) is None:
            raise ValueError(f"unknown document {doc_type!r}")
        with self._tx() as conn:
            self._require(conn, student_id)
            conn.execute("UPDATE submissions SET chat_id = ? WHERE student_id = ?", (chat_id, student_id))
            conn.execute(
                "UPDATE submitted_documents SET status = ?"
                " WHERE student_id = ? AND doc_type = ? AND status != ?",
                (REPLACED, student_id, doc_type, REPLACED),
            )
            conn.execute(
                "INSERT INTO submitted_documents (student_id, doc_type, kind, file_id, file_name,"
                " mime_type, file_size, status, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (student_id, doc_type, kind, file_id, file_name, mime_type, file_size, UPLOADED, now_iso()),
            )
            self._refresh(conn, catalog, student_id)
            return self._submission(conn, student_id)

    def submit(self, catalog: Catalog, student_id: str, chat_id: int) -> SubmitResult:
        """Blocked while a required document is missing; otherwise every new
        file goes to review and the submission becomes 'under review'."""
        with self._tx() as conn:
            submission = self._submission(conn, student_id)
            if submission is None:
                raise LookupError(f"no submission for {student_id}: choose a track first")
            missing = missing_required(catalog, submission.track, submission.documents)
            if missing:
                return SubmitResult(missing=missing, sent=[], submission=submission)

            relevant = {doc.id for doc in catalog.for_track(submission.track)}
            new_ids = [
                doc.id for doc in submission.documents.values()
                if doc.doc_type in relevant and doc.status == UPLOADED
            ]
            if new_ids:
                conn.executemany(
                    "UPDATE submitted_documents SET status = ? WHERE id = ?",
                    [(UNDER_REVIEW, doc_id) for doc_id in new_ids],
                )
                conn.execute(
                    "UPDATE submissions SET submitted_at = ?, chat_id = ? WHERE student_id = ?",
                    (now_iso(), chat_id, student_id),
                )
            self._refresh(conn, catalog, student_id)
            submission = self._submission(conn, student_id)
            sent = [doc for doc in submission.documents.values() if doc.id in new_ids]
            return SubmitResult(missing=[], sent=sent, submission=submission)

    def review(
        self,
        catalog: Catalog,
        document_id: int,
        *,
        verified: bool,
        reviewer: str,
        reason: str | None = None,
    ) -> ReviewResult | None:
        """Verify or reject one file. None if it is no longer waiting for review
        (already decided, or replaced by a newer upload)."""
        if not verified and reason not in REJECT_REASONS:
            raise ValueError(f"unknown reject reason {reason!r}")
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM submitted_documents WHERE id = ?", (document_id,)).fetchone()
            if row is None or row["status"] != UNDER_REVIEW:
                return None
            student_id = row["student_id"]
            before = conn.execute(
                "SELECT status FROM submissions WHERE student_id = ?", (student_id,)
            ).fetchone()["status"]
            conn.execute(
                "UPDATE submitted_documents SET status = ?, reason = ?, reviewer = ?, reviewed_at = ?"
                " WHERE id = ?",
                (VERIFIED if verified else REJECTED, None if verified else reason, reviewer, now_iso(), document_id),
            )
            after = self._refresh(conn, catalog, student_id)
            updated = conn.execute("SELECT * FROM submitted_documents WHERE id = ?", (document_id,)).fetchone()
            return ReviewResult(
                document=_document(updated),
                submission=self._submission(conn, student_id),
                completed=before != VERIFIED and after == VERIFIED,
            )

    def pending_reviews(self, limit: int = 50) -> list[tuple[StoredDocument, str]]:
        """(file, track) pairs waiting for the admissions office, oldest first."""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT d.*, s.track AS submission_track FROM submitted_documents d"
                " JOIN submissions s ON s.student_id = d.student_id"
                " WHERE d.status = ? ORDER BY d.id LIMIT ?",
                (UNDER_REVIEW, limit),
            ).fetchall()
            return [(_document(row), row["submission_track"]) for row in rows]


_stores: dict[Path, DocumentStore] = {}


def store() -> DocumentStore:
    """The database at DB_PATH, opened once per process."""
    path = Path(DB_PATH)
    if path not in _stores:
        _stores[path] = DocumentStore(path)
    return _stores[path]
