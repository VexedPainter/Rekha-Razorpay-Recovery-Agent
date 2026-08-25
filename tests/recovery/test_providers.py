"""Providers: selection, transport errors, and the JSON models actually return.

No network. `httpx.post` is stubbed, so what is under test is the request each
provider builds and the response handling -- not whether Google's servers are up.

The `_parse_model_json` cases are not hypothetical. Models wrap JSON in a
```json fence and prepend a sentence despite being told not to, and both are
recoverable. What is *not* recoverable is raised, because guessing at malformed
output is how a half-parsed proposal reaches the control plane.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from recovery.providers import (
    AnthropicProvider,
    GeminiProvider,
    GroqProvider,
    ProviderError,
    ReplayProvider,
    available_providers,
    resolve_provider,
)
from recovery.providers.base import _parse_model_json
from recovery.providers.providers import _to_gemini_schema

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class _FakeResponse:
    def __init__(self, status: int, body: Any, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text or json.dumps(body)

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture outgoing requests instead of sending them."""
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, headers: dict[str, str], json: Any, timeout: float) -> Any:
        calls.append({"url": url, "headers": headers, "payload": json})
        return _FakeResponse(200, calls[-1].get("_response", _default_for(url)))

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def _default_for(url: str) -> Any:
    if "generativelanguage" in url:
        return {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
    if "groq" in url:
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}
    return {"content": [{"text": '{"ok": true}'}]}


# ------------------------------------------------------------------ the requests


def test_gemini_sends_a_schema_and_the_key_in_a_header(captured: list) -> None:
    """The key goes in a header, not a query string: a URL ends up in logs."""
    result = GeminiProvider("k-123").complete_json(system="s", user="u", schema=SCHEMA)
    assert result == {"ok": True}
    call = captured[0]
    assert "gemini-3.6-flash" in call["url"]
    assert call["headers"]["x-goog-api-key"] == "k-123"
    assert call["payload"]["generationConfig"]["responseMimeType"] == "application/json"
    assert call["payload"]["generationConfig"]["responseSchema"]["type"] == "object"
    assert call["payload"]["systemInstruction"]["parts"][0]["text"] == "s"


def test_groq_uses_json_mode_and_inlines_the_schema(captured: list) -> None:
    """No schema mode, so the shape goes in the prompt -- JSON mode alone
    guarantees valid JSON, not the right JSON."""
    GroqProvider("k").complete_json(system="s", user="u", schema=SCHEMA)
    payload = captured[0]["payload"]
    assert payload["response_format"] == {"type": "json_object"}
    assert "properties" in payload["messages"][0]["content"]
    assert captured[0]["headers"]["Authorization"] == "Bearer k"


def test_anthropic_sends_the_version_header(captured: list) -> None:
    AnthropicProvider("k").complete_json(system="s", user="u", schema=SCHEMA)
    assert captured[0]["headers"]["anthropic-version"] == "2023-06-01"
    assert captured[0]["headers"]["x-api-key"] == "k"


def test_a_model_override_is_honoured(captured: list) -> None:
    GeminiProvider("k", "gemini-3.5-flash-lite").complete_json(
        system="s", user="u", schema=SCHEMA
    )
    assert "gemini-3.5-flash-lite" in captured[0]["url"]


# ------------------------------------------------------------------- failures


def test_a_non_200_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(429, {}, text="rate limit exceeded"),
    )
    with pytest.raises(ProviderError, match="429"):
        GeminiProvider("k").complete_json(system="s", user="u", schema=SCHEMA)


def test_a_transport_failure_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("dns")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(ProviderError, match="request failed"):
        GroqProvider("k").complete_json(system="s", user="u", schema=SCHEMA)


def test_an_empty_gemini_response_names_the_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"The model returned nothing" and "the model said do nothing" are different
    facts, and a truncated or safety-blocked response is the former."""
    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _FakeResponse(200, {"candidates": [{"finishReason": "MAX_TOKENS"}]}),
    )
    with pytest.raises(ProviderError, match="MAX_TOKENS"):
        GeminiProvider("k").complete_json(system="s", user="u", schema=SCHEMA)


def test_no_candidates_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, {"candidates": []}))
    with pytest.raises(ProviderError, match="no candidates"):
        GeminiProvider("k").complete_json(system="s", user="u", schema=SCHEMA)


def test_no_choices_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(200, {"choices": []}))
    with pytest.raises(ProviderError, match="no choices"):
        GroqProvider("k").complete_json(system="s", user="u", schema=SCHEMA)


def test_a_non_json_body_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResponse(200, ValueError("nope"), text="<html>")
    )
    with pytest.raises(ProviderError, match="not JSON"):
        GeminiProvider("k").complete_json(system="s", user="u", schema=SCHEMA)


# -------------------------------------------------------------- output parsing


def test_plain_json_parses() -> None:
    assert _parse_model_json('{"a": 1}') == {"a": 1}


def test_a_fenced_block_parses() -> None:
    """Models do this despite being told not to."""
    assert _parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_model_json('```\n{"a": 1}\n```') == {"a": 1}


def test_a_leading_sentence_is_tolerated() -> None:
    assert _parse_model_json('Here is the result:\n{"a": 1}') == {"a": 1}


def test_genuinely_malformed_output_raises() -> None:
    for bad in ("not json at all", "", "{{{", '["a", "list"]'):
        with pytest.raises(ProviderError):
            _parse_model_json(bad)


# ------------------------------------------------------------------- selection


def test_free_providers_are_preferred_over_paid() -> None:
    """Nobody should be asked to pay for what a free tier covers."""
    env = {"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}
    assert available_providers(env) == ["gemini", "groq", "anthropic"]
    assert resolve_provider(env=env).name == "gemini"


def test_the_next_provider_is_used_when_the_first_key_is_absent() -> None:
    assert resolve_provider(env={"GROQ_API_KEY": "q"}).name == "groq"
    assert resolve_provider(env={"ANTHROPIC_API_KEY": "a"}).name == "anthropic"


def test_no_key_falls_back_to_recorded_fixtures() -> None:
    """The system stays runnable with no credentials at all."""
    assert resolve_provider(env={}).name == "replay"


def test_an_explicit_choice_wins() -> None:
    env = {"GEMINI_API_KEY": "g", "ANTHROPIC_API_KEY": "a"}
    assert resolve_provider("anthropic", env=env).name == "anthropic"


def test_requesting_a_provider_without_its_key_fails_clearly() -> None:
    with pytest.raises(ProviderError, match="GROQ_API_KEY"):
        resolve_provider("groq", env={"GEMINI_API_KEY": "g"})


def test_the_env_var_selects_a_provider() -> None:
    assert resolve_provider(env={"RECOVERY_PROVIDER": "replay"}).name == "replay"


def test_a_model_can_be_overridden_by_env() -> None:
    provider = resolve_provider(env={"GEMINI_API_KEY": "g", "GEMINI_MODEL": "gemini-x"})
    assert provider.model == "gemini-x"


# ---------------------------------------------------------------------- replay


def test_replay_serves_a_recorded_response(tmp_path: Any) -> None:
    provider = ReplayProvider(tmp_path)
    key = provider._key("s", "u")
    (tmp_path / f"{key}.json").write_text(
        json.dumps({"response": {"ok": True}}), encoding="utf-8"
    )
    assert provider.complete_json(system="s", user="u", schema=SCHEMA) == {"ok": True}


def test_a_missing_fixture_says_what_to_do(tmp_path: Any) -> None:
    """A changed prompt is a changed request and needs a new fixture -- the error
    has to say so, or the failure looks like a broken model."""
    with pytest.raises(ProviderError, match="no fixture"):
        ReplayProvider(tmp_path).complete_json(system="s", user="u", schema=SCHEMA)


def test_replay_records_from_a_live_provider(tmp_path: Any, captured: list) -> None:
    """How the offline suite gets its data without anyone hand-writing output."""
    recorder = ReplayProvider(tmp_path, live=GeminiProvider("k"))
    assert recorder.complete_json(system="s", user="u", schema=SCHEMA) == {"ok": True}

    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    saved = json.loads(written[0].read_text(encoding="utf-8"))
    assert saved["provider"] == "gemini"
    assert saved["system"] == "s"
    assert saved["response"] == {"ok": True}

    # Second call is served from disk, not the network.
    before = len(captured)
    assert recorder.complete_json(system="s", user="u", schema=SCHEMA) == {"ok": True}
    assert len(captured) == before


# ------------------------------------------------------------------ schema prep


def test_gemini_schema_strips_unsupported_keywords() -> None:
    """Gemini rejects these, and leaving them in produces an opaque 400."""
    cleaned = _to_gemini_schema(
        {
            "type": "object",
            "title": "Thing",
            "additionalProperties": False,
            "$defs": {"X": {}},
            "properties": {
                "a": {"type": "string", "title": "A"},
                "b": {"type": "array", "items": {"type": "object", "title": "B"}},
            },
        }
    )
    assert "title" not in cleaned
    assert "additionalProperties" not in cleaned
    assert "$defs" not in cleaned
    assert "title" not in cleaned["properties"]["a"]
    assert "title" not in cleaned["properties"]["b"]["items"]
    assert cleaned["properties"]["a"]["type"] == "string"
