"""
app.py
------
Streamlit UI for Bug Whisperer.

Run:
    streamlit run app.py

Opens at http://localhost:8501
"""

import streamlit as st

from search import (
    retrieve, synthesize, JIRA_BASE_URL, LLM_MODEL,
    list_release_versions, check_release_readiness,
)

# ---- Page setup ------------------------------------------------------------
st.set_page_config(
    page_title="Bug Whisperer",
    page_icon="🐛",
    layout="wide",
)

st.title("🐛 Bug Whisperer")
st.caption(
    "Ask anything about historical bugs, or check whether a release is "
    "clear to ship."
)

# ---- Sidebar ---------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    k = st.slider(
        "Bugs to retrieve",
        min_value=3, max_value=15, value=5,
        help="More = broader context but slower synthesis + verification.",
    )
    show_raw = st.checkbox("Show raw retrieved text", value=False)

    st.divider()
    st.markdown(f"**LLM:** `{LLM_MODEL}` (Ollama, local)")
    st.markdown("**Embeddings:** `nomic-embed-text` (Ollama, local)")
    st.markdown("**Vector store:** ChromaDB (local)")
    st.markdown("**Verification:** independent LLM pass per retrieved bug")

    st.divider()
    st.markdown("**Try asking:**")
    st.markdown(
        "- Have we seen payment failures with international cards?\n"
        "- What causes login flakiness on Safari?\n"
        "- How have we fixed memory leaks in the worker service?\n"
        "- Any known issues with exporting large files?\n"
        "- Have we shipped duplicate notification bugs before?"
    )

# ---- Helpers ---------------------------------------------------------------
def verification_badge(v: dict) -> str:
    """Return an emoji/colored label for a verification result."""
    if not v:
        return ""
    if v.get("error"):
        return "⚠️ Verification failed (not a real judgment)"
    action = v.get("suggested_action", "")
    confidence = v.get("confidence", "?")
    if action == "duplicate":
        return f"🟢 Same root cause ({confidence})"
    if action == "related":
        return f"🟡 Related, not same ({confidence})"
    if action == "not_relevant":
        return f"🔴 False positive ({confidence})"
    return f"⚪ {action} ({confidence})"


def decision_badge(decision: str) -> str:
    """Return an emoji/colored label for a release go/no-go decision."""
    if decision == "GO":
        return "✅ GO"
    if decision == "NO-GO":
        return "🛑 NO-GO"
    return "❓ UNKNOWN"


search_tab, release_tab = st.tabs(["🔍 Search bug history", "🚦 Release readiness"])


# ---- Search tab -------------------------------------------------------------
with search_tab:
    query = st.text_input(
        "Your question:",
        placeholder="e.g., have we seen checkout errors with PayPal?",
    )

    if query:
        with st.spinner("Searching bug history..."):
            bugs = retrieve(query, k=k, verify=True)

        with st.spinner(f"Asking {LLM_MODEL} to synthesize..."):
            answer = synthesize(query, bugs)

        # ---- Answer ----------------------------------------------------------
        st.markdown("### Answer")
        st.markdown(answer)

        # ---- Sources -----------------------------------------------------------
        st.markdown("### Source Bugs")
        if not bugs:
            st.info("No matching bugs found.")
        else:
            # Sort so verified-relevant bugs appear first
            bugs_sorted = sorted(
                bugs,
                key=lambda b: (
                    not b.get("verification", {}).get("is_relevant", False),
                    -b["similarity"],
                ),
            )

            for b in bugs_sorted:
                meta = b["metadata"]
                v = b.get("verification", {})
                header = (
                    f"[{b['bug_id']}] {meta['title']} "
                    f"— similarity {b['similarity']:.2f} — {verification_badge(v)}"
                )
                with st.expander(header):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.markdown(f"**Component**\n\n{meta.get('component', '-')}")
                    c2.markdown(f"**Severity**\n\n{meta.get('severity', '-')}")
                    c3.markdown(f"**Status**\n\n{meta.get('status', '-')}")
                    c4.markdown(
                        f"**JIRA**\n\n[Open {b['bug_id']}]"
                        f"({JIRA_BASE_URL}{b['bug_id']})"
                    )

                    if v:
                        st.markdown("**Verification**")
                        st.markdown(
                            f"- **Action:** {v.get('suggested_action', '-')}\n"
                            f"- **Confidence:** {v.get('confidence', '-')}\n"
                            f"- **Reasoning:** {v.get('reasoning', '-')}"
                        )

                    if meta.get("resolution"):
                        st.markdown(f"**Resolution:** {meta['resolution']}")
                    if show_raw:
                        st.code(b["document"])
    else:
        st.info("Type a question above to get started.")

# ---- Release readiness tab --------------------------------------------------
with release_tab:
    st.markdown(
        "Checks a release against a fixed rule: **NO-GO if any bug tagged "
        "with this release is still open (not Closed) and is High or "
        "Blocker priority.** The go/no-go call itself is a plain rule, not "
        "a model guess — only the explanation below it is LLM-written."
    )

    versions = list_release_versions()
    if not versions:
        st.warning("No release versions found. Run ingest.py first.")
    else:
        selected_version = st.selectbox("Release version:", versions, index=len(versions) - 1)

        if st.button("Check readiness", type="primary"):
            with st.spinner(f"Evaluating {selected_version}..."):
                result = check_release_readiness(selected_version)

            st.markdown(f"## {decision_badge(result['decision'])} — {result['release_version']}")
            st.markdown(result["recommendation"])

            if result["blocking_bugs"]:
                st.markdown("### Blocking bugs")
                for b in result["blocking_bugs"]:
                    meta = b["metadata"]
                    with st.expander(
                        f"[{b['bug_id']}] {meta.get('title', '')} "
                        f"— {meta.get('priority', '?')} priority — {meta.get('status', '?')}"
                    ):
                        st.markdown(f"**Component:** {meta.get('component', '-')}")
                        st.markdown(f"**Severity:** {meta.get('severity', '-')}")
                        st.markdown(
                            f"**JIRA:** [Open {b['bug_id']}]"
                            f"({JIRA_BASE_URL}{b['bug_id']})"
                        )
                        st.code(b["document"])

            if result["all_bugs"]:
                st.markdown(f"### All {len(result['all_bugs'])} bugs tagged with {selected_version}")
                st.dataframe(
                    [
                        {
                            "ID": b["bug_id"],
                            "Title": b["metadata"].get("title", ""),
                            "Component": b["metadata"].get("component", ""),
                            "Priority": b["metadata"].get("priority", ""),
                            "Status": b["metadata"].get("status", ""),
                        }
                        for b in result["all_bugs"]
                    ],
                    use_container_width=True,
                    hide_index=True,
                )
