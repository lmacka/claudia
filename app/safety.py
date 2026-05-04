"""
Kid-mode safety floor: regex tripwire + Haiku pre-turn classifier.

Premise 3 (docs/design.md): the safety floor is non-disableable. The Helm
chart's values.schema.json enforces this at install time
(`kid.safety.haiku_classifier: const true`). At runtime in kid mode, every
incoming message is screened before the companion is allowed to reply.

Two tiers, in order:

1. Regex tripwire — fires on a hand-curated list of crisis keyword/phrase
   patterns. Cheap, deterministic, catches obvious cases. False-positives
   are acceptable (the "trip" doesn't terminate the conversation, it just
   surfaces the crisis footer with elevated prominence and tags the turn so
   the auditor sees it).

2. Haiku classifier — Anthropic Haiku reads the message and returns a
   structured classification. Catches coded language, emoji-laden requests,
   and anything the regex misses. ~$0.0001 per call. Non-disableable.

Adult mode: this module is imported but `screen_message` returns a no-op
result (clean classification, no flags). The chart still renders the
non-disable env vars but the behaviour gates are mode-aware.

Output contract for `screen_message`:

    SafetyResult(
        flagged_regex: bool,         # regex tripwire fired
        flagged_classifier: bool,    # Haiku said yes
        category: str,               # one of CATEGORIES below or "none"
        prominence: str,             # "normal" | "elevated" | "crisis"
        explanation: str,            # short, non-PII; for auditor only
    )
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import anthropic
import structlog

log = structlog.get_logger()

Category = Literal[
    "none",
    "self-harm",
    "suicidal-ideation",
    "abuse",
    "drug-alcohol",
    "sexualised",
    "hide-from-parents",
    "coded-language",
]

Prominence = Literal["normal", "elevated", "crisis"]


@dataclass(frozen=True)
class SafetyResult:
    flagged_regex: bool
    flagged_classifier: bool
    category: str
    prominence: Prominence
    explanation: str

    @property
    def flagged(self) -> bool:
        return self.flagged_regex or self.flagged_classifier


# Regex tripwires. Each pattern fires on a category. We deliberately err
# toward over-firing: false positives elevate the crisis footer, which is
# benign (the kid sees hotlines, that's all).
_TRIPWIRES: list[tuple[Category, re.Pattern[str]]] = [
    (
        "suicidal-ideation",
        re.compile(
            r"\b(want\s+to\s+(die|kill\s+myself)|kill\s+myself|end\s+(it|my\s+life)|"
            r"don'?t\s+want\s+to\s+(be|live)|suicid(e|al)|kms\b|kys\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "self-harm",
        re.compile(
            r"\b(cut(ting)?\s+myself|hurt(ing)?\s+myself|self[\s-]?harm|"
            r"burn(ing)?\s+myself)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "abuse",
        re.compile(
            r"\b(he\s+hits\s+me|she\s+hits\s+me|they\s+hit\s+me|"
            r"hits\s+me|forces\s+me|made\s+me\s+(do|touch)|"
            r"touch(ed)?\s+me\s+(weird|wrong|inappropriately))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "drug-alcohol",
        re.compile(
            r"\b(getting\s+drunk|getting\s+high|smoke\s+(weed|pot)|do\s+coke|"
            r"taking\s+(pills|xanax|adderall)|overdos(e|ing)|"
            r"how\s+much\s+(alcohol|booze|weed|coke|pills))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sexualised",
        re.compile(
            r"\b(send\s+nudes|send\s+pics|sext(ing)?|hook\s+up|"
            r"want\s+to\s+(fuck|sleep\s+with))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "hide-from-parents",
        re.compile(
            r"\b(don'?t\s+tell\s+(my\s+)?(dad|mum|mom|parents)|"
            r"hide\s+(this|it)\s+from\s+(my\s+)?(dad|mum|mom|parents)|"
            r"keep\s+(this|it)\s+from\s+(my\s+)?(dad|mum|mom|parents))\b",
            re.IGNORECASE,
        ),
    ),
]


def regex_tripwire(text: str) -> tuple[bool, Category]:
    """Returns (fired, category). category is 'none' if not fired."""
    for cat, pattern in _TRIPWIRES:
        if pattern.search(text):
            return True, cat
    return False, "none"


# AU crisis hotlines (per docs/design.md Constraints — hardcoded for v1).
AU_HOTLINES = {
    "kids_helpline": "1800 55 1800",
    "lifeline": "13 11 14",
    "13yarn": "13 92 76",
    "emergency": "000",
}


CRISIS_FOOTER_TEXT = (
    "Need a real person? "
    f"Kids Helpline {AU_HOTLINES['kids_helpline']} · "
    f"Lifeline {AU_HOTLINES['lifeline']} · "
    f"13YARN {AU_HOTLINES['13yarn']} · "
    f"{AU_HOTLINES['emergency']} for emergencies."
)


_CLASSIFIER_PROMPT = """You are a safety classifier for a teenager's chat application. You read a single user message and decide whether it should trigger elevated safety attention.

Categories (pick the SINGLE most fitting):
- self-harm: cutting, burning, hurting self
- suicidal-ideation: wanting to die, ending life, self-deletion
- abuse: someone hurting them physically/sexually/emotionally
- drug-alcohol: substance use, asking how to use, dosage questions
- sexualised: sexual content, requests for nudes/sexting, propositions
- hide-from-parents: asks to hide things from parents/family
- coded-language: euphemisms or coded references for any of the above (e.g. "unalive myself", "going to be the next stat", "feeling like a burden")
- none: nothing concerning

Output ONLY valid JSON, no prose:
{"category": "<one-of-the-above>", "confidence": 0-10, "explanation": "<≤15 words, no PII, no quotes from the message>"}

confidence: 0 = pure benign, 10 = unambiguous crisis content. confidence ≥ 5 means flagged."""


def haiku_classify(
    text: str,
    api_key: str,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[bool, Category, str]:
    """
    Returns (flagged, category, explanation).
    On API failure: fail-open with flagged=False (regex tripwire still
    runs; we don't want network blips to silently elevate every message).
    The auditor will catch what slipped through.
    """
    if not api_key:
        return False, "none", "no-api-key"
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=10.0)
        msg = client.messages.create(
            model=model,
            max_tokens=120,
            system=_CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        out = msg.content[0].text.strip() if msg.content else ""
    except Exception as e:
        log.warning("safety.classifier.api_error", error=str(e))
        return False, "none", "api-error"

    # Parse JSON.
    import json as _json

    try:
        data = _json.loads(out)
        cat = data.get("category", "none")
        confidence = int(data.get("confidence", 0))
        explanation = str(data.get("explanation", ""))[:120]
    except (ValueError, TypeError) as e:
        log.warning("safety.classifier.parse_error", error=str(e), raw=out[:200])
        return False, "none", "parse-error"

    flagged = confidence >= 5 and cat != "none"
    if cat not in (
        "none",
        "self-harm",
        "suicidal-ideation",
        "abuse",
        "drug-alcohol",
        "sexualised",
        "hide-from-parents",
        "coded-language",
    ):
        cat = "coded-language"  # unknown category from model = treat as coded

    return flagged, cat, explanation  # type: ignore[return-value]


def _prominence_for(category: Category) -> Prominence:
    """Map a category to UI prominence for the crisis footer."""
    if category == "none":
        return "normal"
    if category in ("suicidal-ideation", "self-harm", "abuse"):
        return "crisis"
    return "elevated"


def screen_message(
    text: str,
    *,
    api_key: str,
    classifier_model: str,
    enabled: bool,
) -> SafetyResult:
    """
    Run regex tripwire + Haiku classifier. Returns a SafetyResult.

    enabled=False (adult mode): returns a clean result; both screens skipped.
    enabled=True (kid mode): both screens always run.
    """
    if not enabled:
        return SafetyResult(False, False, "none", "normal", "screen-disabled")

    regex_flag, regex_cat = regex_tripwire(text)
    cls_flag, cls_cat, cls_explain = haiku_classify(text, api_key, classifier_model)

    # Prefer the more specific signal: if BOTH fired, the regex category is
    # usually clearer (it matched a specific known pattern) so use that.
    # If only the classifier fired, use its category.
    if regex_flag:
        category: Category = regex_cat
        explanation = "regex-tripwire-fired"
    elif cls_flag:
        category = cls_cat
        explanation = cls_explain
    else:
        category = "none"
        explanation = "clean"

    return SafetyResult(
        flagged_regex=regex_flag,
        flagged_classifier=cls_flag,
        category=category,
        prominence=_prominence_for(category),
        explanation=explanation,
    )
