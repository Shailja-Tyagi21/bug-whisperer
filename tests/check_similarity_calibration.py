"""
check_similarity_calibration.py
--------------------------------
Answers one question: is _DUPLICATE_GUARDRAIL_SIMILARITY_FLOOR still sitting
where it needs to sit?

The floor (0.75) exists to separate two real cases found during manual
testing, both of which are already frozen as unit tests in
tests/test_guardrails.py:

  - BUG-1002 (PayPal return_url) is a GENUINE duplicate of the PayPal
    query. Its reasoning uses "related to" as an ordinary connector word.
    It must land ABOVE the floor so the guardrail leaves it alone.

  - BUG-1053 (tyre search) is a same-component FALSE match for the tyre
    search popup query. It must land BELOW the floor so the guardrail
    downgrades it from "duplicate" to "related".

test_guardrails.py passes those similarity values in by hand (0.84 and
0.57), so it will keep passing no matter what the live stack actually
produces. That's a real gap: nothing checks that a live query still yields
similarities on the correct side of the floor. If the embedding model, the
distance metric, or the corpus changes, the floor can quietly stop
separating anything and every test stays green.

This script closes that gap. Run it after ANY re-ingest, embedding model
change, or hnsw:space change.

Run:
    python3 tests/check_similarity_calibration.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import search  # noqa: E402
from search import (  # noqa: E402
    _DUPLICATE_GUARDRAIL_SIMILARITY_FLOOR as FLOOR,
    embed_query,
    get_distance_space,
    retrieve,
)

# (query, bug_id, expected_side_of_floor)
# "above" = must be treated as a genuine duplicate candidate
# "below" = must be catchable by the guardrail as a same-component false match
ANCHORS = [
    ("PayPal redirect issue after checkout", "SCRUM-9", "above"),
    ("Are there known issues with the tyre search popup?", "SCRUM-29", "below"),
]

# How much clearance we want on either side of the floor. A value that only
# just clears it will flip on the next model or corpus change.
MIN_MARGIN = 0.03


def check_normalization() -> bool:
    """Both distance->similarity formulas in search.py return true cosine
    similarity ONLY if the embedding vectors are unit length. If they aren't,
    the numbers the floor is compared against are not cosine similarity at
    all, and the threshold means whatever it happens to mean."""
    vec = embed_query("connection pool exhausted during import job")
    norm = sum(x * x for x in vec) ** 0.5
    normalized = abs(norm - 1.0) < 0.01
    print(f"  embedding dimension : {len(vec)}")
    print(f"  L2 norm             : {norm:.4f} "
          f"({'unit-normalized ✅' if normalized else 'NOT normalized ⚠️'})")
    if not normalized:
        print("     ⚠️  Vectors are not unit length, so distance_to_similarity()")
        print("         is not returning true cosine similarity. The anchor")
        print("         check below is the thing that actually matters — but")
        print("         expect the raw numbers to differ from the 0.84/0.57")
        print("         values recorded in test_guardrails.py.")
    return normalized


def check_anchors() -> list:
    """Measure the two anchor cases against the live stack."""
    failures = []
    for query, bug_id, expected_side in ANCHORS:
        # verify=False -- we only want retrieval similarity here, no LLM calls.
        bugs = retrieve(query, k=10, verify=False)
        match = next((b for b in bugs if b["bug_id"] == bug_id), None)

        print(f"\n  Query : {query!r}")
        if match is None:
            top = ", ".join(b["bug_id"] for b in bugs[:5])
            print(f"    ❌ {bug_id} not in top 10. Got: {top}")
            failures.append(f"{bug_id} no longer retrieved for {query!r}")
            continue

        sim = match["similarity"]
        rank = bugs.index(match) + 1
        margin = sim - FLOOR
        side = "above" if sim >= FLOOR else "below"
        ok = side == expected_side
        icon = "✅" if ok else "❌"

        print(f"    {icon} {bug_id}: similarity {sim:.3f} (rank {rank}) — "
              f"{side} the floor, expected {expected_side}")

        if not ok:
            failures.append(
                f"{bug_id} scored {sim:.3f}, which is {side} the floor "
                f"{FLOOR} but should be {expected_side}"
            )
        elif abs(margin) < MIN_MARGIN:
            print(f"    ⚠️  only {abs(margin):.3f} clear of the floor "
                  f"(want >{MIN_MARGIN}) — fragile, consider re-tuning")

    return failures


def main():
    print("Similarity calibration check\n" + "=" * 46)
    print(f"\nCollection distance metric : {get_distance_space()!r}")
    print(f"Embedding model            : {search.EMBEDDING_MODEL}")
    print(f"Guardrail floor            : {FLOOR}\n")

    print("Embedding normalization:")
    check_normalization()

    print("\nAnchor cases:")
    failures = check_anchors()

    print("\n" + "=" * 46)
    if failures:
        print("❌ CALIBRATION FAILED\n")
        for f in failures:
            print(f"  - {f}")
        print(f"\nThe floor ({FLOOR}) no longer separates a genuine duplicate")
        print("from a same-component false match. Either re-tune it to sit")
        print("between the two measured values above, or update the anchors")
        print("in this file and in tests/test_guardrails.py if the intended")
        print("behaviour has genuinely changed.")
        sys.exit(1)

    print(f"✅ Floor {FLOOR} still separates both anchor cases correctly.")


if __name__ == "__main__":
    main()
