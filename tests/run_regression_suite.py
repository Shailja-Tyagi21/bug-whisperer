"""
run_regression_suite.py
------------------------
Runs the full demo question set + both release-gate checks against the
LIVE stack (real Ollama, real ChromaDB collection), SCORES the generated
answers, writes a timestamped markdown report, and diffs the result
against the previous run.

The scoring is the point. An earlier version of this suite checked only
whether ask() threw, whether a verification call errored, and whether the
release gate returned the expected string. All three passed on a run whose
GO recommendation cited ten bug IDs that do not exist. The answer text was
written to the report and never checked against anything, so quality could
degrade indefinitely under a green checkmark. quality_checks.py now
asserts on the content; this file runs those assertions live.

Requires: ollama serve running, models pulled, ingest.py already run.

Run:
    python3 tests/run_regression_suite.py

Output:
    tests/reports/regression_<timestamp>.md    human-readable report
    tests/reports/latest.json                  machine-readable baseline
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search import ask, check_release_readiness, list_all_bug_ids  # noqa: E402
from quality_checks import (  # noqa: E402
    score_search_answer,
    score_release_recommendation,
)

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
BASELINE_PATH = REPORTS_DIR / "latest.json"

# ---- The question set -----------------------------------------------------
# Same 10 questions used for manual demo testing, kept here so a code change
# gets checked against the exact same ground truth every time.
SEARCH_QUERIES = [
    "PayPal redirect issue after checkout",
    "checkout broken for European customers",
    "has our refund process had any silent failures?",
    "how did we fix the memory leak in the worker service?",
    "what caused the database connection pool issue?",
    "any performance issues with dashboards or reports loading slowly?",
    "what MFA or login delivery issues have we seen?",
    "Are there known issues with the tyre search popup?",
    "do we have any Bluetooth pairing bugs?",
    "has billing ever charged customers twice?",
]

# (release_version, expected_decision) -- expected_decision is what the
# data was DESIGNED to produce; the report flags a mismatch loudly.
RELEASE_CHECKS = [
    ("v2.5.0", "NO-GO"),
    ("v2.6.0", "GO"),
]


def run_search_query(query: str, known_ids: set) -> dict:
    start = time.time()
    try:
        answer, bugs = ask(query, k=5)
        elapsed = round(time.time() - start, 1)
        badges = [
            f"[{b['bug_id']}] {b.get('verification', {}).get('suggested_action', '?')}"
            f"{' (ERROR)' if b.get('verification', {}).get('error') else ''}"
            for b in bugs
        ]
        error_count = sum(1 for b in bugs if b.get("verification", {}).get("error"))
        quality = score_search_answer(answer, bugs, known_ids, elapsed_s=elapsed)
        return {
            "kind": "search",
            "name": query,
            "ok": True,
            "elapsed_s": elapsed,
            "answer": answer,
            "badges": badges,
            "error_count": error_count,
            "quality_errors": [str(f) for f in quality.errors],
            "quality_warnings": [str(f) for f in quality.warnings],
            "passed": quality.passed,
        }
    except Exception as e:
        return {
            "kind": "search",
            "name": query,
            "ok": False,
            "elapsed_s": round(time.time() - start, 1),
            "error": f"{type(e).__name__}: {e}",
            "quality_errors": [],
            "quality_warnings": [],
            "passed": False,
        }


def run_release_check(version: str, expected: str, known_ids: set) -> dict:
    start = time.time()
    try:
        result = check_release_readiness(version)
        elapsed = round(time.time() - start, 1)
        blocking_ids = [b["bug_id"] for b in result["blocking_bugs"]]
        release_ids = {b["bug_id"] for b in result["all_bugs"]}
        quality = score_release_recommendation(
            result["recommendation"], result["decision"],
            blocking_ids, release_ids, known_ids, elapsed_s=elapsed,
        )
        matches = result["decision"] == expected
        return {
            "kind": "release",
            "name": version,
            "ok": True,
            "elapsed_s": elapsed,
            "decision": result["decision"],
            "expected": expected,
            "matches_expected": matches,
            "blocking_ids": blocking_ids,
            "recommendation": result["recommendation"],
            "quality_errors": [str(f) for f in quality.errors],
            "quality_warnings": [str(f) for f in quality.warnings],
            "passed": quality.passed and matches,
        }
    except Exception as e:
        return {
            "kind": "release",
            "name": version,
            "ok": False,
            "elapsed_s": round(time.time() - start, 1),
            "error": f"{type(e).__name__}: {e}",
            "quality_errors": [],
            "quality_warnings": [],
            "passed": False,
        }


def load_baseline() -> dict:
    """The previous run's pass/fail per check, so this run can report what
    CHANGED rather than only what's currently broken. Answering "is it worse
    than last time?" is the whole reason this suite exists."""
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text())
    except Exception:
        return {}


def diff_against_baseline(results: list, baseline: dict) -> tuple:
    """Returns (newly_broken, newly_fixed) check names."""
    prev = baseline.get("results", {})
    if not prev:
        return [], []
    newly_broken = [
        r["name"] for r in results
        if not r["passed"] and prev.get(r["name"], {}).get("passed") is True
    ]
    newly_fixed = [
        r["name"] for r in results
        if r["passed"] and prev.get(r["name"], {}).get("passed") is False
    ]
    return newly_broken, newly_fixed


def main():
    print("Loading corpus bug IDs...", end=" ", flush=True)
    known_ids = list_all_bug_ids()
    print(f"{len(known_ids)} bugs in collection.\n")

    print(f"Running {len(SEARCH_QUERIES)} search queries + "
          f"{len(RELEASE_CHECKS)} release checks against the live stack...\n")

    results = []

    for q in SEARCH_QUERIES:
        print(f"  Searching: {q!r}...", end=" ", flush=True)
        r = run_search_query(q, known_ids)
        results.append(r)
        if not r["ok"]:
            print(f"CRASHED ({r['elapsed_s']}s)")
        else:
            status = "OK" if r["passed"] else "QUALITY FAIL"
            notes = []
            if r.get("error_count"):
                notes.append(f"{r['error_count']} verification error(s)")
            if r["quality_errors"]:
                notes.append(f"{len(r['quality_errors'])} quality error(s)")
            if r["quality_warnings"]:
                notes.append(f"{len(r['quality_warnings'])} warning(s)")
            suffix = f", {', '.join(notes)}" if notes else ""
            print(f"{status} ({r['elapsed_s']}s{suffix})")
            for f in r["quality_errors"] + r["quality_warnings"]:
                print(f"        {f}")

    for version, expected in RELEASE_CHECKS:
        print(f"  Checking release {version}...", end=" ", flush=True)
        r = run_release_check(version, expected, known_ids)
        results.append(r)
        if not r["ok"]:
            print(f"CRASHED ({r['elapsed_s']}s)")
        else:
            if not r["matches_expected"]:
                status = "GATE MISMATCH"
            elif not r["passed"]:
                status = "QUALITY FAIL"
            else:
                status = "OK"
            print(f"{status} (got {r['decision']}, expected {r['expected']}, "
                  f"{r['elapsed_s']}s)")
            for f in r["quality_errors"] + r["quality_warnings"]:
                print(f"        {f}")

    # ---- tallies ----
    crashes = sum(1 for r in results if not r["ok"])
    verification_errors = sum(r.get("error_count", 0) for r in results)
    gate_mismatches = sum(
        1 for r in results
        if r["kind"] == "release" and r["ok"] and not r["matches_expected"]
    )
    quality_errors = sum(len(r["quality_errors"]) for r in results)
    quality_warnings = sum(len(r["quality_warnings"]) for r in results)
    failing = [r for r in results if not r["passed"]]

    baseline = load_baseline()
    newly_broken, newly_fixed = diff_against_baseline(results, baseline)

    # ---- write markdown report ----
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"regression_{timestamp}.md"

    lines = [f"# Regression report — {timestamp}\n"]
    lines.append(
        f"**Summary:** {len(results)} checks — "
        f"{len(results) - len(failing)} passed, {len(failing)} failed. "
        f"{crashes} crash(es), {verification_errors} verification-call error(s), "
        f"{gate_mismatches} release-gate mismatch(es), "
        f"{quality_errors} quality error(s), {quality_warnings} warning(s).\n"
    )
    if failing:
        lines.append("❌ **Failures — see below.**\n")
    else:
        lines.append("✅ All checks passed, including answer-quality assertions.\n")

    if baseline:
        prev_ts = baseline.get("timestamp", "?")
        lines.append(f"\n### Compared to previous run ({prev_ts})\n")
        if newly_broken:
            lines.append("**🔻 Newly broken:** " + ", ".join(newly_broken) + "\n")
        if newly_fixed:
            lines.append("**🔼 Newly fixed:** " + ", ".join(newly_fixed) + "\n")
        if not newly_broken and not newly_fixed:
            lines.append("No change in pass/fail status.\n")
    else:
        lines.append("\n_No previous run to compare against — "
                     "this run becomes the baseline._\n")

    lines.append("\n## Search queries\n")
    for r in [r for r in results if r["kind"] == "search"]:
        lines.append(f"### {r['name']}")
        if not r["ok"]:
            lines.append(f"**CRASHED:** `{r['error']}`\n")
            continue
        lines.append(f"*{r['elapsed_s']}s — {'PASS' if r['passed'] else 'FAIL'}*\n")
        for f in r["quality_errors"] + r["quality_warnings"]:
            lines.append(f"{f}\n")
        lines.append(f"> {r['answer']}\n")
        lines.append("Sources: " + ", ".join(r["badges"]) + "\n")

    lines.append("\n## Release readiness\n")
    for r in [r for r in results if r["kind"] == "release"]:
        lines.append(f"### {r['name']}")
        if not r["ok"]:
            lines.append(f"**CRASHED:** `{r['error']}`\n")
            continue
        marker = "✅" if r["matches_expected"] else "❌ GATE MISMATCH"
        lines.append(f"{marker} — got **{r['decision']}**, expected "
                     f"**{r['expected']}** ({r['elapsed_s']}s)")
        if r["blocking_ids"]:
            lines.append(f"Blocking: {', '.join(r['blocking_ids'])}")
        for f in r["quality_errors"] + r["quality_warnings"]:
            lines.append(f"\n{f}")
        lines.append(f"\n> {r['recommendation']}\n")

    report_path.write_text("\n".join(lines))

    # ---- write machine-readable baseline for the next run ----
    BASELINE_PATH.write_text(json.dumps({
        "timestamp": timestamp,
        "results": {
            r["name"]: {
                "passed": r["passed"],
                "quality_errors": r["quality_errors"],
                "elapsed_s": r["elapsed_s"],
            }
            for r in results
        },
    }, indent=2))

    print(f"\nReport written to {report_path}")
    print(f"Baseline updated at {BASELINE_PATH}")

    if newly_broken:
        print(f"\n*** {len(newly_broken)} check(s) REGRESSED since the last run: "
              f"{', '.join(newly_broken)} ***")
    if newly_fixed:
        print(f"*** {len(newly_fixed)} check(s) fixed since the last run: "
              f"{', '.join(newly_fixed)} ***")

    if failing:
        print(f"\n*** {len(failing)} of {len(results)} checks FAILED. ***")
        sys.exit(1)
    print(f"\nAll {len(results)} checks passed.")


if __name__ == "__main__":
    main()
