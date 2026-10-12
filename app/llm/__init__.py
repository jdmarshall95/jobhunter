"""Optional LLM layer (Gemini over REST)."""

from app.llm.gemini import (
    GeminiClient,
    LLMError,
    LLMResult,
    LLMService,
    NullLLM,
    load_prompt,
)

__all__ = ["GeminiClient", "LLMError", "LLMResult", "LLMService", "NullLLM", "load_prompt"]
