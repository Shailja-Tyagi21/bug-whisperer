# 🐛 Bug Whisperer

A searchable "brain" of your team's historical bugs. Ask questions in plain English, get synthesized answers with citations to the original tickets.

**100% local** — embeddings, vector store, and LLM all run on your machine. Your bug data never leaves the laptop. That's the leadership pitch.

---

## How it works

```
┌─────────────────┐    ┌──────────────────┐    ┌──────────────┐
│  JIRA CSV       │ ─► │ nomic-embed-text │ ─► │  ChromaDB    │
│  export         │    │ (local, Ollama)  │    │  (cosine)    │
└─────────────────┘    └──────────────────┘    └──────┬───────┘
                                                      │
                                                      ▼
┌─────────────────┐    ┌──────────────────┐    ┌──────────────┐
│  User question  │ ─► │ Same embedder    │ ─► │ Top-K match  │
└─────────────────┘    └──────────────────┘    └──────┬───────┘
                                                      │
                                                      ▼
                       ┌──────────────────┐    ┌──────────────┐
                       │ Per-bug verify   │ ◄─ │  Ollama LLM  │
                       │ (1 call per bug) │    │  (local)     │
                       └────────┬─────────┘    └──────────────┘
                                │  false positives dropped
                                ▼
                       ┌──────────────────┐
                       │  Synthesized     │  ← ungrounded citations
                       │  answer + cites  │    stripped in Python
                       └──────────────────┘
```

### Where the LLM is *not* trusted

The interesting part of this build is the boundary between what the model
decides and what Python decides:

| Decision | Made by | Why |
|---|---|---|
| Which bugs are semantically similar | embeddings | that's what they're for |
| Is a retrieved bug actually relevant | LLM, one call per bug | judgment call |
| `duplicate` vs `related` | LLM, then a similarity-floor guardrail | model conflates same-component with same-defect |
| **Release GO / NO-GO** | **Python, deterministic rule** | a gate that can hallucinate is not a gate |
| Which bug IDs may be cited | Python filter | the model will invent IDs; see `strip_ungrounded_citations` |
| Priority/count breakdowns in the write-up | Python | the model's arithmetic silently swapped counts in a live run |

The LLM writes the *explanation* for a go/no-go verdict it is handed. It
never makes the verdict.

---

## Setup (one-time, ~10 minutes)

### 1. Install Python dependencies
```bash
cd bug-whisperer
pip install -r requirements.txt
```

### 2. Install and start Ollama
Download from <https://ollama.com>. After install, pull a model:
```bash
ollama pull llama3.1
```
> Other good options: `mistral`, `qwen2.5`, `gemma2`. Edit `OLLAMA_MODEL` in `search.py` to switch.

Make sure Ollama is running (`ollama serve` — usually auto-starts).

### 3. Build the vector store
```bash
python ingest.py
```
First run downloads the embedding model (~80MB). After that it's offline-only.

### 4. Launch the app
```bash
streamlit run app.py
```
Opens at <http://localhost:8501>.

---

## Using your own bug data

Export from JIRA as CSV with these columns:
```
ID, Title, Description, Comments, Resolution, Component, Severity, Status, Created
```

Then:
```bash
python ingest.py --csv path/to/your_bugs.csv
```

> Tip: include the **Comments** column. That's where engineers actually discuss root causes — the most semantically valuable text in any bug.

> Include **Priority** and **ReleaseVersion** too — the release readiness gate needs both.

> ⚠️ Changing the embedding model changes vector dimensionality, and different-dimension vectors cannot coexist in one Chroma collection. `ingest.py` always deletes and recreates.

---

## Configuration

Edit constants at the top of `search.py`:

| Constant | Purpose |
|---|---|
| `EMBEDDING_MODEL` | Must match what `ingest.py` built the collection with |
| `OLLAMA_MODEL` | Any model you've pulled into Ollama |
| `OLLAMA_HOST` | Change if Ollama runs elsewhere |
| `JIRA_BASE_URL` | Your company's JIRA URL so source cards link properly |

---

## Demo strategy for leadership

**Open with pain.** Real story: "Last month, an engineer spent 4 hours investigating a checkout bug before realizing we'd debugged the same thing in 2023. Here's what happens with Bug Whisperer."

**Show four kinds of queries** to demonstrate different superpowers:

1. **Exact-match win** — _"PayPal redirect issue"_ → finds [BUG-1002] immediately
2. **Semantic match (different words, same problem)** — _"checkout broken for European customers"_ → still finds the Visa/EU bug because the embedding understands meaning, not just keywords
3. **Cross-bug pattern** — _"what causes our memory leaks?"_ → synthesizes across multiple bugs and points out the common root cause
4. **Fix lookup** — _"how have we historically fixed Safari issues?"_ → returns the actual resolutions from past tickets

**Close with the math.** Use real numbers: `avg investigation time × duplicates/month × engineer cost`. Even conservative estimates land in five figures annually.

**Tease the roadmap.** "Next: auto-suggest historical bugs on every new JIRA ticket. Then: Slack bot. Then: same brain for incidents and postmortems."

---

## Project layout

```
bug-whisperer/
├── README.md           # this file
├── requirements.txt    # Python deps
├── sample_bugs.csv     # 60 realistic sample bugs (priority + release tagged)
├── ingest.py           # one-time: CSV → embeddings → ChromaDB
├── jira_fetch.py       # one-shot JIRA export → CSV
├── search.py           # retrieve + verify + synthesize + release gate
├── quality_checks.py   # content assertions on generated answers (library)
├── app.py              # Streamlit UI
└── tests/
    ├── test_guardrails.py      # unit tests, deterministic helpers
    ├── test_quality_checks.py  # unit tests, run against real bad outputs
    ├── check_similarity_calibration.py  # is the guardrail floor still valid?
    ├── run_regression_suite.py # live end-to-end run + scoring + baseline diff
    └── reports/                # timestamped reports + latest.json baseline
```

---

## Testing

Three layers, fastest first:

```bash
# 1. Unit tests — instant, no Ollama needed
python3 tests/test_guardrails.py
python3 tests/test_quality_checks.py

# 2. Calibration — run after ANY re-ingest or model change
python3 tests/check_similarity_calibration.py

# 3. Full live regression run — needs Ollama + a built collection
python3 tests/run_regression_suite.py
```

`check_similarity_calibration.py` covers a gap the unit tests structurally
cannot: `test_guardrails.py` passes similarity values in by hand, so it
keeps passing regardless of what the live stack actually produces. The
calibration script measures two real anchor cases — a genuine duplicate
that must score above `_DUPLICATE_GUARDRAIL_SIMILARITY_FLOOR`, and a
same-component false match that must score below it — and fails if the
floor has stopped separating them.

The regression suite runs all ten demo questions plus both release gates
against the live stack, **scores the generated text**, and diffs the result
against the previous run — so it reports what *changed*, not just what's
currently broken.

Why the scoring matters: an earlier version of the suite checked only for
crashes and gate mismatches. It marked a run "✅ Everything ran clean" whose
GO recommendation cited ten bug IDs that don't exist in the corpus. The
model had copied the placeholder `[BUG-1234]` out of its own prompt and
invented nine more in the same shape. `tests/quality_checks.py` now asserts
that every cited ID exists, that every cited ID was in the retrieved
context, that "these two bugs" is followed by two actual citations, and
that the relevance badges never contradict the answer text.

Every failing case in `test_quality_checks.py` is verbatim text from a real
run that shipped past a green checkmark.

### Reproducibility

All LLM calls pin `seed` as well as `temperature=0`. Without a fixed seed,
the same query could flip a bug between `related` and `not_relevant` across
runs — which makes report-to-report diffing meaningless, because you can't
tell a real regression from sampling noise. Override with `OLLAMA_SEED=0`
if you want to deliberately sample variability.

---

## Troubleshooting

**`Connection refused` to Ollama** — make sure `ollama serve` is running. On macOS the app handles this automatically; on Linux you may need to start it manually.

**`Collection not found`** — you forgot to run `python ingest.py` after the dependencies installed.

**Slow synthesis** — Ollama performance depends on your local hardware. Try a smaller model like `gemma2:2b` or `qwen2.5:3b` for the demo.

**Verification pass is slow** — the retrieve step makes one LLM call per retrieved bug, concurrently. Ollama defaults to `OLLAMA_NUM_PARALLEL=1`, which serializes them server-side no matter how many the client sends. Set `OLLAMA_NUM_PARALLEL=4` before `ollama serve` starts to actually get the concurrency.

**Similarity scores look wrong after a re-ingest** — `ingest.py` pins `hnsw:space` to cosine. A collection built without it falls back to Chroma's default squared-L2, which is a silent scale change rather than an error. `search.py` reads the metric back off the collection and converts accordingly, but a stale collection should just be rebuilt.
