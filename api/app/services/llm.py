"""
api/app/services/llm.py

Direct REST-based LLM provider abstraction for Incident Copilot.

Provides:
  - LLMUnavailableError: Domain exception raised when the LLM service is down,
    rate-limited, or misconfigured.
  - LLMResult: Dataclass containing generated text and token metrics.
  - LLMProvider: Abstract base class.
  - GeminiProvider: Direct HTTP REST implementation using httpx with retries,
    per-model thinkingConfig adaptation, and strict response parsing.
  - OllamaProvider: Offline fallback stub.
  - get_llm_provider: Factory function.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMUnavailableError(Exception):
    """Raised when the LLM provider is down, rate-limited, unauthenticated, or fails."""

    pass


@dataclass
class LLMResult:
    """Result of an LLM generation call."""

    text: str
    tokens_in: int
    tokens_out: int
    model: str
    truncated: bool = False


class LLMProvider(ABC):
    """Abstract base class for LLM generation backends."""

    @abstractmethod
    async def generate(
        self,
        system: str,
        user: str,
        max_output_tokens: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> LLMResult:
        """Generates completion text given system and user prompts."""
        pass


class GeminiProvider(LLMProvider):
    """
    Direct REST integration for Google Gemini models via httpx.

    Avoids SDK overhead and provides direct control over headers, timeouts,
    retries, and thinking budget suppression.
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.gemini_api_key:
            logger.warning("GEMINI_API_KEY is not configured. Calls to /ask will return 503.")

    async def generate(
        self,
        system: str,
        user: str,
        max_output_tokens: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> LLMResult:
        settings = get_settings()
        if not settings.gemini_api_key:
            raise LLMUnavailableError("GEMINI_API_KEY is not configured.")

        eff_max_output_tokens = max_output_tokens or settings.llm_max_output_tokens
        eff_timeout_s = timeout_s or settings.llm_timeout_s

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.gemini_model}:generateContent"
        )
        headers = {
            "x-goog-api-key": settings.gemini_api_key,
            "Content-Type": "application/json",
        }

        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": settings.llm_temperature,
                "maxOutputTokens": eff_max_output_tokens,
            },
        }

        # For models starting with gemini-2.5 or supporting thinkingConfig, disable thinking budget
        if settings.gemini_model.startswith("gemini-2.5"):
            body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

        total_attempts = 1 + max(0, settings.llm_max_retries)
        resp = None
        for attempt in range(total_attempts):
            try:
                async with httpx.AsyncClient(timeout=eff_timeout_s) as client:
                    resp = await client.post(url, headers=headers, json=body)

                if resp.status_code in (429, 503) and attempt < settings.llm_max_retries:
                    logger.warning(
                        "Gemini returned status %d on attempt %d",
                        resp.status_code,
                        attempt + 1,
                    )
                    delay = 1.5 if attempt == 0 else 3.0
                    await asyncio.sleep(delay)
                    continue

                if resp.status_code != 200:
                    logger.error("Gemini returned status %d", resp.status_code)
                    raise LLMUnavailableError(f"Gemini API returned status {resp.status_code}")

                break
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException) as exc:
                if attempt < settings.llm_max_retries:
                    logger.warning("Gemini timeout on attempt %d", attempt + 1)
                    delay = 1.5 if attempt == 0 else 3.0
                    await asyncio.sleep(delay)
                    continue
                raise LLMUnavailableError("Gemini request timed out") from exc
            except httpx.RequestError as exc:
                raise LLMUnavailableError(f"Gemini connection error: {type(exc).__name__}") from exc

        if resp is None:
            raise LLMUnavailableError("Gemini API call failed without a response.")

        try:
            data = resp.json()
        except Exception as exc:
            raise LLMUnavailableError(f"Failed to parse Gemini response: {exc}") from exc

        candidates = data.get("candidates", [])
        if not candidates:
            raise LLMUnavailableError("Gemini returned no candidates.")

        first_cand = candidates[0]
        finish_reason = first_cand.get("finishReason", "STOP")
        parts = first_cand.get("content", {}).get("parts", [])
        text_segments = [
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and "text" in part
            and not part.get("thought")
        ]
        text = "".join(text_segments).strip()

        if finish_reason in ("SAFETY", "RECITATION", "OTHER") or not text:
            raise LLMUnavailableError(
                f"Gemini generation stopped or blocked with reason: {finish_reason}"
            )

        truncated = (finish_reason == "MAX_TOKENS")
        tokens_in = data.get("usageMetadata", {}).get("promptTokenCount", 0)
        tokens_out = data.get("usageMetadata", {}).get("candidatesTokenCount", 0)

        return LLMResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=settings.gemini_model,
            truncated=truncated,
        )


class OllamaProvider(LLMProvider):
    """Offline fallback LLM provider using Ollama."""

    async def generate(
        self,
        system: str,
        user: str,
        max_output_tokens: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> LLMResult:
        raise NotImplementedError("Ollama provider is not implemented yet")


_provider_instance: Optional[LLMProvider] = None


def get_llm_provider() -> LLMProvider:
    """Returns the singleton LLMProvider instance based on application settings."""
    global _provider_instance
    if _provider_instance is None:
        settings = get_settings()
        provider_name = settings.llm_provider.lower()
        if provider_name == "gemini":
            _provider_instance = GeminiProvider()
        elif provider_name == "ollama":
            _provider_instance = OllamaProvider()
        else:
            raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
    return _provider_instance


__all__ = [
    "LLMUnavailableError",
    "LLMResult",
    "LLMProvider",
    "GeminiProvider",
    "OllamaProvider",
    "get_llm_provider",
]
