"""Azure DevOps Boards — work items.

Subcommands:
    list     --assigned-to <user> [--state <state>] [--type <type>] [--limit N]
    show     <id>
    comments <id> [--since ISO_DATE] [--include-deleted]
    create   --type <type> --title <title> [--description <text>] [--assigned-to <user>]
    update   <id> [--state <state>] [--assigned-to <user>] [--comment <text>] [--add-tag <tag>] [--blocked yes|clear]

Examples:
    python azdo_boards.py list --assigned-to me --state Active
    python azdo_boards.py show 12345
    python azdo_boards.py comments 12345                  # read all comments
    python azdo_boards.py comments 12345 --since 2026-06-15
    python azdo_boards.py create --type Bug --title "Login broken" --assigned-to me
    python azdo_boards.py update 12345 --state Closed --comment "Fixed in #456"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow importing azdo_common when invoked as a script.
sys.path.insert(0, str(Path(__file__).parent))
import azdo_common as ac  # noqa: E402


BLOCKED_FIELD = "Microsoft.VSTS.CMMI.Blocked"


def _shorten(wi) -> dict:
    """Flatten a work item SDK object into a Discord-friendly dict.

    In SDK v7.1, `wi.fields` is a plain dict (not a model object), so we
    just read from it directly. Older SDK versions used a model with
    `.additional_properties`; we handle both for safety.
    """
    f = wi.fields
    if isinstance(f, dict):
        fields = f
    else:
        fields = getattr(f, "additional_properties", {}) or {}

    assigned = fields.get("System.AssignedTo")
    if isinstance(assigned, dict):
        assigned_to = assigned.get("displayName")
    else:
        assigned_to = assigned

    return {
        "id": wi.id,
        "rev": getattr(wi, "rev", None),
        "type": fields.get("System.WorkItemType"),
        "title": fields.get("System.Title"),
        "state": fields.get("System.State"),
        "description": fields.get("System.Description"),
        "assigned_to": assigned_to,
        "blocked": fields.get(BLOCKED_FIELD),
        "created": fields.get("System.CreatedDate"),
        "changed": fields.get("System.ChangedDate"),
        "tags": fields.get("System.Tags"),
    }


def cmd_list(args) -> None:
    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_work_item_tracking_client()

    # Build WIQL incrementally. Use [Me] token for --assigned-to me.
    assigned = args.assigned_to or ""
    if assigned.lower() == "me":
        assigned_clause = "[Assigned To] = @Me"
    elif assigned:
        assigned_clause = f"[Assigned To] = '{assigned.replace(chr(39), chr(39)*2)}'"
    else:
        assigned_clause = ""

    clauses = [f"[System.TeamProject] = '{project}'"]
    if args.state:
        clauses.append(f"[System.State] = '{args.state}'")
    if args.type:
        clauses.append(f"[System.WorkItemType] = '{args.type}'")
    if assigned_clause:
        clauses.append(assigned_clause)

    where = " AND ".join(clauses)
    query = (
        f"SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType], "
        f"[System.AssignedTo], [System.CreatedDate], [System.ChangedDate], [System.Tags] "
        f"FROM WorkItems WHERE {where} ORDER BY [System.ChangedDate] DESC"
    )

    refs = ac.wiql(client, project, query)
    if not refs:
        ac.output([], args.format)
        return

    ids = [r.id for r in refs if getattr(r, "id", None) is not None]
    if args.limit and args.limit > 0:
        ids = ids[: args.limit]

    if not ids:
        ac.output([], args.format)
        return

    # Batch-fetch full work items (one round trip).
    full = client.get_work_items(ids=ids, project=project, expand="Fields")
    rows = [_shorten(w) for w in (full or [])]
    ac.output(rows, args.format)


def cmd_show(args) -> None:
    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_work_item_tracking_client()
    wi = client.get_work_item(id=args.id, project=project, expand="All")
    if not wi:
        ac.error(f"work item {args.id} not found")
    ac.output(_shorten(wi), args.format)


def _shorten_comment(c) -> dict:
    """Flatten a work-item comment SDK object into a JSON-friendly dict."""
    author = getattr(c, "created_by", None)
    if isinstance(author, dict):
        author_name = author.get("displayName") or author.get("unique_name")
    else:
        author_name = getattr(author, "display_name", None) or getattr(author, "unique_name", None)

    modifier = getattr(c, "modified_by", None)
    if isinstance(modifier, dict):
        modifier_name = modifier.get("displayName") or modifier.get("unique_name")
    else:
        modifier_name = getattr(modifier, "display_name", None) or getattr(modifier, "unique_name", None)

    return {
        "id": getattr(c, "id", None),
        "text": getattr(c, "text", None),
        "author": author_name,
        "created_date": str(getattr(c, "created_date", "") or ""),
        "modified_by": modifier_name,
        "modified_date": str(getattr(c, "modified_date", "") or ""),
        "is_deleted": getattr(c, "is_deleted", None),
    }


def cmd_comments(args) -> None:
    """Read the comment thread for a work item (Chris's feedback, agent clarifications)."""
    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_work_item_tracking_client()

    comments = []
    continuation = None
    while True:
        result = client.get_comments(
            project=project,
            work_item_id=args.id,
            top=200,
            continuation_token=continuation,
        )
        batch = getattr(result, "comments", None) or []
        comments.extend(_shorten_comment(c) for c in batch)
        continuation = getattr(result, "continuation_token", None)
        if not continuation:
            break

    if args.since:
        # Normalise both sides to plain "YYYY-MM-DD HH:MM:SS" so string
        # comparison works whether the user passes "2026-06-15" (date only),
        # "2026-06-15T05:00:00" (ISO T), or "2026-06-15 05:00:00" (space).
        def _norm(s: str) -> str:
            s = (s or "").replace("T", " ")
            # Trim anything past seconds: "2026-06-15 05:00:00.123+00:00" -> "2026-06-15 05:00:00"
            if len(s) >= 19 and s[10] == " ":
                return s[:19]
            return s

        cutoff = _norm(args.since)
        comments = [c for c in comments if _norm(c.get("created_date") or "") >= cutoff]

    if not args.include_deleted:
        comments = [c for c in comments if not c.get("is_deleted")]

    # Chronological order (oldest first) so feedback reads naturally
    comments.sort(key=lambda c: c.get("created_date") or "")

    ac.output(comments, args.format)


def cmd_create(args) -> None:
    from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation

    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_work_item_tracking_client()

    patches = [
        JsonPatchOperation(op="add", path="/fields/System.Title", value=args.title),
    ]
    if args.type:
        patches.append(JsonPatchOperation(op="add", path="/fields/System.WorkItemType", value=args.type))
    if args.description:
        patches.append(JsonPatchOperation(op="add", path="/fields/System.Description", value=args.description))
    if args.assigned_to:
        patches.append(JsonPatchOperation(op="add", path="/fields/System.AssignedTo", value=args.assigned_to))

    wi = client.create_work_item(document=patches, project=project, type=args.type or "Task")
    ac.output(_shorten(wi), args.format)


def cmd_update(args) -> None:
    from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation

    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_work_item_tracking_client()

    # Guard: never modify System.Description. This tool is read-only for
    # descriptions. Agents that need to set a description must use a dedicated
    # subcommand, not the generic update path.
    if args.description is not None:
        ac.error(
            "refusing to modify System.Description via azdo boards update. "
            "This tool does not support description changes."
        )

    patches = []
    if args.state:
        patches.append(JsonPatchOperation(op="add", path="/fields/System.State", value=args.state))
    if args.assigned_to:
        patches.append(JsonPatchOperation(op="add", path="/fields/System.AssignedTo", value=args.assigned_to))
    if args.add_tag:
        patches.append(JsonPatchOperation(op="add", path="/fields/System.Tags", value=args.add_tag))
    if args.comment:
        patches.append(JsonPatchOperation(
            op="add",
            path="/fields/System.History",
            value=args.comment,
        ))
    if args.blocked:
        blocked = args.blocked.lower()
        if blocked in ("yes", "true", "set", "blocked", "1"):
            patches.append(JsonPatchOperation(
                op="add",
                path=f"/fields/{BLOCKED_FIELD}",
                value="Yes",
            ))
        elif blocked in ("no", "false", "clear", "unblocked", "0"):
            # ADO's Blocked field is blank-or-Yes, so clearing removes the field.
            patches.append(JsonPatchOperation(
                op="remove",
                path=f"/fields/{BLOCKED_FIELD}",
            ))
        else:
            ac.error("--blocked must be one of: yes/no, true/false, set/clear, blocked/unblocked")

    if not patches:
        ac.error("update requires at least one of: --state, --assigned-to, --add-tag, --comment, --blocked")

    wi = client.update_work_item(document=patches, id=args.id, project=project)
    ac.output(_shorten(wi), args.format)


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", help="Override $AZDO_PROJECT")
    common.add_argument("--format", choices=["json", "table", "markdown"], default="json")

    p = argparse.ArgumentParser(
        prog="azdo boards",
        description="Azure DevOps Boards CLI",
        parents=[common],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List work items", parents=[common])
    p_list.add_argument("--assigned-to", help="Filter by assignee, or 'me'")
    p_list.add_argument("--state", help="Filter by state, e.g. Active/Closed/New")
    p_list.add_argument("--type", help="Filter by work item type, e.g. Bug/Task/User Story")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show one work item", parents=[common])
    p_show.add_argument("id", type=int)
    p_show.set_defaults(func=cmd_show)

    p_comments = sub.add_parser("comments", help="Read the comment thread for a work item", parents=[common])
    p_comments.add_argument("id", type=int, help="Work item ID")
    p_comments.add_argument("--since", help="Only return comments created on/after this ISO date (e.g. 2026-06-15)")
    p_comments.add_argument("--include-deleted", action="store_true", help="Include deleted comments")
    p_comments.set_defaults(func=cmd_comments)

    p_create = sub.add_parser("create", help="Create a work item", parents=[common])
    p_create.add_argument("--type", default="Task", help="Work item type (default: Task)")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--description")
    p_create.add_argument("--assigned-to")
    p_create.set_defaults(func=cmd_create)

    p_update = sub.add_parser("update", help="Update a work item", parents=[common])
    p_update.add_argument("id", type=int)
    p_update.add_argument("--state")
    p_update.add_argument("--assigned-to")
    p_update.add_argument("--comment")
    p_update.add_argument("--add-tag")
    p_update.add_argument(
        "--blocked",
        help="Set or clear Microsoft.VSTS.CMMI.Blocked. Accepts yes/no, true/false, set/clear.",
    )
    p_update.add_argument(
        "--description",
        help="(BLOCKED) Modifying description via this command is intentionally disabled.",
    )
    p_update.set_defaults(func=cmd_update)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
