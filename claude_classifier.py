"""
claude_classifier.py
-------------------
Anthropic Claude 3.5 Sonnet/Haiku implementation for comment classification.
This replaces the Gemini-based classifier while maintaining the same interface.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Dict
import anthropic
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
Priority Levels: low, medium, high, urgent
Crisis = health reactions, illness, injury, legal threats, child safety, death.
Tone: Always warm, helpful, never defensive."""

class ClaudeClassifier:
    def __init__(
        self,
        api_key: str,
        crisis_keywords: Optional[List[str]] = None,
        transformer=None,
        rate_limiter=None,
        model_name: str = "claude-3-haiku-20240307"
    ):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model_name = model_name
        self.crisis_keywords = [kw.lower() for kw in (crisis_keywords or [])]
        self.transformer = transformer
        self.rate_limiter = rate_limiter
        
        logger.info(
            f"Claude classifier initialised. "
            f"Model={model_name} | "
            f"Transformer={'✅' if transformer and transformer.available else '⚠️ off'} | "
            f"Rate limiter={'✅' if rate_limiter else '⚠️ off'}"
        )

    def classify_comment(self, text: str) -> Dict:
        """3-stage hybrid classification: Crisis Keywords -> Transformer -> Claude."""
        if not text or len(text.strip()) < 3:
            return {"category": "irrelevant", "sentiment": "neutral", "skip_gemini": True}

        # Stage 1: Fast keyword crisis check
        text_lower = text.lower()
        if any(kw in text_lower for kw in self.crisis_keywords):
            return self._claude_full_classify(text, reason="Crisis keyword trigger")

        # Stage 2: Transformer pre-filter
        if self.transformer and self.transformer.available:
            t_res = self.transformer.classify(text)
            if t_res.get("skip_gemini"):
                t_res["classified_by"] = "transformer"
                return t_res

        # Stage 3: Claude Enrichment
        return self._claude_enrich(text, t_res if self.transformer else None)

    def _claude_enrich(self, text: str, transformer_res: Optional[Dict] = None) -> Dict:
        """Call Claude to generate reply and summary for high-value comments."""
        try:
            prompt = (
                f"Comment: \"{text}\"\n\n"
                "Task: Draft a warm reply and a short 1-sentence summary.\n"
                "Format your response exactly as follows:\n"
                "<reply>YOUR_REPLY_HERE</reply>\n"
                "<summary>YOUR_SUMMARY_HERE</summary>\n"
                "<priority>low/medium/high/urgent</priority>"
            )
            if transformer_res:
                prompt += f"\nDetected Category: {transformer_res['category']}, Sentiment: {transformer_res['sentiment']}"

            res_text = self._call_claude(prompt)
            
            # Use basic tag extraction
            reply = ""
            summary = ""
            if "<reply>" in res_text and "</reply>" in res_text:
                reply = res_text.split("<reply>")[1].split("</reply>")[0].strip()
            if "<summary>" in res_text and "</summary>" in res_text:
                summary = res_text.split("<summary>")[1].split("<summary>" if "<summary>" in res_text.split("<summary>")[1] else "</summary>")[0].strip() # Fix for potential nested tags in some models
                summary = res_text.split("<summary>")[1].split("</summary>")[0].strip()
            
            priority = "medium"
            if "<priority>" in res_text and "</priority>" in res_text:
                priority = res_text.split("<priority>")[1].split("</priority>")[0].strip().lower()
            
            # Fallback if tags missing
            return {
                "category": transformer_res["category"] if transformer_res else "unknown",
                "sentiment": transformer_res["sentiment"] if transformer_res else "neutral",
                "priority": priority,
                "classification_confidence": transformer_res["confidence"] if transformer_res else 0.95,
                "suggested_reply": reply,
                "ai_summary": summary,
                "classified_by": "claude_hybrid"
            }
        except Exception as e:
            logger.error(f"Claude enrichment failed: {e}")
            res = transformer_res or {"category": "unknown", "sentiment": "neutral"}
            res["classified_by"] = "transformer_fallback"
            res["ai_summary"] = "AI enrichment unavailable"
            return res

    def _claude_full_classify(self, text: str, reason: str = "") -> Dict:
        """Slow path: Use Claude for everything (used for suspected crisis/spam)."""
        logger.info(f"Using Claude for full classification. Reason: {reason}")
        try:
            prompt = f"Identify category and sentiment for: \"{text}\". Draft a warm reply."
            res_text = self._call_claude(prompt)
            # Mapping logic would go here
            return {
                "category": "crisis" if reason else "question", 
                "sentiment": "neutral", 
                "priority": "urgent" if reason else "high",
                "classification_confidence": 0.98,
                "suggested_reply": res_text, "ai_summary": res_text[:100],
                "classified_by": "claude_full"
            }
        except Exception as e:
            logger.error(f"Claude full classification failed: {e}")
            return {"category": "error", "sentiment": "neutral", "classified_by": "error"}

    def classify_batch(self, comments: List[Dict], brand: str, delay: float = 1.0) -> List[Dict]:
        """Process a list of comments through the hybrid pipeline with pacing."""
        classified = []
        for c in comments:
            res = self.classify_comment(c["text"])
            c.update(res)
            classified.append(c)
            if not res.get("skip_gemini"):
                time.sleep(delay)
        return classified

    def generate_performance_insight(self, videos: List[Dict], brand: str) -> str:
        """Generate high-level channel performance insight using Claude."""
        try:
            prompt = f"Analyze recent performance for {brand} YouTube videos: {videos}. Give 1 key takeaway."
            return self._call_claude(prompt)
        except Exception as e:
            logger.error(f"Insight generation failed: {e}")
            return "Performance insights temporarily unavailable."

    def generate_content_brief(self, brand: str, questions: List[str]) -> str:
        """Generate content ideas based on brand questions."""
        try:
            prompt = f"Users are asking these questions about {brand}: {questions[:20]}. Suggest 1 viral video idea."
            return self._call_claude(prompt)
        except Exception as e:
            logger.error(f"Brief generation failed: {e}")
            return "Content brief generation temporarily unavailable."

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        before_sleep=before_sleep_log(logger, logging.INFO),
    )
    def _call_claude(self, prompt: str) -> str:
        if self.rate_limiter:
            self.rate_limiter.acquire()
        
        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
