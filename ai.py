"""AI providers behind one small interface.

core.py (and grade_bot.py) never call a model API directly - they call
ai.generate(). Which model answers is chosen in .env:

    AI_PROVIDER=gemini            Google Gemini (the default)
    AI_PROVIDER=deepseek          DeepSeek
    AI_PROVIDER=openai            OpenAI, or any API in OpenAI's format
                                  (OpenRouter, Groq, a local Ollama...) via OPENAI_BASE_URL
    AI_PROVIDER=gemini,deepseek   Gemini first; DeepSeek answers when Gemini fails

Every provider reads <NAME>_API_KEY, <NAME>_MODEL and an optional
<NAME>_FALLBACK_MODEL from .env - for example DEEPSEEK_API_KEY.

Adding a provider:
    speaks OpenAI's format  -> one entry in PROVIDERS, no new code
    has its own API         -> a class with complete() (see Gemini) + a branch in _build()
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Sequence

import httpx
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------- config ----

# Per HTTP call. GEMINI_TIMEOUT_SECONDS is the older name of the same setting.
REQUEST_TIMEOUT = float(os.getenv("AI_TIMEOUT_SECONDS") or os.getenv("GEMINI_TIMEOUT_SECONDS") or "15")
TEMPERATURE = 0.2
MAX_ATTEMPTS = 3   # attempts per model before moving on to the next one
RETRY_PAUSE = 1.5  # seconds; grows with each attempt


@dataclass(frozen=True)
class Message:
    role: str  # "user" or "assistant"
    text: str


class ProviderError(Exception):
    """A failed call, sorted into a kind that core.MESSAGES can explain:
    rate_limit, busy, connection, timeout (worth retrying), api_error or
    unexpected (not worth it)."""

    RETRYABLE = {"rate_limit", "busy", "connection", "timeout"}

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE


def kind_for_status(status: int | None) -> str:
    if status == 429:
        return "rate_limit"
    if status in (500, 502, 503, 504):
        return "busy"
    return "api_error"  # wrong key, no balance left, unknown model, bad request


# -------------------------------------------------------------- clients ----


def _looks_like_network_error(exc: BaseException) -> bool:
    """True for 'the server is unreachable' style failures (DNS, TLS, timeout)."""
    name = type(exc).__name__.lower()
    return any(word in name for word in ("timeout", "connect", "network", "ssl", "protocol"))


class Gemini:
    """Google Gemini through the google-genai SDK. The SDK is imported only
    when Gemini is actually chosen, so other setups start faster."""

    def __init__(self, api_key: str) -> None:
        from google import genai
        from google.genai import errors, types

        self._errors, self._types = errors, types
        try:
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT * 1000)),
            )
        except Exception:  # noqa: BLE001 - older google-genai builds ignore http_options
            self._client = genai.Client(api_key=api_key)

    def complete(self, system: str, messages: Sequence[Message], model: str, timeout: float) -> str:
        # `timeout` is not used: the SDK applies REQUEST_TIMEOUT to every call.
        types = self._types
        contents = [
            types.Content(role="model" if m.role == "assistant" else "user", parts=[types.Part(text=m.text)])
            for m in messages
        ]
        try:
            response = self._client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=TEMPERATURE,
                    # We pass no tools; without this the SDK logs a warning on every call.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
        except self._errors.APIError as exc:
            raise ProviderError(kind_for_status(exc.code), str(exc)) from exc
        except Exception as exc:
            if _looks_like_network_error(exc):
                raise ProviderError("connection", repr(exc)) from exc
            raise
        return response.text or ""


class OpenAICompatible:
    """Any API in OpenAI's Chat Completions format: DeepSeek, OpenAI,
    OpenRouter, Groq, Ollama... Plain HTTP, so it needs no extra SDK."""

    def __init__(self, base_url: str, api_key: str, extra_body: dict[str, Any] | None = None) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.extra_body = extra_body or {}
        self._http = httpx.Client(headers={"Authorization": f"Bearer {api_key}"})

    def complete(self, system: str, messages: Sequence[Message], model: str, timeout: float) -> str:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.text} for m in messages],
            "temperature": TEMPERATURE,
            **self.extra_body,
        }
        # A null in <NAME>_EXTRA_BODY removes a field, e.g. {"temperature": null}
        # for models that only accept their default temperature.
        body = {key: value for key, value in body.items() if value is not None}
        try:
            response = self._http.post(self.url, json=body, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderError("connection", repr(exc)) from exc
        except httpx.TimeoutException as exc:  # connected, but the answer took too long
            raise ProviderError("timeout", repr(exc)) from exc
        except httpx.TransportError as exc:
            raise ProviderError("connection", repr(exc)) from exc

        if response.status_code >= 400:
            detail = f"HTTP {response.status_code}: {response.text[:300]}"
            raise ProviderError(kind_for_status(response.status_code), detail)
        try:
            return response.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("unexpected", f"unexpected reply: {response.text[:300]}") from exc


# ------------------------------------------------------------ providers ----

# The default model of each provider; .env can change it with <NAME>_MODEL.
# OpenAI-format providers can also take <NAME>_BASE_URL, and <NAME>_EXTRA_BODY:
# JSON merged into every request, e.g. {"thinking": {"type": "enabled"}}.
PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini": {"api": "gemini", "model": "gemini-flash-latest"},
    "deepseek": {
        "api": "openai",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        # deepseek-flash thinks before answering by default, which makes
        # answers several times slower - and US5 wants them in 5 seconds.
        "extra_body": {"thinking": {"type": "disabled"}},
    },
    "openai": {"api": "openai", "base_url": "https://api.openai.com/v1", "model": ""},
}


@dataclass
class Provider:
    name: str
    models: list[str]  # the main model, then the optional fallback
    client: Any        # Gemini or OpenAICompatible


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _json_env(name: str) -> dict[str, Any]:
    raw = _env(name)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f".env: {name} дұрыс JSON емес ({exc})") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f'.env: {name} JSON объект болуы керек, мысалы {{"key": "value"}}')
    return value


def provider_names() -> list[str]:
    """AI_PROVIDER as a list: "gemini, deepseek" -> ["gemini", "deepseek"]."""
    names = (name.strip().lower() for name in os.getenv("AI_PROVIDER", "gemini").split(","))
    return list(dict.fromkeys(name for name in names if name)) or ["gemini"]


def missing_settings() -> list[str]:
    """What .env still lacks for the chosen providers; empty when all is set."""
    missing: list[str] = []
    for name in provider_names():
        spec = PROVIDERS.get(name)
        if spec is None:
            missing.append(f"AI_PROVIDER='{name}' белгісіз (бары: {', '.join(PROVIDERS)})")
            continue
        prefix = name.upper()
        if not _env(f"{prefix}_API_KEY"):
            missing.append(f"{prefix}_API_KEY")
        if not (_env(f"{prefix}_MODEL") or spec["model"]):
            missing.append(f"{prefix}_MODEL")
    return missing


def _build(name: str) -> Provider:
    spec, prefix = PROVIDERS[name], name.upper()
    api_key = _env(f"{prefix}_API_KEY")
    if spec["api"] == "gemini":
        client: Any = Gemini(api_key)
    else:
        client = OpenAICompatible(
            base_url=_env(f"{prefix}_BASE_URL") or spec["base_url"],
            api_key=api_key,
            extra_body={**spec.get("extra_body", {}), **_json_env(f"{prefix}_EXTRA_BODY")},
        )
    models = [_env(f"{prefix}_MODEL") or spec["model"], _env(f"{prefix}_FALLBACK_MODEL")]
    return Provider(name, [model for model in models if model], client)


_chain: list[Provider] | None = None


def chain() -> list[Provider]:
    """The configured providers, in the order they are tried. Built once;
    raises RuntimeError with a clear message when .env is incomplete."""
    global _chain
    if _chain is None:
        missing = missing_settings()
        if missing:
            raise RuntimeError(
                f".env файлында мыналар жетіспейді: {'; '.join(missing)} "
                f"(AI_PROVIDER={','.join(provider_names())}). .env файлы core.py-мен "
                "бір папкада тұруы тиіс. Үлгі: env.example"
            )
        _chain = [_build(name) for name in provider_names()]
    return _chain


def describe() -> list[str]:
    """Every model that may answer, in the order they are tried: ["deepseek/deepseek-flash", ...]."""
    return [f"{provider.name}/{model}" for provider in chain() for model in provider.models]


# ------------------------------------------------------------- generate ----


def generate(system: str, messages: Sequence[Message], deadline: float | None = None) -> str:
    """The first answer any configured model gives.

    Temporary errors (rate limit, overload, network) are retried with a
    growing pause; after that the fallback model, then the next provider, is
    tried. `deadline` is a time.monotonic() value: once it passes we stop, so
    the student is never left waiting past the response budget.

    Raises ProviderError - the last failure - when nothing answered.
    """
    last: ProviderError | None = None
    for provider in chain():
        for model in provider.models:
            for attempt in range(MAX_ATTEMPTS):
                timeout = REQUEST_TIMEOUT
                if deadline is not None:
                    timeout = min(timeout, deadline - time.monotonic())
                    if timeout <= 0:
                        raise last or ProviderError("timeout", "the response budget is used up")
                try:
                    reply = provider.client.complete(system, messages, model, timeout)
                except ProviderError as exc:
                    last = exc
                except Exception as exc:  # noqa: BLE001 - an SDK surprise: still try the next model
                    last = ProviderError("unexpected", repr(exc))
                else:
                    if last is not None:
                        print(f"[ai] answered by {provider.name}/{model} after: {last}")
                    return reply

                print(f"[ai] {provider.name}/{model}, attempt {attempt + 1}: {last}")
                if not last.retryable or attempt + 1 >= MAX_ATTEMPTS:
                    break
                pause = RETRY_PAUSE * (attempt + 1)
                if deadline is not None and time.monotonic() + pause >= deadline:
                    break
                time.sleep(pause)

    raise last or ProviderError("unexpected", "no AI provider is configured")
