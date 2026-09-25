"""
gemini_classifier.py
--------------------
Gemini Flash AI classification for YouTube comments.
Production-grade: exponential backoff retries via tenacity.

Hybrid mode (default):
  1. TransformerClassifier runs locally (free, fast) for sentiment + category.
  2. If skip_gemini=True → return transformer result directly (no API call).
  3. If skip_gemini=False → rate-limiter gate → Gemini for reply + summary only.

Fallback (transformers not installed): full Gemini-only mode, same as before.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional, List

import google.generativeai as genai
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an AI comment intelligence engine for a YouTube channel manager.
Classify comments and draft warm, on-brand responses.
Categories: question, complaint, compliment, spam, crisis, irrelevant
Crisis = health reactions, illness, injury, legal threats, child safety, death.
Tone: Always warm, helpful, never defensive."""


class GeminiClassifier:
    def __init__(
        self,
        api_key: str,
        crisis_keywords: Optional[List[str]] = None,
        transformer=None,     # Optional[TransformerClassifier]
        rate_limiter=None,    # Optional[RateLimiter]
    ):
        self.model = None
        try:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel(
                model_name=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
                system_instruction=SYSTEM_PROMPT,
            )
        except Exception as e:
            logger.warning(
                f"[GEMINI] ⚠️ Could not initialize Gemini model ({e}). "
                "The app will run in 'Transformer-Only' mode for now. "
                "Update with: pip install --upgrade google-generativeai"
            )

        self.crisis_keywords = [kw.lower() for kw in (crisis_keywords or [])]
        self.transformer     = transformer    # may be None (graceful degradation)
        self.rate_limiter    = rate_limiter   # may be None (no rate limiting)
        logger.info(
            f"Gemini classifier initialised. "
            f"Gemini={'✅' if self.model else '⚠️ off'} | "
            f"Transformer={'✅' if transformer and transformer.available else '⚠️ off'} | "
            f"Rate limiter={'✅' if rate_limiter else '⚠️ off'}"
        )

    # ── Crisis keyword pre-check ───────────────────────────────────────────────
    def _keyword_crisis_check(self, text: str) -> bool:
        t = text.lower()
        return any(kw in t for kw in self.crisis_keywords)

    # ── Rate-limited Gemini call ───────────────────────────────────────────────
    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(4),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _call_gemini(self, prompt: str) -> str:
        """Call Gemini with automatic exponential backoff on failure."""
        if not self.model:
            raise RuntimeError("Gemini model not initialized. Check package version.")
        
        if self.rate_limiter:
            self.rate_limiter.acquire()   # block until a token is available
        return self.model.generate_content(prompt).text.strip()

    # ── Single comment classification ─────────────────────────────────────────
    def classify_comment(self, comment: dict, brand: str = "General") -> dict:
        text = comment.get("text", "").strip()

        # 1. Empty comment — instant return
        if not text:
            comment.update({
                "category": "irrelevant", "sentiment": "neutral", "priority": "low",
                "crisis_flag": False, "classification_confidence": 0.0,
                "suggested_reply": None, "ai_summary": "",
            })
            return comment

        # 2. Keyword crisis check (instant, no API cost)
        if self._keyword_crisis_check(text):
            logger.warning(f"CRISIS KEYWORD detected: {comment.get('comment_id', '')[:20]}")
            comment.update({
                "category": "crisis", "sentiment": "negative", "priority": "urgent",
                "crisis_flag": True, "crisis_reason": "Keyword trigger",
                "classification_confidence": 0.95,
                "suggested_reply": None, "ai_summary": "Crisis keyword detected",
            })
            return comment

        # 3. Transformer pre-filter (local, free, ~50-150ms)
        transformer_result = self._run_transformer(text)

        if transformer_result.get("skip_gemini"):
            # Transformer is confident enough — skip Gemini entirely
            logger.info(
                f"[TRANSFORMER] Skipping Gemini for "
                f"{transformer_result.get('category', '?')} comment "
                f"(conf={transformer_result.get('confidence', 0):.2f})"
            )
            comment.update({
                "category":                 transformer_result["category"],
                "sentiment":                transformer_result["sentiment"],
                "priority":                 _category_to_priority(transformer_result["category"]),
                "crisis_flag":              False,
                "crisis_reason":            None,
                "classification_confidence": transformer_result["confidence"],
                "suggested_reply":          None,
                "ai_summary":               f"Auto-classified by local model.",
                "classified_by":            "transformer",
            })
            return comment

        # 4. Gemini enrichment (reply + summary only, not full classification)
        #    Use transformer fields as a base; Gemini fills in reply/summary.
        if transformer_result.get("confidence", 0) >= 0.55:
            return self._gemini_enrich(comment, brand, transformer_result)

        # 5. Full Gemini classification (transformer unavailable or very low confidence)
        return self._gemini_full_classify(comment, brand, transformer_result)

    # ── Transformer helper ────────────────────────────────────────────────────
    def _run_transformer(self, text: str) -> dict:
        """Run local transformer pre-filter. Returns fallback dict if unavailable."""
        if self.transformer and self.transformer.available:
            try:
                return self.transformer.classify(text)
            except Exception as exc:
                logger.debug(f"[TRANSFORMER] classify() error: {exc}")
        return {"sentiment": "neutral", "category": "irrelevant", "confidence": 0.0, "skip_gemini": False}

    # ── Gemini: reply + summary only (transformer already classified) ──────────
    def _gemini_enrich(self, comment: dict, brand: str, transformer_result: dict) -> dict:
        """Call Gemini only for suggested_reply and ai_summary; trust transformer for category/sentiment."""
        text     = comment.get("text", "")
        category = transformer_result.get("category", "irrelevant")
        sentiment= transformer_result.get("sentiment", "neutral")

        prompt = (
            f'A YouTube comment on a {brand} video has been pre-classified as:\n'
            f'  Category: {category}, Sentiment: {sentiment}\n\n'
            f'Comment: "{text}"\n\n'
            'Respond ONLY in valid JSON (no markdown):\n'
            '{"suggested_reply":"<warm reply or null for spam>","summary":"<max 8 words>",'
            '"crisis_flag":<true|false>,"crisis_reason":"<reason or null>"}'
        )

        try:
            raw = self._call_gemini(prompt)
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            result = json.loads(raw)

            # Override category and sentiment to crisis if Gemini detects it
            if result.get("crisis_flag"):
                category  = "crisis"
                sentiment = "negative"

            comment.update({
                "category":                 category,
                "sentiment":                sentiment,
                "priority":                 _category_to_priority(category),
                "crisis_flag":              bool(result.get("crisis_flag", False)),
                "crisis_reason":            result.get("crisis_reason"),
                "classification_confidence": transformer_result.get("confidence", 0.0),
                "suggested_reply":          result.get("suggested_reply"),
                "ai_summary":               result.get("summary", ""),
                "classified_by":            "hybrid",
            })
        except Exception as e:
            logger.error(f"Gemini enrichment failed after retries: {e}")
            # Fall back to pure transformer result
            comment.update({
                "category":                 category,
                "sentiment":                sentiment,
                "priority":                 _category_to_priority(category),
                "crisis_flag":              False,
                "crisis_reason":            None,
                "classification_confidence": transformer_result.get("confidence", 0.0),
                "suggested_reply":          None,
                "ai_summary":               "Enrichment failed",
                "classified_by":            "transformer_fallback",
            })

        return comment

    # ── Gemini: full classification (transformer unavailable / very low conf) ──
    def _gemini_full_classify(self, comment: dict, brand: str, transformer_result: dict) -> dict:
        """Full Gemini classification — same as original behaviour."""
        text = comment.get("text", "")
        prompt = (
            f'Analyse this YouTube comment on a {brand} video. Respond ONLY in valid JSON, no markdown.\n\n'
            f'Comment: "{text}"\n\n'
            'Return:\n'
            '{"category":"<question|complaint|compliment|spam|crisis|irrelevant>",'
            '"sentiment":"<positive|neutral|negative>",'
            '"priority":"<urgent|high|medium|low>",'
            '"crisis_flag":<true|false>,'
            '"crisis_reason":"<reason or null>",'
            '"confidence":<0.0-1.0>,'
            '"suggested_reply":"<warm reply or null for spam/crisis>",'
            '"summary":"<max 8 words>"}'
        )

        try:
            raw = self._call_gemini(prompt)
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            result = json.loads(raw)
            comment.update({
                "category":                 result.get("category", "irrelevant"),
                "sentiment":                result.get("sentiment", "neutral"),
                "priority":                 result.get("priority", "low"),
                "crisis_flag":              bool(result.get("crisis_flag", False)),
                "crisis_reason":            result.get("crisis_reason"),
                "classification_confidence": float(result.get("confidence", 0.0)),
                "suggested_reply":          result.get("suggested_reply"),
                "ai_summary":               result.get("summary", ""),
                "classified_by":            "gemini",
            })
        except Exception as e:
            logger.error(f"Gemini full classification failed after retries: {e}")
            comment.update({
                "category": "error", "sentiment": "neutral", "priority": "low",
                "crisis_flag": False, "classification_confidence": 0.0,
                "suggested_reply": None, "ai_summary": "Classification failed",
                "classified_by": "error",
            })

        return comment

    # ── Batch classification ──────────────────────────────────────────────────
    def classify_batch(self, comments: List[dict], brand: str = "General", delay: float = 0.4) -> List[dict]:
        classified = []
        total = len(comments)
        gemini_calls  = 0
        skipped_calls = 0

        for i, c in enumerate(comments, 1):
            text = (c.get("text") or "").strip()
            logger.info(f"  Classifying {i}/{total}...")
            result = self.classify_comment(c, brand=brand)
            classified.append(result)

            # Count Gemini vs. transformer-only
            classified_by = result.get("classified_by", "gemini")
            if classified_by == "transformer":
                skipped_calls += 1
            else:
                gemini_calls += 1

            # Only sleep between Gemini calls (transformer calls are instant)
            if i < total and classified_by != "transformer":
                time.sleep(delay)

        cats = [c.get("category", "?") for c in classified]
        dist = {cat: cats.count(cat) for cat in set(cats)}
        savings_pct = int(skipped_calls / total * 100) if total else 0
        logger.info(
            f"  Batch done: {len(classified)} comments | "
            f"Crisis: {sum(1 for c in classified if c.get('crisis_flag'))} | "
            f"Gemini calls: {gemini_calls} | Skipped (transformer): {skipped_calls} "
            f"({savings_pct}% saved) | Distribution: {dist}"
        )
        return classified

    # ── Performance insight ───────────────────────────────────────────────────
    def generate_performance_insight(self, video_data: List[dict], channel_name: str) -> str:
        if not video_data:
            return f"No video data available for {channel_name}."
        summaries = "\n".join(
            f"- '{v.get('title', '?')}' ({v.get('published_at', '')[:10]}): "
            f"{v.get('view_count', 0):,} views, {v.get('like_count', 0):,} likes, "
            f"{v.get('comment_count', 0):,} comments"
            for v in video_data[:10]
        )
        prompt = (
            f"Analytics for {channel_name} YouTube channel.\n\n"
            f"Recent videos:\n{summaries}\n\n"
            "Write 3-paragraph weekly insight: 1) best/worst video, 2) trends, "
            "3) 2-3 recommendations. Plain paragraphs, no bullets/headers, under 200 words."
        )
        try:
            return self._call_gemini(prompt)
        except Exception as e:
            logger.error(f"Insight generation failed: {e}")
            return "Insight generation temporarily unavailable."

    # ── Content brief ─────────────────────────────────────────────────────────
    def generate_content_brief(self, channel_name: str, top_comments: List[str]) -> str:
        sample = "\n".join(f'- "{c}"' for c in top_comments[:20])
        prompt = (
            f"Based on these YouTube comments from {channel_name}:\n{sample}\n\n"
            "Write 150-word content brief: 1) audience themes, 2) one video concept with hook, "
            "3) best upload time. 3 short paragraphs, no headers."
        )
        try:
            return self._call_gemini(prompt)
        except Exception as e:
            logger.error(f"Content brief generation failed: {e}")
            return "Content brief generation temporarily unavailable."


# ── Helper ────────────────────────────────────────────────────────────────────

def _category_to_priority(category: str) -> str:
    return {
        "crisis":     "urgent",
        "complaint":  "high",
        "question":   "medium",
        "compliment": "low",
        "spam":       "low",
        "irrelevant": "low",
    }.get(category, "low")