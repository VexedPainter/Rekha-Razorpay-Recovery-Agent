"""Language-model access, behind one narrow interface.

Why an interface rather than a vendor SDK call at the point of use:

1. **Nothing on the authorization path may depend on a model**, so the model has
   to be replaceable without touching anything else. Making that structurally
   true is more convincing than asserting it.
2. **A free provider must be a first-class option.** Gemini's free tier and
   Groq's free tier both work here; nothing about the design assumes a paid key.
3. **CI must never call a paid API.** `ReplayProvider` serves recorded fixtures,
   so the default test suite exercises the same code path offline.

Why raw HTTP via `httpx` rather than three vendor SDKs:

- `httpx` is already present (transitive via `mcp`), so this adds no dependency.
  Three SDKs would add three, each with its own transitive tree, for code that
  amounts to one POST and one JSON parse.
- Every byte sent to a model is visible in this package. For a project whose
  entire argument is "the model cannot exceed its authority", an auditable
  request is worth more than an ergonomic client.
- The providers differ only in URL, headers, and where the JSON schema goes.
  That is genuinely a small difference, and one code path makes it obvious.

Selection order (`resolve_provider`): an explicit choice wins, then the first
provider whose API key is present, in order of cost -- free before paid. So the
system works out of the box for whoever has a key, without asking.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol

_DOTENV_CACHE: dict[str, str] | None = None


class ProviderError(RuntimeError):
    """A provider could not produce a usable answer.

    Deliberately not a `BelayError`: spec §11's registry governs the governed
    protocol surface, and an LLM being unreachable is not a financial event. The
    caller decides what a failed diagnosis means -- and the safe reading is
    always "propose nothing", never "proceed unadvised".
    """


class LLMProvider(Protocol):
    """One method, because one method is all the recovery layer needs.

    `complete_json` returns parsed JSON conforming to `schema`. Providers that
    support native schema-constrained decoding (Gemini, and OpenAI-compatible
    JSON mode) use it; the rest are instructed in the prompt and validated on
    return. Either way the caller re-validates against a Pydantic model, so a
    provider that ignores the schema fails loudly rather than silently.
    """

    name: str
    model: str

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8192
    ) -> dict[str, Any]:
        ...


#: A value is treated as an unfilled template placeholder if it contains four or
#: more consecutive `x`. Deliberately not "contains `xxx`": a real API key is
#: base64-ish and could plausibly contain three, and silently discarding a valid
#: key produces a baffling "no provider available" instead of an error anyone can
#: act on. Four consecutive `x` matches every placeholder in `.env.example` and
#: essentially no real credential.
_PLACEHOLDER = re.compile(r"x{4,}", re.IGNORECASE)


def is_placeholder(value: str) -> bool:
    """Whether `value` looks like an unfilled `.env.example` placeholder."""
    return bool(_PLACEHOLDER.search(value))


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Read `.env` once, merged under the real environment.

    Minimal by design: no interpolation, no export syntax, no dependency. The
    real environment always wins, so CI cannot be surprised by a stray file.

    Unfilled placeholders are dropped rather than returned, so a `.env` copied
    from the template does not look like a configured key.
    """
    global _DOTENV_CACHE
    if _DOTENV_CACHE is None:
        values: dict[str, str] = {}
        candidate = Path(path)
        if candidate.exists():
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cleaned = value.strip().strip("'\"")
                if cleaned and not is_placeholder(cleaned):
                    values[key.strip()] = cleaned
        _DOTENV_CACHE = values
    return {**_DOTENV_CACHE, **os.environ}


def reset_env_cache() -> None:
    """Forget the cached `.env`. For tests."""
    global _DOTENV_CACHE
    _DOTENV_CACHE = None


def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float = 180.0,
    attempts: int = 3,
) -> dict[str, Any]:
    """One POST, one JSON response, retried on transient failures.

    Retries exist because a real recording run is ~20 sequential calls, and a
    single dropped connection two thirds of the way through would waste all of
    them. Observed in practice: `RemoteProtocolError: Server disconnected without
    sending a response` on a large generation.

    Retried: transport errors and 429/5xx. NOT retried: 4xx other than 429 -- a
    bad key or a nonexistent model will fail identically on every attempt, and
    hammering it just delays a clear error message.
    """
    import httpx

    last: str = "no attempt made"
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            last = f"request failed: {type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(2.0 * attempt)
                continue
            raise ProviderError(last) from exc
        else:
            if response.status_code == 200:
                try:
                    parsed = response.json()
                except ValueError as exc:
                    raise ProviderError(
                        f"response was not JSON: {response.text[:200]}"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise ProviderError(
                        f"expected a JSON object, got {type(parsed).__name__}"
                    )
                return parsed

            last = f"HTTP {response.status_code}: {response.text[:400]}"
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < attempts:
                time.sleep(2.0 * attempt)
                continue
            raise ProviderError(last)

    raise ProviderError(last)  # pragma: no cover - loop always returns or raises


def _parse_model_json(text: str) -> dict[str, Any]:
    """Parse a model's JSON output, tolerating the two things models actually do.

    Some wrap JSON in a ```json fence despite being told not to; some emit a
    leading sentence. Both are recoverable by locating the outermost object.
    Anything beyond that is a genuine failure and is raised -- guessing at
    malformed output is how a half-parsed proposal reaches the control plane.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1]
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise ProviderError(f"model output was not JSON: {text[:200]}") from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProviderError(f"model output was not JSON: {text[:200]}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
