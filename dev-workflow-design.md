# Hermes-Orchestrated Development Workflow — Design Document

- **Author:** Chris Shelton (design) + Herm (analysis)
- **Date:** 2026-06-14
- **Status:** Design locked, ready for implementation
- **Host:** thinkcentre (Hermes runs here)
- **Source of this doc:** distilled from the 14 Jun 2026 design discussion + `dev-workflow-state-machine.html`

---

## 1. Goal

Build a ticket-driven, multi-agent software development pipeline where **Hermes is the orchestrator** and **OpenCode CLI is the worker**. Chris drives everything from the **Azure DevOps (ADO) board only** — he never touches the internal machinery. The pipeline plans, codes, and reviews work autonomously, surfacing to Chris only at defined human-decision points.

### The one-line mental model

> **ADO is your surface. Kanban is the engine room. You never open the engine room.**

---

## 2. Core architectural principle — two layers, never conflated

| Layer | Purpose | Who looks at it |
|---|---|---|
| **Azure DevOps Board** | Business source of truth — *what stage is the work in* (human terms) | Chris (humans) |
| **Hermes Kanban** | Internal worker handoff — durability, retries, concurrency, handoff payload | Agents (never Chris) |

**Why both are needed:** Putting agent-to-agent handoff on the ADO ticket (in comments) makes stall detection, crash recovery, concurrency, and reassignment impossible, and turns Discord into a noisy state-mirror. The Kanban card carries the *actual work payload* (plan path, worktree, branch, PR URL, OpenCode session ID, test output). ADO shows the *current human-facing state*. They are linked but distinct.

**ADO columns describe phases. Kanban states describe the lifecycle of a single unit of work moving through a phase. Don't merge them.**

### Rejected idea: the "Scrum Master" agent
A Scrum Master is a *job, not an agent*. Its real function ("detect ticket moved → pick the right worker → dispatch") is a **state machine + dispatcher**, not a persona. Building it as an agent wastes effort teaching it not to do dumb things. It is replaced by a small ADO poller cron + the built-in Kanban dispatcher.

---

## 3. Chris's complete interaction surface — 3 actions per ticket

| What Chris does | Where | Agent response |
|---|---|---|
| Create ticket + paste PRD, move to **Ready for Design** | ADO | Planner starts automatically |
| Read plan comment, move to **Ready for Development** (or comment + move back) | ADO | Coder starts automatically |
| At **Reviewed**: read PR threads, then move to **Done** (merge) or back to **Ready for Development** (fixes needed) | ADO | Done, or coder addresses PR threads |

That's it. ADO is where Chris lives. The only exception is **planner clarification questions** (see §9).

---

## 4. The state machine (ADO board states)

Code: Azure DevOps **Repos + Boards**. ADO PAT already configured. Custom states **not yet created** (10-min setup job in Project Settings).

```
To Do
Ready for Design       ← YOU move here (ticket + PRD added)
Design                 ← planner agent moves here when it starts
Design Review          ← planner agent moves here when plan is posted as comment
Ready for Development  ← YOU move here (plan approved, or sending coder back for fixes)
Development            ← coder agent moves here when it starts
In Review              ← coder agent moves here when PR opened
Reviewed               ← reviewer agent moves here (inline PR comments posted)  [NEW — the circuit breaker]
Done                   ← YOU move here (PR merged + closed)
```

### Who moves what

| State | Moved by | Trigger |
|---|---|---|
| Ready for Design | **Chris** | New ticket ready for planning |
| Design | Planner agent | Planner starts working |
| Design Review | Planner agent | Plan complete, comment posted |
| Ready for Development | **Chris** | Plan approved (or sending coder back for fixes) |
| Development | Coder agent | Coder starts implementing |
| In Review | Coder agent | PR opened, URL posted |
| Reviewed | Reviewer agent | Inline PR comments posted, vote set |
| Done | **Chris** | PR merged |

### Rejection / rework paths
- **Plan rejected:** Chris comments why + moves `Design Review → Ready for Design`. Poller respawns planner with Chris's comment as context.
- **Reviewer requests changes:** Reviewer posts PR threads and moves to **Reviewed** (it does **not** auto-loop). Chris decides at Reviewed.
- **Coder blocked:** Coder posts blocker comment + moves `Development → Ready for Design`, Discord-pings Chris. No automatic retry — surfaced immediately.

### The "Reviewed" circuit breaker (key decision)
Rather than an arbitrary "N fix cycles before surfacing" counter, the **Reviewed** state makes **Chris the circuit breaker**. The reviewer always stops at Reviewed and waits. Chris examines and either:

| Chris's action at Reviewed | What happens next |
|---|---|
| Move to **Done** | Chris merges the PR; poller detects Done → cleanup |
| Move to **Ready for Development** | Poller spawns a fix card; coder reads active PR threads, addresses each, replies + marks fixed, pushes to same branch/PR; ADO → In Review; reviewer re-reviews |

---

## 5. Agent profiles (4 profiles, not 3)

| Profile | Has Discord | ADO write | Git push | Purpose |
|---|---|---|---|---|
| `default` | yes | yes | no | Triage, ADO poller, cleanup, human interface (orchestrator — no OpenCode) |
| `planner` | no | read | no | Reads PRD, asks Chris questions, writes plan artifact |
| `coder` | no | read | **yes** | Reads plan, implements, opens PRs, addresses PR threads |
| `reviewer` | no | read + comment | **no** | Checks plan vs diff, posts inline PR threads, casts vote |

**PATs:** minimum two — one full-scope (for `default`/`coder`), one comment-only (for `reviewer`). **Reviewer never gets push permission — that's the whole point.**

The **missing role** in Chris's original 3-profile idea was the **planner**. Without a plan artifact as the reference object, you pay for planning twice (in coder and reviewer) and neither pass has a real contract. With it: planner front-loads questions, coder starts immediately, reviewer checks diff *against the plan* (a checkable question, not a judgment question).

### Per-profile config layout

```
~/.hermes/profiles/
  planner/   persona.md   opencode.yaml   conventions.md
  coder/     persona.md   opencode.yaml
  reviewer/  persona.md   opencode.yaml
```

`opencode.yaml` shape (model is configurable per profile — decision #1):
```yaml
# planner
model: anthropic/claude-sonnet-4   # strong reasoning — writes plans
agent: plan                         # no code writes
variant: high
# coder
model: anthropic/claude-sonnet-4   # swappable to cheaper model per ticket complexity
agent: build
variant: high
# reviewer
model: anthropic/claude-sonnet-4   # strong reasoning — the quality gate
agent: plan                         # plan agent for analysis, not code writing
variant: max
```

---

## 6. The Hermes profile is the context assembler; OpenCode is the worker

> Each profile gathers everything OpenCode needs, fires it, reads the output, then does the ADO write-back.

### Planner
1. Receives from Kanban card: `ticket_id`, PRD text, repo URL, conventions path, workspace path.
2. Creates `~/.hermes/workspaces/AB-1234/`, assembles `context.md` (PRD + repo structure + conventions), clones/pulls repo.
3. OpenCode call:
   ```bash
   opencode run --agent plan --model anthropic/claude-sonnet-4 \
     "You are a technical planner. Produce a structured implementation plan: acceptance criteria (testable), files to create/modify, data model / API changes, test strategy, open questions (list, do not block). Write to plan.md. Do not write code." \
     -f context.md
   ```
   (workdir = `~/.hermes/workspaces/AB-1234/`)
4. After exit: reads `plan.md`, posts it as ADO comment, moves `Design → Design Review`, `kanban_complete` with artifact path + session ID, Discord ping *"Plan ready on AB-1234"*.

### Coder
1. Receives: `ticket_id`, `plan_path`, `repo_path`, `branch`.
2. Creates a **git worktree** (isolated — never touches `main`): `git worktree add ../worktree feature/AB-1234`.
3. Assembles `task.md` (plan contents + rules: stay within planned files, run tests before commit, commit format `feat(AB-1234): ...`, do NOT open PR from inside OpenCode, write `BLOCKED.md` if tests fail after 2 attempts).
4. OpenCode call: `opencode run --agent build --model ... "$(cat task.md)"` (workdir = worktree).
5. **Success path:** verify commit → push branch → open PR via ADO API → post PR URL as ADO comment → move `Development → In Review` → `kanban_complete` (pr_url, session_id) → Discord ping *"PR open on AB-1234"*.
6. **Blocked path (surface immediately, no retry — decision #3):** read `BLOCKED.md` → post as ADO comment → move `Development → Ready for Design` → `kanban_block` → Discord ping *"🚧 Coder blocked on AB-1234 — design issue."*

### Reviewer
Preferred approach — manual diff vs plan (checks plan vs diff explicitly):
```bash
git diff main...feature/AB-1234 > /tmp/AB-1234.patch
opencode run --agent plan --variant max \
  "Review the PR diff against the plan. For each file: matches plan file list? acceptance criteria met? bugs/security/test gaps? Output verdict APPROVED / CHANGES_REQUESTED / DESIGN_ISSUE with inline comments (file+line+issue). Write to verdict.md" \
  -f plan.md -f /tmp/AB-1234.patch
```
After exit: posts each finding as an **inline PR thread** (active), posts summary comment, sets PR **vote**, moves `In Review → Reviewed`, `kanban_complete` with thread IDs, Discord ping. **Then stops and waits for Chris.**

---

## 7. PR thread lifecycle (the fix cycle)

Issues are raised as real inline PR threads via the ADO PR API, exactly like a human review:

1. **Reviewer** posts inline threads (`status: active`), sets vote "Waiting for Author", stores `comment_threads` (thread_id, file, line, issue) in `kanban_complete` metadata, moves to **Reviewed**.
2. **Chris** at Reviewed moves to Ready for Development (if fixes needed).
3. **Coder** (fix card, child of review card — inherits thread IDs): fetches each thread, addresses the issue in code, **replies to the thread** (`"Fixed in commit a3f9c12 — ..."`) and **marks it `fixed`** (`PATCH .../threads/{id}` status=fixed), pushes to the **same branch/PR**, moves back to `In Review`, Discord ping *"AB-1234 fixes pushed — 3/3 threads resolved"*.
4. **Reviewer** re-reviews the new diff, checks thread resolutions, re-votes → back to Reviewed.

ADO PR API endpoints used:
- `POST /pullRequests/{pr_id}/threads` — create inline thread (with `threadContext.filePath` + line range, `status: active`)
- `POST /pullRequests/{pr_id}/threads/{thread_id}/comments` — coder reply
- `PATCH /pullRequests/{pr_id}/threads/{thread_id}` — set `status: fixed`
- PR vote API — "Waiting for Author" (`-5`) / "Approved"

---

## 8. ADO integration — the `azure-devops` skill (existing, with two small extensions)

All four profiles and the default-profile poller reach ADO through a single skill: **`azure-devops`** (at `~/.hermes/skills/azure-devops/`). It already exists, already documents the 9 custom states, and already has the reviewer→coder thread pattern in its SKILL.md. **We do NOT build a new skill.** We extend this one in two small ways.

### 8.1 What the existing skill already covers

**Boards (work items)** — `azdo boards <feature>`:
- `list` — WIQL queries, `--assigned-to me`, `--state`, `--type`, `--format {json,table,markdown}`
- `show <id>` — fetch a single work item
- `create --type Bug|... --title ... --description ... --assigned-to me`
- `update <id> --state ... --comment "..." --add-tag ...` — state transitions + comment posting + tag add
- PAT scopes are explicit in the SKILL.md; the script exits with a clear error if env vars are missing

**Pull requests** — `azdo prs <feature>`:
- `list / show / create / comment / approve`
- PR vote integers already documented (`-5` waiting-for-author, `5` approved-with-suggestions, `10` approved, `-10` rejected)
- Thread status integers already documented (`1` active, `2` fixed, `3` won't fix, `4` closed, `255` unknown)
- The SKILL.md already contains the reviewer/coder thread SDK snippets (with the right `commentType: 1`, `threadContext.filePath` + line range, etc.) — but only as reference, not as CLI subcommands

**Auth — env-var-based, profile-scoped:**
- `AZDO_PAT`, `AZDO_ORG`, `AZDO_PROJECT` resolved from the *calling agent profile's* environment
- The PAT-split design we need for least-privilege falls out of this naturally: each profile gets its own env block with the right scope
  - `default` (poller) — full Work Items + Code read, write on Work Items
  - `planner` — read-only on Work Items (just fetches the PRD), no Code access
  - `coder` — full Work Items + Code (needs push/PR create)
  - `reviewer` — read + comment-only on Code, read-only on Work Items (just moves state to Reviewed, never pushes)

**Process template awareness** — the skill already knows Goldhub (Basic) uses `To Do/Doing/Done` and explicitly translates "active" → `To Do` for that project. For the dev-workflow project (custom inherited process), it already documents all 9 of our states.

### 8.2 Per-profile use of the skill

Each profile loads `azure-devops` on demand (via the `skills=[...]` arg when the Kanban card is created, or directly for the cron poller):

| Profile | Loads skill for | Commands it actually uses |
|---|---|---|
| `default` (poller) | yes | `boards list` (WIQL on Ready-for-Design / Ready-for-Development / In Review) for state-change detection; `boards update` (state transitions on cleanup, post Discord-archive comment) |
| `planner` | yes (read-only) | `boards show <id>` — fetches the PRD into `context.md`. No write commands. |
| `coder` | yes (full) | `prs create` (open PR after first commit); `prs thread reply` + `thread status fixed` (fix cycle); `boards update` (post PR URL as ADO comment, move Development→In Review); on blocked: `boards update --state "Ready for Design"` + comment with `BLOCKED.md` contents |
| `reviewer` | yes (comment-only PAT) | `prs thread create` (inline findings); PR vote via the SDK's `create_pull_request_reviewer`; `boards update` (post summary comment, move In Review→Reviewed) |

### 8.3 What the skill is missing — two small extensions to build

The skill is missing two things the workflow needs. Both are backward-compatible additions, not a rewrite.

**Extension A — add PR thread subcommands to `azdo prs` (must-have).**

Right now `azdo prs` covers list/show/create/comment/approve, but the reviewer→coder thread pattern is only documented as inline SDK code in the SKILL.md. The reviewer and coder would have to drop down to raw Python in every session and re-derive the same 4 SDK calls. Add three subcommands to `scripts/azdo_prs.py`:

```bash
# Reviewer: post an inline finding
azdo prs thread create <pr_id> --repo X --project P \
    --file /path/to/file.py --line 42 \
    --body "Issue description" --status active

# Coder: reply to a thread
azdo prs thread reply <pr_id> <thread_id> --repo X --project P \
    --body "Fixed in commit abc123 — parameterised query"

# Coder: mark a thread fixed
azdo prs thread status <pr_id> <thread_id> --repo X --project P \
    --status fixed
```

Implementation notes:
- Follow the existing `--repo` / `--project` / `--format` patterns in `azdo_prs.py`
- Use `argparse subparsers` with `parents=[common]` (the skill's own SKILL.md flags this as a regression trap — subparsers don't inherit parent optionals, must use the `parents=[common]` pattern)
- Mirror the `scripts/` path layout exactly: `SKILL_DIR = Path(__file__).resolve().parent` — **do NOT** append another `scripts/` when building paths (the existing dispatcher bug is documented as a regression trap)
- Status values: accept the integer (`1`/`2`/`3`/`4`) or the string (`active`/`fixed`/`wontfix`/`closed`) — string is friendlier for cron/poller use
- Add a smoke-test path to `scripts/smoke_test.sh` that creates a thread on a test PR, replies to it, marks it fixed, and asserts the state changes

**Extension B — handle the planner-clarification `Blocked` signal (small, your call).**

Design doc §10 has the planner posting questions and then needing to surface to Chris. Two options:

- **Option A (cheaper, recommended for v1):** don't add a 10th ADO state. Planner posts the question as an ADO comment, adds a `blocked` tag (`boards update --add-tag blocked`), and moves the ticket back to `Ready for Design`. The poller treats `Ready for Design + tag=blocked` as a clarification request and Discord-pings Chris with the comment + ticket link. **Works today** with existing skill commands; zero Project Settings work.

- **Option B (cleaner, future):** add `Blocked` as a 10th custom state. 2-min Project Settings change + update the skill's "dev-workflow project custom states" section to list 10 states. Poller treats `Blocked` as a special "needs Chris" state.

**Recommendation: Option A for v1.** Less moving parts, the tag signal is unambiguous, and we can add `Blocked` later if it gets noisy.

### 8.4 The poller's state-change detection — not a skill gap, but worth pinning down

The poller needs to detect *new* ticket arrivals in each phase, not just "list tickets in state X". The skill itself doesn't (and shouldn't) own this — it's a polling concern, not an ADO API concern. Two options:

- **Option 1 (recommended for v1): poll + diff.** Keep a small JSON file at `~/.hermes/workspaces/ado-poller-state.json` keyed on `{ticket_id: last_seen_state, last_changed_at}`. Each tick: `boards list --state "Ready for Design"`, diff against the file, create Kanban cards for new entries. ~50 lines of bash/Python, matches the design doc's ~5 min cadence, reuses the existing skill.

- **Option 2: ADO webhooks.** Register a service hook on the dev-workflow project for `workitem.updated`, push to a Hermes HTTP endpoint, dispatch from there. More real-time but more moving parts (webhook endpoint, signature verification, retry handling). Save for later if sub-minute responsiveness is ever needed.

**Recommendation: Option 1.** The design doc already calls for a 5-min poller; webhook infra is overkill for that.

### 8.5 Build order updated

Section 13 already lists the 9 build steps. With the ADO-skill extensions, fold two new prep steps in **before** the poller (Step 4):

| Step | What | Notes |
|---|---|---|
| 1 | ADO custom states (Project Settings) | Chris, ~10 min |
| 2 | Create 3 profiles + `opencode.yaml` + `persona.md` | Build together |
| **2a** | **Add `azdo prs thread create/reply/status` subcommands to `azure-devops` skill** | **Build (small) — required by reviewer + coder** |
| **2b** | **Set per-profile `AZDO_*` env vars (full-scope for `default`/`coder`, comment-only for `reviewer`)** | **Chris, ~10 min — generate the comment-only PAT** |
| 3 | ~~Kanban card schema/storage~~ | **Built in — skip** |
| 4 | ADO poller cron (detects state via poll+diff, calls `kanban_create`) | Build (~50 lines + state file) |
| 5 | Planner worker (context → OpenCode → `kanban_complete` → ADO write-back) | Build |
| 6 | Coder worker (worktree → OpenCode → PR → write-back; PR-thread fix flow) | Build |
| 7 | Reviewer worker (diff → OpenCode → verdict → inline PR threads → write-back) | Build |
| 8 | Cleanup cron (Done → archive + worktree removal) | Build |
| 9 | End-to-end test with one real ticket | Iterate |

Steps 2a and 2b are the new ADO-integration prep work. They're prerequisites for Steps 6 and 7 (coder + reviewer) which would otherwise need to inline raw Python in every session.

---

## 9. Hermes Kanban — what's built in (do NOT rebuild)

Step 3 ("Kanban card schema + storage") **drops off the build list** — Hermes Kanban is a full built-in system.

| Feature | API |
|---|---|
| Card creation | `kanban_create(title, assignee, body, parents=[])` |
| Dependency gating | `parents=[t1,t2]` — child stays `todo` until parents done |
| Worker dispatch | Dispatcher auto-spawns the right profile when a card hits `ready` |
| Heartbeats / stall detection | `kanban_heartbeat()` |
| Crash recovery | Dispatcher reclaims cards with no heartbeat after TTL (~15 min) |
| Block for human input | `kanban_block(reason=...)` |
| Completion handoff | `kanban_complete(summary, metadata={...})` — structured data downstream workers read |
| Comments | `kanban_comment(body=...)` |
| Git worktree workspace | `workspace="worktree"` — built-in worktree lifecycle |
| CLI | `hermes kanban list/show/block/complete/reclaim/reassign` |
| SQLite persistence | Cards survive crashes/restarts |
| Dashboard | `hermes kanban` — visual board with ⚠ badges |
| Goal mode | `goal_mode=True` — worker runs until acceptance criteria met |

### Card creation pattern (poller does this)
```python
plan_card = kanban_create(
    title="plan: AB-1234 — <title>", assignee="planner",
    body="ado_ticket: AB-1234\nprd: <PRD>\nworkspace: ~/.hermes/workspaces/AB-1234/\n"
         "opencode_model: anthropic/claude-sonnet-4\nopencode_agent: plan\nopencode_variant: high",
    workspace="worktree")
implement_card = kanban_create(title="implement: AB-1234", assignee="coder",
    body="...plan_path, branch, opencode_*...",
    parents=[plan_card["task_id"]], workspace="worktree")
review_card = kanban_create(title="review: AB-1234", assignee="reviewer",
    body="...", parents=[implement_card["task_id"]], workspace="worktree")
```

### Durability via OpenCode session resume (decision #4)
Store `opencode_session_id` in `kanban_complete` metadata. On reclaim of a stalled card:
```
session_id present? YES → opencode run --session ses_abc123 "Continue. Re-read plan.md and check git status first."
                    NO  → fresh start (fallback)
```
A crashed coder picks up its exact OpenCode session rather than starting cold.

---

## 10. The one exception — planner clarifications

The only time an agent breaks out to Discord instead of waiting on ADO: planner can't proceed without info not in the PRD. Pattern — planner posts questions as an ADO comment, moves ticket to a `Blocked`/`To Do` (tagged) state, Discord-pings Chris with question + ADO link. Chris answers on ADO, moves back to Ready for Design, poller picks it up.

---

## 11. Cleanup (default profile, decision #2)

When the poller detects a ticket moved to **Done**:
1. Read Kanban cards for the ticket (all phases).
2. Archive workspace: `tar -czf ~/.hermes/workspaces/archive/AB-1234.tar.gz ~/.hermes/workspaces/AB-1234/`
3. Remove worktree: `git worktree remove .../worktree --force`
4. `rm -rf ~/.hermes/workspaces/AB-1234/`
5. Mark all Kanban cards archived.
6. (Optional) Discord ping.

Archive gives a recoverable record of plan + reviewer verdict.

---

## 12. The ADO poller (the one piece of real plumbing)

Cron on the `default` profile, every ~5 min, **idempotent**:
```
for each ADO ticket in [Ready for Design, Ready for Development, In Review]:
    if no Kanban card for this ticket+phase exists: create it, assign correct profile
    if exists and in_progress: leave it
    if exists and failed: retry or Discord ping
```
Card ID keyed on `{ado-ticket-id}/{phase}` so running twice never duplicates cards. Because Kanban handles all state, the poller shrinks to "call `kanban_create` with the right args when ADO state changes" (~50 lines).

---

## 13. Build order

| Step | What | Who | Notes |
|---|---|---|---|
| 1 | ADO custom states (Project Settings → inherited process) | Chris | ~10 min. Map: To Do→Proposed; Design/Review/Dev states→Active; Done→Completed |
| 2 | Create 3 profiles + `opencode.yaml` + `persona.md` each | Build together | `default` already exists |
| 3 | ~~Kanban card schema/storage~~ | — | **Built in — skip** |
| 4 | ADO poller cron (detects state, calls `kanban_create`) | Build | small (~50 lines) |
| 5 | Planner worker (context → OpenCode → `kanban_complete` → ADO write-back) | Build | |
| 6 | Coder worker (worktree → OpenCode → PR → write-back; PR-thread fix flow) | Build | |
| 7 | Reviewer worker (diff → OpenCode → verdict → inline PR threads → write-back) | Build | |
| 8 | Cleanup cron (Done → archive + worktree removal) | Build | small |
| 9 | End-to-end test with one real ticket | Iterate | |

**Build philosophy:** get a minimal single-ticket loop working end to end first. Don't add parallel lanes, multiple reviewers, or Discord summaries until that loop is solid.

---

## 14. Prerequisites & environment

- **OpenCode** installed + authed (`opencode auth list` shows a provider). Watch binary resolution (`which -a opencode`); pin path if Hermes resolves a different binary.
- **ADO PAT** configured (done). Reviewer needs a comment-only PAT (least privilege).
- Plans live at `~/.hermes/workspaces/<ticket-id>/plan.md`; worktree at `~/.hermes/workspaces/<ticket-id>/worktree/`.
- Agent chat history is **ephemeral** — the durable state is the artifacts (plan file, branch, PR) + Kanban card metadata. A fresh worker reconstructs from those, not from chat.

---

## 15. Decisions locked in this discussion

1. **Models configurable per profile** — yes (`opencode.yaml` per profile).
2. **Cleanup owned by `default` profile** — yes (on Done).
3. **Blocked handling** — surface immediately, no automatic retry.
4. **Store OpenCode session ID** for crash-resume — yes.
5. **Reviewer raises inline PR threads**, coder addresses + replies + marks fixed on the same PR — yes.
6. **"Reviewed" state** added — Chris is the circuit breaker (replaces an N-cycle counter).
7. **Code platform** — Azure DevOps Repos + Boards.

---

## 16. Open / not-yet-done items

- **ADO custom states not yet created** (Step 1 — Chris's task).
- **Swimlane activity diagram** (cron / Chris / agents / artifacts / actions) was requested but **not yet produced** — outstanding deliverable.
- Persona + conventions files per profile — not yet written.
- Confirm exact ADO PR API auth/scopes for the comment-only reviewer PAT.
- Decide concurrency target (how many tickets in flight at once) — Kanban supports N worktrees/branches; no hard cap chosen yet.

---

## Reference artifact
- Visual state machine: `/home/cshelton/dev-workflow-state-machine.html` (v2, 9 states incl. amber Reviewed, two rejection arrows, PR thread resolution callout).
```

