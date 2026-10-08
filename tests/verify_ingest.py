"""
verify_ingest.py
----------------
One-shot health check after fetch + ingest. Cross-checks the CSV, the ChromaDB
collection, the embedding cache and the release data against each other, and
exits 1 if anything is wrong. No LLM calls (the optional --query check only
needs the Ollama EMBEDDING model).

Run from the project root, after:
    python3 jira_fetch.py
    python3 ingest.py --csv jira_bugs.csv

Then:
    python3 tests/verify_ingest.py
    python3 tests/verify_ingest.py --expect SCRUM-65
    python3 tests/verify_ingest.py --expect SCRUM-65 \
        --query "invoice shows wrong tax rate for India customers" --top 1

Options:
    --csv PATH       CSV that was ingested (default: jira_bugs.csv)
    --expect ID ...  bug IDs that must exist in the collection
    --query TEXT     run a retrieval (vector + BM25, no verification) ...
    --top N          ... and require the --expect IDs to rank within the top N
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

import ingest  # noqa: E402
import search  # noqa: E402

EXPECTED_DIM = 768
REQUIRED_META = ["bug_id", "title", "component", "severity", "priority",
                 "status", "release_version", "resolution", "created"]
# Metadata that should be filled on every bug (resolution/created may be empty).
FILLED_META = ["title", "component", "severity", "priority", "status",
               "release_version"]

failures: list = []
warnings: list = []


def ok(msg):
    print(f"  ✅ {msg}")


def fail(msg):
    print(f"  ❌ {msg}")
    failures.append(msg)


def warn(msg):
    print(f"  ⚠️  {msg}")
    warnings.append(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="jira_bugs.csv")
    ap.add_argument("--expect", nargs="*", default=[])
    ap.add_argument("--query", default=None)
    ap.add_argument("--top", type=int, default=3)
    args = ap.parse_args()

    # ---- 1. CSV ------------------------------------------------------------
    print("\n[1] CSV")
    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path} (run from the project root)")
    df = pd.read_csv(csv_path)
    csv_ids = [str(i) for i in df["ID"]]
    ok(f"{csv_path}: {len(df)} rows")
    if len(set(csv_ids)) != len(csv_ids):
        fail("duplicate IDs in the CSV")

    # ---- 2. Chroma vs CSV --------------------------------------------------
    print("\n[2] ChromaDB vs CSV")
    try:
        col = search.get_collection()
    except Exception as e:
        sys.exit(f"Could not open the collection ({type(e).__name__}: {e}). "
                 f"Has ingest.py been run, and are you in the project root?")
    data = col.get(include=["documents", "metadatas", "embeddings"])
    chroma_ids = list(data["ids"])
    print(f"      distance metric: {search.get_distance_space()!r}")
    if len(chroma_ids) == len(df):
        ok(f"collection holds {len(chroma_ids)} bugs, same as the CSV")
    else:
        fail(f"collection holds {len(chroma_ids)} bugs but the CSV has {len(df)} "
             f"(did you ingest a different CSV, or run ingest before fetch?)")
    missing = sorted(set(csv_ids) - set(chroma_ids))
    extra = sorted(set(chroma_ids) - set(csv_ids))
    if missing:
        fail(f"in CSV but not in Chroma: {', '.join(missing[:10])}")
    if extra:
        fail(f"in Chroma but not in CSV (stale data?): {', '.join(extra[:10])}")
    if not missing and not extra:
        ok("IDs match exactly")
    if search.get_distance_space() != "cosine":
        fail("collection is not using cosine distance")

    # ---- 3. Record quality -------------------------------------------------
    print("\n[3] Record quality")
    bad_dim, empty_doc, bad_meta, empty_fields, dup_res = [], [], [], {}, []
    for i, bug_id in enumerate(chroma_ids):
        doc = data["documents"][i] or ""
        meta = data["metadatas"][i] or {}
        vec = data["embeddings"][i]
        if len(vec) != EXPECTED_DIM:
            bad_dim.append(bug_id)
        if not doc.strip():
            empty_doc.append(bug_id)
        if [k for k in REQUIRED_META if k not in meta]:
            bad_meta.append(bug_id)
        for k in FILLED_META:
            if not str(meta.get(k, "")).strip():
                empty_fields.setdefault(k, []).append(bug_id)
        if doc.count("Resolution:") > 1:
            dup_res.append(bug_id)
    (fail if bad_dim else ok)(
        f"{len(bad_dim)} record(s) with a wrong embedding dimension: {bad_dim[:5]}"
        if bad_dim else f"all embeddings are {EXPECTED_DIM}-dimensional")
    (fail if empty_doc else ok)(
        f"empty document text: {empty_doc[:5]}" if empty_doc
        else "every record has document text")
    (fail if bad_meta else ok)(
        f"missing metadata keys on: {bad_meta[:5]}" if bad_meta
        else "every record has all metadata keys")
    if empty_fields:
        for k, ids in empty_fields.items():
            warn(f"metadata '{k}' is empty on {len(ids)} bug(s): {ids[:5]}")
    else:
        ok("component / severity / priority / status / release are filled on every bug")
    (fail if dup_res else ok)(
        f"'Resolution:' appears twice in the embedded text of: {dup_res[:5]}"
        if dup_res else "no duplicated Resolution text in any embedded document")

    # ---- 4. Embedding cache ------------------------------------------------
    print("\n[4] Embedding cache")
    cache_path = Path(ingest.EMBED_CACHE_PATH)
    if not cache_path.exists():
        warn(f"{cache_path} not found (was ingest run with --no-cache?)")
    else:
        cache = json.loads(cache_path.read_text())
        ok(f"{cache_path}: {len(cache)} cached vector(s)")
        missing_cache, mismatched = [], []
        for i, bug_id in enumerate(chroma_ids):
            h = ingest._text_hash(data["documents"][i])
            if h not in cache:
                missing_cache.append(bug_id)
                continue
            a, b = cache[h], data["embeddings"][i]
            if len(a) != len(b) or max(abs(x - y) for x, y in zip(a, b)) > 1e-4:
                mismatched.append(bug_id)
        (fail if missing_cache else ok)(
            f"no cache entry for: {missing_cache[:5]}" if missing_cache
            else "every stored document has a cache entry")
        (fail if mismatched else ok)(
            f"cached vector differs from the stored vector for: {mismatched[:5]}"
            if mismatched else "cached vectors match the stored vectors")
        if len(cache) > len(chroma_ids):
            print(f"      (cache holds {len(cache) - len(chroma_ids)} orphan "
                  f"vector(s) from deleted/edited bugs; harmless)")

    stamp = Path(search.CHROMA_DIR) / search.INGEST_STAMP_FILE \
        if hasattr(search, "INGEST_STAMP_FILE") else None
    if stamp is not None:
        (ok if stamp.exists() else warn)(
            "ingest stamp present" if stamp.exists()
            else "no .ingest_stamp (ingest was run with the original ingest.py)")

    # ---- 5. Releases (deterministic gate, no LLM) --------------------------
    print("\n[5] Releases (computed with the same rule as the gate)")
    versions = search.list_release_versions()
    if not versions:
        fail("no release versions found in the collection")
    print(f"      {'version':<10}{'bugs':>5}{'open':>6}{'blocking':>10}  decision")
    for v in versions:
        bugs = search.get_bugs_for_release(v)
        open_b = [b for b in bugs if search._is_open(b)]
        blocking = [b for b in open_b
                    if b["metadata"].get("priority") in search.BLOCKING_PRIORITIES]
        print(f"      {v:<10}{len(bugs):>5}{len(open_b):>6}{len(blocking):>10}  "
              f"{'NO-GO' if blocking else 'GO'}")
    for v, want in (("v2.5.0", "NO-GO"), ("v2.6.0", "GO")):
        if v not in versions:
            warn(f"{v} not in the collection (regression suite expects it)")
            continue
        bugs = search.get_bugs_for_release(v)
        blocking = [b for b in bugs if search._is_open(b)
                    and b["metadata"].get("priority") in search.BLOCKING_PRIORITIES]
        got = "NO-GO" if blocking else "GO"
        (ok if got == want else fail)(
            f"{v} is {got} (expected {want})")

    # ---- 6. Expected bugs / retrieval -------------------------------------
    if args.expect:
        print("\n[6] Expected bugs")
        for bug_id in args.expect:
            if bug_id in chroma_ids:
                m = data["metadatas"][chroma_ids.index(bug_id)]
                ok(f"{bug_id} is in the collection — {m.get('title')} "
                   f"[{m.get('release_version')}, {m.get('priority')}, "
                   f"{m.get('status')}]")
            else:
                fail(f"{bug_id} is NOT in the collection")

    if args.query:
        print(f"\n[7] Retrieval: {args.query!r}")
        try:
            hits = search.retrieve(args.query, k=max(args.top, 5), verify=False)
        except Exception as e:
            fail(f"retrieval failed ({type(e).__name__}: {e}) — is Ollama running?")
            hits = []
        for rank, b in enumerate(hits, 1):
            print(f"      {rank}. {b['bug_id']:<10} sim {b['similarity']:.3f}  "
                  f"{b['metadata'].get('title', '')[:60]}")
        for bug_id in args.expect:
            ids = [b["bug_id"] for b in hits]
            if bug_id in ids and ids.index(bug_id) < args.top:
                ok(f"{bug_id} ranks #{ids.index(bug_id) + 1} (within top {args.top})")
            elif hits:
                fail(f"{bug_id} is not in the top {args.top} for this query")

    # ---- summary -----------------------------------------------------------
    print("\n" + "=" * 50)
    print(f"{len(failures)} failure(s), {len(warnings)} warning(s)")
    if failures:
        print("❌ VERIFICATION FAILED")
        sys.exit(1)
    print("✅ Everything cross-checks.")


if __name__ == "__main__":
    main()
