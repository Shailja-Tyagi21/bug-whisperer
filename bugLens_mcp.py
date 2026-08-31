"""
bugLens_mcp.py
---------------------
MCP (Model Context Protocol) server for BugLens.

Exposes three tools that any MCP-compatible AI assistant (GitHub Copilot,
Claude Code, Claude Desktop, Cursor, Windsurf, etc.) can call:

    search_bugs      - natural-language search over defect history
    check_release     - GO/NO-GO release readiness gate
    list_releases     - available release versions for check_release

The tools wrap search.py directly — same retrieval, same verification,
same quality guardrails. No separate API server needed; MCP runs over
stdio, so the host process (VS Code, Claude Desktop, etc.) launches
this script as a subprocess and communicates via stdin/stdout.

SETUP:
    pip install "mcp[cli]"

TEST WITH MCP INSPECTOR:
    mcp dev bugLens_mcp.py

INSTALL INTO CLAUDE DESKTOP:
    mcp install bugLens_mcp.py

CONFIGURE IN VS CODE (settings.json or .vscode/mcp.json):
    {
      "mcpServers": {
        "buglens": {
          "command": "python3",
          "args": ["<full-path-to>/bugLens_mcp.py"],
          "env": {
            "JIRA_URL": "https://hackathon-team-michelin.atlassian.net"
          }
        }
      }
    }
"""
import json
import sys
import os
from pathlib import Path

# Ensure search.py is importable — it lives in the same directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server import MCPServer

from search import (
    ask,
    check_release_readiness,
    list_release_versions,
    list_all_bug_ids,
    JIRA_BASE_URL,
)

mcp = MCPServer("BugLens")


@mcp.tool()
def search_bugs(query: str, num_results: int = 5) -> str:
    """Search the team's defect history using natural language (BugLens).

    Ask any question about past bugs — root causes, how things were fixed,
    which components were affected, whether a specific failure has happened
    before. The search uses semantic similarity over embedded Jira tickets,
    then verifies each match with a separate LLM pass to filter false
    positives.

    Examples:
        "PayPal redirect issue after checkout"
        "has our refund process had any silent failures?"
        "how did we fix the memory leak in the worker service?"
        "any performance issues with dashboards loading slowly?"
        "do we have any Bluetooth pairing bugs?"

    Args:
        query: Natural language question about bug history.
        num_results: Number of candidate bugs to retrieve and verify (1-10, default 5).
    """
    k = max(1, min(10, num_results))
    answer, bugs = ask(query, k=k)

    # Format the response for the AI assistant to read and relay.
    parts = [f"**Answer:** {answer}", ""]

    if bugs:
        parts.append("**Source Bugs:**")
        for b in bugs:
            v = b.get("verification", {})
            meta = b.get("metadata", {})
            action = v.get("suggested_action", "unknown")
            confidence = v.get("confidence", "?")
            is_relevant = v.get("is_relevant", False)
            jira_link = f"{JIRA_BASE_URL}{b['bug_id']}"

            relevance_marker = "✅" if is_relevant else "❌"
            parts.append(
                f"- {relevance_marker} **{b['bug_id']}** — {meta.get('title', '')} "
                f"(similarity {b['similarity']:.2f}, {action}, "
                f"{confidence} confidence)"
            )
            parts.append(f"  JIRA: {jira_link}")
            if is_relevant and v.get("reasoning"):
                parts.append(f"  Reasoning: {v['reasoning']}")

    return "\n".join(parts)


@mcp.tool()
def check_release(version: str) -> str:
    """Check whether a release version is ready to ship.

    Returns GO or NO-GO based on a deterministic rule: NO-GO if any bug
    tagged with this release version is still open AND has High or Blocker
    priority. The decision is made in Python, not by the LLM — the LLM
    only writes the explanation.

    Args:
        version: Release version string, e.g. "v2.5.0" or "v2.6.0".
                 Use list_releases to see available versions.
    """
    available = list_release_versions()
    if version not in available:
        return (
            f"Version '{version}' not found. "
            f"Available versions: {', '.join(available)}"
        )

    result = check_release_readiness(version)
    decision = result["decision"]
    recommendation = result["recommendation"]

    parts = [f"**{decision}** — {version}", "", recommendation, ""]

    if result["blocking_bugs"]:
        parts.append(f"**Blocking bugs ({len(result['blocking_bugs'])}):**")
        for b in result["blocking_bugs"]:
            meta = b["metadata"]
            jira_link = f"{JIRA_BASE_URL}{b['bug_id']}"
            parts.append(
                f"- **{b['bug_id']}** — {meta.get('title', '')} "
                f"({meta.get('priority', '?')} priority, {meta.get('status', '?')})"
            )
            parts.append(f"  JIRA: {jira_link}")

    all_bugs = result.get("all_bugs", [])
    open_bugs = [
        b for b in all_bugs
        if b["metadata"].get("status", "").lower()
        not in ("done", "closed", "resolved")
    ]
    if open_bugs:
        parts.append(f"\n**Open bugs in {version} ({len(open_bugs)}):**")
        for b in open_bugs:
            meta = b["metadata"]
            jira_link = f"{JIRA_BASE_URL}{b['bug_id']}"
            parts.append(
                f"- **{b['bug_id']}** — {meta.get('title', '')} "
                f"({meta.get('priority', '?')}, {meta.get('status', '?')})"
            )
            parts.append(f"  JIRA: {jira_link}")

    parts.append(f"\nTotal bugs tagged {version}: {len(all_bugs)} "
                  f"({len(open_bugs)} open, {len(all_bugs) - len(open_bugs)} resolved)")

    return "\n".join(parts)


@mcp.tool()
def list_releases() -> str:
    """List all release versions available for readiness checks.

    Use the returned version strings as input to check_release.
    """
    versions = list_release_versions()
    if not versions:
        return "No release versions found. Has ingest.py been run?"
    return "Available release versions:\n" + "\n".join(f"- {v}" for v in versions)


if __name__ == "__main__":
    mcp.run()
