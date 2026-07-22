"""
search.py
---------
Core retrieval + synthesis + verification. Embeds the user's question via
a local Ollama embedding model, fetches the most similar historical bugs
from ChromaDB, verifies each retrieved bug against the query (an independent
LLM call per bug), then asks a local Ollama model to synthesize an answer
grounded in the retrieved bugs.

Can be used as a library (from app.py) or run standalone:
    python search.py "have we seen payment failures with international cards?"
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Tuple

import chromadb
import ollama
from dotenv import load_dotenv

# ---- Config ----------------------------------------------------------------
load_dotenv()

EMBEDDING_MODEL = "nomic-embed-text"   # must match what ingest.py used to build the collection
LLM_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
CHROMA_DIR = "./chroma_db"
COLLECTION_NAME = "bugs"

# Determinism. temperature=0 alone does NOT make Ollama reproducible -- without
# a fixed seed the same query can flip a bug between "related" and
# "not_relevant" across runs, which makes run-to-run report diffing useless
# (you can't tell a real regression from sampling noise). Override with
# OLLAMA_SEED=0 only if you deliberately want to sample variability.
LLM_SEED = int(os.getenv("OLLAMA_SEED", "42"))


def _chat_options(temperature: float = 0.0) -> dict:
    """Options for every LLM call: fixed seed so runs are comparable."""
    return {"temperature": temperature, "seed": LLM_SEED}

# For clickable links in the UI. Change this to your real JIRA base URL.
JIRA_BASE_URL = "https://your-company.atlassian.net/browse/"
# ---------------------------------------------------------------------------


# Lazy-loaded singletons so importing this module is cheap
_ollama_client = None
_collection = None


def get_ollama_client() -> ollama.Client:
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = ollama.Client(host=OLLAMA_HOST)
    return _ollama_client


def get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        _collection = client.get_collection(name=COLLECTION_NAME)
    return _collection


def get_distance_space() -> str:
    """Which distance metric the collection was actually built with.

    This matters because every similarity threshold in this file (notably
    _DUPLICATE_GUARDRAIL_SIMILARITY_FLOOR) is expressed in cosine-similarity
    units. If the collection silently changes metric -- e.g. an ingest that
    forgets to pass hnsw:space and falls back to Chroma's default squared-L2 --
    the raw distances change scale and every threshold quietly means something
    different. Reading the metric back from the collection and converting
    accordingly keeps the thresholds honest either way.
    """
    meta = getattr(get_collection(), "metadata", None) or {}
    return meta.get("hnsw:space", "l2")


def distance_to_similarity(distance: float, space: str) -> float:
    """Convert a Chroma distance into cosine similarity in 0..1."""
    if space == "cosine":
        # cosine distance = 1 - cos_sim
        sim = 1.0 - distance
    elif space == "ip":
        sim = distance
    else:
        # Chroma's "l2" is SQUARED euclidean. On normalized vectors,
        # d = 2 - 2*cos_sim, so cos_sim = 1 - d/2.
        sim = 1.0 - distance / 2.0
    return max(0.0, min(1.0, sim))


def list_all_bug_ids() -> set:
    """Every bug ID currently in the collection. Used by the quality checks
    to tell a real citation from a fabricated one."""
    return set(get_collection().get(include=[])["ids"])


def embed_query(query: str) -> list:
    """Embed a single query string using a local Ollama embedding model."""
    response = get_ollama_client().embed(model=EMBEDDING_MODEL, input=query)
    return response.embeddings[0]


# Phrases that show up when the model's own reasoning hedges toward
# "same component, different issue" language, but it picked "duplicate"
# as the label anyway. Seen in practice: reasoning says "...which is
# related to the user's question..." while suggested_action == "duplicate".
_DUPLICATE_HEDGE_PHRASES = (
    "related to", "related, but", "related but", "similar to",
    "similar issue", "same component", "same feature", "same area",
)

# A near-identical semantic match is itself strong evidence of a genuine
# duplicate. Below this similarity, "related to"/"similar to" language in
# the reasoning is a real red flag (same component, different defect).
# Above it, that same language is often just how a model naturally phrases
# a correct duplicate explanation -- don't second-guess those.
_DUPLICATE_GUARDRAIL_SIMILARITY_FLOOR = 0.75


def _guard_duplicate_label(result: Dict, similarity: float = None) -> Dict:
    """Catch label/reasoning contradictions on 'duplicate' verdicts.

    This is a text-level guardrail, not a semantic re-check: it only fires
    when the model's own reasoning already hedges toward "related" language
    while the suggested_action says "duplicate", AND the similarity score
    is low enough that a same-component false match is plausible. Local
    models are less consistent than a hosted API at holding these two
    fields in sync, so this is a cheap safety net against that specific
    failure mode -- but the phrase check alone is too broad (ordinary
    duplicate explanations often use "related to" as a normal connector),
    so the similarity floor keeps it from firing on high-confidence matches.
    """
    if result.get("suggested_action") != "duplicate":
        return result

    if similarity is not None and similarity >= _DUPLICATE_GUARDRAIL_SIMILARITY_FLOOR:
        return result

    reasoning = (result.get("reasoning") or "").lower()
    if any(phrase in reasoning for phrase in _DUPLICATE_HEDGE_PHRASES):
        print(
            f"      [guardrail] downgraded duplicate -> related "
            f"(similarity={similarity}, reasoning hedged): "
            f"{result.get('reasoning', '')[:120]!r}"
        )
        result = dict(result)
        result["suggested_action"] = "related"
    return result


# Matches a cited ticket reference like [BUG-1002]. Deliberately generic on
# the project prefix so it still works when pointed at a real Jira project.
_CITATION_RE = re.compile(r"\[([A-Z][A-Z0-9]*-\d+)\]")


def extract_cited_ids(text: str) -> List[str]:
    """Every bracketed ticket ID cited in a block of generated text."""
    return _CITATION_RE.findall(text or "")


def normalize_citations(text: str, allowed_ids) -> str:
    """Wrap bare grounded IDs in square brackets.

    The prompt asks for bracketed citations but deliberately shows no example
    ID -- an earlier version demonstrated the format with a literal
    "[BUG-1234]", which the model then emitted as a real citation and used as
    a template for nine more inventions. Removing the example fixed the
    hallucination and cost the format: a live run produced a factually
    correct answer that wrote "BUG-2001" bare, so nothing downstream could
    recognise it as a citation.

    Bracketing is a mechanical transformation on a known set of IDs, so it
    does not need to be a prompt instruction at all. Do it here and the model
    never has to be shown an ID it might copy.
    """
    if not text:
        return text
    # Longest first so BUG-10 can't bracket the prefix of BUG-1002.
    for bug_id in sorted(set(allowed_ids), key=len, reverse=True):
        text = re.sub(
            rf"(?<!\[)\b{re.escape(bug_id)}\b(?!\])",
            f"[{bug_id}]",
            text,
        )
    return text


def strip_ungrounded_citations(text: str, allowed_ids) -> Tuple[str, List[str]]:
    """Remove any cited ID that wasn't in the context the model was given.

    The failure this exists for: a local model, told to "cite bug IDs in
    square brackets like [BUG-1234]", copies the example ID straight out of
    the prompt -- and then, having emitted one plausible-looking ID, keeps
    going and invents nine more. That output is indistinguishable from a
    real citation to anyone reading the UI. The model cannot be trusted to
    only cite what it was shown, so this enforces it in Python: an ID that
    wasn't in the grounded context never reaches the screen.

    Returns (cleaned_text, removed_ids).
    """
    allowed = set(allowed_ids)
    removed = []

    def _sub(match):
        bug_id = match.group(1)
        if bug_id in allowed:
            return match.group(0)
        removed.append(bug_id)
        return ""

    if not text:
        return text, removed

    cleaned = _CITATION_RE.sub(_sub, text)
    if removed:
        print(f"      [guardrail] removed {len(removed)} ungrounded citation(s): "
              f"{', '.join(sorted(set(removed)))}")
        # Tidy the whitespace/punctuation the removals left behind.
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)
        cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
        cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n")).strip()
    return cleaned, removed


def _guard_relevance_consistency(result: Dict) -> Dict:
    """Keep is_relevant and suggested_action from contradicting each other.

    These are two fields the model fills in independently, and it does not
    reliably hold them in sync. When they disagree the UI goes visibly
    incoherent: synthesize() filters the answer on is_relevant, while the
    source badges render suggested_action -- so a query can print "none of
    these substantively address your question" directly above a list of
    bugs badged "related". Whichever way the model split them, the two
    fields have to agree before they leave this function.
    """
    action = result.get("suggested_action")
    is_relevant = result.get("is_relevant")

    if is_relevant is False and action in ("duplicate", "related"):
        result = dict(result)
        result["suggested_action"] = "not_relevant"
        print(f"      [guardrail] is_relevant=False but action={action!r} "
              f"-> forced not_relevant")
    elif is_relevant is True and action == "not_relevant":
        result = dict(result)
        result["is_relevant"] = False
        print("      [guardrail] action='not_relevant' but is_relevant=True "
              "-> forced is_relevant=False")
    return result


def verify_bug_match(query: str, bug: Dict) -> Dict:
    """Independent LLM call: does this bug actually match the query?

    Returns a verification dict with:
      - is_relevant (bool): does this bug substantively answer the query?
      - confidence (str): "high" | "medium" | "low"
      - reasoning (str): short explanation
      - suggested_action (str): "duplicate" | "related" | "not_relevant"
    """
    meta = bug["metadata"]
    bug_summary = (
        f"Bug ID: {bug['bug_id']}\n"
        f"Title: {meta.get('title', '')}\n"
        f"Component: {meta.get('component', '')}\n"
        f"Description + Comments + Resolution:\n{bug['document']}"
    )

    system_prompt = (
        "You are a QA analyst helping a teammate find bugs related to their question. "
        "Given a user's question and one retrieved bug ticket, decide whether the bug "
        "is genuinely relevant.\n\n"
        "Guidelines:\n"
        "- Use 'duplicate' ONLY when the bug describes the SAME defect or SAME "
        "underlying root cause as what the user is asking about — not merely the "
        "same feature.\n"
        "- Use 'related' when the bug affects the SAME feature/component/UI element "
        "the user is asking about but describes a DIFFERENT specific issue "
        "(e.g., styling vs. behavior vs. analytics on the same UI element).\n"
        "- Use 'not_relevant' when the bug touches a DIFFERENT feature or scenario, "
        "even if some keywords overlap. Watch for false-friend matches: e.g. a bug "
        "about a 'tyre search popup' is NOT relevant to a question about a 'Save "
        "vehicle popup' — they are different popups.\n\n"
        "Read the bug title and description carefully to identify WHICH specific "
        "feature or component it affects. Do not classify based on shared keywords alone. "
        "Respond with JSON only, matching the given schema, and nothing else."
    )

    # Plain JSON Schema passed straight into Ollama's `format` param (its
    # structured-outputs feature). No OpenAI-style wrapper needed.
    verification_schema = {
        "type": "object",
        "properties": {
            "is_relevant": {
                "type": "boolean",
                "description": "true if this bug substantively addresses the same issue or same feature the user is asking about",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "reasoning": {
                "type": "string",
                "description": "one or two sentences explaining the judgment, including what specific feature the bug affects",
            },
            "suggested_action": {
                "type": "string",
                "enum": ["duplicate", "related", "not_relevant"],
                "description": "duplicate = describes the SAME defect/root cause as the question; related = same feature/component but a DIFFERENT specific defect (e.g. styling vs behavior); not_relevant = different feature entirely, even if keywords overlap",
            },
        },
        "required": ["is_relevant", "confidence", "reasoning", "suggested_action"],
        "additionalProperties": False,
    }

    try:
        response = get_ollama_client().chat(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"User's question:\n{query}\n\n"
                        f"Retrieved bug:\n{bug_summary}\n\n"
                        "Classify this bug's relevance to the question."
                    ),
                },
            ],
            format=verification_schema,
            options=_chat_options(temperature=0.0),
        )
        parsed = json.loads(response.message.content)
        parsed = _guard_duplicate_label(parsed, similarity=bug.get("similarity"))
        return _guard_relevance_consistency(parsed)
    except Exception as e:
        # Local models / a local server can fail for many different reasons
        # (bad JSON, Ollama not running, timeout, schema mismatch). Log the
        # real cause for debugging, and tag the result as an error rather
        # than silently returning a confident-looking "not_relevant" that's
        # indistinguishable from a genuine negative verification in the UI.
        print(f"      [verify_bug_match] ERROR on {bug.get('bug_id', '?')}: "
              f"{type(e).__name__}: {e}")
        return {
            "is_relevant": False,
            "confidence": "low",
            "reasoning": f"Verification call failed ({type(e).__name__}) — not a real judgment.",
            "suggested_action": "not_relevant",
            "error": True,
        }


def retrieve(query: str, k: int = 5, verify: bool = True) -> List[Dict]:
    """Return the top-k bugs most semantically similar to the query.

    If verify=True, each bug gets an independent verification pass from the LLM,
    checking whether it substantively addresses the same root cause as the query.

    Verification calls run concurrently via a thread pool. IMPORTANT: this only
    speeds things up if your Ollama server allows more than one in-flight
    generation. By default OLLAMA_NUM_PARALLEL=1, which serializes requests on
    the server side regardless of how many the client sends concurrently — set
    OLLAMA_NUM_PARALLEL=4 (or similar) as an environment variable before
    `ollama serve` starts to actually get the benefit of this.
    """
    embedding = embed_query(query)
    space = get_distance_space()
    results = get_collection().query(query_embeddings=[embedding], n_results=k)

    bugs = []
    for i in range(len(results["ids"][0])):
        # Convert the raw distance into cosine similarity (0-1, higher =
        # more similar) using whichever metric the collection was built with.
        distance = results["distances"][0][i]
        similarity = distance_to_similarity(distance, space)
        bugs.append({
            "bug_id": results["ids"][0][i],
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "similarity": similarity,
        })

    if verify:
        with ThreadPoolExecutor(max_workers=min(len(bugs), 5)) as executor:
            verifications = list(executor.map(lambda b: verify_bug_match(query, b), bugs))
        for b, v in zip(bugs, verifications):
            b["verification"] = v

    return bugs


def synthesize(query: str, bugs: List[Dict]) -> str:
    """Ask the LLM to write an answer grounded in the (verified) retrieved bugs.

    If verification data is present on bugs, only genuinely relevant ones are
    passed as context — false positives filtered out to prevent noisy answers.
    """
    if not bugs:
        return "I couldn't find any historical bugs related to that question."

    # Prefer verified-relevant bugs for the answer synthesis, if verification ran
    grounded_bugs = [
        b for b in bugs
        if b.get("verification", {}).get("is_relevant", True)
    ]

    if not grounded_bugs:
        return (
            "I found some bugs by semantic similarity, but none of them appear "
            "to substantively address your question after verification. "
            "Try rephrasing, or check the retrieved bugs below to confirm."
        )

    context_blocks = []
    for i, b in enumerate(grounded_bugs, 1):
        context_blocks.append(
            f"--- Bug {i} (ID: {b['bug_id']}, "
            f"Status: {b['metadata'].get('status', '?')}) ---\n"
            f"{b['document']}"
        )
    context = "\n\n".join(context_blocks)

    response = get_ollama_client().chat(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a senior QA engineer helping a teammate find relevant "
                    "bug history. Answer using ONLY the bug reports provided. "
                    "Cite bug IDs in square brackets, copying each ID exactly as "
                    "it appears in the bugs above. NEVER write a bug ID that does "
                    "not appear verbatim in the provided bugs — inventing an ID is "
                    "worse than citing nothing. "
                    "If you refer to a number of bugs (\"both\", \"these two\"), you "
                    "must cite that many distinct IDs in the same answer. "
                    "If multiple bugs share a root cause, point that out. "
                    "If the provided bugs don't actually answer the question, say so. "
                    "Be concise. Lead with the answer."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Historical bugs retrieved from our tracker:\n\n{context}\n\n"
                    f"Question: {query}\n\n"
                    f"Answer:"
                ),
            },
        ],
        options=_chat_options(temperature=0.2),
    )
    grounded_ids = [b["bug_id"] for b in grounded_bugs]
    # Bracket first (so correct-but-unformatted citations survive), then
    # strip anything that still isn't in the grounded set.
    answer = normalize_citations(response.message.content, grounded_ids)
    answer, _removed = strip_ungrounded_citations(answer, grounded_ids)
    return answer


def list_release_versions() -> List[str]:
    """Return every distinct release_version tag currently in the collection,
    sorted for display in a dropdown."""
    results = get_collection().get(include=["metadatas"])
    versions = {m.get("release_version", "") for m in results["metadatas"]}
    versions.discard("")
    return sorted(versions)


def get_bugs_for_release(release_version: str) -> List[Dict]:
    """Fetch every bug tagged with a given release version via an exact
    metadata filter — a structured lookup, not a semantic search, since
    a go/no-go gate needs the complete set of bugs for that release, not
    the ones that happen to be similar to some query text."""
    collection = get_collection()
    results = collection.get(
        where={"release_version": release_version},
        include=["metadatas", "documents"],
    )
    bugs = []
    for i, bug_id in enumerate(results["ids"]):
        bugs.append({
            "bug_id": bug_id,
            "document": results["documents"][i],
            "metadata": results["metadatas"][i],
        })
    return bugs


# Priorities that block a release if left open. This is the actual go/no-go
# rule — deliberately a plain constant + comparison, not an LLM judgment
# call, so the gate decision itself stays deterministic and auditable.
BLOCKING_PRIORITIES = {"High", "Blocker"}


def _group_by_status(bugs: List[Dict]) -> Dict[str, List[Dict]]:
    """Group bugs by their actual status. Used to hand the LLM pre-built
    groups instead of letting it invent its own — a model recombining bugs
    across different real statuses (e.g. lumping an Open bug in with two
    Ready for Test bugs) is exactly the kind of factual slip that erodes
    trust in the write-up."""
    groups: Dict[str, List[Dict]] = {}
    for b in bugs:
        status = b["metadata"].get("status", "Unknown")
        groups.setdefault(status, []).append(b)
    return groups


def _clean_recommendation_text(text: str) -> str:
    """Strip preamble artifacts local models sometimes prepend (e.g. 'Here
    is a 2-3 sentence recommendation:') and any quote marks wrapping the
    whole response. A hosted API tends not to do this; local models are
    more prone to echoing the instruction framing back as a lead-in, which
    reads as unpolished/broken to anyone looking at the actual UI."""
    text = text.strip()
    # Leading "Here is ...:" / "Here's ...:" as its own line or inline lead-in
    text = re.sub(r'^(here is|here\'s)\b[^:\n]*:\s*', '', text, flags=re.IGNORECASE)
    text = text.strip()
    # Wrapping quote marks around the entire remaining response
    if len(text) >= 2 and text[0] in '"“' and text[-1] in '"”':
        text = text[1:-1].strip()

    # Scaffolding artifacts. A live GO run emitted:
    #     // Breakdown:
    #     // - 10 tracked bugs
    #     // - Remaining lower-priority bugs: [list of bug descriptions]
    # i.e. the model sketching, as code comments, the appendix the prompt had
    # told it Python would add. None of the content checks caught it -- it is
    # not an invented bug ID and not an English instruction-echo -- so it went
    # straight to the UI under a green GO verdict. Comment-style lines are
    # never legitimate output here, so drop them.
    text = "\n".join(
        line for line in text.split("\n")
        if not re.match(r'^\s*(//|#\s|/\*|\*/)', line)
    )
    # Unfilled placeholders like "[list of bug descriptions]" or "[insert X]":
    # bracketed prose rather than a ticket ID.
    text = re.sub(
        r'\[(?![A-Z][A-Z0-9]*-\d+\])[^\]]*\b(list|insert|your|todo|placeholder|descriptions?)\b[^\]]*\]',
        '', text, flags=re.IGNORECASE)

    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _synthesize_release_recommendation(
    release_version: str, decision: str, blocking_bugs: List[Dict], all_bugs: List[Dict]
) -> str:
    """Ask the LLM to write the human-readable explanation for a decision
    that has ALREADY been made deterministically above. The model is not
    asked to decide go/no-go — only to explain a decision it's handed,
    citing specific bug IDs. This keeps the gate itself trustworthy even
    though the write-up is LLM-generated."""

    def _fmt(bugs: List[Dict]) -> str:
        return "\n".join(
            f"- [{b['bug_id']}] {b['metadata'].get('title', '')} "
            f"({b['metadata'].get('priority', '?')} priority, {b['metadata'].get('status', '?')})"
            for b in bugs
        )

    if decision == "GO":
        non_closed = [b for b in all_bugs if b["metadata"].get("status") != "Closed"]
        prompt = (
            f"Release {release_version} has {len(all_bugs)} tracked bug(s) and none are "
            f"open at High or Blocker priority, so this release is cleared to ship. "
            f"Write exactly ONE sentence confirming this release is cleared to ship. "
            f"Do NOT state any bug counts, priorities, or IDs yourself, and do "
            f"NOT sketch or outline what a breakdown would contain — anything of "
            f"that kind is computed and appended after your sentence, and does "
            f"not belong in your output. Write the one clearance sentence and "
            f"nothing else."
        )
    else:
        blocking_ids = ", ".join(f"[{b['bug_id']}]" for b in blocking_bugs)
        status_groups = _group_by_status(blocking_bugs)
        grouped_text = "\n\n".join(
            f'Status "{status}" ({len(bugs)} bug(s)):\n{_fmt(bugs)}'
            for status, bugs in status_groups.items()
        )
        prompt = (
            f"Release {release_version} has {len(blocking_bugs)} open bug(s) at High or "
            f"Blocker priority, which blocks release per policy. The bugs below are "
            f"ALREADY grouped by their real status — use these groups exactly as given, "
            f"do not recombine bugs from different status groups into one description "
            f"(e.g. a bug that's still 'Open' has NOT started testing yet, even if "
            f"another blocking bug happens to be 'Ready for Test' — never describe them "
            f"the same way).\n\n{grouped_text}\n\n"
            f"Write a 2-3 sentence NO-GO recommendation for a release manager. You MUST "
            f"cite every single one of the {len(blocking_bugs)} blocking bug IDs — not a "
            f"subset: {blocking_ids}. Describe what each status group still needs "
            f"(e.g. bugs still 'Open' need a fix written; bugs 'In Progress' need the "
            f"fix finished; bugs 'Ready for Test' need testing/verification)."
        )

    # The system prompt is split by branch on purpose. A single shared prompt
    # that says "cite bug IDs in square brackets like [BUG-1234]" directly
    # contradicts the GO branch's "do NOT state any IDs yourself" — and when a
    # local model gets contradictory instructions it tends to resolve them by
    # following the concrete example, i.e. by emitting the placeholder ID from
    # the prompt and then inventing more in the same shape. Each branch now
    # gets one unambiguous instruction and no example ID to copy.
    _SHARED_RULES = (
        "You are a release manager's assistant. The go/no-go decision has "
        "already been made — your job is only to explain it clearly and "
        "concisely. Never contradict or second-guess the decision you're given. "
        "Output ONLY the recommendation text itself — no preamble like 'Here "
        "is a recommendation:', no restating the instructions, no wrapping "
        "the response in quotation marks."
    )
    if decision == "GO":
        system_content = (
            _SHARED_RULES
            + " Your entire output is ONE sentence confirming the release is "
              "cleared to ship. Do NOT write any bug IDs, bug counts, or "
              "priorities. Do NOT write a summary, a breakdown, a bulleted "
              "list, code comments, or placeholder text of any kind. One "
              "sentence, then stop."
        )
    else:
        system_content = (
            _SHARED_RULES
            + " Cite bug IDs in square brackets, copying each ID exactly as it "
              "appears in the list you are given. NEVER write a bug ID that is "
              "not in that list — inventing an ID is worse than citing nothing. "
              "When asked to cite a specific set of bug IDs, cite every one of "
              "them — dropping any of them is a mistake, not a valid summary. "
              "When bugs are given to you pre-grouped by status, respect those "
              "groups exactly — never describe two bugs the same way if their "
              "given status differs."
        )

    response = get_ollama_client().chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ],
        options=_chat_options(temperature=0.2),
    )
    text = _clean_recommendation_text(response.message.content)

    # Whatever the branch, the model may only cite bugs from THIS release.
    # On the GO path that allowed set is effectively a backstop: the prompt
    # says cite nothing, so anything bracketed is already off-script.
    citable = [b["bug_id"] for b in
               (blocking_bugs if decision == "NO-GO" else all_bugs)]
    if decision == "NO-GO":
        text = normalize_citations(text, citable)
    text, _removed = strip_ungrounded_citations(text, citable)
    text = _clean_recommendation_text(text)

    # Deterministic guardrails: don't trust the model's own arithmetic or
    # citation completeness for facts that are trivial to compute exactly.
    if decision == "NO-GO":
        # Local models sometimes summarize down to a few representative IDs
        # even when explicitly told to list all of them. Check what actually
        # got cited and append anything missing.
        missing = [b["bug_id"] for b in blocking_bugs if b["bug_id"] not in text]
        if missing:
            missing_str = ", ".join(f"[{bid}]" for bid in missing)
            text += f"\n\nAlso blocking: {missing_str}."
    else:
        # GO: never let the model state priority counts itself -- a real run
        # once swapped "3 low / 4 medium" into "4 low / 3 medium" while also
        # dropping a bug ID, presented with full confidence and nothing on
        # screen to signal it was wrong. Build this list in Python instead.
        non_closed = [b for b in all_bugs if b["metadata"].get("status") != "Closed"]
        if non_closed:
            by_priority: Dict[str, List[Dict]] = {}
            for b in non_closed:
                by_priority.setdefault(b["metadata"].get("priority", "Unknown"), []).append(b)
            priority_order = ["Blocker", "High", "Medium", "Low", "Unknown"]
            parts = []
            for p in priority_order:
                bugs_p = by_priority.get(p)
                if bugs_p:
                    ids = ", ".join(f"[{b['bug_id']}]" for b in bugs_p)
                    parts.append(f"{len(bugs_p)} {p.lower()}-priority ({ids})")
            text += (
                f"\n\n{len(non_closed)} bug(s) remain open at lower priority, none "
                f"blocking: {'; '.join(parts)}."
            )
        else:
            text += "\n\nNo bugs remain open for this release."

    return text


def check_release_readiness(release_version: str) -> Dict:
    """Deterministic go/no-go gate for a release version.

    Rule: NO-GO if any bug tagged with this release is not Closed AND is
    High or Blocker priority. Otherwise GO. Returns the decision, the list
    of blocking bugs (if any), and an LLM-written recommendation grounded
    in those facts.
    """
    bugs = get_bugs_for_release(release_version)

    if not bugs:
        return {
            "release_version": release_version,
            "decision": "UNKNOWN",
            "blocking_bugs": [],
            "all_bugs": [],
            "recommendation": (
                f"No bugs are tagged with release {release_version} in the tracker. "
                f"Double-check the version string, or this release may not exist yet."
            ),
        }

    blocking_bugs = [
        b for b in bugs
        if b["metadata"].get("status") != "Closed"
        and b["metadata"].get("priority") in BLOCKING_PRIORITIES
    ]
    decision = "NO-GO" if blocking_bugs else "GO"
    recommendation = _synthesize_release_recommendation(
        release_version, decision, blocking_bugs, bugs
    )

    return {
        "release_version": release_version,
        "decision": decision,
        "blocking_bugs": blocking_bugs,
        "all_bugs": bugs,
        "recommendation": recommendation,
    }


def ask(query: str, k: int = 5) -> Tuple[str, List[Dict]]:
    """One-shot: retrieve + verify + synthesize. Returns (answer, source_bugs)."""
    bugs = retrieve(query, k=k, verify=True)
    answer = synthesize(query, bugs)
    return answer, bugs


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or \
        "have we seen payment failures with international cards?"
    print(f"Q: {q}\n")
    answer, bugs = ask(q)
    print(f"A: {answer}\n")
    print("Sources:")
    for b in bugs:
        v = b.get("verification", {})
        badge = v.get("suggested_action", "?")
        print(f"  [{b['bug_id']}] {b['metadata']['title']} "
              f"(sim={b['similarity']:.2f}, verification={badge}, "
              f"confidence={v.get('confidence', '?')})")
