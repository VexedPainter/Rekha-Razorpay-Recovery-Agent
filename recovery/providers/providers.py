"""Concrete providers: Gemini (free), Groq (free), Anthropic (paid), Replay (offline).

Each is a thin wrapper over one POST. The differences between them are exactly
the differences that exist -- URL, auth header, and where the JSON schema goes --
which is why they share `_post_json` and `_parse_model_json` rather than each
carrying an SDK.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recovery.providers.base import (
    LLMProvider,
    ProviderError,
    _parse_model_json,
    _post_json,
    load_env,
)


class GeminiProvider:
    """Google Gemini. The default: a genuinely free tier, no card required.

    Supports schema-constrained decoding via `responseSchema`, so malformed JSON
    is largely impossible rather than merely discouraged. Get a key at
    `aistudio.google.com`.
    """

    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-3.6-flash") -> None:
        self._api_key = api_key
        self.model = model

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8192
    ) -> dict[str, Any]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(schema),
                "maxOutputTokens": max_tokens,
                # Diagnosis should be reproducible enough to rehearse a demo
                # against. Not zero: a little variation keeps the reasoning from
                # collapsing into one template across every payment.
                "temperature": 0.2,
            },
        }
        body = _post_json(
            url, headers={"x-goog-api-key": self._api_key}, payload=payload
        )
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError(f"no candidates in response: {json.dumps(body)[:300]}")

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts")
        if not isinstance(parts, list) or not parts:
            # A truncated or safety-blocked response lands here. Surfaced rather
            # than treated as an empty answer, because "the model returned
            # nothing" and "the model said do nothing" are different facts.
            reason = candidate.get("finishReason", "unknown")
            raise ProviderError(f"empty response (finishReason={reason})")

        # Gemini 3.x models reason before answering, and the reasoning arrives as
        # its own part in the SAME response. Reading `parts[0]` blindly picks up a
        # thought like "Here is the JSON requested:" and fails to parse -- which is
        # exactly how this went wrong the first time. Skip thought parts and join
        # what remains.
        answer = "".join(
            str(part.get("text", ""))
            for part in parts
            if isinstance(part, dict) and not part.get("thought")
        )
        if not answer.strip():
            reason = candidate.get("finishReason", "unknown")
            raise ProviderError(
                f"response contained only reasoning, no answer "
                f"(finishReason={reason}; try a larger max_tokens)"
            )
        return _parse_model_json(answer)


class GroqProvider:
    """Groq. Free tier, OpenAI-compatible, and very fast.

    JSON mode rather than a schema, so the returned object is validated by the
    caller's Pydantic model. Get a key at `console.groq.com`.

    The default model is checked against Groq's live catalogue rather than
    remembered: `llama-3.3-70b-versatile` was the obvious choice and has since
    been retired, which produced a 404 on the first real call.
    """

    name = "groq"

    def __init__(self, api_key: str, model: str = "openai/gpt-oss-120b") -> None:
        self._api_key = api_key
        self.model = model

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8192
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    # Without a schema mode, the schema goes in the prompt. Stated
                    # as a requirement rather than a suggestion, because JSON mode
                    # alone guarantees valid JSON, not the right shape.
                    "content": (
                        f"{system}\n\nRespond with JSON matching exactly this schema:\n"
                        f"{json.dumps(schema)}"
                    ),
                },
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        body = _post_json(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            payload=payload,
        )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError(f"no choices in response: {json.dumps(body)[:300]}")
        return _parse_model_json(str(choices[0].get("message", {}).get("content", "")))


class AnthropicProvider:
    """Anthropic Claude. Paid ($5 minimum credit), and the on-brand choice.

    Razorpay Agent Studio is built on Anthropic's Claude Agent SDK and Razorpay
    ships as an official Claude connector, so this is the same stack Razorpay's
    own agent product runs on. Entirely optional: the project is designed so the
    free providers are not a downgrade in anything but that talking point.
    """

    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        self._api_key = api_key
        self.model = model

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8192
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "system": (
                f"{system}\n\nRespond with JSON matching exactly this schema, and "
                f"nothing else -- no prose, no code fence:\n{json.dumps(schema)}"
            ),
            "messages": [{"role": "user", "content": user}],
        }
        body = _post_json(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            payload=payload,
        )
        content = body.get("content")
        if not isinstance(content, list) or not content:
            raise ProviderError(f"no content in response: {json.dumps(body)[:300]}")
        return _parse_model_json(str(content[0].get("text", "")))


class ReplayProvider:
    """Serves recorded responses from disk. No network, no key, no cost.

    This is what makes the default test suite honest: the same code path runs, the
    same JSON is parsed, the same Pydantic validation applies -- only the
    transport is a file read. A test suite that mocked out the diagnosis function
    itself would prove that the mock works.

    Keyed by a digest of the request, so a fixture cannot silently answer a
    question it was not recorded for. `record_to` captures new fixtures during a
    real run.
    """

    name = "replay"

    def __init__(
        self,
        fixture_dir: str | Path,
        *,
        model: str = "replay",
        record_to: str | Path | None = None,
        live: LLMProvider | None = None,
    ) -> None:
        self.model = model
        self._dir = Path(fixture_dir)
        self._record_to = Path(record_to) if record_to else None
        self._live = live

    def _key(self, system: str, user: str) -> str:
        from rekha.canonical import canonical_bytes, sha256_hex

        return sha256_hex(canonical_bytes({"system": system, "user": user}))[:20]

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 8192
    ) -> dict[str, Any]:
        key = self._key(system, user)
        path = self._dir / f"{key}.json"
        if path.exists():
            recorded = json.loads(path.read_text(encoding="utf-8"))
            return dict(recorded["response"])

        if self._live is None:
            raise ProviderError(
                f"no fixture for request {key} in {self._dir}. Re-record with a real "
                f"provider (RECOVERY_RECORD=1), or check whether the prompt changed -- "
                f"a changed prompt is a changed request and needs a new fixture."
            )

        response = self._live.complete_json(
            system=system, user=user, schema=schema, max_tokens=max_tokens
        )
        target = self._record_to or self._dir
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{key}.json").write_text(
            json.dumps(
                {
                    "provider": self._live.name,
                    "model": self._live.model,
                    "system": system,
                    "user": user,
                    "response": response,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return response


#: Free providers first. Selection walks this in order and takes the first whose
#: key is present, so nobody is asked to pay for a capability a free tier covers.
PROVIDER_ORDER: list[tuple[str, str, type]] = [
    ("gemini", "GEMINI_API_KEY", GeminiProvider),
    ("groq", "GROQ_API_KEY", GroqProvider),
    ("anthropic", "ANTHROPIC_API_KEY", AnthropicProvider),
]


def available_providers(env: dict[str, str] | None = None) -> list[str]:
    """Names of providers whose API key is present."""
    resolved = env if env is not None else load_env()
    return [name for name, key, _ in PROVIDER_ORDER if resolved.get(key)]


def resolve_provider(
    choice: str | None = None,
    *,
    env: dict[str, str] | None = None,
    fixture_dir: str | Path | None = None,
    record: bool = False,
) -> LLMProvider:
    """Pick a provider: explicit choice, else the first free one with a key.

    Falls back to `ReplayProvider` when no key is present at all, so the system
    remains runnable -- and fails with a clear message if a fixture is missing,
    rather than pretending to reason.

    `record=True` wraps the live provider so a real run captures fixtures on the
    way past. That is how the offline suite gets its data without anyone
    hand-writing model output.
    """
    resolved = env if env is not None else load_env()
    fixtures = Path(fixture_dir) if fixture_dir else Path(__file__).parent.parent / "fixtures"

    selected = (choice or resolved.get("RECOVERY_PROVIDER") or "").strip().lower()

    if selected == "replay" or (not selected and not available_providers(resolved)):
        return ReplayProvider(fixtures)

    for name, key_name, cls in PROVIDER_ORDER:
        if selected and selected != name:
            continue
        api_key = resolved.get(key_name)
        if not api_key:
            if selected:
                raise ProviderError(
                    f"provider {name!r} was requested but {key_name} is not set. "
                    f"Available: {available_providers(resolved) or ['replay (fixtures)']}"
                )
            continue
        model_override = resolved.get(f"{name.upper()}_MODEL")
        live: LLMProvider = cls(api_key, model_override) if model_override else cls(api_key)
        if record:
            return ReplayProvider(fixtures, model=live.model, live=live)
        return live

    raise ProviderError(
        f"no usable provider. Set one of "
        f"{[key for _, key, _ in PROVIDER_ORDER]}, or use RECOVERY_PROVIDER=replay."
    )


def _to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip JSON Schema keywords Gemini's `responseSchema` rejects.

    Gemini accepts a subset of OpenAPI schema. `additionalProperties`, `$defs`
    and `title` are the ones Pydantic emits that it will not take, so they are
    removed rather than left to cause an opaque 400.
    """
    unsupported = {"additionalProperties", "$defs", "title", "$schema", "definitions"}
    if not isinstance(schema, dict):
        return schema
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in unsupported:
            continue
        if isinstance(value, dict):
            cleaned[key] = _to_gemini_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [
                _to_gemini_schema(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned
