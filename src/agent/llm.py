"""LLM client abstraction — pluggable provider for text + vision inference.

Operators choose a provider via the LLM_PROVIDER config setting.
Each provider implements the LLMClient protocol: a single `ask()` method
that accepts a system prompt, user text, optional base64 screenshot,
and max_tokens. Returns the raw text response.

Supported providers:
  - anthropic (default) — Claude via AsyncAnthropic
  - bedrock — Claude via Amazon Bedrock (uses AWS credentials, no API key needed)
  - openai — GPT-4o via AsyncOpenAI
"""

from __future__ import annotations

from typing import Any, Protocol

from src.core.logging import get_logger

log = get_logger(__name__)


class LLMClient(Protocol):
    async def ask(
        self,
        system: str,
        user_text: str,
        screenshot_b64: str | None = None,
        max_tokens: int = 1024,
    ) -> str: ...


class AnthropicClient:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise RuntimeError(
                "Install the anthropic extra: uv pip install 'waycore-bank-scraper[anthropic]'"
            ) from e

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def ask(
        self,
        system: str,
        user_text: str,
        screenshot_b64: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        content: list[Any] = []
        if screenshot_b64:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": screenshot_b64,
                    },
                }
            )
        content.append({"type": "text", "text": user_text})

        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        if not msg.content or not hasattr(msg.content[0], "text"):
            raise ValueError("LLM returned empty or non-text response")
        return str(msg.content[0].text)


class BedrockClient:
    """Claude via Amazon Bedrock — uses AWS credentials, no API key needed."""

    def __init__(
        self, model: str = "us.anthropic.claude-sonnet-4-6-v1", region: str = "us-east-1"
    ) -> None:
        try:
            from anthropic import AsyncAnthropicBedrock
        except ImportError as e:
            raise RuntimeError(
                "Install the anthropic extra: uv pip install 'waycore-bank-scraper[anthropic]'"
            ) from e

        self._client = AsyncAnthropicBedrock(aws_region=region)
        self._model = model

    async def ask(
        self,
        system: str,
        user_text: str,
        screenshot_b64: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        content: list[Any] = []
        if screenshot_b64:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": screenshot_b64,
                    },
                }
            )
        content.append({"type": "text", "text": user_text})

        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        if not msg.content or not hasattr(msg.content[0], "text"):
            raise ValueError("LLM returned empty or non-text response")
        return str(msg.content[0].text)


class OpenAIClient:
    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError(
                "Install the openai extra: uv pip install 'waycore-bank-scraper[openai]'"
            ) from e

        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def ask(
        self,
        system: str,
        user_text: str,
        screenshot_b64: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

        user_content: list[dict[str, Any]] = []
        if screenshot_b64:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                }
            )
        user_content.append({"type": "text", "text": user_text})
        messages.append({"role": "user", "content": user_content})

        resp = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=messages,
        )
        text = resp.choices[0].message.content
        if not text:
            raise ValueError("LLM returned empty response")
        return str(text)


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        from src.core.config import settings

        provider = settings.llm_provider
        if provider == "anthropic":
            _client = AnthropicClient(
                api_key=settings.anthropic_api_key.get_secret_value(),
                model=settings.llm_model or "claude-sonnet-4-6",
            )
        elif provider == "bedrock":
            _client = BedrockClient(
                model=settings.llm_model or "us.anthropic.claude-sonnet-4-6-v1",
                region=settings.aws_region,
            )
        elif provider == "openai":
            _client = OpenAIClient(
                api_key=settings.openai_api_key.get_secret_value(),
                model=settings.llm_model or "gpt-4o",
            )
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {provider}")
        log.info("llm.client_initialized", provider=provider)
    return _client
