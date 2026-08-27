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
import os
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


def embed_batch(client: ollama.Client, texts: list, batch_size: int = EMBED_BATCH_SIZE) -> list:
    """Embed a list of texts using a local Ollama embedding model, batching for efficiency.

    Requires the model to be pulled first: `ollama pull nomic-embed-text`.
    """
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        print(f"      Embedding batch {i // batch_size + 1} "
              f"({len(batch)} texts)...")
        try:
            response = client.embed(model=EMBEDDING_MODEL, input=batch)
        except ollama.ResponseError as e:
            sys.exit(
                f"Ollama embedding call failed: {e}\n"
                f"Is 'ollama serve' running, and have you run "
                f"'ollama pull {EMBEDDING_MODEL}'?"
            )
        all_embeddings.extend(response.embeddings)
    return all_embeddings


def main():
    parser = argparse.ArgumentParser(description="Ingest bugs into BugLens")
    parser.add_argument("--csv", default=DEFAULT_CSV, help="Path to bug CSV")
    args = parser.parse_args()

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

    print(f"[4/4] Writing to ChromaDB at '{CHROMA_DIR}'...")
    # NOTE: switching embedding models changes vector dimensionality (e.g.
    # nomic-embed-text is 768-dim, OpenAI's text-embedding-3-small was
    # 1536-dim). Different-dim vectors CANNOT coexist in the same Chroma
    # collection — always delete and recreate on ingestion (which we do below).
    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    try:
        chroma_client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
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

    print(f"\nDone. Stored {len(ids)} bugs in collection '{COLLECTION_NAME}'.")
    print(f"Embedding dimension: {len(embeddings[0])}")
    print(f"Next step: streamlit run app.py")


if __name__ == "__main__":
    main()
