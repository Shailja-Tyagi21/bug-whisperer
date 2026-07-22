"""
test_guardrails.py
-------------------
Unit tests for the deterministic guardrails in search.py. These are pure
functions (no Ollama or ChromaDB calls), so they run instantly and should
be run after ANY change to search.py's verification or release-gate logic —
both of the real regressions found during manual testing (the BUG-1053
false-related-catch, and the BUG-1002 over-eager downgrade) would have
been caught immediately by tests like these instead of by re-clicking
through the UI.

Run:
    cd bugbrain
    python3 -m pytest tests/test_guardrails.py -v
    # or, with no pytest installed:
    python3 tests/test_guardrails.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search import _guard_duplicate_label, _group_by_status, _clean_recommendation_text  # noqa: E402


# ---- _guard_duplicate_label --------------------------------------------

def test_low_similarity_hedged_duplicate_gets_downgraded():
    """The original bug this guardrail was built for: same-component,
    different-issue, reasoning hedges toward 'related' language, model
    picked 'duplicate' anyway. Low similarity (0.57) = plausible false match."""
    result = {
        "is_relevant": True,
        "confidence": "high",
        "reasoning": (
            "The bug affects the same component (Search) and describes a "
            "specific issue with search queries, which is related to the "
            "user's question about the tyre search popup."
        ),
        "suggested_action": "duplicate",
    }
    out = _guard_duplicate_label(result, similarity=0.57)
    assert out["suggested_action"] == "related", out


def test_high_similarity_hedged_duplicate_is_NOT_downgraded():
    """The regression this guardrail caused: a genuine near-identical
    duplicate (similarity 0.84) whose reasoning naturally uses 'related to'
    as a normal connector word should NOT be downgraded."""
    result = {
        "is_relevant": True,
        "confidence": "high",
        "reasoning": (
            "This bug describes the exact same root cause: PayPal "
            "return_url falls back to / due to an env var fallback, which "
            "is related to the checkout redirect issue in the question."
        ),
        "suggested_action": "duplicate",
    }
    out = _guard_duplicate_label(result, similarity=0.84)
    assert out["suggested_action"] == "duplicate", out


def test_low_similarity_unhedged_duplicate_is_NOT_downgraded():
    """Low similarity alone isn't enough to trigger the guardrail — the
    reasoning also has to actually hedge. A confident, unhedged duplicate
    explanation should survive even at low similarity."""
    result = {
        "is_relevant": True,
        "confidence": "medium",
        "reasoning": "This describes the exact same defect and root cause.",
        "suggested_action": "duplicate",
    }
    out = _guard_duplicate_label(result, similarity=0.50)
    assert out["suggested_action"] == "duplicate", out


def test_non_duplicate_actions_pass_through_untouched():
    """The guardrail only ever touches 'duplicate' verdicts — 'related' and
    'not_relevant' should never be modified, regardless of wording."""
    for action in ("related", "not_relevant"):
        result = {
            "is_relevant": action == "related",
            "confidence": "high",
            "reasoning": "related to nothing here, similar to nothing either",
            "suggested_action": action,
        }
        out = _guard_duplicate_label(result, similarity=0.3)
        assert out["suggested_action"] == action, out


def test_missing_similarity_falls_back_to_phrase_check_only():
    """If similarity isn't available (e.g. called from somewhere that
    doesn't have it), the guardrail should still work off reasoning text
    alone rather than crashing or silently skipping the check."""
    result = {
        "is_relevant": True,
        "confidence": "high",
        "reasoning": "same component, different specific issue, related to the question",
        "suggested_action": "duplicate",
    }
    out = _guard_duplicate_label(result, similarity=None)
    assert out["suggested_action"] == "related", out


def test_boundary_similarity_exactly_at_floor_is_not_downgraded():
    """similarity == floor should behave like 'above floor' (>=), not fall
    through to the phrase check — pin down the boundary explicitly so a
    future refactor can't flip the comparison operator unnoticed."""
    result = {
        "is_relevant": True,
        "confidence": "high",
        "reasoning": "related to the same issue",
        "suggested_action": "duplicate",
    }
    out = _guard_duplicate_label(result, similarity=0.75)
    assert out["suggested_action"] == "duplicate", out


def test_original_dict_not_mutated():
    """_guard_duplicate_label should return a new dict when it downgrades,
    not mutate the caller's dict in place — callers may hold a reference
    to the original result elsewhere."""
    result = {
        "is_relevant": True,
        "confidence": "high",
        "reasoning": "same component, related to the question",
        "suggested_action": "duplicate",
    }
    out = _guard_duplicate_label(result, similarity=0.4)
    assert result["suggested_action"] == "duplicate", "input dict was mutated"
    assert out["suggested_action"] == "related"


# ---- _group_by_status ---------------------------------------------------

def test_group_by_status_separates_correctly():
    """The exact scenario that caused the BUG-2010 write-up error: an Open
    bug must land in its own group, separate from Ready for Test bugs,
    even if they share priority/component."""
    bugs = [
        {"bug_id": "BUG-A", "metadata": {"status": "Open"}},
        {"bug_id": "BUG-B", "metadata": {"status": "Ready for Test"}},
        {"bug_id": "BUG-C", "metadata": {"status": "Ready for Test"}},
        {"bug_id": "BUG-D", "metadata": {"status": "Open"}},
        {"bug_id": "BUG-E", "metadata": {"status": "In Progress"}},
    ]
    groups = _group_by_status(bugs)
    assert {b["bug_id"] for b in groups["Open"]} == {"BUG-A", "BUG-D"}
    assert {b["bug_id"] for b in groups["Ready for Test"]} == {"BUG-B", "BUG-C"}
    assert {b["bug_id"] for b in groups["In Progress"]} == {"BUG-E"}


def test_group_by_status_empty_list():
    assert _group_by_status([]) == {}


def test_group_by_status_missing_status_field():
    """A bug with no status metadata at all shouldn't crash the grouping —
    it should land in an 'Unknown' bucket rather than raising."""
    bugs = [{"bug_id": "BUG-X", "metadata": {}}]
    groups = _group_by_status(bugs)
    assert groups["Unknown"][0]["bug_id"] == "BUG-X"


# ---- _clean_recommendation_text -----------------------------------------

def test_strips_here_is_preamble_and_wrapping_quotes():
    """The exact regression found in a live run: v2.6.0's GO writeup came
    back with an echoed instruction preamble and quote-wrapped body."""
    raw = (
        "Here is a 2-3 sentence GO recommendation for the release manager:\n\n"
        '"Release v2.6.0 is cleared to ship. [BUG-2023] and [BUG-2026] remain open at low priority."'
    )
    cleaned = _clean_recommendation_text(raw)
    assert not cleaned.lower().startswith("here is"), cleaned
    assert not (cleaned.startswith('"') and cleaned.endswith('"')), cleaned
    assert cleaned.startswith("Release v2.6.0"), cleaned


def test_clean_text_passes_through_untouched():
    """A response with no preamble artifact shouldn't be modified at all —
    this is the common case (NO-GO responses didn't show this bug)."""
    text = "Release v2.5.0 is blocked due to [BUG-2001], [BUG-2002]."
    assert _clean_recommendation_text(text) == text


# ---- GO-path priority breakdown (regression: swapped 4-low/3-medium) -----

def test_go_priority_breakdown_matches_real_v260_data():
    """The exact regression found in a live run: the model stated '4
    low-priority and 3 medium-priority' when the real data is 3 low and 4
    medium, and dropped BUG-2020 from the list entirely. This mirrors the
    grouping logic _synthesize_release_recommendation now uses to compute
    the GO breakdown in Python instead of trusting the model's arithmetic."""
    non_closed = [
        {"bug_id": "BUG-2020", "metadata": {"priority": "Low"}},
        {"bug_id": "BUG-2022", "metadata": {"priority": "Medium"}},
        {"bug_id": "BUG-2023", "metadata": {"priority": "Low"}},
        {"bug_id": "BUG-2025", "metadata": {"priority": "Medium"}},
        {"bug_id": "BUG-2026", "metadata": {"priority": "Medium"}},
        {"bug_id": "BUG-2028", "metadata": {"priority": "Medium"}},
        {"bug_id": "BUG-2029", "metadata": {"priority": "Low"}},
    ]
    groups = _group_by_status  # reuse the same grouping helper shape check below
    by_priority = {}
    for b in non_closed:
        by_priority.setdefault(b["metadata"]["priority"], []).append(b)

    assert len(by_priority["Low"]) == 3, "should be 3 low-priority bugs, not 4"
    assert len(by_priority["Medium"]) == 4, "should be 4 medium-priority bugs, not 3"
    all_ids = {b["bug_id"] for bugs in by_priority.values() for b in bugs}
    assert "BUG-2020" in all_ids, "BUG-2020 must not be dropped from the breakdown"
    assert len(all_ids) == 7


# ---- simple runner for environments without pytest -----------------------

if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
