"""The unified AI assistant: one conversation for everything the bot can do.

A student just writes "What deadlines do I have?", "Show me my grades" or
"Are there free rooms tomorrow from 14 to 16?". The model answers from the
knowledge base (core.py) or calls one of the bot's services as a tool:

    answer = await run(question, history, lang, toolbox)

The tools are the existing, tested features - grades from the portal
(grade.py), Moodle deadlines (moodle.py), free rooms (rooms.py), campus
events, the registration guide and the enrollment documents. This module only
describes them to the model and runs the conversation with it, through the
provider configured in core.py (Gemini or DeepSeek). It knows nothing about
Telegram: assistant_bot.py runs the tools inside the bot and shows their
results as the same cards the commands show.

Passwords never come near the model. A personal tool for an account that is
not linked answers auth="portal" / "moodle"; the conversation then stops, and
the bot offers its own secure sign-in and asks the question again afterwards.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

import httpx
from google.genai import errors

import core

MAX_STEPS = 3  # model turns for one question: tool calls, then the answer
MAX_CALLS = 4  # tool calls the model may make in one turn

# --------------------------------------------------------------- tools ----


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema of the arguments


def _args(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


DATE = {
    "type": "string",
    "description": "A date as YYYY-MM-DD in campus time; work it out from today's date "
    "for words like 'tomorrow' or 'on Friday'.",
}


def _clock(what: str) -> dict[str, str]:
    return {"type": "string", "description": f"{what} as HH:MM, 24-hour campus time."}


TOOLS: tuple[Tool, ...] = (
    Tool(
        "get_my_grades",
        "The student's own grades from the university portal (my.sdu.edu.kz) transcript: every "
        "semester with its courses, final letter grades and ECTS credits, the semester GPA (SPA) "
        "and the cumulative GPA. Use it for any question about their own grades, GPA, transcript "
        "or the result of one course (\"what did I get in physics\"); for one course ALWAYS pass "
        "`course`, and the card then shows only that course.",
        _args(course={
            "type": "string",
            "description": "Only this course: its code (MAT 156) or its official English name as on a "
            "transcript (Discrete Mathematics). Turn nicknames and other languages into that name, "
            "e.g. 'дискретка' -> 'Discrete Mathematics', 'матан' -> 'Calculus', 'физика' -> 'Physics', "
            "'история Казахстана' -> 'History of Kazakhstan'. Leave it out for all grades.",
        }),
    ),
    Tool(
        "get_my_deadlines",
        "The student's own upcoming (and recently missed) assignment and quiz deadlines from "
        "Moodle (moodle.sdu.edu.kz), with the course, the title and the due time. Only course "
        "work: NOT university dates such as registration, add/drop, withdrawal, exams or "
        "holidays - those are get_academic_calendar and the knowledge base.",
        _args(),
    ),
    Tool(
        "get_academic_calendar",
        "The official academic calendar of the current term: when course registration, add/drop, "
        "late registration, early and late withdrawal, pre-final and final grades, final exams, "
        "the FX application and resit exams open and close. When the student is signed in to the "
        "portal it shows the portal's System Calendar (exact dates and times, open now or not, and "
        "the student's own permissions, such as their own registration window); otherwise it says "
        "to answer from the academic calendar in the knowledge base. Never ask for a sign-in for "
        "these dates.",
        _args(),
    ),
    Tool(
        "find_free_rooms",
        "Classrooms that are free according to the class schedule. Without times: the class "
        "period that is on now (or the next one). With only start_time: the class period at "
        "that time. With start_time and end_time: rooms free for that whole interval.",
        _args(date=DATE, start_time=_clock("Start"), end_time=_clock("End")),
    ),
    Tool(
        "get_campus_events",
        "The published campus events with their date, time, venue and whether they are "
        "upcoming or have passed; optionally only those that start between from_date and to_date.",
        _args(from_date=DATE, to_date=DATE),
    ),
    Tool(
        "get_registration_guide",
        "Shows the official step-by-step course registration (schedule creation) guide, with a "
        "button to the student portal.",
        _args(),
    ),
    Tool(
        "get_required_documents",
        "The official list of documents a new student submits for enrollment at SDU (one list "
        "for a state grant, one for tuition-based study) and, if the student has started a "
        "submission in the bot, which of them are still missing. Also lets them open the "
        "document submission.",
        _args(track={
            "type": "string",
            "enum": ["grant", "paid"],
            "description": "grant = state educational grant, paid = tuition-based. Leave it out if unknown.",
        }),
    ),
    Tool(
        "sign_in",
        "Opens the bot's secure sign-in when the student asks to log in or to link an account.",
        _args(account={
            "type": "string",
            "enum": ["portal", "moodle"],
            "description": "portal = my.sdu.edu.kz (grades, documents); moodle = moodle.sdu.edu.kz (deadlines).",
        }),
    ),
)

# Replaces core.COMMANDS_RULE: with tools, nobody has to type a command.
SERVICES_RULE = """- TOOLS: you can act, not only answer. The student's own data and the bot's
  services are tools - use them instead of telling the student to type a
  command: get_my_grades (their grades, GPA, transcript), get_my_deadlines
  (their Moodle deadlines), get_academic_calendar (university dates),
  find_free_rooms (free classrooms; turn "now",
  "tomorrow", "after 2 pm" into a date and HH:MM), get_campus_events,
  get_registration_guide, get_required_documents (enrollment documents and the
  student's own submission), sign_in (when they ask to log in). Do not use the
  fallback sentence for these questions.
- University dates - course registration, add/drop, withdrawal ("уиздроул",
  "withdraw"), final exams, FX, resits - are answered from the academic
  calendar in the knowledge base with the exact dates, or with
  get_academic_calendar when the student is signed in to the portal (see the
  end of this prompt). Never use get_my_deadlines for them: it is only the student's own
  Moodle assignments and quizzes. Holidays, the first and last day of classes
  and the next semesters are in the knowledge base.
- Through these tools you CAN see the student's own grades and deadlines (the
  bot signs them in securely when needed), so never say you have no access to
  them. "What did I get in physics?", "Сколько я получил по истории
  Казахстана?", "Физикадан қанша алдым?" are get_my_grades with that course.
- A tool's result is shown to the student as a card (the full list, with
  buttons) right after your text. So NEVER list its items again - no list of
  grades, deadlines, rooms or events: answer only the exact question in 1-2
  sentences (for example the GPA, the nearest deadline, how many rooms are
  free), or write one short sentence that introduces the card. If a tool says
  the data could not be loaded, say so briefly. Never invent grades,
  deadlines, rooms or events.
- NEVER ask for a password, a verification code or any other credentials in
  the chat: the bot signs the student in through its own secure sign-in."""


# ---------------------------------------------------------- the results ----


@dataclass(frozen=True)
class Call:
    id: str  # the provider's call id ("" when it gives none)
    name: str
    args: dict[str, Any]


@dataclass
class Reply:
    """One model turn: text, tool calls, or both."""

    text: str
    calls: list[Call]


@dataclass
class ToolResult:
    data: dict[str, Any]   # what the model reads: compact facts, never secrets
    summary: str = ""      # kept in the conversation history for follow-up questions
    auth: str | None = None  # "portal" / "moodle": the student has to sign in first
    shown: bool = False    # the toolbox showed it as a card (the full list)


# Added to a shown result: models follow a note next to the data more reliably
# than a rule far away in the system prompt.
SHOWN_NOTE = ("The student already sees all of this as a card right below your reply. Do NOT list "
              "these items again; answer the question in 1-2 sentences or introduce the card in one.")


class Toolbox(Protocol):
    async def run(self, name: str, args: dict[str, Any], lang: str) -> ToolResult: ...

    def notes(self) -> str:
        """Extra rules about this student right now (e.g. signed in to the portal); may be ""."""
        ...


@dataclass
class Answer:
    text: str                 # "" when the cards say everything
    lang: str                 # the language the question is written in
    auth: str | None = None   # sign in to this account first, then ask again
    tools: list[str] = field(default_factory=list)      # tools that ran, in order
    summaries: list[str] = field(default_factory=list)  # for the history


# ---------------------------------------------------- the conversation ----


def _history(history: Sequence[Any] | None) -> list[tuple[str, str]]:
    """The recent turns as ("user" | "assistant", text) pairs."""
    turns = []
    for turn in list(history or [])[-core.HISTORY_TURNS:]:
        role, text = core._as_turn(turn)  # "user" or "model"
        if text:
            turns.append(("user" if role == "user" else "assistant", text))
    return turns


class GeminiChat:
    """A conversation with Gemini function calling (google-genai)."""

    def __init__(self, system: str, history: list[tuple[str, str]], question: str,
                 tools: Sequence[Tool] = TOOLS) -> None:
        from google.genai import types

        self.types = types
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in tools
            ])],
            # The bot runs the tools itself, after checking the student's account.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self.contents = [
            types.Content(role="model" if role == "assistant" else "user", parts=[types.Part(text=text)])
            for role, text in history
        ]
        self.contents.append(types.Content(role="user", parts=[types.Part(text=question)]))

    def ask(self, model: str, timeout: float) -> Reply:
        # `timeout` is not used: the client in core.py applies REQUEST_TIMEOUT.
        response = core.client.models.generate_content(model=model, contents=self.contents, config=self.config)
        content = response.candidates[0].content if response.candidates else None
        parts = list(content.parts or []) if content is not None else []
        calls = [
            Call(id=part.function_call.id or "", name=part.function_call.name or "",
                 args=dict(part.function_call.args or {}))
            for part in parts if part.function_call
        ]
        text = "".join(part.text for part in parts if part.text and not part.thought)
        if calls:
            # Sent back as it came: Gemini 3 needs its thought signatures with the results.
            self.contents.append(content)
        return Reply(text=text, calls=calls)

    def add_results(self, results: list[tuple[Call, dict[str, Any]]]) -> None:
        types = self.types
        self.contents.append(types.Content(role="user", parts=[
            types.Part(function_response=types.FunctionResponse(
                id=call.id or None, name=call.name, response={"result": data}))
            for call, data in results
        ]))


class DeepSeekChat:
    """A conversation with DeepSeek's OpenAI-compatible tool calling."""

    def __init__(self, system: str, history: list[tuple[str, str]], question: str,
                 tools: Sequence[Tool] = TOOLS) -> None:
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.messages += [{"role": role, "content": text} for role, text in history]
        self.messages.append({"role": "user", "content": question})
        self.tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]

    def ask(self, model: str, timeout: float) -> Reply:
        response = httpx.post(
            core.DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {core.API_KEY}"},
            json={"model": model, "messages": self.messages, "tools": self.tools, "stream": False,
                  "thinking": {"type": "disabled"}},
            timeout=timeout,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        calls = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:  # the model may write invalid JSON: run the tool without arguments
                args = {}
            calls.append(Call(id=str(item.get("id") or ""), name=str(function.get("name") or ""),
                              args=args if isinstance(args, dict) else {}))
        if calls:
            self.messages.append({"role": "assistant", "content": message.get("content") or "",
                                  "tool_calls": message["tool_calls"]})
        return Reply(text=message.get("content") or "", calls=calls)

    def add_results(self, results: list[tuple[Call, dict[str, Any]]]) -> None:
        for call, data in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id,
                                  "content": json.dumps(data, ensure_ascii=False)})


def new_chat(system: str, history: list[tuple[str, str]], question: str):
    chat = GeminiChat if core.AI_PROVIDER == "gemini" else DeepSeekChat
    return chat(system, history, question)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, errors.APIError):
        return exc.code in core.RETRYABLE
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in core.RETRYABLE
    return core._is_transport_error(exc)


def _ask_blocking(chat, deadline: float) -> Reply:
    """One model turn with core.py's retries and fallback model, until `deadline`."""
    models = [core.MODEL] + ([core.FALLBACK_MODEL] if core.FALLBACK_MODEL else [])
    last: BaseException | None = None
    for model in models:
        for attempt in range(core.MAX_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise core.AssistantError(core._error_key(last) if last else "connection")
            try:
                return chat.ask(model, min(core.REQUEST_TIMEOUT, remaining))
            except Exception as exc:  # noqa: BLE001 - classified just below
                last = exc
                print(f"[assistant] {core.AI_PROVIDER} error ({model}, attempt {attempt + 1}): {exc}")
                if not _retryable(exc):
                    raise
                pause = core.RETRY_PAUSE * (attempt + 1)
                if attempt + 1 >= core.MAX_ATTEMPTS or time.monotonic() + pause >= deadline:
                    break
                time.sleep(pause)
    if last is not None:
        raise last
    raise core.AssistantError("connection")


async def _ask(chat) -> Reply:
    """One model turn within the US1 budget: past RESPONSE_BUDGET seconds the
    student gets "Connection error" instead of waiting longer."""
    started = time.monotonic()
    future = core._AI_POOL.submit(_ask_blocking, chat, started + core.RESPONSE_BUDGET)
    try:
        reply = await asyncio.wait_for(asyncio.wrap_future(future), timeout=core.RESPONSE_BUDGET)
    except asyncio.TimeoutError:
        print(f"[assistant] no answer within {core.RESPONSE_BUDGET:g}s - stopped waiting (US1: Connection error)")
        raise core.AssistantError("connection") from None
    except core.AssistantError:
        raise
    except Exception as exc:  # noqa: BLE001 - show the real cause in the terminal
        key = core._error_key(exc)
        if key == "unexpected":
            print(f"[assistant] unexpected error: {exc!r}")
        raise core.AssistantError(key) from exc
    core.log_timing("model turn", time.monotonic() - started)
    return reply


async def run(question: str, history: Sequence[Any] | None, lang: str, toolbox: Toolbox) -> Answer:
    """Answer one message: from the knowledge base, or through the tools.

    Raises core.AssistantError (empty, too_long, connection, busy, ...) with a
    key the caller turns into a localized message.
    """
    text = (question or "").strip()
    if not text:
        raise core.AssistantError("empty", status_code=400)
    if len(text) > core.MAX_QUESTION_LENGTH:
        raise core.AssistantError("too_long", status_code=400)

    prompt, reply_lang = core.system_prompt(text, lang, services=SERVICES_RULE)
    # After the long knowledge base, so the model reads it last.
    prompt += toolbox.notes() if hasattr(toolbox, "notes") else ""
    chat = new_chat(prompt, _history(history), text)
    answer = Answer(text="", lang=reply_lang)

    for _ in range(MAX_STEPS):
        reply = await _ask(chat)
        if not reply.calls:
            # A plain answer goes through the US1 rules (fallback sentence plus
            # contacts); after tools, the cards carry the data and the text may be short.
            answer.text = reply.text.strip() if answer.tools else core.finish_reply(reply.text, reply_lang)
            return answer
        results = []
        for call in reply.calls[:MAX_CALLS]:
            result = await toolbox.run(call.name, call.args, reply_lang)
            answer.tools.append(call.name)
            if result.summary:
                answer.summaries.append(result.summary)
            if result.auth:  # the bot asks the student to sign in, then asks this again
                answer.auth = result.auth
                return answer
            results.append((call, {**result.data, "note": SHOWN_NOTE} if result.shown else result.data))
        chat.add_results(results)
    return answer  # still calling tools after MAX_STEPS: the cards are the answer
