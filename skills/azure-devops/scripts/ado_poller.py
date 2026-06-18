#!/usr/bin/env python3
"""
ADO Poller — phase dispatcher for the Hermes dev-workflow pipeline.

What it does each tick:
  Ready for Development  →  Development  (assign to Coding Agent)
  Ready for Review       →  In Review    (assign to Review Agent)

State file: ~/.hermes/workspaces/ado-poller-state.json
  Tracks {ticket_id: {phase, dispatched_at}} so each ticket is only
  dispatched once per phase.  Re-queuing (e.g. coder sends back for rework)
  is handled by clearing the phase key when the ticket leaves and re-enters
  a Ready state.

Exit codes:
  0 — ran OK (may have dispatched 0 tickets)
  1 — error (missing env, API failure, etc.)
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Resolve the azdo venv SDK ────────────────────────────────────────────────
AZDO_VENV = Path.home() / ".hermes/venvs/azdo"
SKILL_SCRIPTS = Path.home() / ".hermes/skills/azure-devops/scripts"

for _p in [str(AZDO_VENV / "lib/python3.12/site-packages"),
           str(AZDO_VENV / "lib/python3.11/site-packages"),
           str(SKILL_SCRIPTS)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import azdo_common as ac  # noqa: E402
except ImportError as e:
    print(f"ERROR: cannot import azdo_common: {e}", file=sys.stderr)
    sys.exit(1)

# ── Config ───────────────────────────────────────────────────────────────────
STATE_FILE = Path.home() / ".hermes/workspaces/ado-poller-state.json"
# Project name is resolved by the skill helper, which checks $GOLDHUB_AZDO_PROJECT
# first, then falls back to the legacy $AZDO_PROJECT. Centralised here so
# per-profile env blocks (default, goldhub-designer, goldhub-coder, …) all work
# with the GOLDHUB_AZDO_* prefix without each caller re-implementing fallback.
PROJECT = ac.get_project()

DISPATCH_RULES = [
    {
        "from_state": "Ready for Design",
        "to_state": "Design",
        "assign_to": "agent.cshelton@gmail.com",
        "label": "Design Agent",
        "phase": "design",
    },
    {
        "from_state": "Ready for Development",
        "to_state": "Development",
        "assign_to": "dev.cshelton@gmail.com",
        "label": "Coding Agent",
        "phase": "development",
    },
    {
        "from_state": "Ready for Review",
        "to_state": "Review",
        "assign_to": "kcaonsirhc@gmail.com",
        "label": "Review Agent",
        "phase": "review",
    },
]

# ── State helpers ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def state_key(ticket_id: int, phase: str, dispatched_at: str) -> str:
    return f"goldhub/{ticket_id}/{phase}/{dispatched_at}"


# ── ADO helpers ───────────────────────────────────────────────────────────────
def query_state(client, project: str, state_name: str) -> list[dict]:
    """Return list of {id, title, assigned_to} for tasks in the given state."""
    from azure.devops.v7_1.work_item_tracking.models import Wiql

    query = (
        f"SELECT [System.Id], [System.Title], [System.AssignedTo] "
        f"FROM WorkItems "
        f"WHERE [System.TeamProject] = '{project}' "
        f"AND [System.State] = '{state_name}' "
        f"AND [System.WorkItemType] = 'Task' "
        f"ORDER BY [System.ChangedDate] ASC"
    )
    refs = ac.wiql(client, project, query)
    if not refs:
        return []

    ids = [r.id for r in refs if getattr(r, "id", None) is not None]
    full = client.get_work_items(
        ids=ids, project=project,
        fields=["System.Id", "System.Title", "System.AssignedTo", "System.State"]
    )
    results = []
    for wi in (full or []):
        f = wi.fields
        assigned = f.get("System.AssignedTo")
        if isinstance(assigned, dict):
            assigned = assigned.get("displayName", "")
        results.append({
            "id": wi.id,
            "title": f.get("System.Title", ""),
            "assigned_to": assigned,
        })
    return results


def dispatch_ticket(client, project: str, ticket_id: int,
                    to_state: str, assign_to: str) -> bool:
    """Update ticket state + assignment. Returns True on success."""
    from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation

    patches = [
        JsonPatchOperation(op="add", path="/fields/System.State", value=to_state),
        JsonPatchOperation(op="add", path="/fields/System.AssignedTo", value=assign_to),
        JsonPatchOperation(
            op="add",
            path="/fields/System.History",
            value=f"[ADO Poller] Dispatched → {to_state} and assigned to {assign_to}",
        ),
    ]
    try:
        client.update_work_item(document=patches, id=ticket_id, project=project)
        return True
    except Exception as e:
        print(f"  ERROR updating ticket {ticket_id}: {e}", file=sys.stderr)
        return False


# ── Kanban card creation ──────────────────────────────────────────────────────
# Worker profiles that handle each phase. The poller only knows the mapping;
# the worker itself fetches its own context from ADO (PRD / comments / PR
# threads) at the start of its run.
PHASE_ASSIGNEE = {
    "design": "goldhub-designer",
    "development": "goldhub-coder",
    "review": "goldhub-reviewer",
}

# Skills that MUST be force-loaded into the worker when its card is created.
# Without this, the worker only has the SOUL.md hint telling it to load a
# skill — it has to remember to do so, and a freshly-spawned agent often
# doesn't. All three phase workers need `azure-devops` to read ADO context.
# Extend per-phase as new skills become mandatory for a role.
PHASE_SKILLS = {
    "design": ["azure-devops"],
    "development": ["azure-devops"],
    "review": ["azure-devops"],
}


def build_kanban_body(ticket_id: int, phase: str, title: str) -> str:
    """Minimal card body. Worker fetches the rest from ADO."""
    lines = [
        f"ado_ticket: {ticket_id}",
        f"ado_project: {PROJECT}",
        f"phase: {phase}",
        f"workspace: ~/.hermes/workspaces/{ticket_id}/",
        f"branch: task/{ticket_id}" if phase != "design" else "branch: (created by coder on first dev dispatch)",
        "",
        f"title: {title}",
        "",
        "Worker: fetch your own context from ADO at the start of your run:",
        "  - PRD (System.Description) → write to <workspace>/PRD.md",
        "  - full comment thread → write to <workspace>/task_comments.md",
        "  - if fix-card or review phase, fetch active PR threads → write to <workspace>/pr_threads.md",
        "Use the `azure-devops` skill (`azdo boards show <id>`, `azdo boards comments <id>`, `azdo prs threads ...`).",
    ]
    return "\n".join(lines)


def create_kanban_card(ticket_id: int, phase: str, title: str) -> dict | None:
    """Create a Kanban card for the given phase. Idempotent on {ticket_id}/{phase}.

    Returns the parsed card JSON (dict) on success, None on failure.
    """
    assignee = PHASE_ASSIGNEE.get(phase)
    if not assignee:
        print(f"  ERROR: no assignee mapping for phase '{phase}'", file=sys.stderr)
        return None

    body = build_kanban_body(ticket_id, phase, title)
    title_card = f"{phase}: #{ticket_id} — {title}"

    cmd = [
        "hermes", "kanban", "create",
        title_card,
        "--assignee", assignee,
        "--body", body,
    ]
    # Force-load the phase's mandatory skills. Repeatable flag, so one
    # --skill per entry.
    for skill in PHASE_SKILLS.get(phase, []):
        cmd += ["--skill", skill]
    if phase == "development":
        cmd += ["--workspace", "worktree", "--branch", f"task/{ticket_id}"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"  ERROR: kanban create failed for #{ticket_id}/{phase}: {result.stderr.strip()}", file=sys.stderr)
            return None
        # `hermes kanban create` prints something like "Created task #N" — we
        # only need success/failure here, not the id. But parse the last line
        # in case it's useful for the summary.
        return {"stdout": result.stdout.strip(), "cmd": " ".join(cmd)}
    except Exception as e:
        print(f"  ERROR: kanban create raised for #{ticket_id}/{phase}: {e}", file=sys.stderr)
        return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    conn = ac.get_connection()
    wit = conn.clients.get_work_item_tracking_client()
    state = load_state()

    dispatched = []
    errors = []

    for rule in DISPATCH_RULES:
        from_state = rule["from_state"]
        to_state = rule["to_state"]
        assign_to = rule["assign_to"]
        label = rule["label"]
        phase = rule["phase"]

        tickets = query_state(wit, PROJECT, from_state)

        for ticket in tickets:
            tid = ticket["id"]
            title = ticket["title"]
            now_iso = datetime.now(timezone.utc).isoformat()
            key = state_key(tid, phase, now_iso)

            print(f"  → Dispatching #{tid} '{title}': {from_state} → {to_state} ({label})")
            ok = dispatch_ticket(wit, PROJECT, tid, to_state, assign_to)

            if ok:
                # Now spawn the Kanban card so the worker picks it up.
                kanban = create_kanban_card(tid, phase, title)
                if kanban is None:
                    errors.append(f"#{tid} '{title}' (kanban create failed)")
                    continue

                state[key] = {
                    "dispatched_at": now_iso,
                    "from_state": from_state,
                    "to_state": to_state,
                    "assign_to": assign_to,
                    "title": title,
                }
                dispatched.append({
                    "id": tid,
                    "title": title,
                    "from": from_state,
                    "to": to_state,
                    "assigned": label,
                    "kanban": kanban["stdout"],
                })
            else:
                errors.append(f"#{tid} '{title}'")

    save_state(state)

    # ── Output summary ────────────────────────────────────────────────────────
    if not dispatched and not errors:
        return 0  # silent — no stdout = no delivery in no_agent mode

    lines = ["**ADO Poller** dispatched:"]
    for d in dispatched:
        lines.append(f"  • **#{d['id']}** _{d['title']}_ → **{d['to']}** (assigned to {d['assigned']})")
        if d.get("kanban"):
            lines.append(f"    ↳ kanban: {d['kanban']}")
    if errors:
        lines.append(f"\n⚠️ Failed: {', '.join(errors)}")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
