"""Unit tests for app/safety.py.

Covers:
- Regex tripwire: each category's curated patterns fire on plausible kid input.
- Regex tripwire: clean messages don't fire.
- Haiku classifier wiring: with no API key, fail-open returns clean.
- Mode-disabled (adult mode): screen_message returns clean even on flagged input.
- Prominence mapping: severity-aware UI elevation.

Test plan source: docs/design.md Premise 3 + /plan-eng-review D10 (red-team).
"""

from __future__ import annotations

import pytest

from app import safety


# ---------------------------------------------------------------------------
# Regex tripwire — positive matches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_category",
    [
        # suicidal-ideation
        ("i want to die", "suicidal-ideation"),
        ("I want to kill myself", "suicidal-ideation"),
        ("don't want to live anymore", "suicidal-ideation"),
        ("kms", "suicidal-ideation"),
        # self-harm
        ("i've been cutting myself", "self-harm"),
        ("thinking about hurting myself", "self-harm"),
        # abuse
        ("dad hits me when he's drunk", "abuse"),
        ("she touched me weird", "abuse"),
        # drug-alcohol
        ("how much alcohol does it take to overdose", "drug-alcohol"),
        ("how much weed should i smoke", "drug-alcohol"),
        # sexualised
        ("send nudes", "sexualised"),
        # hide-from-parents
        ("don't tell my dad about this", "hide-from-parents"),
        ("hide this from my parents please", "hide-from-parents"),
    ],
)
def test_regex_tripwire_fires(text: str, expected_category: str) -> None:
    fired, cat = safety.regex_tripwire(text)
    assert fired, f"expected tripwire to fire on: {text!r}"
    assert cat == expected_category, f"got {cat!r} for {text!r}"


# ---------------------------------------------------------------------------
# Regex tripwire — clean messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "what's the weather like",
        "I'm feeling stressed about a maths test",
        "Sofia hasn't replied to my text",
        "I had a fight with mum about screen time",
        "homework is so much",
        "I want to go to the formal but I'm nervous",
        # near-miss but should not fire (no concrete crisis intent)
        "this game is killing me",
        "she'd kill me if she knew I broke the cup",
    ],
)
def test_regex_tripwire_clean(text: str) -> None:
    fired, cat = safety.regex_tripwire(text)
    assert not fired, f"unexpected tripwire on benign: {text!r} (cat={cat})"
    assert cat == "none"


# ---------------------------------------------------------------------------
# screen_message — adult mode (disabled)
# ---------------------------------------------------------------------------


def test_screen_disabled_returns_clean() -> None:
    """In adult mode (enabled=False), even crisis text returns clean."""
    result = safety.screen_message(
        "i want to kill myself",
        api_key="",
        classifier_model="claude-haiku-4-5",
        enabled=False,
    )
    assert not result.flagged
    assert result.category == "none"
    assert result.prominence == "normal"
    assert result.explanation == "screen-disabled"


# ---------------------------------------------------------------------------
# screen_message — kid mode, no API key (fail-open on classifier, regex still fires)
# ---------------------------------------------------------------------------


def test_screen_kid_no_api_key_regex_still_fires() -> None:
    result = safety.screen_message(
        "i want to die",
        api_key="",
        classifier_model="claude-haiku-4-5",
        enabled=True,
    )
    # Regex catches this even without classifier
    assert result.flagged
    assert result.flagged_regex
    assert not result.flagged_classifier
    assert result.category == "suicidal-ideation"
    assert result.prominence == "crisis"


def test_screen_kid_no_api_key_clean_text_clean_result() -> None:
    """Benign text + no API key + kid mode = clean."""
    result = safety.screen_message(
        "I'm stuck on this maths problem",
        api_key="",
        classifier_model="claude-haiku-4-5",
        enabled=True,
    )
    assert not result.flagged
    assert result.category == "none"
    assert result.prominence == "normal"


# ---------------------------------------------------------------------------
# Prominence mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category,expected_prominence",
    [
        ("none", "normal"),
        ("suicidal-ideation", "crisis"),
        ("self-harm", "crisis"),
        ("abuse", "crisis"),
        ("drug-alcohol", "elevated"),
        ("sexualised", "elevated"),
        ("hide-from-parents", "elevated"),
        ("coded-language", "elevated"),
    ],
)
def test_prominence_mapping(category: str, expected_prominence: str) -> None:
    """Category-to-UI-prominence is severity-aware."""
    # Use the helper indirectly via screen_message + a regex that lands
    # on the right category. Cover the mapping by invoking _prominence_for
    # directly (it's an internal helper but stable enough to test).
    assert safety._prominence_for(category) == expected_prominence  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Crisis footer text — should mention all four AU hotlines
# ---------------------------------------------------------------------------


def test_crisis_footer_contains_all_au_hotlines() -> None:
    text = safety.CRISIS_FOOTER_TEXT
    assert "1800 55 1800" in text  # Kids Helpline
    assert "13 11 14" in text  # Lifeline
    assert "13 92 76" in text  # 13YARN
    assert "000" in text  # emergency
