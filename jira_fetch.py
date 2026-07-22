"""
jira_fetch.py
-------------
Fetch bugs from JIRA Cloud via the REST API and write a CSV that ingest.py
can consume.

Uses the new `/rest/api/3/search/jql` endpoint (the legacy `/search` was
fully removed in 2025) with `nextPageToken` pagination.

Setup:
    1. Copy .env.example to .env and fill in your values:
       cp .env.example .env
    2. Run:
       python3 jira_fetch.py
    3. Then ingest the results:
       python3 ingest.py --csv jira_bugs.csv
"""

import csv
import os
import sys
import time

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

load_dotenv()

# ---- Config (from .env) ----------------------------------------------------
JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_JQL = os.getenv(
    "JIRA_JQL",
    "issuetype = Bug AND statusCategory = Done ORDER BY created DESC",
)
OUTPUT_CSV = os.getenv("OUTPUT_CSV", "jira_bugs.csv")
MAX_BUGS = int(os.getenv("MAX_BUGS", "500"))

PAGE_SIZE = 50  # JIRA Cloud caps maxResults for /search/jql
REQUEST_TIMEOUT = 30
# ---------------------------------------------------------------------------


def adf_to_text(node) -> str:
    """Flatten Atlassian Document Format (ADF) JSON into plain text.

    JIRA Cloud stores descriptions and comments as a nested JSON tree
    (paragraphs, headings, lists, code blocks, etc). For embedding we just
    want plain text. This handles the common node types and falls through
    to recursion for anything else.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(item) for item in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    content = node.get("content", [])

    if node_type == "text":
        return node.get("text", "")
    if node_type in ("hardBreak", "rule"):
        return "\n"
    if node_type in ("paragraph", "heading", "blockquote"):
        return adf_to_text(content) + "\n"
    if node_type in ("bulletList", "orderedList"):
        items = [f"- {adf_to_text(item).strip()}" for item in content]
        return "\n".join(items) + "\n"
    if node_type == "listItem":
        return adf_to_text(content)
    if node_type == "codeBlock":
        return f"\n{adf_to_text(content)}\n"
    if node_type == "mention":
        attrs = node.get("attrs", {})
        return f"@{attrs.get('text', attrs.get('displayName', 'user'))}"

    # Default: just recurse into whatever children exist
    return adf_to_text(content)


def fetch_page(auth: HTTPBasicAuth, next_page_token: str | None = None) -> dict:
    """Fetch one page of issues from the new /search/jql endpoint."""
    url = f"{JIRA_URL}/rest/api/3/search/jql"
    payload = {
        "jql": JIRA_JQL,
        "fields": [
            "summary",
            "description",
            "comment",
            "resolution",
            "components",
            "priority",
            "status",
            "created",
            "issuetype",
        ],
        "maxResults": PAGE_SIZE,
    }
    if next_page_token:
        payload["nextPageToken"] = next_page_token

    response = requests.post(
        url,
        json=payload,
        auth=auth,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def issue_to_row(issue: dict) -> dict:
    """Convert a JIRA issue JSON into the CSV row format ingest.py expects."""
    key = issue.get("key", "")
    fields = issue.get("fields", {}) or {}

    title = (fields.get("summary") or "").strip()
    description = adf_to_text(fields.get("description")).strip()

    # Comments - the most semantically valuable content
    comment_field = fields.get("comment") or {}
    comments = comment_field.get("comments", []) if isinstance(comment_field, dict) else []
    comment_chunks = []
    for c in comments:
        author = (c.get("author") or {}).get("displayName", "Someone")
        body = adf_to_text(c.get("body")).strip()
        if body:
            comment_chunks.append(f"{author}: {body}")
    comments_text = " || ".join(comment_chunks)

    # Resolution: prefer the resolution field; if it's just "Done"/"Fixed",
    # fall back to the last comment (which usually contains the fix narrative).
    resolution_name = ""
    res = fields.get("resolution")
    if isinstance(res, dict):
        resolution_name = res.get("name", "") or ""
    if resolution_name in ("", "Done", "Fixed", "Resolved") and comment_chunks:
        resolution_name = comment_chunks[-1][:500]

    # Components -> comma-separated names
    component_list = fields.get("components") or []
    components = ", ".join(c.get("name", "") for c in component_list if isinstance(c, dict))

    # Priority -> severity column
    priority = ""
    pri = fields.get("priority")
    if isinstance(pri, dict):
        priority = pri.get("name", "") or ""

    # Status name
    status = ""
    st = fields.get("status")
    if isinstance(st, dict):
        status = st.get("name", "") or ""

    # Created date - keep just YYYY-MM-DD
    created = (fields.get("created") or "")[:10]

    return {
        "ID": key,
        "Title": title,
        "Description": description,
        "Comments": comments_text,
        "Resolution": resolution_name,
        "Component": components,
        "Severity": priority,
        "Status": status,
        "Created": created,
    }


def main():
    if not (JIRA_URL and JIRA_EMAIL and JIRA_API_TOKEN):
        sys.exit(
            "Missing credentials. Create a .env file (see .env.example) with:\n"
            "  JIRA_URL=https://your-company.atlassian.net\n"
            "  JIRA_EMAIL=you@example.com\n"
            "  JIRA_API_TOKEN=...\n"
        )

    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)

    print(f"Fetching from: {JIRA_URL}")
    print(f"JQL:           {JIRA_JQL}")
    print(f"Max bugs:      {MAX_BUGS}")
    print()

    rows = []
    next_page_token = None
    page_num = 0

    while True:
        page_num += 1
        print(f"  Page {page_num}...", end=" ", flush=True)
        try:
            data = fetch_page(auth, next_page_token)
        except requests.HTTPError as e:
            body = e.response.text[:500] if e.response is not None else ""
            sys.exit(f"\nJIRA API error: {e}\n{body}")
        except requests.RequestException as e:
            sys.exit(f"\nNetwork error talking to JIRA: {e}")

        issues = data.get("issues", []) or []
        print(f"got {len(issues)} issues")

        for issue in issues:
            rows.append(issue_to_row(issue))
            if len(rows) >= MAX_BUGS:
                break

        if len(rows) >= MAX_BUGS:
            print(f"  Reached MAX_BUGS limit ({MAX_BUGS}). Stopping.")
            break

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break
        time.sleep(0.3)  # be polite

    print(f"\nFetched {len(rows)} bugs total.")
    if not rows:
        sys.exit("No bugs returned. Double-check your JQL.")

    fieldnames = [
        "ID", "Title", "Description", "Comments", "Resolution",
        "Component", "Severity", "Status", "Created",
    ]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {OUTPUT_CSV}")
    print(f"\nNext: python3 ingest.py --csv {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
