"""
jira_fetch.py
-------------
Fetch bugs from JIRA Cloud via the REST API and write a CSV that ingest.py
can consume without any changes.

This is the Jira-integration version -- it pulls the three CUSTOM DROPDOWN
fields that this team-managed project uses in place of the missing native
Components / Fix Version / Severity fields:

    "Bug Component"   -> customfield_10076  -> CSV "Component"
    "Release Version" -> customfield_10077  -> CSV "ReleaseVersion"
    "Severity"        -> customfield_10078  -> CSV "Severity"

The output CSV column names match sample_bugs.csv exactly, so ingest.py
consumes it with no changes:
    ID, Title, Description, Comments, Resolution, Component, Severity,
    Priority, Status, Created, ReleaseVersion

The custom field IDs were confirmed via list_jira_fields.py. If you point
this at a different Jira project, run that script again and update the
three FIELD_ID_* constants.

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
    "issuetype = Bug ORDER BY created DESC",
)
OUTPUT_CSV = os.getenv("OUTPUT_CSV", "jira_bugs.csv")
MAX_BUGS = int(os.getenv("MAX_BUGS", "500"))

# Custom fields on this project -- these carry Component, ReleaseVersion,
# and Severity because the team-managed project has no native versions of
# those fields exposed on the Bug work type. If you point this at a
# different project, re-run list_jira_fields.py and update these.
FIELD_ID_BUG_COMPONENT = "customfield_10076"
FIELD_ID_RELEASE_VERSION = "customfield_10077"
FIELD_ID_SEVERITY = "customfield_10078"

PAGE_SIZE = 50  # JIRA Cloud caps maxResults for /search/jql
REQUEST_TIMEOUT = 30
# ---------------------------------------------------------------------------


def adf_to_text(node) -> str:
    """Flatten Atlassian Document Format (ADF) JSON into plain text.

    JIRA Cloud stores descriptions and comments as a nested JSON tree
    (paragraphs, headings, lists, code blocks, etc). For embedding we just
    want plain text. This handles the common node types and falls through
    to recursion for anything else."""
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

    return adf_to_text(content)


def fetch_page(auth: HTTPBasicAuth, next_page_token=None) -> dict:
    """Fetch one page of issues from the /search/jql endpoint.

    Requests the three custom fields by ID explicitly. Jira does not
    return custom fields by default -- you either ask for "*all" (heavy
    and unnecessary) or name the specific IDs you want, which we do."""
    url = f"{JIRA_URL}/rest/api/3/search/jql"
    payload = {
        "jql": JIRA_JQL,
        "fields": [
            "summary", "description", "comment", "resolution",
            "priority", "status", "created", "issuetype",
            FIELD_ID_BUG_COMPONENT,
            FIELD_ID_RELEASE_VERSION,
            FIELD_ID_SEVERITY,
        ],
        "maxResults": PAGE_SIZE,
    }
    if next_page_token:
        payload["nextPageToken"] = next_page_token

    response = requests.post(
        url,
        json=payload,
        auth=auth,
        headers={"Accept": "application/json",
                  "Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _option_value(field_value) -> str:
    """Extract .value from a single-select dropdown field. These come back
    as {"value": "...", "id": "...", ...} or None."""
    if isinstance(field_value, dict):
        return field_value.get("value", "") or ""
    return ""


def issue_to_row(issue: dict) -> dict:
    """Convert a JIRA issue JSON into the CSV row format ingest.py expects.

    The CSV column names match sample_bugs.csv exactly so that ingest.py
    and everything downstream (search.py, app.py, quality_checks.py) needs
    no changes to work off Jira data instead of the local sample data."""
    key = issue.get("key", "")
    fields = issue.get("fields", {}) or {}

    title = (fields.get("summary") or "").strip()
    description = adf_to_text(fields.get("description")).strip()

    # Comments -- most semantically valuable content, keep them all
    # joined with a delimiter that's unlikely to appear naturally.
    comment_field = fields.get("comment") or {}
    comments = comment_field.get("comments", []) if isinstance(comment_field, dict) else []
    comment_chunks = []
    for c in comments:
        author = (c.get("author") or {}).get("displayName", "Someone")
        body = adf_to_text(c.get("body")).strip()
        if body:
            comment_chunks.append(f"{author}: {body}")
    comments_text = " || ".join(comment_chunks)

    # Resolution: prefer the resolution field; if it's just a boilerplate
    # "Done"/"Fixed" label, fall back to the last comment.
    #
    # Special case: upload_bugs_to_jira.py writes comments in a structured
    # format: "Comments: <investigation>\n\nResolution: <fix>". When the
    # fallback grabs the whole comment, we need to extract just the
    # Resolution part — otherwise the UI shows both the investigation notes
    # and the fix under the same "Resolution:" header.
    resolution_name = ""
    res = fields.get("resolution")
    if isinstance(res, dict):
        resolution_name = res.get("name", "") or ""
    if resolution_name in ("", "Done", "Fixed", "Resolved") and comment_chunks:
        last_comment = comment_chunks[-1][:500]
        # Strip the "Author: " prefix that comment_chunks adds
        if ": " in last_comment:
            last_comment = last_comment.split(": ", 1)[1]
        # If the comment has "Resolution:" embedded, extract just that part
        if "Resolution:" in last_comment:
            resolution_name = last_comment.split("Resolution:", 1)[1].strip()
        else:
            resolution_name = last_comment

    # Priority (real Jira field) -- returns as {"name": "High", ...}
    priority = ""
    pri = fields.get("priority")
    if isinstance(pri, dict):
        priority = pri.get("name", "") or ""

    # Status name
    status = ""
    st = fields.get("status")
    if isinstance(st, dict):
        status = st.get("name", "") or ""

    # Created date -- keep just YYYY-MM-DD
    created = (fields.get("created") or "")[:10]

    # Three custom dropdown fields -- all shaped {"value": "...", ...}
    component = _option_value(fields.get(FIELD_ID_BUG_COMPONENT))
    release_version = _option_value(fields.get(FIELD_ID_RELEASE_VERSION))
    severity = _option_value(fields.get(FIELD_ID_SEVERITY))

    return {
        "ID": key,
        "Title": title,
        "Description": description,
        "Comments": comments_text,
        "Resolution": resolution_name,
        "Component": component,
        "Severity": severity,
        "Priority": priority,
        "Status": status,
        "Created": created,
        "ReleaseVersion": release_version,
    }


# Column order matches sample_bugs.csv exactly so ingest.py sees the same
# shape whether it's reading sample data or Jira export.
CSV_FIELDS = [
    "ID", "Title", "Description", "Comments", "Resolution",
    "Component", "Severity", "Priority", "Status", "Created", "ReleaseVersion",
]


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
    print(f"Output:        {OUTPUT_CSV}")
    print()

    rows = []
    next_page_token = None
    page_num = 0

    while True:
        page_num += 1
        print(f"  Fetching page {page_num}...", end=" ", flush=True)
        try:
            data = fetch_page(auth, next_page_token)
        except requests.HTTPError as e:
            body = e.response.text[:400] if e.response is not None else ""
            sys.exit(f"\nHTTP error on page {page_num}: {e}\n{body}")

        issues = data.get("issues", []) or []
        print(f"got {len(issues)} issue(s)")

        for issue in issues:
            rows.append(issue_to_row(issue))
            if len(rows) >= MAX_BUGS:
                break

        if len(rows) >= MAX_BUGS:
            print(f"  Hit MAX_BUGS cap ({MAX_BUGS}).")
            break

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break

    if not rows:
        sys.exit("No issues returned. Check JIRA_JQL in your .env.")

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS,
                                 quoting=csv.QUOTE_ALL,
                                 extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} bug(s) to {OUTPUT_CSV}")

    # Quick sanity check: warn if any custom field came back empty on every
    # row -- that's usually a sign the field ID changed on the Jira project
    # and you need to re-run list_jira_fields.py.
    for label, key_name in [
        ("Bug Component", "Component"),
        ("Release Version", "ReleaseVersion"),
        ("Severity", "Severity"),
    ]:
        filled = sum(1 for r in rows if r[key_name])
        if filled == 0:
            print(f"  ⚠️  {label} was empty on all {len(rows)} rows. "
                  f"The custom field ID may have changed -- run "
                  f"list_jira_fields.py --search \"{label.lower()}\" to "
                  f"check.")
        else:
            print(f"  {label}: populated on {filled}/{len(rows)} rows")

    print(f"\nNext:")
    print(f"  python3 ingest.py --csv {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
