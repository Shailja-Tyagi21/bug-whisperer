# 🔍 BugLens

A searchable "brain" of your team's historical bugs. Ask questions in plain English, get synthesized answers with citations to the original tickets — plus a deterministic release readiness gate.

**100% local** — embeddings, vector store, and LLM all run on your machine. Your bug data never leaves the laptop. That's the leadership pitch.

Also ships as an **MCP server**, so the same engine is callable directly from GitHub Copilot, Claude Desktop, Cursor, or any MCP-compatible AI assistant — no separate UI required.

> Formerly "Bug Whisperer." Renamed to BugLens for the hackathon submission — same engine, same guardrails.

---

## How it works

```
┌─────────────────┐    ┌──────────────────┐    ┌──────────────┐
│  JIRA (live)     │ ─► │ jira_fetch.py    │ ─► │  CSV export  │
│  REST API        │    │ + custom fields  │    │              │
└─────────────────┘    └──────────────────┘    └──────┬───────┘
                                                       │
                                                       ▼
                       ┌──────────────────┐    ┌──────────────┐
                       │ nomic-embed-text │ ◄─ │  ingest.py   │
                       │ (local, Ollama)  │    │              │
                       └────────┬─────────┘    └──────────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │  ChromaDB        │
                       │  (cosine)        │
                       └────────┬─────────┘
                                │
       ┌────────────────────────┼────────────────────────┐
       ▼                        ▼                         │
┌──────────────┐    ┌──────────────────┐                  │
│ User question│ ─► │ Vector search    │                  │
└──────────────┘    │ (top 3k, wide net)│                 │
                     └────────┬─────────┘                 │
                              │        ┌──────────────────┘
                              ▼        ▼
                     ┌──────────────────┐
                     │ BM25 keyword     │  ← scores ALL bugs;
                     │ scoring (hybrid) │    rescues strong keyword
                     └────────┬─────────┘    matches vector search missed
                              │  merge: 0.7·vector + 0.3·bm25
                              ▼
                     ┌──────────────────┐    ┌──────────────┐
                     │ Top-k re-ranked  │ ─► │ Per-bug      │ ◄─ Ollama LLM
                     │ candidates       │    │ verification │    (local, one
                     └──────────────────┘    │ + guardrails │     call per bug,
                                              └──────┬───────┘     concurrent)
                                                     │  false positives dropped
                                                     ▼
                                            ┌──────────────────┐
                                            │  Synthesized     │  ← ungrounded citations
                                            │  answer + cites  │    stripped in Python
                                            └──────────────────┘
```

**Two front doors, one engine:** the Streamlit app (`app.py`) and the MCP server (`bugLens_mcp.py`) both call straight into `search.py` — same retrieval, same verification, same guardrails, no duplicated logic.

### Why hybrid search

Vector search alone dilutes distinctive keywords when they're surrounded by common words. On a real test: *"checkout broken for European customers"* ranked the actually-correct bug (Visa cards issued in Europe) at **#2 (0.645)** by pure vector similarity — a BM25 keyword pass, which specifically rewards rare terms like "European," promoted it to **#1 (0.752)**. BM25 also has a rescue path: if it finds a strong keyword match vector search never even retrieved, it pulls that bug into the candidate pool (with a low placeholder vector score) rather than letting it disappear silently.

### Where the LLM is *not* trusted

The interesting part of this build is the boundary between what the model decides and what Python decides:

| Decision | Made by | Why |
|---|---|---|
| Which bugs are semantically similar | embeddings + BM25 | that's what they're for |
| Is a retrieved bug actually relevant | LLM, one call per bug, concurrent | judgment call |
| `duplicate` vs `related` | LLM, then a similarity-floor guardrail | model conflates same-component with same-defect |
| `is_relevant` vs the displayed badge agreeing | LLM, then a consistency guardrail | the two fields are filled in independently and can contradict each other; whichever says "not relevant" always wins |
| **Release GO / NO-GO** | **Python, deterministic rule** | a gate that can hallucinate is not a gate |
| Which bug IDs may be cited | Python filter | the model will invent IDs; see `strip_ungrounded_citations` |
| Priority/count breakdowns in the write-up | Python | the model's arithmetic silently swapped counts in a live run |

The LLM writes the *explanation* for a go/no-go verdict it is handed. It never makes the verdict.

---

## Setup (one-time, ~10 minutes)

### 1. Install Python dependencies
```bash
cd BugWhisperer_hackathon_JIRA
pip install -r requirements.txt
```

### 2. Install and start Ollama
Download from <https://ollama.com>. After install, pull both models this project needs:
```bash
ollama pull llama3.1
ollama pull nomic-embed-text
```
> Other good LLM options: `mistral`, `qwen2.5`, `gemma2`. Edit `OLLAMA_MODEL` env var or the default in `search.py` to switch. The embedding model must stay `nomic-embed-text` unless you re-ingest everything — see the warning below.

Make sure Ollama is running (`ollama serve` — usually auto-starts).

### 3. Connect to Jira and build the vector store

Copy `.env.example` to `.env` and fill in your Jira credentials:
```bash
cp .env.example .env
# JIRA_URL=https://your-company.atlassian.net
# JIRA_EMAIL=you@example.com
# JIRA_API_TOKEN=...
```

Then pull real bugs and build the local database:
```bash
python3 jira_fetch.py
python3 ingest.py --csv jira_bugs.csv
```

`jira_fetch.py` fetches from Jira's `/search/jql` endpoint, paginating until done, and flattens Jira's ADF (rich-text) descriptions and comments into plain text. It also resolves three **custom dropdown fields** (Bug Component, Release Version, Severity) by ID — team-managed Jira projects often don't expose native equivalents. A sanity check at the end warns if any custom field came back empty across every row, which usually means the field ID has changed.

`ingest.py` embeds everything locally via Ollama and writes to a fresh ChromaDB collection (pinned to cosine distance — see the troubleshooting note below on why that matters). First run downloads the embedding model (~80MB); after that it's offline-only.

No Jira access? You can still run everything against the bundled `sample_bugs.csv`:
```bash
python3 ingest.py --csv sample_bugs.csv
```

### 4. Launch the app
```bash
streamlit run app.py
```
Opens at <http://localhost:8501>.

### 5. (Optional) Enable the MCP server

```bash
pip install "mcp[cli]"
mcp dev bugLens_mcp.py          # test in MCP Inspector
mcp install bugLens_mcp.py      # install into Claude Desktop
```

For VS Code / GitHub Copilot, add to `.vscode/mcp.json`:
```json
{
  "servers": {
    "buglens": {
      "command": "python3",
      "args": ["bugLens_mcp.py"],
      "env": { "JIRA_URL": "https://your-company.atlassian.net" }
    }
  }
}
```
Then in Copilot Chat: `@buglens search bug history for <your question>`.

Three tools are exposed: `search_bugs`, `check_release`, `list_releases` — thin wrappers around the exact same `search.py` functions the Streamlit app uses.

> **Heads up:** an AI assistant calling these tools may rephrase your question before it reaches `retrieve()` — Copilot has been observed doing this — which can change retrieval results compared to typing the same words directly into Streamlit. The grounded reasoning still happens locally either way; only the input phrasing is out of your direct control via MCP.

---

## Using your own bug data

If not pulling from Jira, export a CSV with these columns:
```
ID, Title, Description, Comments, Resolution, Component, Severity, Priority, Status, Created, ReleaseVersion
```

Then:
```bash
python3 ingest.py --csv path/to/your_bugs.csv
```

> Tip: include the **Comments** column. That's where engineers actually discuss root causes — the most semantically valuable text in any bug. `build_text_for_embedding()` embeds the full, untruncated text regardless of length; only the *displayed* metadata field is capped at 500 characters for UI tidiness.

> Include **Priority** and **ReleaseVersion** too — the release readiness gate needs both.

> ⚠️ Changing the embedding model changes vector dimensionality, and different-dimension vectors cannot coexist in one Chroma collection. `ingest.py` always deletes and recreates the collection on ingestion.

---

## Configuration

Edit constants at the top of `search.py`, or set via `.env`:

| Constant | Purpose |
|---|---|
| `EMBEDDING_MODEL` | Hardcoded, not `.env` — must match what `ingest.py` built the collection with, or vector comparisons silently break |
| `OLLAMA_MODEL` | Any model you've pulled into Ollama (`.env`: `OLLAMA_MODEL`) |
| `OLLAMA_HOST` | Change if Ollama runs elsewhere |
| `OLLAMA_SEED` | Fixed seed (default 42) so identical queries give identical answers across runs — see Reproducibility below |
| `HYBRID_BM25_WEIGHT` | Blend ratio for hybrid search, default `0.3` (30% keyword, 70% vector) |
| `JIRA_URL` / `JIRA_BASE_URL` | Your company's Jira URL, used both for fetching and for building clickable source links |

---

## Demo strategy for leadership

**Open with pain.** Real story: "Last month, an engineer spent 4 hours investigating a checkout bug before realizing we'd debugged the same thing in 2023. Here's what happens with BugLens."

**Show a few kinds of queries** to demonstrate different superpowers:

1. **Keyword-sensitive match** — *"checkout broken for European customers"* → hybrid search correctly surfaces the Visa/EU bug at #1, something pure vector search under-ranks
2. **Honest rejection** — *"do we have any Bluetooth pairing bugs?"* → no such bug exists; every candidate is independently verified and rejected rather than forcing an answer
3. **Multiple genuine matches** — *"refund process silent failures"* → two distinct root causes, both cited, badged differently (direct match vs. related)
4. **Release readiness** — pick a version, show a NO-GO with the blocking bugs table, and point out the decision is a plain Python rule, not a model guess

**Close with the math.** Use real numbers: `avg investigation time × duplicates/month × engineer cost`. Even conservative estimates land in five figures annually.

**Tease the roadmap.** Per-component/continuous risk scoring instead of a binary gate, multi-project support via a broader JQL plus a metadata filter (no re-architecture needed), and hybrid search weight tuning as the corpus grows past today's 60 bugs.

---

## Project layout

```
BugWhisperer_hackathon_JIRA/
├── README.md              # this file
├── requirements.txt       # Python deps
├── .env.example           # Jira credentials + config template
├── sample_bugs.csv        # 60 realistic sample bugs (priority + release tagged)
├── jira_fetch.py          # live Jira export → CSV, incl. custom fields + ADF flattening
├── ingest.py              # CSV → embeddings → ChromaDB
├── search.py              # hybrid retrieve + verify + synthesize + release gate
├── quality_checks.py      # content assertions on generated answers (library)
├── app.py                 # Streamlit UI
├── bugLens_mcp.py         # MCP server — same engine, exposed to AI assistants
├── .vscode/mcp.json       # MCP config for VS Code Copilot
└── tests/
    ├── test_guardrails.py             # unit tests, deterministic helpers
    ├── test_quality_checks.py         # unit tests, run against real bad outputs
    ├── check_similarity_calibration.py# is the guardrail floor still valid?
    ├── run_regression_suite.py        # live end-to-end run + scoring + baseline diff
    └── reports/                       # timestamped reports + latest.json baseline
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

`check_similarity_calibration.py` covers a gap the unit tests structurally cannot: `test_guardrails.py` passes similarity values in by hand, so it keeps passing regardless of what the live stack actually produces. The calibration script measures real anchor cases — a genuine duplicate that must score above `_DUPLICATE_GUARDRAIL_SIMILARITY_FLOOR`, and a same-component false match that must score below it — and fails if the floor has stopped separating them.

The regression suite runs the full demo query set plus release gates against the live stack, **scores the generated text**, and diffs the result against the previous run — so it reports what *changed*, not just what's currently broken.

Why the scoring matters: an earlier version of the suite checked only for crashes and gate mismatches. It marked a run "✅ Everything ran clean" whose GO recommendation cited ten bug IDs that don't exist in the corpus. The model had copied a placeholder ID out of its own prompt and invented nine more in the same shape. `quality_checks.py` now asserts that every cited ID exists, that every cited ID was in the retrieved context, that "these two bugs" is followed by two actual citations, that a NO-GO write-up cites every blocking bug, that a GO write-up cites nothing outside the release, and that the relevance flag and displayed badge never contradict each other.

Every failing case in `test_quality_checks.py` is verbatim text from a real run that shipped past a green checkmark — including a GO recommendation that swapped its own priority counts (`3 low / 4 medium` → `4 low / 3 medium`) while also silently dropping a bug ID.

### Reproducibility

All LLM calls pin `seed` as well as `temperature=0`. Temperature alone doesn't guarantee determinism — small hardware-level floating-point noise can still flip a borderline decision even at `temperature=0`. Without a fixed seed, the same query could flip a bug between `related` and `not_relevant` across runs, which makes report-to-report diffing meaningless, because you can't tell a real regression from sampling noise. Override with `OLLAMA_SEED=0` if you want to deliberately sample variability.

---

## Troubleshooting

**`Connection refused` to Ollama** — make sure `ollama serve` is running. On macOS the app handles this automatically; on Linux you may need to start it manually.

**`Collection not found`** — you forgot to run `python3 ingest.py` after fetching/preparing your CSV.

**A custom field came back empty on every row** — `jira_fetch.py`'s sanity check will warn you by name. This usually means the field's internal ID (`customfield_XXXXX`) has changed on the Jira project; re-confirm the correct ID via Jira's `/rest/api/3/field` endpoint and update the `FIELD_ID_*` constants at the top of the file.

**Slow synthesis** — Ollama performance depends on your local hardware. Try a smaller model like `gemma2:2b` or `qwen2.5:3b` for the demo.

**Verification pass is slow** — the retrieve step makes one LLM call per retrieved bug, concurrently, via a thread pool. Ollama defaults to `OLLAMA_NUM_PARALLEL=1`, which serializes them server-side no matter how many the client sends. Set `OLLAMA_NUM_PARALLEL=4` before `ollama serve` starts to actually get the concurrency.

**Similarity scores look wrong after a re-ingest** — `ingest.py` pins `hnsw:space` to cosine. A collection built without it falls back to Chroma's default squared-L2, which is a silent scale change rather than an error — retrieval still "works," but every similarity threshold in `search.py` quietly starts meaning something else. `search.py` reads the metric back off the collection and converts accordingly, but a stale collection should just be rebuilt.

**MCP tool returns different results than the Streamlit app for the "same" question** — check what text actually reached the tool call. AI assistants sometimes rephrase before calling `search_bugs`, and different phrasing produces different embeddings and different results. Use the Streamlit UI for precision demos where exact wording matters.
