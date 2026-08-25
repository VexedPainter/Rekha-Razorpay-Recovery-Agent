"""Language-model providers for the AI recovery layer.

Free tiers first: Gemini and Groq both work with no payment. Anthropic is
supported and optional. `ReplayProvider` serves recorded fixtures so the default
test suite never touches a network or a paid API.

See `base.py` for why this is an interface over raw HTTP rather than three vendor
SDKs.
"""

from recovery.providers.base import (
    LLMProvider,
    ProviderError,
    load_env,
    reset_env_cache,
)
from recovery.providers.providers import (
    AnthropicProvider,
    GeminiProvider,
    GroqProvider,
    ReplayProvider,
    available_providers,
    resolve_provider,
)

__all__ = [
    "AnthropicProvider",
    "GeminiProvider",
    "GroqProvider",
    "LLMProvider",
    "ProviderError",
    "ReplayProvider",
    "available_providers",
    "load_env",
    "reset_env_cache",
    "resolve_provider",
]
