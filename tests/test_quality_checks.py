"""
test_quality_checks.py
-----------------------
Unit tests for quality_checks.py, written against VERBATIM TEXT from real
runs that the old regression suite marked "✅ Everything ran clean".

Every failing case below is copied out of tests/reports/. That's the point:
these are not hypothetical failure modes, they are three runs' worth of
degradation that shipped past a green checkmark. If a change to the prompts
or the model reintroduces any of them, these fail in under a second without
Ollama running.

Run:
    python3 -m pytest tests/test_quality_checks.py -v
    # or, with no pytest installed:
    python3 tests/test_quality_checks.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quality_checks import (  # noqa: E402
    cited_ids,
    bare_ids,
    check_citation_format,
    check_no_scaffolding,
    check_citations_exist,
    check_citations_grounded,
    check_count_claims,
    check_badge_consistency,
    check_fallback_consistency,
    check_blocking_bugs_all_cited,
    check_go_text_is_clean,
    score_search_answer,
)

# A stand-in for the real corpus. Real IDs only.
KNOWN = {
    "BUG-1001", "BUG-1002", "BUG-1003", "BUG-1004", "BUG-1020", "BUG-1023",
    "BUG-1050", "BUG-1070", "BUG-1072", "BUG-1078", "BUG-1081",
    "BUG-2001", "BUG-2002", "BUG-2003", "BUG-2008", "BUG-2009", "BUG-2010",
    "BUG-2013", "BUG-2020", "BUG-2022", "BUG-2023", "BUG-2025", "BUG-2026",
    "BUG-2028", "BUG-2029",
}


# ---- fabricated citations (regression_20260824_123001, v2.6.0 GO) --------

# Verbatim from the report. Every ID in the first paragraph is invented.
REPORT3_GO_TEXT = (
    "Release v2.6.0 has been cleared to ship as all tracked issues have been "
    "addressed at their respective priority levels.\n\n"
    "[BUG-1234] [BUG-5678] [BUG-9012] [BUG-3456] [BUG-7890] [BUG-2345] "
    "[BUG-6789] [BUG-4567] [BUG-8901] [BUG-3210]\n\n"
    "7 bug(s) remain open at lower priority, none blocking: 4 medium-priority "
    "([BUG-2022], [BUG-2025], [BUG-2026], [BUG-2028]); 3 low-priority "
    "([BUG-2020], [BUG-2023], [BUG-2029])."
)


def test_fabricated_ids_in_go_text_are_caught():
    findings = check_citations_exist(REPORT3_GO_TEXT, KNOWN)
    assert len(findings) == 1, findings
    msg = findings[0].message
    for fake in ("BUG-1234", "BUG-5678", "BUG-3210"):
        assert fake in msg, f"{fake} should be reported as nonexistent"
    assert findings[0].severity == "error"


def test_the_placeholder_id_from_the_prompt_is_caught_specifically():
    """[BUG-1234] was the example ID in the system prompt. The model copying
    it verbatim is the single highest-signal failure in the whole system."""
    findings = check_citations_exist("Cleared to ship. [BUG-1234]", KNOWN)
    assert findings and "BUG-1234" in findings[0].message


def test_go_text_may_not_cite_bugs_outside_the_release():
    release_ids = {"BUG-2020", "BUG-2022", "BUG-2023", "BUG-2025",
                   "BUG-2026", "BUG-2028", "BUG-2029"}
    findings = check_go_text_is_clean(REPORT3_GO_TEXT, release_ids)
    assert findings, "stray IDs in a GO write-up must be flagged"


def test_clean_go_text_passes():
    release_ids = {"BUG-2020", "BUG-2022", "BUG-2023"}
    text = ("Release v2.6.0 is cleared to ship.\n\n"
            "3 bug(s) remain open at lower priority, none blocking: "
            "1 medium-priority ([BUG-2022]); 2 low-priority "
            "([BUG-2020], [BUG-2023]).")
    assert not check_go_text_is_clean(text, release_ids)
    assert not check_citations_exist(text, KNOWN)


# ---- dangling count claim (regression_20260824_122042, refund query) -----

REPORT2_REFUND_ANSWER = (
    "Yes, the refund process has had silent failures.\n\n"
    "[BUG-2001] reports that partial refunds over $500 fail silently with a "
    "200 API response but no actual refund creation in the payment processor.\n\n"
    "These two bugs share a root cause of silent failure in the refund process."
)


def test_dangling_two_bugs_reference_is_caught():
    """Says "these two bugs", names one. The other was dropped mid-answer."""
    findings = check_count_claims(REPORT2_REFUND_ANSWER)
    assert len(findings) == 1, findings
    assert findings[0].severity == "error"
    assert "1 distinct" in findings[0].message


def test_same_answer_with_both_bugs_named_passes():
    """The run-1 and run-3 version of the same query, which is correct."""
    good = (
        "Yes, the refund process has had silent failures.\n\n"
        "[BUG-2001] reports that partial refunds over $500 fail silently. "
        "[BUG-1070] also indicates that refunds for Apple Pay orders failed "
        "silently due to an incorrect refund endpoint.\n\n"
        "These two bugs share a root cause."
    )
    assert not check_count_claims(good)


def test_both_claim_with_two_citations_passes():
    text = "[BUG-1023] and [BUG-2009] both relate to slow dashboards."
    assert not check_count_claims(text)


def test_count_claim_without_citations_is_caught():
    assert check_count_claims("Both bugs share the same root cause.")


def test_prose_without_a_count_claim_is_ignored():
    assert not check_count_claims(
        "[BUG-1072] was the cause: the import job held connections open."
    )


# ---- badge / answer contradiction (Bluetooth query, all three runs) ------

FALLBACK_ANSWER = (
    "I found some bugs by semantic similarity, but none of them appear to "
    "substantively address your question after verification. Try rephrasing, "
    "or check the retrieved bugs below to confirm."
)


def _bug(bug_id, is_relevant, action):
    return {
        "bug_id": bug_id,
        "similarity": 0.6,
        "verification": {
            "is_relevant": is_relevant,
            "suggested_action": action,
            "confidence": "medium",
            "reasoning": "…",
        },
    }


def test_relevant_badge_on_irrelevant_bug_is_caught():
    """The visible symptom: "none of them appear relevant" printed directly
    above source badges reading "related"."""
    bugs = [_bug("BUG-2010", False, "related"), _bug("BUG-1040", False, "related")]
    findings = check_badge_consistency(bugs)
    assert len(findings) == 2, findings
    assert all(f.severity == "error" for f in findings)


def test_not_relevant_badge_on_relevant_bug_is_caught():
    findings = check_badge_consistency([_bug("BUG-2015", True, "not_relevant")])
    assert len(findings) == 1


def test_consistent_badges_pass():
    bugs = [_bug("BUG-1001", True, "related"), _bug("BUG-1040", False, "not_relevant")]
    assert not check_badge_consistency(bugs)


def test_fallback_answer_with_relevant_bugs_is_caught():
    bugs = [_bug("BUG-1011", True, "related")]
    assert check_fallback_consistency(FALLBACK_ANSWER, bugs)


def test_fallback_answer_with_no_relevant_bugs_passes():
    bugs = [_bug("BUG-1011", False, "not_relevant")]
    assert not check_fallback_consistency(FALLBACK_ANSWER, bugs)


def test_verification_errors_are_not_counted_as_contradictions():
    """A failed verification call is already surfaced separately; it should
    not also fire the consistency check."""
    bugs = [{
        "bug_id": "BUG-1001",
        "verification": {
            "is_relevant": False, "suggested_action": "not_relevant", "error": True,
        },
    }]
    assert not check_badge_consistency(bugs)


# ---- NO-GO completeness --------------------------------------------------

def test_missing_blocking_bug_is_caught():
    text = "Blocked by [BUG-2001] and [BUG-2002]."
    blocking = ["BUG-2001", "BUG-2002", "BUG-2003", "BUG-2013"]
    findings = check_blocking_bugs_all_cited(text, blocking)
    assert findings and "BUG-2003" in findings[0].message


def test_all_blocking_bugs_cited_passes():
    text = "Blocked by [BUG-2001], [BUG-2002] and [BUG-2003]."
    assert not check_blocking_bugs_all_cited(
        text, ["BUG-2001", "BUG-2002", "BUG-2003"]
    )


# ---- grounding -----------------------------------------------------------

def test_real_id_that_was_not_retrieved_is_caught():
    """Answering from parametric memory rather than the retrieved context."""
    findings = check_citations_grounded(
        "The cause was [BUG-1078].", {"BUG-1020", "BUG-1072"}
    )
    assert findings and "BUG-1078" in findings[0].message


# ---- helpers -------------------------------------------------------------

def test_cited_ids_dedupes_and_preserves_order():
    assert cited_ids("[BUG-2] then [BUG-1] then [BUG-2]") == ["BUG-2", "BUG-1"]


def test_cited_ids_handles_other_project_prefixes():
    assert cited_ids("see [MICH-4213]") == ["MICH-4213"]


def test_cited_ids_on_empty_text():
    assert cited_ids("") == [] and cited_ids(None) == []


# ---- end-to-end aggregate ------------------------------------------------

def test_score_search_answer_flags_a_known_bad_run():
    bugs = [_bug("BUG-2001", True, "related"), _bug("BUG-1070", True, "related")]
    result = score_search_answer(REPORT2_REFUND_ANSWER, bugs, KNOWN, elapsed_s=25.4)
    assert not result.passed, "the run-2 refund answer must not pass"
    assert any(f.check == "count_claims" for f in result.errors)


def test_score_search_answer_passes_a_known_good_run():
    answer = (
        "We fixed the memory leak by wrapping PDF processing in a try/finally "
        "block with explicit doc.unload() calls [BUG-1020]."
    )
    bugs = [_bug("BUG-1020", True, "related"), _bug("BUG-1078", False, "not_relevant")]
    result = score_search_answer(answer, bugs, KNOWN, elapsed_s=22.8)
    assert result.passed, [str(f) for f in result.findings]


# ---- unbracketed citations (regression_20260824_131604, refund query) ----

# Verbatim. Factually correct — both bugs found, both claims right — but the
# model wrote the IDs bare, so every citation check scored it as citing
# nothing. Caused by removing the "[BUG-1234]" example from the prompt: the
# instruction survived, the demonstration of the format did not.
REPORT4_REFUND_ANSWER = """Yes, based on the provided bugs:

* BUG-2001 reports that partial refunds over $500 fail silently because the \
Stripe webhook for refund.created never fires.
* BUG-1070 also involves a silent failure in the refund process, specifically \
for orders paid via Apple Pay.

These two bugs indicate that there have been silent failures in our refund \
process."""


def test_bare_ids_are_detected():
    assert bare_ids(REPORT4_REFUND_ANSWER) == ["BUG-2001", "BUG-1070"]


def test_bracketed_ids_are_not_counted_as_bare():
    assert bare_ids("see [BUG-1002] and [BUG-1070]") == []


def test_unbracketed_grounded_ids_are_flagged():
    findings = check_citation_format(
        REPORT4_REFUND_ANSWER, {"BUG-2001", "BUG-1070"}
    )
    assert len(findings) == 1, findings
    assert "BUG-2001" in findings[0].message


def test_properly_bracketed_answer_passes_format_check():
    text = "Yes. [BUG-2001] and [BUG-1070] both fail silently."
    assert not check_citation_format(text, {"BUG-2001", "BUG-1070"})


def test_fabricated_bare_id_is_caught():
    """A hallucinated ID without brackets is the same failure in disguise."""
    findings = check_citations_exist("The cause was BUG-9999.", KNOWN)
    assert findings and "BUG-9999" in findings[0].message


# ---- scaffolding leak (regression_20260824_131604, v2.6.0 GO) -----------

# Verbatim. This PASSED every check in the previous version of the scorer:
# not an invented ID, not an English instruction-echo. It would have rendered
# as visible template text under a green GO verdict.
REPORT4_GO_TEXT = """Release v2.6.0 has been cleared to ship as all critical \
issues have been addressed.

// Breakdown:
// - 10 tracked bugs
// - None at High or Blocker priority
// - Remaining lower-priority bugs: [list of bug descriptions]

7 bug(s) remain open at lower priority, none blocking: 4 medium-priority \
([BUG-2022], [BUG-2025], [BUG-2026], [BUG-2028]); 3 low-priority \
([BUG-2020], [BUG-2023], [BUG-2029])."""


def test_comment_scaffolding_is_caught():
    findings = check_no_scaffolding(REPORT4_GO_TEXT)
    assert any("comment-style" in f.message for f in findings), findings
    assert all(f.severity == "error" for f in findings)


def test_unfilled_placeholder_is_caught():
    findings = check_no_scaffolding("Cleared to ship. [list of bug descriptions]")
    assert findings and "placeholder" in findings[0].message


def test_real_citations_are_not_mistaken_for_placeholders():
    text = ("Cleared to ship.\n\n3 bug(s) remain: [BUG-2022], [BUG-2025], "
            "[BUG-2020].")
    assert not check_no_scaffolding(text)


def test_clean_go_text_has_no_scaffolding():
    assert not check_no_scaffolding(
        "Release v2.6.0 is cleared to ship. 2 bug(s) remain: [BUG-2020]."
    )


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
