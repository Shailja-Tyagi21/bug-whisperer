"""
ingest.py
---------
One-time (or re-runnable) ingestion of bug history into a local ChromaDB
vector store. Reads a CSV, embeds the meaningful text fields using a local
Ollama embedding model (nomic-embed-text, 768-dim vectors), and persists
the collection to disk.

Run:
    python ingest.py
    python ingest.py --csv my_bugs.csv
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pandas as pd
import chromadb
import ollama
from dotenv import load_dotenv

# ---- Config ----------------------------------------------------------------
load_dotenv()

EMBEDDING_MODEL = "nomic-embed-text"   # 768-dim vectors, pulled via `ollama pull nomic-embed-text`
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
CHROMA_DIR = "./chroma_db"
COLLECTION_NAME = "bugs"
DEFAULT_CSV = "sample_bugs.csv"
EMBED_BATCH_SIZE = 50   # Ollama accepts arrays too; smaller batches keep memory/latency sane locally

# Embedding cache: text-hash -> embedding vector, persisted to disk. This
# is a speed optimization for re-ingestion (skip recomputing embeddings
# for bugs whose text hasn't changed since the last run) -- it does NOT
# fix the "stale chroma_db" issue on its own, since ChromaDB's own
# on-disk state is still what gets read at query time. See
# _rebuild_chroma_dir() below for the actual fix for that.
EMBED_CACHE_PATH = "./embedding_cache.json"
# ---------------------------------------------------------------------------

_ollama_client = None


def get_ollama_client() -> ollama.Client:
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = ollama.Client(host=OLLAMA_HOST)
    return _ollama_client


def build_text_for_embedding(row: pd.Series) -> str:
    """Combine the meaningful fields into a single blob.

    What we include here directly affects retrieval quality. Comments
    usually hold the gold (root cause discussions), so include them.
    """
    parts = [
        f"Title: {row.get('Title', '')}",
        f"Component: {row.get('Component', '')}",
        f"Description: {row.get('Description', '')}",
    ]
    if pd.notna(row.get("Comments")) and str(row["Comments"]).strip():
        parts.append(f"Comments: {row['Comments']}")
    if pd.notna(row.get("Resolution")) and str(row["Resolution"]).strip():
        parts.append(f"Resolution: {row['Resolution']}")
    return "\n".join(parts)


def build_metadata(row: pd.Series) -> dict:
    """Metadata stored alongside each vector. Used for display and filtering."""
    resolution = row.get("Resolution", "")
    resolution = "" if pd.isna(resolution) else str(resolution)
    return {
        "bug_id": str(row["ID"]),
        "title": str(row.get("Title", "")),
        "component": str(row.get("Component", "")),
        "severity": str(row.get("Severity", "")),
        "priority": str(row.get("Priority", "")),
        "status": str(row.get("Status", "")),
        "release_version": str(row.get("ReleaseVersion", "")),
        # Truncate long resolutions so metadata stays small
        "resolution": resolution[:500],
        "created": str(row.get("Created", "")),
    }


def _text_hash(text: str) -> str:
    """Content hash used as the embedding cache key. Includes the
    embedding model name so switching models can't silently return a
    stale cached vector computed by a different model."""
    return hashlib.sha256(f"{EMBEDDING_MODEL}::{text}".encode("utf-8")).hexdigest()


def _load_embed_cache() -> dict:
    if Path(EMBED_CACHE_PATH).exists():
        try:
            with open(EMBED_CACHE_PATH) as f:
                return json.load(f)
        except Exception as e:
            print(f"      ⚠️  couldn't load embedding cache, starting fresh: "
                  f"{type(e).__name__}: {e}")
    return {}


def _save_embed_cache(cache: dict) -> None:
    try:
        with open(EMBED_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"      ⚠️  couldn't save embedding cache: {type(e).__name__}: {e}")


def embed_batch(client: ollama.Client, texts: list, batch_size: int = EMBED_BATCH_SIZE) -> list:
    """Embed a list of texts using a local Ollama embedding model, batching
    for efficiency, and reusing cached vectors for text that hasn't
    changed since a previous ingest run.

    Requires the model to be pulled first: `ollama pull nomic-embed-text`.
    """
    cache = _load_embed_cache()
    hashes = [_text_hash(t) for t in texts]

    # Figure out what's actually new -- only these get sent to Ollama.
    to_embed_idx = [i for i, h in enumerate(hashes) if h not in cache]
    hit_count = len(texts) - len(to_embed_idx)
    if hit_count:
        print(f"      Embedding cache: {hit_count}/{len(texts)} unchanged, "
              f"reusing cached vectors.")

    if to_embed_idx:
        to_embed_texts = [texts[i] for i in to_embed_idx]
        for i in range(0, len(to_embed_texts), batch_size):
            batch = to_embed_texts[i:i + batch_size]
            print(f"      Embedding batch {i // batch_size + 1} "
                  f"({len(batch)} new/changed texts)...")
            try:
                response = client.embed(model=EMBEDDING_MODEL, input=batch)
            except ollama.ResponseError as e:
                sys.exit(
                    f"Ollama embedding call failed: {e}\n"
                    f"Is 'ollama serve' running, and have you run "
                    f"'ollama pull {EMBEDDING_MODEL}'?"
                )
            for j, emb in enumerate(response.embeddings):
                idx = to_embed_idx[i + j]
                cache[hashes[idx]] = emb

        _save_embed_cache(cache)

    return [cache[h] for h in hashes]


def _rebuild_chroma_dir() -> None:
    """Guarantee a genuinely clean rebuild by deleting the ENTIRE chroma_db
    directory on disk before recreating it, rather than relying on
    chroma_client.delete_collection() alone.

    Why: delete_collection() removes the collection's metadata entry, but
    the underlying on-disk index segments don't always get fully purged --
    in practice this showed up as ingest.py appearing to "succeed" while
    stale bug IDs from a previous ingest were still being returned by
    queries, and the only reliable fix was manually deleting the whole
    chroma_db folder. This does that automatically, every run, so nobody
    has to remember to do it by hand again.

    A second real cause of the same symptom is concurrent access: if
    Streamlit (or anything else holding a PersistentClient connection to
    this same directory) is still running while ingest.py executes, you
    get two independent connections writing to the same on-disk store at
    once, which is exactly the kind of situation SQLite-backed storage
    handles poorly. This function can't fix that part -- make sure
    Streamlit is fully stopped before running ingest.py.
    """
    chroma_path = Path(CHROMA_DIR)
    if chroma_path.exists():
        print(f"      Removing existing {CHROMA_DIR} directory entirely "
              f"(not just the collection) for a guaranteed-clean rebuild...")
        shutil.rmtree(chroma_path)


def main():
    parser = argparse.ArgumentParser(description="Ingest bugs into BugLens")
    parser.add_argument("--csv", default=DEFAULT_CSV, help="Path to bug CSV")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Ignore the embedding cache and recompute every vector fresh.",
    )
    args = parser.parse_args()

    if args.no_cache and Path(EMBED_CACHE_PATH).exists():
        Path(EMBED_CACHE_PATH).unlink()
        print("Cleared embedding cache (--no-cache).")

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    print(f"[1/4] Loading bugs from {csv_path}...")
    df = pd.read_csv(csv_path)
    print(f"      Loaded {len(df)} bugs.")
    required = {"ID", "Title", "Description"}
    missing = required - set(df.columns)
    if missing:
        sys.exit(f"CSV missing required columns: {missing}")

    print(f"[2/4] Connecting to Ollama at {OLLAMA_HOST}...")
    ollama_client = get_ollama_client()
    try:
        ollama_client.list()
    except Exception as e:
        sys.exit(
            f"Could not reach Ollama at {OLLAMA_HOST}: {e}\n"
            f"Make sure 'ollama serve' is running."
        )

    print(f"[3/4] Building text + computing embeddings for {len(df)} bugs "
          f"via '{EMBEDDING_MODEL}'...")
    texts = [build_text_for_embedding(row) for _, row in df.iterrows()]
    embeddings = embed_batch(ollama_client, texts)
    metadatas = [build_metadata(row) for _, row in df.iterrows()]
    ids = [str(row["ID"]) for _, row in df.iterrows()]

    print(f"[4/4] Rebuilding ChromaDB at '{CHROMA_DIR}'...")
    # NOTE: switching embedding models changes vector dimensionality (e.g.
    # nomic-embed-text is 768-dim, OpenAI's text-embedding-3-small was
    # 1536-dim). Different-dim vectors CANNOT coexist in the same Chroma
    # collection -- always delete and recreate on ingestion (which we do
    # below, via a full directory wipe rather than delete_collection()
    # alone -- see _rebuild_chroma_dir()'s docstring for why).
    _rebuild_chroma_dir()

    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    # Pin the distance metric explicitly. Leaving this off falls back to
    # Chroma's default (squared L2), which is a silent behaviour change rather
    # than an error: retrieval still "works", but every similarity threshold in
    # search.py -- which are all expressed in cosine units -- quietly starts
    # meaning something else. search.py reads this metric back off the
    # collection and converts distances accordingly, so the two stay in sync.
    collection = chroma_client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    collection.add(
        embeddings=embeddings,
        documents=texts,
        metadatas=metadatas,
        ids=ids,
    )

    # Sanity check: confirm what actually landed in the fresh collection
    # matches what we just tried to write, rather than trusting that
    # collection.add() silently succeeded.
    stored_count = collection.count()
    if stored_count != len(ids):
        print(f"      ⚠️  WARNING: wrote {len(ids)} bugs but collection now "
              f"reports {stored_count}. Something didn't land correctly.")
    else:
        print(f"      Verified: collection now contains exactly "
              f"{stored_count} bugs, matching the CSV.")

    print(f"\nDone. Stored {len(ids)} bugs in collection '{COLLECTION_NAME}'.")
    print(f"Embedding dimension: {len(embeddings[0])}")
    print(f"Next step: streamlit run app.py")


if __name__ == "__main__":
    main()
