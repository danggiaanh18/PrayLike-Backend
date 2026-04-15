"""AI chat service with pluggable providers.

This module keeps the provider-specific wiring in one place so routers only
need to pass chat messages. It currently supports OpenAI, Azure OpenAI,
Anthropic Claude, Google Gemini, and any OpenAI-compatible endpoint.
"""

from dataclasses import dataclass
import os
from typing import Any, Dict, List, Optional

import httpx


class AIProviderError(Exception):
    """Raised when an AI provider is misconfigured or returns an error."""


@dataclass
class AIChatResult:
    provider: str
    model: str
    message: Dict[str, str]
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    raw: Optional[Dict[str, Any]] = None


class AIChatService:
    """Dispatch chat requests to multiple AI providers."""

    def __init__(self, timeout_seconds: float = 30.0):
        self.timeout_seconds = timeout_seconds
        self.default_provider = os.getenv("AI_PROVIDER", "openai").lower()

    def chat(
        self,
        messages: List[Dict[str, str]],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AIChatResult:
        resolved_provider = (provider or self.default_provider or "openai").lower()

        if resolved_provider in {"openai", "openai-compatible", "compat", "custom"}:
            return self._openai_style(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label=resolved_provider,
            )

        if resolved_provider in {"azure", "azure-openai", "azure_openai"}:
            return self._azure_openai(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                provider_label=resolved_provider,
            )

        if resolved_provider == "gemini":
            return self._gemini(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        if resolved_provider in {"claude", "anthropic"}:
            return self._claude(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        raise AIProviderError(
            f"未知的 AI provider: {resolved_provider}. 請使用 openai/azure/gemini/claude 或提供兼容的 openai endpoint。"
        )

    def _openai_style(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        provider_label: str,
    ) -> AIChatResult:
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        api_key = os.getenv("OPENAI_API_KEY")
        compat_base_url = os.getenv("AI_COMPAT_BASE_URL")
        compat_key = os.getenv("AI_COMPAT_API_KEY")

        resolved_base = compat_base_url or base_url
        resolved_key = compat_key or api_key
        resolved_model = model or os.getenv("AI_COMPAT_MODEL") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        if not resolved_key:
            raise AIProviderError("OpenAI 或相容服務未設定 API Key")

        url = f"{resolved_base.rstrip('/')}/chat/completions"
        payload: Dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
        }

        resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
        resp.raise_for_status()
        data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        role = message.get("role") or "assistant"

        return AIChatResult(
            provider=provider_label,
            model=data.get("model") or resolved_model,
            message={"role": role, "content": content},
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
            raw=data,
        )

    def _azure_openai(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
        provider_label: str,
    ) -> AIChatResult:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-01-preview")

        if not endpoint or not deployment or not api_key:
            raise AIProviderError("Azure OpenAI 設定不完整，請確認 endpoint/deployment/api key")

        resolved_model = model or os.getenv("AZURE_OPENAI_MODEL") or deployment
        url = (
            f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions"
            f"?api-version={api_version}"
        )

        payload: Dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }

        resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
        resp.raise_for_status()
        data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        role = message.get("role") or "assistant"

        return AIChatResult(
            provider=provider_label,
            model=data.get("model") or resolved_model,
            message={"role": role, "content": content},
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
            raw=data,
        )

    def _gemini(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> AIChatResult:
        api_key = os.getenv("GEMINI_API_KEY")
        base_url = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
        resolved_model = model or os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

        if not api_key:
            raise AIProviderError("Gemini 未設定 API Key")

        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        dialog_messages = [m for m in messages if m.get("role") != "system"]

        contents = []
        for item in dialog_messages:
            role = item.get("role") or "user"
            mapped_role = "user" if role == "user" else "model"
            contents.append({
                "role": mapped_role,
                "parts": [{"text": item.get("content", "")}],
            })

        payload: Dict[str, Any] = {
            "contents": contents,
        }
        if system_parts:
            payload["system_instruction"] = {
                "role": "system",
                "parts": [{"text": "\n\n".join(system_parts)}],
            }

        generation_config: Dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if generation_config:
            payload["generationConfig"] = generation_config

        url = f"{base_url.rstrip('/')}/models/{resolved_model}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}

        resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates") or []
        if not candidates:
            raise AIProviderError("Gemini 回傳為空")
        first = candidates[0]
        parts = (first.get("content") or {}).get("parts") or []
        text_segments = [part.get("text", "") for part in parts]
        content = "".join(text_segments)

        return AIChatResult(
            provider="gemini",
            model=resolved_model,
            message={"role": "assistant", "content": content},
            finish_reason=first.get("finishReason"),
            usage=data.get("usage_metadata") or data.get("usage"),
            raw=data,
        )

    def _claude(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> AIChatResult:
        api_key = os.getenv("CLAUDE_API_KEY")
        base_url = os.getenv("CLAUDE_BASE_URL", "https://api.anthropic.com/v1")
        resolved_model = model or os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")

        if not api_key:
            raise AIProviderError("Claude 未設定 API Key")

        system_messages = [m["content"] for m in messages if m.get("role") == "system"]
        dialog_messages = [m for m in messages if m.get("role") != "system"]

        claude_messages = []
        for msg in dialog_messages:
            role = msg.get("role") or "user"
            mapped_role = "assistant" if role == "assistant" else "user"
            claude_messages.append({
                "role": mapped_role,
                "content": msg.get("content", ""),
            })

        payload: Dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": max_tokens or 1024,
            "messages": claude_messages,
        }
        if system_messages:
            payload["system"] = "\n\n".join(system_messages)
        if temperature is not None:
            payload["temperature"] = temperature

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        url = f"{base_url.rstrip('/')}/messages"
        resp = httpx.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
        resp.raise_for_status()
        data = resp.json()

        content_blocks = data.get("content") or []
        text_segments = [block.get("text", "") for block in content_blocks if isinstance(block, dict)]
        content = "".join(text_segments)

        return AIChatResult(
            provider="claude",
            model=resolved_model,
            message={"role": "assistant", "content": content},
            finish_reason=data.get("stop_reason"),
            usage=data.get("usage"),
            raw=data,
        )
