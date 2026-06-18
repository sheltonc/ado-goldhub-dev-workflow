---
name: azure-devops
description: Interact with Azure DevOps Boards (work items) and Pull Requests via the official microsoft/azure-devops-python-api SDK. Use when a user wants to list/create/update work items, query WIQL, or list/show/create/comment/approve PRs on Azure DevOps. Auth is PAT-based and resolved from the calling agent profile's environment — no config files, no CLI flags for secrets.
---

# Azure DevOps Skill

## When to use
- User mentions "Azure DevOps", "AZDO", "ADO", "VSTS", "DevOps board", "work item", "WIQL", "DevOps PR", "DevOps pull request", or the project name "Goldhub" (Chris's primary AZDO project — see "Chris's AZDO instance" below)
- User wants to find/list/close bugs or tasks in a project
- User wants to create, review, or approve a pull request
- User asks to script a recurring check (cron) for stale PRs or aging work items

## Environment

The skill reads from the **agent profile's environment**. Set these in the profile that will invoke `azdo` commands:

| Variable        | Required | Example                              |
|-----------------|----------|--------------------------------------|
| `GOLDHUB_AZDO_PAT`      | yes      | `<personal access token>`            |
| `GOLDHUB_AZDO_ORG`      | yes      | `https://dev.azure.com/chris0527`    |
| `GOLDHUB_AZDO_PROJECT`  | yes for default, can override per-call | `Goldhub`                |

The bare `AZDO_PAT` / `AZDO_ORG` / `AZDO_PROJECT` names are still accepted as fallbacks so legacy callers and the smoke test (run from a plain shell) keep working. Prefer the `GOLDHUB_AZDO_*` names going forward — they're project-scoped, which is the right shape now that each Hermes profile (`default`, `coder-goldhub`, `designer-goldhub`, …) carries its own ADO credentials.

Required PAT scopes (in AZDO → User Settings → Personal Access Tokens):
- **Work Items**: Read & Write (Read only for read-only profiles)
- **Code**: Read & Write (Read only for read-only profiles)
- **Project and Team**: Read

If a variable is missing, the script exits with code 2 and a clear message pointing to the variable name.

## Chris's AZDO instance (the canonical target)

- **Org**: `https://dev.azure.com/chris0527`
- **Project (default)**: `Goldhub` (Basic process template, private)
- **Repos**: `Goldhub` (default branch `main`), `Journal` (default branch `main`)
- **AZDO display name**: "Chris Noack" (not "Chris Shelton" — `@Me` resolves to the display name; use `--assigned-to "Chris Noack"` to filter by name explicitly)
- **Board states** (Basic process): `To Do`, `Doing`, `Done`. "Active" in natural language = `To Do` for Goldhub.

### Dev-workflow project custom states

### Dev-workflow project custom states

For the Hermes agent dev workflow project (Goldhub), the board uses 10 custom states added to an inherited process template:

```
To Do
Ready for Design
Design
Design Review
Ready for Development
Development
Ready for Review
Review               ← note: NOT "In Review" — Chris renamed on 2026-06-15
Reviewed             ← reviewer agent posts inline PR threads + sets vote, then moves here
Done
```

When querying/updating dev workflow tickets, use these exact state strings. `Reviewed` is the human decision point — Chris moves to Done (merge) or back to Ready for Development (send coder back).
When querying/updating dev workflow tickets, use these exact state strings. `Reviewed` is the human decision point — Chris moves to Done (merge) or back to Ready for Development (send coder back).

## PR thread lifecycle (reviewer → coder fix pattern)

Used by the `reviewer` profile to post inline findings and by the `coder` profile to resolve them.

### Reviewer: create thread per finding

```python
# POST /pullRequests/{pr_id}/threads
thread = git_client.create_thread(
    comment_thread={
        "comments": [{"content": "Issue description here", "commentType": 1}],
        "threadContext": {
            "filePath": "/path/to/file.py",
            "rightFileStart": {"line": 42, "offset": 1},
            "rightFileEnd": {"line": 42, "offset": 80},
        },
        "status": 1,   # 1 = active
    },
    repository_id="repo-name",
    pull_request_id=pr_id,
    project="ProjectName",
)
thread_id = thread.id
```

### Reviewer: set PR vote

```python
# Vote values: 10=approved, 5=approved-with-suggestions, 0=no-vote, -5=waiting-for-author, -10=rejected
git_client.create_pull_request_reviewer(
    reviewer={"vote": -5},   # -5 = waiting for author (issues found)
    repository_id="repo-name",
    pull_request_id=pr_id,
    reviewer_id=reviewer_identity_id,
    project="ProjectName",
)
```

The reviewer votes as the **PAT holder identity** (the agent's service account), not as `$REVIEWER` (Chris). Do not try to vote as `$REVIEWER` — that requires their credentials. The agent posts threads and sets its own vote; Chris's vote is separate.

### Coder fix: reply to thread + mark fixed

```python
# Reply to the thread
git_client.create_comment(
    comment={"content": f"Fixed in commit {commit_sha} — <explanation>", "commentType": 1},
    repository_id="repo-name",
    pull_request_id=pr_id,
    thread_id=thread_id,
    project="ProjectName",
)

# Mark thread as fixed
git_client.update_thread(
    comment_thread={"status": 2},  # 2 = fixed
    repository_id="repo-name",
    pull_request_id=pr_id,
    thread_id=thread_id,
    project="ProjectName",
)
```

### Thread status values

| Value | Meaning |
|---|---|
| 1 | Active |
| 2 | Fixed |
| 3 | Won't fix |
| 4 | Closed |
| 255 | Unknown/pending |

Store thread IDs in Kanban card metadata (`kanban_complete`) so the coder fix card can fetch and resolve them without re-scanning the whole PR.

Env values in `~/.hermes/.env` (or a per-profile `.env` under `~/.hermes/profiles/<name>/`):
```
GOLDHUB_AZDO_ORG=https://dev.azure.com/chris0527
GOLDHUB_AZDO_PROJECT=Goldhub
GOLDHUB_AZDO_PAT=<full-access PAT>
```

For PR operations, no default repo env var exists yet — always pass
`--repo Goldhub` (or `--repo Journal`). A future `AZDO_REPO` env var
would be a clean addition; not built yet.

## Python environment

PEP 668 blocks system pip on this host. The SDK is installed in a dedicated venv:

```bash
# If it doesn't exist yet, recreate it:
uv venv ~/.hermes/venvs/azdo --python 3.12
uv pip install --python ~/.hermes/venvs/azdo/bin/python azure-devops
```

The skill's helper script auto-detects this venv by adding `~/.hermes/skills/azure-devops/scripts` to `sys.path` and the SDK is resolved because the venv is activated. **If `python3` can't import `azure.devops`, the user needs to install in the venv above.**

## How to invoke

Use the dispatcher: `~/.hermes/skills/azure-devops/scripts/azdo <feature> <subcommand> [args]`

The dispatcher re-execs the right feature script. Always run scripts through this entry point so paths resolve cleanly.

## Commands

### Boards — work items

> **State names depend on the project's process template.** For Goldhub (Basic),
> use `To Do` / `Doing` / `Done` — not `Active` / `Closed`. See the
> process-templates table in the pitfalls section below for the full mapping.

```bash
# List my active work items (Goldhub: use "To Do" not "Active")
azdo boards list --assigned-to me --state "To Do"

# List by type (Goldhub)
azdo boards list --type Bug --state "To Do"

# Show a single work item
azdo boards show 12345

# Read the comment thread (Chris's feedback + agent clarifications)
azdo boards comments 12345
azdo boards comments 12345 --since 2026-06-15T05:00:00

# Read the comment thread (Chris's feedback + agent clarifications)
azdo boards comments 12345
azdo boards comments 12345 --since 2026-06-15T05:00:00

# Create a bug
azdo boards create --type Bug --title "Login broken" --description "Repro steps..." --assigned-to me
```bash
# Update work item state and add a comment
azdo boards update 12345 --state Done --comment "Fixed in PR #456"

# Set or clear the Task "Blocked" details field (Microsoft.VSTS.CMMI.Blocked)
azdo boards update 12345 --blocked yes
azdo boards update 12345 --blocked clear

# Add a tag
azdo boards update 12345 --add-tag "released-2026-06"
```

Notes:
- `azdo boards show <id>` now returns a `description` field in its JSON output. Previously it was silently dropped by `_shorten()` even though the SDK fetched the full work item with `expand="All"`. Fixed 2026-06-18.
- `azdo boards update` accepts a `--description` flag but **intentionally blocks it at the tool level**. Agents must never write to `System.Description` — workflow guidance lives in SOUL.md, and PRD content belongs in local workspace files, not in the ADO work item's description field.

Override the default project: `azdo boards list --project SomeOtherProject --assigned-to me`

> When the user says "active" or "open" in natural language, *do not* default
> to `--state Active`. Translate based on the project's process template —
> for Goldhub (Basic), it's `To Do`. See the process-templates table in the
> Pitfalls section and the worked example in `references/initial-debug-session.md`
> § "AZDO process templates use different state name sets".

### Pull requests
```bash
# List active PRs in a repo
azdo prs list --repo MyRepo

# List the active PR for a task branch (excludes completed/abandoned PRs)
azdo prs list --repo MyRepo --source-branch "task/<id>" --target-branch main --status active

# Show a specific PR
azdo prs show 789 --repo MyRepo

# Create a PR
azdo prs create --repo MyRepo --source feature/login-fix --target main --title "Fix login bug" --description "Closes #12345"

# Link a PR to an Azure Boards work item / task
azdo prs link-work-item 789 --repo MyRepo --work-item 12345
azdo prs link-work-item 789 --repo MyRepo --work-item 12345 --validate-only

# Assign a PR by adding a reviewer. Prefer email/principal name; display names can collide.
azdo prs assign 789 --repo MyRepo --user reviewer@example.com
azdo prs assign 789 --repo MyRepo --user reviewer@example.com --required
azdo prs assign 789 --repo MyRepo --user reviewer@example.com --resolve-only

# Add a comment
azdo prs comment 789 --repo MyRepo --body "LGTM, ship it"

# List all comment threads on a PR (review findings + replies)
azdo prs threads 789 --repo MyRepo
azdo prs threads 789 --repo MyRepo --status active   # only unaddressed findings

# Approve a PR
azdo prs approve 789 --repo MyRepo89 --repo MyRepo --body "LGTM, ship it"

# List all comment threads on a PR (review findings + replies)
azdo prs threads 789 --repo MyRepo
azdo prs threads 789 --repo MyRepo --status active   # only unaddressed findings

# Approve a PR
azdo prs approve 789 --repo MyRepo
```

## Output formats

Default is `json` (pipeable, machine-readable). For humans or Discord:
- `--format table` — ASCII columns
- `--format markdown` — markdown table (best for posting to Discord)

## Programmatic use

The helpers in `azdo_common.py` are importable from a Python session:
```python
import sys; sys.path.insert(0, "~/.hermes/skills/azure-devops/scripts")
import azdo_common as ac

conn = ac.get_connection()
wit = conn.clients.get_work_item_tracking_client()
projects = wit.get_projects()  # or whatever API call you need
```

The helper functions are:
- `get_connection()` — returns a `Connection` from env
- `get_project(explicit=None)` — resolves project from arg or env
- `output(data, fmt="json")` — emit to stdout in chosen format
- `error(msg)` — write to stderr, exit 1
- `flatten_paged(responses)` — drain SDK paged generators
- `wiql(client, project, query)` — run a WIQL query, return list of refs

## Extending PR review/thread support

When building ticket-driven reviewer/coder workflows on ADO Repos, avoid making each worker re-derive raw SDK calls. Extend `scripts/azdo_prs.py` with stable subcommands before wiring automation around PR threads:

```bash
# Reviewer: post an inline finding
azdo prs thread create <pr_id> --repo X --project P \
  --file /path/to/file.py --line 42 \
  --body "Issue description" --status active

# Coder/reviewer: list/fetch active findings
azdo prs thread list <pr_id> --repo X --project P --status active
azdo prs thread show <pr_id> <thread_id> --repo X --project P

# Coder: reply to and resolve a finding
azdo prs thread reply <pr_id> <thread_id> --repo X --project P \
  --body "Fixed in commit abc123 — parameterised query"
azdo prs thread status <pr_id> <thread_id> --repo X --project P --status fixed

# Reviewer: vote as the reviewer identity, not as the PR creator
azdo prs vote <pr_id> --repo X --project P --vote waiting
azdo prs vote <pr_id> --repo X --project P --vote approved
```

Implementation notes:

- Keep using `argparse` subparsers with `parents=[common]` so `--project`, `--repo`, and `--format` work after the subcommand.
- Accept thread status as either integer (`1`/`2`/`3`/`4`) or string (`active`/`fixed`/`wontfix`/`closed`).
- Add smoke tests for create → reply → mark fixed → list/show.
- For voting, do not copy the current `approve` shortcut if it votes as `pr.created_by`; reviewer workflows need the PAT/service-account identity or an explicit `--reviewer-id`.

## Pitfalls

- **The venv is per-host**: if you migrate this skill to another machine, recreate `~/.hermes/venvs/azdo` and reinstall the SDK.
- **`GOLDHUB_AZDO_PROJECT` is required** for any boards command (or the legacy `AZDO_PROJECT` fallback). PRs need both `--project` and `--repo`.
- **WIQL date filters use OData, not ISO**: `[System.ChangedDate] >= @Today - 7`. Use `me` not `@Me` in `--assigned-to` (the script handles the `@Me` token).
- **Branch refs**: `refs/heads/main` is the full form. The script adds the prefix if you pass bare `main`.
- **Votes**: SDK uses integers (10=approved, 5=approved w/ suggestions, 0=no vote, -5=waiting, -10=rejected). The output dict shows them as-is.
- **PAT rotation**: env-var-based auth means a profile with a stale PAT fails clearly (`401 Unauthorized`) — no silent degradation.
- **Custom state names may differ from design docs**: always confirm actual state names via `wit.get_work_item_type_states(project, type)` before writing WIQL or dispatching — do not assume the design doc's names match what was configured. Example: the design doc said `In Review` but the configured state was `Review`. Run the check first, then write the rule.
- **When building a multi-phase poller, implement ALL dispatch rules at once**: omitting even one "Ready for X → X" rule causes live tickets to stall silently. Don't defer any phase — build the full rule table from the design doc in the first pass. See `references/ado-poller-pattern.md`.
- **Poller re-queue bug pattern**: using a state file key as a permanent "already dispatched" guard (with `continue`) blocks re-dispatch when a ticket is sent back for rework and re-enters a Ready state. The correct pattern: if a ticket appears in the query (meaning it IS in a Ready state right now), delete any stale key and re-dispatch — the query is the source of truth, not the state file. See `references/ado-poller-pattern.md` for the corrected implementation.
- **`get_work_items` rejects combining `expand` and `fields`**: the SDK raises `AzureDevOpsServiceError: The expand parameter can not be used with the fields parameter` if you pass both. Use one or the other — pass `fields=[...]` for lightweight fetches, or `expand="Fields"` (no `fields` arg) for full field bags. The `query_state` helper in the poller uses `fields=[...]` only.
- **Task Blocked field is blank-or-Yes**: the Details pane "Blocked" dropdown maps to `Microsoft.VSTS.CMMI.Blocked` with value `Yes`. Clearing it is a JSON Patch `remove` on `/fields/Microsoft.VSTS.CMMI.Blocked`, not setting the value to `No`. The boards helper exposes this as `azdo boards update <id> --blocked yes|clear`; `show`/`list` include a `blocked` property.
- **PR-to-work-item links use an internal ArtifactLink URL, not the PR API URL**: link PRs with `azdo prs link-work-item <pr_id> --repo <repo> --work-item <id>`. The helper builds `vstfs:///Git/PullRequestId/{projectId}/{repositoryId}/{pullRequestId}` and adds it to the work item's `/relations/-` as `ArtifactLink` with name `Pull Request`. Do not use the human PR URL or `_apis/git/.../pullRequests/...` URL — Azure Boards rejects those as invalid resource link targets.
- **PR assignment means adding a reviewer**: Azure Repos PRs do not have a task-style assignee field. Use `azdo prs assign <pr_id> --repo <repo> --user <email>` to add a reviewer with vote `0` (no vote yet), or `--required` to mark the reviewer as required. Prefer email/principal name over display name because display names can collide; `--resolve-only` checks identity resolution without changing the PR. The PR must be editable/active — abandoned/completed PRs return `TF401181`.
- **No tests yet**: this skill ships without unit tests. Verify changes by hand against a real project before relying on them in cron.
- **State names depend on the project's process template** (Basic/Agile/Scrum/CMMI). When the user says "active" or "open" work items, do NOT default to `--state Active` — see `references/initial-debug-session.md` § "AZDO process templates use different state name sets" for the full table. For Chris's Goldhub specifically (Basic template), "active" = `To Do`. If unsure, run an open WIQL query first and infer from the state values you see.
- **`get_work_items()` does not accept `expand` and `fields` together (SDK v7.1).** Passing both raises `The expand parameter can not be used with the fields parameter`. Pick one: either pass `expand="Fields"` to get the full work-item record (and ignore individual `fields=...`), OR pass `fields=[...]` to ask for a specific subset. The boards `list` command and the ADO poller both hit this — use the `fields=` form, which is cheaper and explicit. Documented in `references/initial-debug-session.md` under "SDK v7.1 API shape quirks" as entry 13.
- **Custom-state `In Review` vs `Review` rename.** The design-doc v1 listed the state as `In Review` (matching the dev-workflow docs at the time). When the project was actually configured in ADO, Chris named it simply `Review`. The skill's "dev-workflow project custom states" table was updated to match. If you read older transcripts that say `In Review`, they refer to the same state — just spelled differently. Live state list for the Goldhub dev-workflow process (confirmed 2026-06-15): `To Do`, `Ready for Design`, `Design`, `Design Review`, `Ready for Development`, `Development`, `Ready for Review`, `Review`, `Reviewed`, `Done`, `Removed`. Querying the live states: `client.get_work_item_type_states(project, 'Task')` returns the canonical list.
- **Dispatcher path bug, fixed at build time** (regression trap if you copy this pattern): the `azdo` entry point lives in `scripts/`, so `Path(__file__).resolve().parent` is *already* the scripts dir. Do NOT append another `scripts/` when building paths to feature scripts, or you'll get `FileNotFoundError: .../scripts/scripts/azdo_boards.py`. The dispatcher uses `SKILL_DIR = Path(__file__).resolve().parent` (which is `scripts/`) and points feature scripts at `SKILL_DIR / "azdo_boards.py"`, not `SKILL_DIR / "scripts" / "azdo_boards.py"`. If you add a new subcommand feature, mirror the same pattern.
- **`GOLDHUB_AZDO_ORG` must be a full URL, not a bare org name**: e.g. `https://dev.azure.com/chris0527`, NOT `chris0527` (the legacy `AZDO_ORG` fallback follows the same rule). The connection helper validates this and exits 2 with a message showing the correct shape. Do NOT auto-prepend `https://dev.azure.com/` based on a bare hostname — that breaks Azure DevOps Server (on-prem TFS) users with custom URLs. See `references/initial-debug-session.md` for the full error transcript that motivated this check.
- **Argparse subparsers don't inherit parent optionals** — if you add a new subcommand feature and put `--format` on the parent parser, the subcommand will reject it. Use the `parents=[common]` pattern (see `azdo_boards.py` and `azdo_prs.py` for the working example). This is documented as a general pattern in `hermes-agent-skill-authoring` under "Patterns for Integration Skills".

- **`created_date` format from `get_comments()` is NOT ISO-8601.** The SDK returns `"2026-06-15 05:41:21.463000+00:00"` (space between date and time), not `"2026-06-15T05:41:21..."`. When users pass `--since 2026-06-15T05:00:00`, naive string comparison misses every comment. **Fix in the helper:** normalise both sides to `YYYY-MM-DD HH:MM:SS` (19 chars) before comparing — replace `T` with space, strip subseconds and timezone. The `azdo boards comments --since` subcommand does this; reuse the same `_norm()` pattern in any new code that does date filtering. First observed building the poller dispatcher (2026-06-15).

- **PR thread `status` can be `null`, not just an integer.** Threads created without an explicit status (e.g. by `azdo prs comment` posting a standalone comment, or by the web UI for non-review comments) come back with `status: null`. The `status_label` lookup in `_shorten_thread()` also returns null in that case. **Implication for `--status` filters:** a filter of `active` will exclude these, which is usually what you want (they're not review findings), but a filter of "everything not closed" will exclude them too. If you need "anything that's not explicitly resolved", use `--include-deleted` plus a Python post-filter on `status in (1, None)` rather than the `--status` flag alone. First observed on the smoke test PR (PR #2, 2026-06-15) where all three threads had `status: null`.

- **`azdo_common.py` helper list is the source of truth for programmatic use.** When adding a new helper (e.g. `query_comments()`, `paged_fetch()`), update the helper list in the SKILL.md "Programmatic use" section in the same commit. The skill consumers (dispatcher, poller) import these helpers — they should not be re-derived from raw SDK calls in every script.

- **Project-scoped + legacy env fallback can silently mask typos.** `azdo_common.py` resolves `GOLDHUB_AZDO_*` first, falls back to `AZDO_*`. That's deliberate (legacy callers + smoke test) but it has a sharp edge: if a profile `.env` has a typo in the new name (e.g. `GOLDHUB_AZDO_O RG=…` with a space) but the global `~/.hermes/.env` carries the correct bare `AZDO_ORG=…`, the helper happily uses the global value and the typo is invisible until you run under a profile that doesn't have the global env loaded (e.g. a cron context, or a new profile). When migrating a new profile's env block, eyeball the keys after writing them. If you want loud failure, add a one-time stderr `WARNING: using legacy AZDO_* env (no GOLDHUB_AZDO_* set)` to `_resolve()` — the trade-off is more noise on every smoke-test run.

## File layout
```
~/.hermes/skills/azure-devops/
├── SKILL.md
├── requirements.txt
├── references/
│   ├── initial-debug-session.md   # error transcripts + diagnoses from the
│   └── ado-poller-pattern.md      # poll+diff dispatch pattern: state file, shim, dispatch rules,
│                                  #   SDK quirks encountered, future extension notes (built 2026-06-15)
│                                  #   first run, plus SDK v7.1 API-shape
│                                  #   quirks discovered during live validation
└── scripts/
    ├── azdo           # dispatcher (entry point)
    ├── azdo_common.py # auth, config, output helpers
    ├── azdo_boards.py # work items (list/show/create/update)
    ├── azdo_prs.py    # pull requests (list/show/create/comment/approve)
    └── smoke_test.sh  # re-runnable end-to-end check; run after any change
                       #   to the SDK wrapper layer (supports --with-pat for
                       #   the mutating create/update paths)
```

## Verifying a fresh checkout / after a change

Run the smoke test from anywhere — it auto-sources `~/.hermes/.env` so it works
from a plain shell, not just from inside Hermes:

```bash
~/.hermes/skills/azure-devops/scripts/smoke_test.sh            # read-only paths
~/.hermes/skills/azure-devops/scripts/smoke_test.sh --with-pat # also creates a Task
```

Expected on a working install: 11+ passes, 0 fails. Catches the SDK v7.1
regressions documented in `references/initial-debug-session.md` (the
"SDK v7.1 vs older docs" section).
