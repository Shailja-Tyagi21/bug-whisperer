# BugLens – Setup Guide

BugLens lets you ask questions about your team's past bugs in plain English and checks whether a release is safe to ship. Everything runs on your own computer. Your bug data never leaves it.

**How to use this guide:** go in order, one step at a time.

- **Part 1 (Steps 1–6)** gets BugLens running with sample bugs. About 20–30 minutes, mostly downloads.
- **Part 2 (Steps 7–8)** connects it to your real Jira bugs.
- **Part 3** is a menu of **scenarios** (search, release check, refresh, switch data, MCP, tests, demo and more). **Part 4** is reference and troubleshooting.

Commands are shown for **Windows (PowerShell)** first. Where Mac or Linux is different, a **Mac / Linux** note follows.

---

# Part 1 – Get BugLens running

## Step 1 – Install three programs

You need Python, Git and Ollama. Install them once.

**Windows**
1. **Python 3.10 or newer**: <https://www.python.org/downloads/>. On the first installer screen, **tick "Add python.exe to PATH"**. This is the most common thing people miss.
2. **Git**: <https://git-scm.com/download/win>. Accept the default options.
3. **Ollama**: <https://ollama.com/download>. After installing, it runs in the system tray (bottom-right). You don't need to start it by hand.

Then open **PowerShell** (Start menu → type "PowerShell") and check:
```powershell
python --version
git --version
ollama --version
```
Each should print a version number. If `python` isn't found, try `py --version`, or reinstall Python and tick the PATH box.

**Mac / Linux**
- Mac: install Python from <https://www.python.org/downloads/> (or `brew install python git`). Git comes with Xcode tools: if asked, run `xcode-select --install`. Install the Ollama app from <https://ollama.com/download>. It runs in the menu bar.
- Linux: install Python 3.10+ and Git with your package manager, install Ollama from <https://ollama.com/download>, and run `ollama serve` in its own terminal.
- Wherever this guide says `python`, type **`python3`**.

> You also need about **6 GB of free disk space** and **16 GB RAM** is recommended (8 GB works with a smaller model, see Scenario 9).

---

## Step 2 – Download the project

**Windows**
```powershell
git clone -b jira-integration https://github.com/Shailja-Tyagi21/bug-whisperer.git
cd bug-whisperer
```

**Mac / Linux:** the same two commands.

From now on, **run every command from inside this `bug-whisperer` folder.** BugLens looks for its database in the folder you run it from.

---

## Step 3 – Create a Python environment and install packages

This keeps BugLens's packages separate from the rest of your computer.

**Windows**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```
When it works, you see `(.venv)` at the start of your prompt.

If PowerShell says **"running scripts is disabled on this system"**, run this once, then repeat the activate line:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
(In Command Prompt, activate with `.venv\Scripts\activate.bat` instead.)

**Mac / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip install -r requirements.txt
```

> **Every time you open a new terminal,** go to the project folder and run the activate line again. Without `(.venv)` showing, commands may fail.

---

## Step 4 – Download the AI models

BugLens uses two small models that run locally. This is a one-time download of about 5 GB, so it takes a few minutes.

**Windows and Mac / Linux (same commands)**
```powershell
ollama pull llama3.1
ollama pull nomic-embed-text
ollama list
```
`ollama list` should show both models.

If you see "connection refused" or "could not connect", Ollama isn't running. On Windows and Mac, open the Ollama app from the Start menu or Applications. On Linux, run `ollama serve`.

---

## Step 5 – Load the sample bugs

The project includes 60 sample bugs, so you can try it without Jira.

**Windows**
```powershell
python ingest.py --csv sample_bugs.csv
```

**Mac / Linux:** `python3 ingest.py --csv sample_bugs.csv`

This reads the bugs and builds BugLens's search database. It takes a minute or two. At the end you should see:
```
Verified: collection now contains exactly 60 bugs, matching the CSV.
```

---

## Step 6 – Start BugLens and try it

**Windows**
```powershell
streamlit run app.py
```

**Mac / Linux:** the same command.

Your browser opens at <http://localhost:8501>. Click the **Search bug history** tab and ask:

> `PayPal redirect issue after checkout`

You should see an answer that cites a bug ID, and a list of source bugs below it.

Things that surprise people:
- **Streamlit may ask for your email in the terminal** the first time. Press **Enter** to skip it. It isn't stuck.
- **The first question can take a minute or more** because Ollama is loading the model. Later ones take 20–30 seconds.
- **If the page doesn't open, or port 8501 is busy,** run `streamlit run app.py --server.port 8502` and open <http://localhost:8502>.
- To stop BugLens, click the terminal and press **Ctrl+C**.

**🎉 Part 1 is done.** BugLens works. Next, connect it to your real bugs.

---

# Part 2 – Use your real Jira bugs

## Step 7 – Connect to Jira

**7a. Create a Jira API token**
1. Open <https://id.atlassian.com/manage-profile/security/api-tokens> and sign in.
2. Click **Create API token**, name it, and **copy the token**. You can't see it again later.

**7b. Create your settings file (`.env`)**

**Windows**
```powershell
Copy-Item .env.example .env
notepad .env
```
In Notepad, fill in these lines, then save:
```
JIRA_URL=https://your-company.atlassian.net
JIRA_EMAIL=you@your-company.com
JIRA_API_TOKEN=paste-your-token-here
JIRA_JQL=project = "YOURKEY" AND issuetype = Bug ORDER BY created DESC
MAX_BUGS=500
```
- Make sure the file is named **`.env`**, not `.env.txt`. In the Save dialog, set "Save as type" to **All files**.
- `JIRA_JQL` chooses which bugs to pull. Replace `YOURKEY` with your Jira project key. Test the query in Jira's search first if you're unsure.
- `MAX_BUGS` must be **higher than your real bug count**, or the export is silently cut off.
- **Never share or commit `.env`.** It holds your token.

**Mac / Linux:** `cp .env.example .env`, then open it in any editor (`nano .env`).

**7c. Fetch the bugs from Jira**

**Windows:** `python jira_fetch.py`  **Mac / Linux:** `python3 jira_fetch.py`

You should see how many bugs were written to `jira_bugs.csv`, and three lines like `Bug Component: populated on 61/61 rows`. If one says `0`, your Jira uses different field IDs: see Scenario 7.

**7d. Load them into BugLens**
```powershell
python ingest.py --csv jira_bugs.csv
```
(Mac / Linux: `python3`.)

**7e. Check that it worked (recommended)**
```powershell
python tests/verify_ingest.py
```
This is a health check with no AI calls. You want to see **`0 failure(s)`** at the end.

**7f. Start the app**
```powershell
streamlit run app.py
```

---

## Step 8 – Refresh when Jira changes

BugLens does **not** update by itself. After bugs are added or edited in Jira, stop the app, then run `jira_fetch.py`, `ingest.py --csv jira_bugs.csv` and `tests/verify_ingest.py` in that order. The exact commands and what you should see are in [Scenario 3](#scenario-3--refresh-after-jira-changes).

**You're set up.** Part 3 is a menu of things you can do next.

---

# Part 3 – Scenarios: what do you want to do?

Once BugLens is set up (Part 1), use this section like a menu. Find what you want to do, then jump to that scenario. Each one is self-contained. Commands are shown for Windows (PowerShell) first, with Mac / Linux notes (on Mac / Linux, type `python3` instead of `python`).

| I want to... | Go to |
|---|---|
| Ask about past bugs | [Scenario 1](#scenario-1--search-bug-history) |
| Check if a release is safe to ship | [Scenario 2](#scenario-2--check-release-readiness-go--no-go) |
| Update BugLens after Jira changed | [Scenario 3](#scenario-3--refresh-after-jira-changes) |
| Test that a new or edited bug flows through correctly | [Scenario 4](#scenario-4--verify-one-new-or-edited-bug-end-to-end) |
| Switch between the sample bugs and my Jira bugs | [Scenario 5](#scenario-5--switch-between-sample-data-and-jira-data) |
| Leave certain bugs out | [Scenario 6](#scenario-6--leave-some-bugs-out) |
| Use a different Jira site or project | [Scenario 7](#scenario-7--use-a-different-jira-site-or-project) |
| Use BugLens from Copilot or Claude Desktop (MCP) | [Scenario 8](#scenario-8--use-buglens-from-an-ai-assistant-mcp) |
| Use a different AI model, or speed things up | [Scenario 9](#scenario-9--use-a-different-ai-model-or-make-it-faster) |
| Run the tests | [Scenario 10](#scenario-10--run-the-tests) |
| Start over from scratch | [Scenario 11](#scenario-11--start-over-from-scratch) |
| Give a demo | [Scenario 12](#scenario-12--give-a-demo) |

---

## Scenario 1 – Search bug history

1. Start the app (`streamlit run app.py`) and open the **Search bug history** tab.
2. Type a question in plain English and click **Search**.
3. Read the **Answer**. It cites bug IDs like `[SCRUM-65]`. Below it, **Source Bugs** lists every candidate with a similarity score and a verdict: **Match**, **Related** or **Not relevant**.

The **Bugs to retrieve** slider (default 5) sets how many candidates are checked. Tick **Show raw retrieved text** in the sidebar to see exactly what the AI was given.

Questions to try:

| Question | What it shows |
|---|---|
| `checkout broken for European customers` | A distinctive word ("European") helps find the right bug. |
| `has billing ever charged customers twice?` | An honest "no" when nothing really matches. |
| `how did we fix the memory leak in the worker service?` | Root-cause and fix lookups. |
| `has our refund process had any silent failures?` | Several genuine matches. |
| `do we have any Bluetooth pairing bugs?` | Nothing exists, so every candidate is rejected. |

---

## Scenario 2 – Check release readiness (GO / NO-GO)

1. Open the **Release readiness** tab.
2. Pick a version from the **Release version** dropdown and click **Check readiness**.
3. You get **GO** or **NO-GO**, the blocking bugs (if any), and a short explanation.

**The decision is made by simple code, not by the AI.** A release is **NO-GO** if any bug in that version is still **open** (status not Closed, Done or Resolved) **and** has priority **High, Highest or Blocker**. Otherwise it is **GO**. The AI only writes the explanation. To change the rule, edit `CLOSED_STATUSES` and `BLOCKING_PRIORITIES` in `search.py`.

To see every release and its counts without the AI, run `python tests/verify_ingest.py` and read section **[5] Releases**.

---

## Scenario 3 – Refresh after Jira changes

BugLens does **not** update by itself. After bugs are added or edited in Jira:

1. **Stop the app** (Ctrl+C in its terminal).
2. Run, one at a time:
```powershell
python jira_fetch.py
python ingest.py --csv jira_bugs.csv
python tests/verify_ingest.py
streamlit run app.py
```

What you'll see:
- `Embedding cache: 60/61 unchanged`: only new or edited bugs were reprocessed. That's the speed-up.
- `pruned 1 orphaned vector(s)`: leftovers from edited bugs were cleaned up.
- `0 failure(s)` from `verify_ingest.py`.

If you forget to stop the app first, it reloads the new data on its next search and prints `chroma_db changed on disk ... reloading`. It's still safer to stop it.

To force every bug to be reprocessed (rarely needed): `python ingest.py --csv jira_bugs.csv --no-cache`.

---

## Scenario 4 – Verify one new or edited bug end to end

Use this to prove the whole pipeline works, or after any change you want to be sure about.

1. In Jira, **edit** a bug (change its title or description) or **create** a test bug. Make its subject something distinctive, for example an invoice VAT bug for Indian GST customers.
2. Stop the app, then run:
```powershell
python jira_fetch.py
python ingest.py --csv jira_bugs.csv
```
3. Check the ingest output:
   - **Edited a bug:** `Embedding cache: 60/61 unchanged` (one re-embedded), then `pruned 1 orphaned vector(s)`.
   - **Created a bug:** `Embedding cache: 61/62 unchanged` and the count goes up by one. Nothing is pruned.
4. Confirm the bug is stored and ranks first for a matching question (replace the ID and query with yours):
```powershell
python tests/verify_ingest.py --expect SCRUM-65 --query "invoice shows wrong tax rate for India customers" --top 1
```
You want ✅ lines saying the bug is in the collection and ranks #1, and `0 failure(s)`.
5. Start the app and ask the same question. The bug should appear as **Match**.

A test bug that you can't delete: leave it out with Scenario 6. Note that other tests expect the original bug counts, so exclude it before running the regression suite.

---

## Scenario 5 – Switch between sample data and Jira data

Loading data **replaces** the whole database every time, so switching is just a re-ingest. Stop the app first.

*Load the sample bugs:*
```powershell
python ingest.py --csv sample_bugs.csv
```
*Load your Jira bugs:*
```powershell
python ingest.py --csv jira_bugs.csv
```
Things to know:
- Only one dataset is loaded at a time. BugLens never mixes them.
- The embedding cache keeps only the vectors for the file you just loaded, so switching back re-embeds everything once (a few minutes).
- The calibration and regression tests are written for the Jira data (see Scenario 10).

---

## Scenario 6 – Leave some bugs out

Use the Jira query. Open `.env` and change `JIRA_JQL`, for example to skip one bug:
```
JIRA_JQL=issuetype = Bug AND key != SCRUM-65 ORDER BY created DESC
```
Or to limit to one project and a time window:
```
JIRA_JQL=project = "YOURKEY" AND issuetype = Bug AND created >= -365d ORDER BY created DESC
```
Then repeat Scenario 3. After `jira_fetch.py`, check that its `JQL:` line shows your new query. If it still shows the default (`issuetype = Bug ORDER BY created DESC`), `.env` isn't being read: check the file is named `.env`, is in the project root, and the line starts with `JIRA_JQL=`.

---

## Scenario 7 – Use a different Jira site or project

BugLens reads three custom Jira fields by ID: Bug Component, Release Version and Severity. The IDs in the code (`customfield_10042`, `10043`, `10044`) belong to the original `hacakthoncg.atlassian.net` project and **won't work on another Jira site**.

To find yours (needs `JIRA_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN` in `.env`):
```powershell
python list_jira_fields.py
```
(Mac / Linux: `python3`.) Find the **custom** rows named Bug Component, Release Version and Severity. Copy their IDs into the three `FIELD_ID_*` lines at the top of `jira_fetch.py`, then repeat Step 7c onward. Use `--all` to see every field.

To pull several projects at once, widen the query: `JIRA_JQL=project in (AAA, BBB) AND issuetype = Bug ORDER BY created DESC`.

---

## Scenario 8 – Use BugLens from an AI assistant (MCP)

BugLens can run as an MCP server, so GitHub Copilot, Claude Desktop or Cursor can use it. It offers three tools: `search_bugs`, `check_release` and `list_releases`. You need data loaded first (Step 5 or Step 7). `mcp` is already installed by `requirements.txt` (it must be version 2 or newer).

**VS Code with GitHub Copilot.** Create `.vscode/mcp.json` in the project (an example is included. Replace the path with yours):

*Windows*
```json
{
  "servers": {
    "buglens": {
      "command": "C:\\Users\\you\\bug-whisperer\\.venv\\Scripts\\python.exe",
      "args": ["bugLens_mcp.py"],
      "env": { "JIRA_URL": "https://your-company.atlassian.net" }
    }
  }
}
```
(In JSON, every backslash is doubled.)

*Mac / Linux:* use `"command": "/full/path/to/bug-whisperer/.venv/bin/python3"`.

Open the project folder in VS Code, start the server from the MCP panel, then ask Copilot Chat something like `search bug history for PayPal redirect issue`.

**Claude Desktop.** Add this to the config file, then restart Claude Desktop.

*Windows* (`%APPDATA%\Claude\claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "buglens": {
      "command": "cmd",
      "args": ["/c", "cd /d C:\\Users\\you\\bug-whisperer && .venv\\Scripts\\python.exe bugLens_mcp.py"],
      "env": { "JIRA_URL": "https://your-company.atlassian.net" }
    }
  }
}
```

*Mac* (`~/Library/Application Support/Claude/claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "buglens": {
      "command": "/bin/bash",
      "args": ["-c", "cd /full/path/to/bug-whisperer && .venv/bin/python3 bugLens_mcp.py"],
      "env": { "JIRA_URL": "https://your-company.atlassian.net" }
    }
  }
}
```
The `cd` matters: BugLens looks for its database in the folder it starts from.

Good to know: an AI assistant may rephrase your question, so results can differ from typing the same words into the app. Keep secrets in `.env`, not in these config files.

---

## Scenario 9 – Use a different AI model, or make it faster

Download another model (for example `ollama pull mistral`) and set it in `.env`: `OLLAMA_MODEL=mistral`. Smaller models (`gemma2:2b`, `qwen2.5:3b`) are faster on modest laptops but check bugs less reliably. After changing the model, run the tests (Scenario 10).

**Do not change the embedding model (`nomic-embed-text`) casually.** If you do, run `python ingest.py --csv jira_bugs.csv --no-cache` to rebuild every vector.

**Speed:** by default Ollama answers one request at a time. To allow 4 at once:
- *Windows:* quit Ollama from the system tray, run `setx OLLAMA_NUM_PARALLEL 4`, then start Ollama again from the Start menu.
- *Mac:* `launchctl setenv OLLAMA_NUM_PARALLEL 4`, then restart Ollama.
- *Linux:* `OLLAMA_NUM_PARALLEL=4 ollama serve`.

**Repeatable answers:** `OLLAMA_SEED` (default 42) makes the same question give the same answer. Set `OLLAMA_SEED=0` to allow variation.

---

## Scenario 10 – Run the tests

Run from the project folder with `(.venv)` active. Fastest first (Mac / Linux: `python3`):

| Command | Needs Ollama? | What it checks |
|---|---|---|
| `python tests/test_guardrails.py` | No | Safety rules (13 tests). |
| `python tests/test_quality_checks.py` | No | Answer-quality checks (32 tests). |
| `python tests/verify_ingest.py` | Embeddings only | The CSV, database, cache and release table agree. |
| `python tests/check_similarity_calibration.py` | Embeddings only | The "duplicate" similarity cut-off (0.75) still works. Run after any re-ingest or model change. |
| `python tests/run_regression_suite.py` | Yes (about 5 min) | 10 questions and 2 release checks end to end, scored and compared with the last run. |

Useful options for `verify_ingest.py`:
```powershell
python tests/verify_ingest.py --csv sample_bugs.csv     # when you loaded the sample data
python tests/verify_ingest.py --expect SCRUM-65         # a bug must exist
python tests/verify_ingest.py --expect SCRUM-65 --query "invoice shows wrong tax rate for India customers" --top 1
```

The calibration and regression tests are written for the Jira data (`SCRUM-xx` bugs, releases `v2.5.0` and `v2.6.0`). On other data they stop early and say why. To use them on your own project, update the anchor bugs in `tests/check_similarity_calibration.py` and the expectations in `tests/run_regression_suite.py`. Regression reports are saved in `tests/reports/`.

---

## Scenario 11 – Start over from scratch

Stop the app first. This deletes only generated data, not your code, CSVs or `.env`.

*Windows*
```powershell
Remove-Item -Recurse -Force chroma_db, embedding_cache.json
python ingest.py --csv jira_bugs.csv
```
*Mac / Linux*
```bash
rm -rf chroma_db embedding_cache.json
python3 ingest.py --csv jira_bugs.csv
```
(Use `sample_bugs.csv` instead of `jira_bugs.csv` if you're on the sample data.)

---

## Scenario 12 – Give a demo

A short plan that shows what BugLens is good at.

**Before the demo (5 minutes)**
1. Refresh the data (Scenario 3) and run `python tests/verify_ingest.py`. You want `0 failure(s)`.
2. Start the app and **ask one question first**, off-screen. This loads the AI model into memory, so the first question in front of people isn't slow.

**During the demo**
1. **Keyword match:** `checkout broken for European customers`. Hybrid search puts the right bug first.
2. **Honest "no":** `do we have any Bluetooth pairing bugs?`. Nothing exists, and every candidate is rejected instead of forcing an answer.
3. **Several real matches:** `has our refund process had any silent failures?`. Two distinct root causes, both cited.
4. **Release gate:** open **Release readiness**, check a version that has open High-priority bugs (on the Jira data, `v2.5.0`), and show the **NO-GO** with its blocking bugs. Then check one that is clear (`v2.6.0`) and show **GO**. Point out that the decision is a plain code rule, not an AI guess.

Each search takes 20–30 seconds, so talk while it runs.

---

# Part 4 – Reference

## Settings (`.env`)

All optional except the Jira ones in Step 7.

| Variable | Default | Purpose |
|---|---|---|
| `JIRA_URL` | none | Your Jira site, e.g. `https://your-company.atlassian.net`. |
| `JIRA_EMAIL` | none | Account email for the API token. |
| `JIRA_API_TOKEN` | none | Your API token. Keep it secret. |
| `JIRA_JQL` | `issuetype = Bug ORDER BY created DESC` | Which bugs to pull. |
| `MAX_BUGS` | `500` | Cap on bugs fetched. Keep it above your real count. |
| `OUTPUT_CSV` | `jira_bugs.csv` | File the fetch writes. |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama runs. |
| `OLLAMA_MODEL` | `llama3.1` | Model that checks bugs and writes answers. |
| `OLLAMA_SEED` | `42` | Fixed seed for repeatable answers. |
| `HYBRID_BM25_WEIGHT` | `0.3` | Keyword share in search. 0 is pure meaning, 1 is pure keyword. |
| `JIRA_BASE_URL` | `JIRA_URL` + `/browse/` | Base for clickable bug links. |

Files BugLens creates (all ignored by git, safe to delete): `jira_bugs.csv`, `chroma_db/`, `embedding_cache.json`.

## Troubleshooting

**Windows-specific**

| Problem | Fix |
|---|---|
| `python` is not recognized | Python isn't on PATH. Reinstall it and tick **Add python.exe to PATH**, or try `py`. |
| `running scripts is disabled on this system` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then activate again. |
| `The token '&&' is not a valid statement separator` | Older PowerShell. Run the commands on separate lines. |
| `.env` is ignored | Notepad saved it as `.env.txt`. Rename it to `.env` (turn on "File name extensions" in File Explorer). |
| `UnicodeEncodeError` or garbled characters | Run `$env:PYTHONUTF8 = "1"` in that window and try again. |

**General**

| Problem | Fix |
|---|---|
| `Connection refused` to Ollama | Ollama isn't running. Open the Ollama app (Windows/Mac) or run `ollama serve` (Linux). |
| `model ... not found` | Run `ollama pull llama3.1` and `ollama pull nomic-embed-text`. |
| `Collection not found`, or empty results | Nothing loaded yet. Run Step 5 or Step 7d from the project folder. |
| Works in one terminal but not another | Different folder, or the environment isn't active. `cd` into the project and activate `.venv`. |
| Streamlit asks for an email | Press Enter to skip. |
| `Port 8501 is already in use` | Stop the other Streamlit, or use `--server.port 8502`. |
| First question takes over a minute | Normal: Ollama is loading the model. |
| Search is always slow | One AI call per bug. Use a smaller model or raise `OLLAMA_NUM_PARALLEL` (Scenario 9). |
| `jira_fetch.py`: 401 or 403 | Wrong email or token, or the token was revoked. Create a new one. |
| `jira_fetch.py`: 0 bugs | Your JQL matches nothing. Test it in Jira's search first. |
| A field says `populated on 0/N rows` | Your Jira uses different field IDs. See Scenario 7. |
| Fewer bugs than expected | `MAX_BUGS` is too low, or your JQL is narrower than you think. |
| Old release counts after a Jira change | You ran fetch but not ingest. Do both (Step 8). |
| Similarity scores look off after changing models | Run `tests/check_similarity_calibration.py`. |
| MCP: `cannot import MCPServer` | You have `mcp` 1.x. Run `pip install "mcp>=2"`. |
| MCP: collection not found | The server started in another folder. Use the `cd` form in Scenario 8. |
| MCP answers differ from the app | The assistant rephrased your question. Compare using the exact words in the app. |
| Old data in a running app | Stop it and run `streamlit run app.py` again. |

**Security:** never commit `.env`. If a token is ever exposed, revoke it on the Atlassian API-token page and create a new one.
