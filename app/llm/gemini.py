"""Optional Gemini re-scoring / draft generation over plain REST + httpx.

Design decisions:

* No SDK — one ``POST`` to ``generativelanguage.googleapis.com`` keeps the
  dependency tree small and lets tests mock it with ``respx``.
* The system is **fully functional without a key**: :class:`NullLLM` returns
  ``None`` everywhere and costs nothing.
* Strict JSON answers only (``responseMimeType: application/json``); anything
  unparsable is treated as "no opinion", never as an exception.
* Budget: ``LLM_DAILY_BUDGET`` is checked before every call; the counter lives
  in the ``llm_usage`` table.
* Cache: by ``sha1(description)`` in the ``kv`` table, so a re-run is free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import APP_ROOT, Profile, Settings
from app.logging_setup import get_logger
from app.models import Job
from app.storage import Database

log = get_logger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
#: Free-tier Gemini answers 429 when calls come too fast; those are worth retrying.
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 20.0
#: Rough price for gemini-2.5-flash, USD per 1M tokens (only used for budgeting).
PRICE_IN_PER_M = 0.30
PRICE_OUT_PER_M = 2.50

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["fit", "maybe", "no"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary_ru": {"type": "string", "maxLength": 400},
        "red_flags": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    },
    "required": ["verdict", "score", "summary_ru"],
}


@dataclass(slots=True)
class LLMResult:
    verdict: str
    score: int
    summary: str
    red_flags: list[str]


class LLMError(RuntimeError):
    pass


def load_prompt(name: str) -> str:
    path = APP_ROOT / "prompts" / f"{name}.md"
    if not path.exists():
        raise LLMError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def _desc_hash(job: Job) -> str:
    payload = f"{job.title}|{job.company}|{job.description[:4000]}"
    return f"llm:{hashlib.sha1(payload.encode()).hexdigest()[:16]}"


class GeminiClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """One ``generateContent`` call with exponential backoff on 429/5xx.

        The free tier answers 429 whenever several calls land in the same second,
        so a plain "no opinion" fallback would silently kill all re-scoring.
        """
        delay = RETRY_BASE_DELAY
        last_status = 0
        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._client.post(
                    url,
                    params={"key": self.settings.gemini_api_key},
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"network: {type(exc).__name__}") from exc
            if resp.status_code < 400:
                return resp.json()  # type: ignore[no-any-return]
            last_status = resp.status_code
            if resp.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES - 1:
                break
            wait = float(resp.headers.get("Retry-After") or 0) or delay
            wait = min(wait, RETRY_MAX_DELAY)
            log.debug("llm http %d, retry in %.1fs", resp.status_code, wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, RETRY_MAX_DELAY)
        raise LLMError(f"http {last_status}")

    async def _generate(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        *,
        max_tokens: int = 600,
        temperature: float = 0.2,
    ) -> tuple[dict[str, Any], int, int]:
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                # 2.5-flash spends its budget on hidden reasoning by default;
                # with a 500-token cap that can leave no room for the answer.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseSchema"] = schema

        url = ENDPOINT.format(model=self.settings.gemini_model)
        data = await self._post(url, body)
        usage = data.get("usageMetadata") or {}
        in_tok = int(usage.get("promptTokenCount") or 0)
        out_tok = int(usage.get("candidatesTokenCount") or 0)
        log.debug("llm tokens in=%d out=%d", in_tok, out_tok)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("empty candidate") from exc
        if not schema:
            return {"text": text.strip()}, in_tok, out_tok
        try:
            return json.loads(text), in_tok, out_tok
        except json.JSONDecodeError as exc:
            raise LLMError(f"bad json: {exc.msg}") from exc

    # ------------------------------------------------------------------ public

    @staticmethod
    def _facts_block(profile: Profile) -> str:
        return "\n".join(f"- {f}" for f in profile.candidate.facts) or "- (нет фактов)"

    async def score(self, job: Job, profile: Profile) -> tuple[LLMResult, float]:
        prompt = (
            load_prompt("rescore")
            .replace("{role}", profile.candidate.headline or "DevOps/SRE engineer")
            .replace("{facts}", self._facts_block(profile))
            .replace("{title}", job.title)
            .replace("{company}", job.company)
            .replace("{location}", job.location or "не указана")
            .replace("{salary}", job.salary_text)
            .replace("{rule_score}", f"{job.score:.0f}")
            .replace("{breakdown}", "; ".join(label for label, _ in job.score_breakdown) or "нет")
            .replace("{description}", job.description[:3500])
        )
        payload, in_tok, out_tok = await self._generate(prompt, SCORE_SCHEMA, max_tokens=500)
        cost = in_tok / 1e6 * PRICE_IN_PER_M + out_tok / 1e6 * PRICE_OUT_PER_M
        result = LLMResult(
            verdict=str(payload.get("verdict") or "maybe"),
            score=int(payload.get("score") or 0),
            summary=str(payload.get("summary_ru") or "").strip(),
            red_flags=[str(x) for x in (payload.get("red_flags") or [])][:4],
        )
        return result, cost

    async def draft(self, job: Job, profile: Profile, lang: str = "ru") -> tuple[str, float] | None:
        prompt = (
            load_prompt("draft")
            .replace("{lang}", "русском" if lang == "ru" else "английском")
            .replace("{facts}", self._facts_block(profile))
            .replace("{title}", job.title)
            .replace("{company}", job.company)
            .replace("{stack}", ", ".join(profile.candidate.stack_core[:8]))
            .replace("{description}", job.description[:2500])
        )
        payload, in_tok, out_tok = await self._generate(
            prompt, None, max_tokens=700, temperature=0.4
        )
        cost = in_tok / 1e6 * PRICE_IN_PER_M + out_tok / 1e6 * PRICE_OUT_PER_M
        text = str(payload.get("text") or "").strip()
        return (text, cost) if text else None


class NullLLM:
    """Stand-in when there is no API key — the pipeline must keep working."""

    async def score(self, job: Job, profile: Profile) -> None:
        return None

    async def draft(self, job: Job, profile: Profile, lang: str = "ru") -> None:
        return None


class LLMService:
    """Budget + cache wrapper used by the pipeline and the bot."""

    def __init__(self, settings: Settings, db: Database, client: Any | None = None) -> None:
        self.settings = settings
        self.db = db
        self._client = client
        self._disabled = False
        self._lock = asyncio.Lock()

    def attach(self, client: Any) -> None:
        """Inject the real transport lazily (used by the composition root)."""
        self._client = client

    def disable(self) -> None:
        """Force the layer off for this process (``--no-llm``)."""
        self._disabled = True

    @property
    def enabled(self) -> bool:
        return self.settings.llm_available and not self._disabled

    async def _budget_left(self) -> bool:
        spent = await self.db.llm_spend_today()
        if spent >= self.settings.llm_daily_budget_usd:
            log.warning(
                "llm budget exhausted spent=%.4f limit=%.2f",
                spent,
                self.settings.llm_daily_budget_usd,
            )
            return False
        return True

    async def rescore(self, job: Job, profile: Profile) -> LLMResult | None:
        """Re-score one job. Returns ``None`` when disabled/budget/cache-miss-free."""
        if not self.enabled or self._client is None:
            return None
        key = _desc_hash(job)
        cached = await self.db.kv_get(key)
        if cached:
            try:
                blob = json.loads(cached)
                return self._apply(
                    job,
                    LLMResult(
                        verdict=blob.get("verdict", "maybe"),
                        score=int(blob.get("score", 0)),
                        summary=blob.get("summary", ""),
                        red_flags=list(blob.get("red_flags") or []),
                    ),
                )
            except (json.JSONDecodeError, ValueError):
                pass
        if not await self._budget_left():
            return None
        try:
            async with self._lock:
                result, cost = await self._client.score(job, profile)
        except LLMError as exc:
            log.warning("llm rescore failed job=%s err=%s", job.id[:8], exc)
            return None
        await self.db.llm_add_usage(1, 0, 0, cost)
        await self.db.kv_set(
            key,
            json.dumps(
                {
                    "verdict": result.verdict,
                    "score": result.score,
                    "summary": result.summary,
                    "red_flags": result.red_flags,
                },
                ensure_ascii=False,
            ),
        )
        await self.db.set_llm(job.id, result.verdict, result.summary)
        return self._apply(job, result)

    @staticmethod
    def _apply(job: Job, result: LLMResult) -> LLMResult:
        """Mirror the verdict onto the job: the row may not exist yet (the pipeline
        upserts *after* re-scoring), so ``set_llm`` alone would silently drop it.
        """
        job.llm_verdict = result.verdict
        job.llm_summary = result.summary
        return result

    async def draft(self, job: Job, profile: Profile, lang: str = "ru") -> str | None:
        if not self.enabled or self._client is None:
            return None
        if not await self._budget_left():
            return None
        try:
            async with self._lock:
                outcome = await self._client.draft(job, profile, lang)
        except LLMError as exc:
            log.warning("llm draft failed job=%s err=%s", job.id[:8], exc)
            return None
        if outcome is None:
            return None
        text, cost = outcome
        await self.db.llm_add_usage(1, 0, 0, cost)
        return text


__all__ = [
    "GeminiClient",
    "LLMError",
    "LLMResult",
    "LLMService",
    "NullLLM",
    "load_prompt",
]
