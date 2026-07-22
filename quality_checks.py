"""
quality_checks.py
------------------
Assertions about the CONTENT of generated answers.

Why this exists: the regression suite used to check only three things --
did ask() throw, did a verification call error, did the release gate return
the expected GO/NO-GO string. All three can pass while the answer itself
degrades badly. A real run produced a GO recommendation citing ten bug IDs
that do not exist in the corpus, and the report marked it as clean. Nothing
was scoring the text.

Everything here is a pure function over (answer text, retrieved bugs,
known bug IDs). No Ollama, no ChromaDB -- so these are unit-testable
against transcripts of past bad runs, and the same checks run live inside
run_regression_suite.py.

Each check returns a list of Finding objects. Severity "error" fails the
suite (exit 1); "warning" is reported but does not fail.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

# Matches a cited ticket reference like [BUG-1002]. Generic on the project
# prefix so this still works pointed at a real Jira project (e.g. [MICH-421]).
CITATION_RE = re.compile(r"\[([A-Z][A-Z0-9]*-\d+)\]")

# The same ID shape WITHOUT brackets. Bare IDs matter for two reasons: a
# correct answer that forgets the brackets is invisible to every citation
# check (a live run cited BUG-2001 and BUG-1070 bare and scored as citing
# nothing), and a FABRICATED bare ID would slip past check_citations_exist
# entirely. search.py brackets grounded bare IDs before the answer is
# returned, so anything still bare by the time it reaches here is either a
# formatting failure or an ID that was never in the corpus.
BARE_ID_RE = re.compile(r"(?<!\[)\b([A-Z][A-Z0-9]*-\d+)\b(?!\])")

# Scaffolding the model sometimes emits instead of prose: code comments, or
# unfilled placeholders like "[list of bug descriptions]". Seen live in a GO
# recommendation, where it rendered as visible template text under the
# verdict. Not an invented ID and not an English instruction-echo, so
# nothing else catches it.
_SCAFFOLD_LINE_RE = re.compile(r"^\s*(//|/\*|\*/)", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(
    r"\[(?![A-Z][A-Z0-9]*-\d+\])[^\]]*\b"
    r"(list|insert|your|todo|tbd|placeholder|descriptions?|etc)\b[^\]]*\]",
    re.IGNORECASE,
)

# "these two bugs", "both issues", "all three tickets" -- a claim about HOW
# MANY things the answer is talking about. Cheap to verify against the
# number of distinct IDs actually cited, and a mismatch is a reliable sign
# the model dropped a bug mid-answer.
_COUNT_WORDS = {"both": 2, "two": 2, "three": 3, "four": 4, "five": 5}
_COUNT_CLAIM_RE = re.compile(
    r"\b(both|two|three|four|five)\b(?:\s+\w+){0,2}?\s+"
    r"(bugs?|issues?|tickets?|defects?)\b",
    re.IGNORECASE,
)

# The exact string synthesize() returns when verification rejects everything.
NO_RELEVANT_RESULTS_MARKER = "none of them appear"


@dataclass
class Finding:
    check: str
    severity: str  # "error" | "warning"
    message: str

    def __str__(self) -> str:
        icon = "❌" if self.severity == "error" else "⚠️"
        return f"{icon} [{self.check}] {self.message}"


@dataclass
class CheckResult:
    findings: List[Finding] = field(default_factory=list)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def extend(self, findings: Iterable[Finding]) -> None:
        self.findings.extend(findings)


def cited_ids(text: str) -> List[str]:
    """Distinct bracketed ticket IDs in a block of generated text, in order."""
    seen, out = set(), []
    for bug_id in CITATION_RE.findall(text or ""):
        if bug_id not in seen:
            seen.add(bug_id)
            out.append(bug_id)
    return out


def bare_ids(text: str) -> List[str]:
    """Distinct ticket IDs mentioned WITHOUT brackets."""
    seen, out = set(), []
    for bug_id in BARE_ID_RE.findall(text or ""):
        if bug_id not in seen:
            seen.add(bug_id)
            out.append(bug_id)
    return out


# ---- individual checks ----------------------------------------------------

def check_citations_exist(text: str, known_ids: Set[str]) -> List[Finding]:
    """No fabricated ticket IDs.

    The regression this was written for: a GO recommendation that opened with
    [BUG-1234] -- the placeholder ID from the prompt's own instruction -- and
    then invented nine more in the same shape. None existed in the corpus.
    """
    if not known_ids:
        return []
    findings = []
    bogus = [b for b in cited_ids(text) if b not in known_ids]
    if bogus:
        findings.append(Finding(
            "citations_exist", "error",
            f"cited {len(bogus)} ID(s) that do not exist in the corpus: "
            f"{', '.join(bogus)}",
        ))
    # A fabricated ID written without brackets is the same failure wearing a
    # different hat -- it reads as a real reference to anyone on the page.
    bogus_bare = [b for b in bare_ids(text) if b not in known_ids]
    if bogus_bare:
        findings.append(Finding(
            "citations_exist", "error",
            f"mentioned {len(bogus_bare)} unbracketed ID(s) that do not exist "
            f"in the corpus: {', '.join(bogus_bare)}",
        ))
    return findings


def check_citation_format(text: str, grounded_ids: Set[str]) -> List[Finding]:
    """Real bug IDs must be bracketed by the time the answer is rendered.

    The regression: an answer that correctly identified both refund bugs but
    wrote them bare, so every citation check scored it as citing nothing.
    search.py::normalize_citations brackets these in Python, so this firing
    means that normalization did not run or did not cover the ID.
    """
    unbracketed = [b for b in bare_ids(text) if b in grounded_ids]
    if not unbracketed:
        return []
    return [Finding(
        "citation_format", "error",
        f"grounded ID(s) mentioned without brackets: {', '.join(unbracketed)}",
    )]


def check_no_scaffolding(text: str) -> List[Finding]:
    """No code comments or unfilled placeholders in user-facing output."""
    findings = []
    if _SCAFFOLD_LINE_RE.search(text or ""):
        findings.append(Finding(
            "scaffolding", "error",
            "output contains comment-style scaffolding lines (// or /* */)",
        ))
    placeholders = _PLACEHOLDER_RE.findall(text or "")
    if placeholders:
        findings.append(Finding(
            "scaffolding", "error",
            f"output contains {len(placeholders)} unfilled placeholder(s) "
            f"like '[list of ...]'",
        ))
    return findings


def check_citations_grounded(text: str, grounded_ids: Set[str]) -> List[Finding]:
    """No citing a real bug that wasn't in the retrieved context.

    Weaker than a fabrication but the same class of problem: the model is
    answering from parametric memory rather than from what it was handed,
    which means the citation is not evidence of anything.
    """
    if not grounded_ids:
        return []
    ungrounded = [b for b in cited_ids(text) if b not in grounded_ids]
    if not ungrounded:
        return []
    return [Finding(
        "citations_grounded", "error",
        f"cited ID(s) not present in the retrieved context: "
        f"{', '.join(ungrounded)}",
    )]


def check_count_claims(text: str) -> List[Finding]:
    """An answer that says "these two bugs" must actually cite two bugs.

    The regression: "[BUG-2001] reports that partial refunds fail silently.
    These two bugs share a root cause." -- only one bug was ever named. The
    second one got dropped between sentences, leaving a dangling reference
    that reads as confident and is simply incoherent.
    """
    n_cited = len(cited_ids(text))
    findings = []
    for match in _COUNT_CLAIM_RE.finditer(text or ""):
        claimed = _COUNT_WORDS[match.group(1).lower()]
        if n_cited < claimed:
            findings.append(Finding(
                "count_claims", "error",
                f"answer says {match.group(0)!r} but cites only "
                f"{n_cited} distinct ID(s)",
            ))
    return findings


def check_answer_cites_something(text: str, grounded_ids: Set[str]) -> List[Finding]:
    """A substantive answer should point at a ticket.

    Skipped when the answer is one of the honest "I found nothing" responses --
    those are correct behaviour, not a failure to cite.
    """
    if not grounded_ids:
        return []
    lowered = (text or "").lower()
    if NO_RELEVANT_RESULTS_MARKER in lowered or "couldn't find" in lowered:
        return []
    if "no known issues" in lowered or "there is no mention" in lowered:
        return []
    if cited_ids(text):
        return []
    return [Finding(
        "answer_cites_something", "warning",
        "answer is substantive but cites no bug IDs at all",
    )]


def check_badge_consistency(bugs: List[Dict]) -> List[Finding]:
    """is_relevant and suggested_action must not contradict each other.

    The visible symptom: an answer reading "none of them appear to
    substantively address your question" printed directly above source
    badges reading "related". synthesize() filters on is_relevant; the UI
    badges render suggested_action; the model sets them independently.
    """
    findings = []
    for b in bugs:
        v = b.get("verification") or {}
        if v.get("error"):
            continue
        is_relevant = v.get("is_relevant")
        action = v.get("suggested_action")
        if is_relevant is False and action in ("duplicate", "related"):
            findings.append(Finding(
                "badge_consistency", "error",
                f"{b.get('bug_id')}: is_relevant=False but badge is {action!r}",
            ))
        elif is_relevant is True and action == "not_relevant":
            findings.append(Finding(
                "badge_consistency", "error",
                f"{b.get('bug_id')}: is_relevant=True but badge is 'not_relevant'",
            ))
    return findings


def check_fallback_consistency(text: str, bugs: List[Dict]) -> List[Finding]:
    """If the answer is the "nothing was relevant" fallback, no bug may be
    badged as relevant -- and vice versa."""
    is_fallback = NO_RELEVANT_RESULTS_MARKER in (text or "").lower()
    relevant = [
        b.get("bug_id") for b in bugs
        if (b.get("verification") or {}).get("is_relevant") is True
    ]
    if is_fallback and relevant:
        return [Finding(
            "fallback_consistency", "error",
            f"answer says nothing was relevant, but {len(relevant)} bug(s) "
            f"are marked relevant: {', '.join(str(r) for r in relevant)}",
        )]
    return []


def check_no_prompt_leakage(text: str) -> List[Finding]:
    """The model echoing its own instructions back into the answer."""
    lowered = (text or "").lower()
    tells = [
        "here is a 2-3 sentence",
        "here is a recommendation",
        "for the release manager:",
        "as an ai",
        "based on the instructions",
    ]
    hits = [t for t in tells if t in lowered]
    if not hits:
        return []
    return [Finding(
        "prompt_leakage", "warning",
        f"answer contains instruction-echo phrasing: {hits}",
    )]


def check_blocking_bugs_all_cited(text: str, blocking_ids: List[str]) -> List[Finding]:
    """Every blocking bug must appear in a NO-GO write-up.

    search.py appends anything the model dropped, so this failing means the
    append-missing safety net itself broke.
    """
    missing = [b for b in blocking_ids if b not in (text or "")]
    if not missing:
        return []
    return [Finding(
        "blocking_bugs_cited", "error",
        f"NO-GO text omits {len(missing)} blocking bug(s): {', '.join(missing)}",
    )]


def check_go_text_is_clean(text: str, release_ids: Set[str]) -> List[Finding]:
    """A GO write-up may only reference bugs from that release."""
    stray = [b for b in cited_ids(text) if b not in release_ids]
    if not stray:
        return []
    return [Finding(
        "go_text_clean", "error",
        f"GO text cites ID(s) not tagged to this release: {', '.join(stray)}",
    )]


def check_latency(elapsed_s: float, budget_s: float = 45.0) -> List[Finding]:
    if elapsed_s <= budget_s:
        return []
    return [Finding(
        "latency", "warning",
        f"took {elapsed_s}s (budget {budget_s}s) — too slow to demo live",
    )]


# ---- aggregates -----------------------------------------------------------

def score_search_answer(
    answer: str,
    bugs: List[Dict],
    known_ids: Set[str],
    elapsed_s: Optional[float] = None,
) -> CheckResult:
    """Run every content check against one search answer."""
    grounded_ids = {
        b["bug_id"] for b in bugs
        if (b.get("verification") or {}).get("is_relevant", True)
    }
    result = CheckResult()
    result.extend(check_citations_exist(answer, known_ids))
    result.extend(check_citations_grounded(answer, grounded_ids))
    result.extend(check_citation_format(answer, grounded_ids))
    result.extend(check_no_scaffolding(answer))
    result.extend(check_count_claims(answer))
    result.extend(check_answer_cites_something(answer, grounded_ids))
    result.extend(check_badge_consistency(bugs))
    result.extend(check_fallback_consistency(answer, bugs))
    result.extend(check_no_prompt_leakage(answer))
    if elapsed_s is not None:
        result.extend(check_latency(elapsed_s))
    return result


def score_release_recommendation(
    text: str,
    decision: str,
    blocking_ids: List[str],
    release_ids: Set[str],
    known_ids: Set[str],
    elapsed_s: Optional[float] = None,
) -> CheckResult:
    """Run every content check against one release recommendation."""
    result = CheckResult()
    result.extend(check_citations_exist(text, known_ids))
    result.extend(check_no_scaffolding(text))
    result.extend(check_no_prompt_leakage(text))
    if decision == "NO-GO":
        result.extend(check_blocking_bugs_all_cited(text, blocking_ids))
        result.extend(check_citations_grounded(text, set(blocking_ids)))
    elif decision == "GO":
        result.extend(check_go_text_is_clean(text, release_ids))
    if elapsed_s is not None:
        result.extend(check_latency(elapsed_s, budget_s=20.0))
    return result
