"""US8: the AI checks WHAT an uploaded enrollment document is.

documents.check_file() only knows the format and the size, so a class
timetable saved as PDF used to pass as a "UNT certificate". Now every file
that passes those checks is also shown to a vision model (Gemini) and sorted
into one document type; the bot compares that type with the requested one:

    verdict = check(data, "application/pdf", requested_doc)
    verdict.outcome    "ok" | "wrong" | "unclear" | "unavailable" | "off"

    requested UNT certificate + UNT certificate .......... ok      (saved)
    requested UNT certificate + timetable / passport ..... wrong   (not saved)
    blurred, dark or cut-off photo ....................... unclear (send a clearer one)
    the AI could not be reached .......................... unavailable (not saved yet: the
                                                            student checks it again or sends
                                                            it to be checked by hand)

The model is NOT told which document was requested, so it cannot lean towards
the expected answer: it only names what it sees.

Privacy: the model is asked only for the TYPE. Its reply is limited by a JSON
schema to a type label, readable yes/no and a confidence - there is no field
in which a name, a passport or IIN number, a date of birth or any other text
from the file could come back. The file's bytes stay in memory for this one
call; nothing is written to disk, and the log holds only the labels.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from google.genai import errors

import core
import documents

# The model's labels. A catalog document is checked against its own id,
# except where several documents look alike (the two receipts).
SAME_KIND = {
    "student_fee_receipt": "payment_receipt",
    "tuition_receipt": "payment_receipt",
    "photo_3x4": "portrait_photo",
}

# What each type looks like, for the model.
DESCRIPTIONS = {
    "application_form": "an admission application form printed or saved from admissions.sdu.edu.kz",
    "school_certificate": "a school leaving certificate (аттестат / attestat) or a college diploma, with a transcript of grades",
    "unt_certificate": "a UNT (ҰБТ / ЕНТ) certificate from the National Testing Center with test subjects and scores",
    "medical_075": "a medical certificate on form 075 (075/у) from a clinic",
    "fluorography": "a chest fluorography / X-ray image or its report from a physician",
    "vaccination_063": "a vaccination record card (form 063)",
    "portrait_photo": "a portrait photo of one person's face and shoulders on a plain background, like a 3x4 ID photo",
    "payment_receipt": "a payment receipt or bank / Kaspi payment confirmation",
    "grant_certificate": "a certificate of a state educational grant award (свидетельство о присуждении гранта)",
    "english_certificate": "an English language certificate (IELTS, TOEFL, SDU Language School)",
    "pedagogical_exam": "the result of a pedagogical examination",
    "interview_protocol": "an interview protocol from a university admissions interview",
    "military_id": "a military ID booklet (военный билет / әскери билет)",
    "passport": "a passport (a booklet page with a photo and a machine-readable zone)",
    "identity_card": "a national identity card (удостоверение личности / жеке куәлік)",
    "student_id_card": "a university student card / student ID",
    "timetable": "a class schedule or timetable",
    "other_document": "some other document that is none of the types above",
    "not_a_document": "not a document at all: an ordinary photo, a screenshot, a drawing",
}
# Types that are never requested, so the bot can name what was sent instead.
OTHER_KINDS = ("passport", "identity_card", "student_id_card", "timetable", "other_document", "not_a_document")

MODEL = os.getenv("DOCUMENT_CHECK_MODEL", "").strip()
# Tried after the configured models, so that a fallback model that no longer
# exists, a model over its per-minute quota or an overloaded model family does
# not switch the check off. gemini-flash-latest runs on other capacity than the
# lite models.
LAST_RESORT = ("gemini-flash-latest", "gemini-flash-lite-latest")
BUDGET = float(os.getenv("DOCUMENT_CHECK_SECONDS", "30"))  # all attempts together
ATTEMPT_SECONDS = 10  # one request: an overloaded model is left for the next one
ROUNDS = 2            # times through the list of models

SYSTEM = (
    "You sort files that students upload to a university admissions checklist by DOCUMENT TYPE. "
    "Look at the layout, the headings and the kind of document only. "
    "PRIVACY RULE: never read out, copy, summarize or return any text or personal data from the "
    "file - no names, ID, passport or IIN numbers, dates of birth, addresses, scores or amounts. "
    "Reply only with the JSON fields asked for."
)


@dataclass(frozen=True)
class Verdict:
    outcome: str            # ok | wrong | unclear | unavailable | off
    detected: str | None = None  # the model's label, for the "it looks like ..." message


def label_for(doc_id: str) -> str:
    return SAME_KIND.get(doc_id, doc_id)


def labels(catalog: documents.Catalog) -> list[str]:
    """Every type the model may answer: the catalog's documents, then the others."""
    found = dict.fromkeys(label_for(doc.id) for doc in catalog.documents)
    return list(found) + [kind for kind in OTHER_KINDS if kind not in found]


def _describe(label: str, catalog: documents.Catalog) -> str:
    if label in DESCRIPTIONS:
        return DESCRIPTIONS[label]
    doc = catalog.get(label)
    return doc.label("en") if doc else label.replace("_", " ")


def schema(catalog: documents.Catalog) -> dict:
    """The only reply the model may give: enums and a boolean, no free text."""
    return {
        "type": "object",
        "properties": {
            "document_type": {"type": "string", "enum": labels(catalog)},
            "readable": {"type": "boolean"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["document_type", "readable", "confidence"],
    }


def prompt(catalog: documents.Catalog) -> str:
    kinds = "\n".join(f"- {label}: {_describe(label, catalog)}" for label in labels(catalog))
    return (
        f"Document types:\n{kinds}\n\n"
        "Which ONE type is this file? Set readable to false when the file is too blurred, dark, "
        "small or cut off to tell what it is. Use confidence low when you are unsure."
    )


# ------------------------------------------------------------- the model ----

_client = None


def _gemini():
    """The Gemini client: core's when Gemini is the chat provider, otherwise one
    from GEMINI_API_KEY (DeepSeek has no image input)."""
    global _client
    if core.AI_PROVIDER == "gemini":
        return core.client
    if _client is None:
        from google import genai
        from google.genai import types

        _client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(timeout=int(core.REQUEST_TIMEOUT * 1000)),
        )
    return _client


def _models() -> list[str]:
    if MODEL:
        models = [MODEL]
    elif core.AI_PROVIDER == "gemini":
        models = [core.MODEL, core.FALLBACK_MODEL]
    else:
        models = [os.getenv("GEMINI_MODEL", "").strip()]
    return list(dict.fromkeys(m.strip() for m in [*models, *LAST_RESORT] if m and m.strip()))


def enabled() -> bool:
    """The check needs a Gemini key; without one, uploads are checked for format only."""
    if os.getenv("DOCUMENT_CHECK", "on").strip().lower() in ("off", "0", "false", "no"):
        return False
    return core.AI_PROVIDER == "gemini" or bool(os.getenv("GEMINI_API_KEY", "").strip())


def classify(data: bytes, mime_type: str, catalog: documents.Catalog) -> dict:
    """The model's answer about one file: {document_type, readable, confidence}.

    Each model is asked once per round, within BUDGET seconds in all: when one
    is overloaded or slow (a 504, a timeout), the next one is asked at once
    instead of the same one again. Raises the last error when none answered.
    """
    from google.genai import types

    contents = [types.Content(role="user", parts=[
        types.Part.from_bytes(data=data, mime_type=mime_type),
        types.Part(text=prompt(catalog)),
    ])]
    deadline = time.monotonic() + BUDGET
    models, last = _models(), None
    for round_ in range(ROUNDS):
        for model in list(models):
            remaining = deadline - time.monotonic()
            if remaining < 2:
                raise last or TimeoutError("no time left for the document check")
            config = types.GenerateContentConfig(
                system_instruction=SYSTEM,
                temperature=0,
                response_mime_type="application/json",
                response_json_schema=schema(catalog),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                http_options=types.HttpOptions(timeout=int(min(ATTEMPT_SECONDS, remaining) * 1000)),
            )
            try:
                response = _gemini().models.generate_content(model=model, contents=contents, config=config)
                result = json.loads(response.text or "")
                if not isinstance(result, dict) or result.get("document_type") not in labels(catalog):
                    raise ValueError("the reply is not one of the document types")
                return result
            except Exception as exc:  # noqa: BLE001 - only the error type is logged, never content
                last = exc
                code = exc.code if isinstance(exc, errors.APIError) else ""
                print(f"[documents] AI check round {round_ + 1} ({model}) failed: {type(exc).__name__} {code}".rstrip())
                if isinstance(exc, errors.APIError) and (exc.code not in core.RETRYABLE or exc.code == 429):
                    models.remove(model)  # gone, refused or over its quota: not asked again
    raise last or TimeoutError("no model is configured for the document check")


def decide(requested: documents.DocType, result: dict) -> Verdict:
    """Compare the model's answer with the requested document."""
    detected = result.get("document_type")
    readable = bool(result.get("readable"))
    confidence = result.get("confidence")
    if detected == label_for(requested.id):
        return Verdict("ok" if readable else "unclear", detected)
    if detected == "not_a_document" and confidence != "low":
        return Verdict("wrong", detected)
    if not readable or confidence == "low":
        return Verdict("unclear", detected)
    return Verdict("wrong", detected)


def check(data: bytes, mime_type: str, requested: documents.DocType, catalog: documents.Catalog) -> Verdict:
    """The verdict for one uploaded file (blocking: run it in a thread)."""
    if not enabled():
        return Verdict("off")
    started = time.monotonic()
    try:
        result = classify(data, mime_type, catalog)
    except Exception:  # noqa: BLE001 - the student checks it again or sends it to be checked by hand
        print(f"[documents] AI check unavailable for {requested.id} after {time.monotonic() - started:.1f}s")
        return Verdict("unavailable")
    verdict = decide(requested, result)
    # Labels only - never anything read from the file.
    print(
        f"[documents] AI check: requested={requested.id} detected={result.get('document_type')} "
        f"readable={result.get('readable')} confidence={result.get('confidence')} -> {verdict.outcome} "
        f"({time.monotonic() - started:.1f}s)"
    )
    return verdict
