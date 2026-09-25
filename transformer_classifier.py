"""
transformer_classifier.py
--------------------------
Local HuggingFace transformer pre-filter for YouTube comment classification.

Uses two lightweight models loaded ONCE at startup:
  1. distilbert-base-uncased-finetuned-sst-2-english  (~67 MB)
     → Sentiment: POSITIVE / NEGATIVE → maps to positive / negative / neutral
  2. cross-encoder/nli-MiniLM2-L6-H768  (~85 MB)
     → Zero-shot category: question / complaint / compliment / spam / irrelevant

This avoids calling Gemini for comments that are clearly spam, irrelevant,
or for straightforward sentiment — saving ~60-80% of API calls.

When `skip_gemini=True` is returned, the caller should NOT call Gemini.
When `skip_gemini=False`, caller should enrich with Gemini (reply + summary only).
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ── Category definitions for zero-shot classification ────────────────────────
CATEGORIES = [
    "question",     # user is asking something
    "complaint",    # user is dissatisfied or reporting a problem
    "compliment",   # user is praising or expressing satisfaction
    "spam",         # promotional, irrelevant, or repetitive noise
    "irrelevant",   # off-topic, emoji-only, or too short to classify
]

# Below this threshold, we fall back to Gemini for ambiguous comments
CATEGORY_CONFIDENCE_THRESHOLD = 0.70
SPAM_IRRELEVANT_SKIP_THRESHOLD = 0.65   # lower bar — better to skip cheap comments
MIN_TEXT_LENGTH = 4                      # skip Gemini for very short comments


class TransformerClassifier:
    """
    Local pre-filter that classifies sentiment and category using CPU-based
    HuggingFace models. Loaded once; thread-safe for read operations.

    Usage:
        classifier = TransformerClassifier()
        result = classifier.classify("I love this product!")
        # {'sentiment': 'positive', 'category': 'compliment',
        #   'confidence': 0.94, 'skip_gemini': True}
    """

    def __init__(self):
        self._sentiment_pipe = None
        self._zeroshot_pipe  = None
        self._available      = False
        self._load_models()

    def _load_models(self) -> None:
        """Lazily load both pipelines. Logs a warning if transformers is not installed."""
        try:
            from transformers import pipeline  # noqa: PLC0415

            logger.info("[TRANSFORMER] Loading sentiment model (distilbert-sst-2)...")
            self._sentiment_pipe = pipeline(
                "text-classification",
                model="distilbert-base-uncased-finetuned-sst-2-english",
                device=-1,        # explicit CPU
                truncation=True,
                max_length=512,
            )

            logger.info("Loading Category model: MoritzLaurer/mDeBERTa-v3-base-mnli-xnli...")
            self._zeroshot_pipe = pipeline(
                "zero-shot-classification",
                model="MoritzLaurer/mDeBERTa-v3-base-mnli-xnli",
                device=-1  # CPU
            )

            self._available = True
            logger.info("[TRANSFORMER] ✅ Both models loaded successfully.")

        except ImportError:
            logger.warning(
                "[TRANSFORMER] ⚠️  'transformers' not installed — "
                "falling back to Gemini-only mode. "
                "Install with: pip install transformers torch"
            )
        except Exception as exc:
            logger.warning(f"[TRANSFORMER] ⚠️  Model load failed ({exc}) — falling back to Gemini-only mode.")

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True if models are loaded and ready."""
        return self._available

    def classify(self, text: str) -> dict:
        """
        Classify a comment using local models.

        Returns a dict with keys:
          sentiment     : 'positive' | 'negative' | 'neutral'
          category      : one of CATEGORIES
          confidence    : float 0–1 (category confidence)
          skip_gemini   : bool — True means caller should NOT call Gemini
        """
        if not self._available:
            return _fallback_result()

        text = (text or "").strip()

        # 1. Empty / very short — skip Gemini entirely
        if len(text) < MIN_TEXT_LENGTH:
            return {
                "sentiment":   "neutral",
                "category":    "irrelevant",
                "confidence":  1.0,
                "skip_gemini": True,
            }

        # 2. Sentiment classification
        sentiment = self._classify_sentiment(text)

        # 3. Category zero-shot
        category, cat_confidence = self._classify_category(text)

        # 4. Decide whether to skip Gemini
        skip_gemini = _should_skip_gemini(category, cat_confidence)

        result = {
            "sentiment":   sentiment,
            "category":    category,
            "priority":    "high" if category in ("question", "complaint") else "medium",
            "classification_confidence": round(cat_confidence, 4),
            "confidence":  round(cat_confidence, 4), # keep for compatibility
            "skip_gemini": skip_gemini,
        }

        logger.debug(
            f"[TRANSFORMER] '{text[:60]}' → "
            f"sentiment={sentiment}, category={category} ({cat_confidence:.2f}), "
            f"skip_gemini={skip_gemini}"
        )
        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _classify_sentiment(self, text: str) -> str:
        """Return 'positive', 'negative', or 'neutral'."""
        try:
            out = self._sentiment_pipe(text[:512])[0]
            label = out["label"].lower()   # 'positive' or 'negative'
            score = out["score"]

            # Map low-confidence detections to neutral
            if score < 0.65:
                return "neutral"
            return label  # already 'positive' or 'negative'

        except Exception as exc:
            logger.debug(f"[TRANSFORMER] Sentiment error: {exc}")
            return "neutral"

    def _classify_category(self, text: str) -> Tuple[str, float]:
        """Return (category_label, confidence)."""
        try:
            out = self._zeroshot_pipe(text[:512], candidate_labels=CATEGORIES, multi_label=False)
            top_label      = out["labels"][0]
            top_confidence = out["scores"][0]
            return top_label, top_confidence

        except Exception as exc:
            logger.debug(f"[TRANSFORMER] Category error: {exc}")
            return "irrelevant", 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _should_skip_gemini(category: str, confidence: float) -> bool:
    """
    Decide whether to skip calling Gemini.

    Skip when:
     - Comment is spam or irrelevant with reasonable confidence
     - Any category with very high confidence (clearly classified)
    """
    if category in ("spam", "irrelevant") and confidence >= SPAM_IRRELEVANT_SKIP_THRESHOLD:
        return True
    # High confidence on any label — transformer is reliable enough alone
    if confidence >= 0.90:
        return True
    return False


def _fallback_result() -> dict:
    """Used when models are unavailable — returns defaults that force Gemini."""
    return {
        "sentiment":   "neutral",
        "category":    "irrelevant",
        "confidence":  0.0,
        "skip_gemini": False,   # always call Gemini if transformers unavailable
    }
