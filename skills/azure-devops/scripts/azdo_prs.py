"""Azure DevOps Pull Requests — Git client.

Subcommands:
    list     [--project <p>] [--repo <r>] [--status active|completed|abandoned|all]
             [--source-branch <branch>] [--target-branch <branch>] [--created-by me|<user>]
    show     <pr_id> [--project <p>] [--repo <r>]
    create   --repo <r> --source <branch> --target <branch> --title <t> [--description <d>]
    comment  <pr_id> --body <text> [--project <p>] [--repo <r>]
    threads  <pr_id> [--status active|fixed|wontfix|closed|unknown] [--include-deleted] [--project <p>] [--repo <r>]
    approve  <pr_id> [--project <p>] [--repo <r>]
    assign   <pr_id> --user <email|display-name|identity-id> [--project <p>] [--repo <r>]
    link-work-item <pr_id> --work-item <id> [--project <p>] [--repo <r>]

Project is resolved from $AZDO_PROJECT unless --project is given.
Repo is required (PRs are repo-scoped in AZDO).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import azdo_common as ac  # noqa: E402


def _shorten_pr(pr) -> dict:
    repo = getattr(pr, "repository", None)
    reviewer_status = []
    for r in (getattr(pr, "reviewers", None) or []):
        reviewer_status.append({
            "name": getattr(r, "display_name", None) or getattr(r, "unique_name", None),
            "vote": r.vote,  # 10 = approved, 5 = approved w/ suggestions, 0 = no vote, -5 = waiting, -10 = rejected
            "required": getattr(r, "is_required", None),
        })
    return {
        "pull_request_id": pr.pull_request_id,
        "title": pr.title,
        "description": (pr.description or "")[:300] if pr.description else None,
        "status": pr.status,
        "created_by": getattr(pr.created_by, "display_name", None),
        "creation_date": pr.creation_date.isoformat() if pr.creation_date else None,
        "source_branch": pr.source_ref_name,
        "target_branch": pr.target_ref_name,
        "is_draft": pr.is_draft,
        "repo": getattr(repo, "name", None),
        "reviewers": reviewer_status,
        "url": getattr(pr, "url", None),
    }


def _branch_ref(branch: str | None) -> str | None:
    if not branch:
        return None
    return branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"


def cmd_list(args) -> None:
    from azure.devops.v7_1.git.models import GitPullRequestSearchCriteria

    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_git_client()

    status = args.status or "active"
    search = GitPullRequestSearchCriteria(
        status=status,
        repository_id=args.repo,
        source_ref_name=_branch_ref(args.source_branch),
        target_ref_name=_branch_ref(args.target_branch),
    )

    prs_iter = client.get_pull_requests(
        project=project,
        repository_id=args.repo,
        search_criteria=search,
        top=args.limit or 50,
    )
    prs = ac.flatten_paged([prs_iter]) if prs_iter else []
    rows = [_shorten_pr(p) for p in prs]

    # Optional creator filter (SDK does not expose createdBy in searchCriteria cleanly).
    if args.created_by and rows:
        target = args.created_by.lower()
        rows = [r for r in rows if (r.get("created_by") or "").lower() == target]

    ac.output(rows, args.format)


def cmd_show(args) -> None:
    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_git_client()
    pr = client.get_pull_request(
        project=project,
        repository_id=args.repo,
        pull_request_id=args.pr_id,
    )
    if not pr:
        ac.error(f"PR {args.pr_id} not found in repo {args.repo}")
    ac.output(_shorten_pr(pr), args.format)


def cmd_create(args) -> None:
    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_git_client()

    source = _branch_ref(args.source)
    target = _branch_ref(args.target)

    pr = client.create_pull_request(
        project=project,
        repository_id=args.repo,
        git_pull_request_to_create={
            "sourceRefName": source,
            "targetRefName": target,
            "title": args.title,
            "description": args.description or "",
        },
    )
    ac.output(_shorten_pr(pr), args.format)


def cmd_comment(args) -> None:
    from azure.devops.v7_1.git.models import Comment

    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_git_client()
    comment = Comment(content=args.body, comment_type=1)
    thread = client.create_thread(
        project=project,
        repository_id=args.repo,
        pull_request_id=args.pr_id,
        comment_thread={"comments": [comment], "status": 1},
    )
    ac.output({"thread_id": getattr(thread, "id", None), "pr_id": args.pr_id}, args.format)


# Status string <-> integer mapping for thread filter
_THREAD_STATUS = {
    "active": 1,
    "fixed": 2,
    "wontfix": 3,
    "closed": 4,
    "unknown": 255,
}


def _shorten_thread(t) -> dict:
    """Flatten a PR comment-thread SDK object into a JSON-friendly dict.

    Includes the file/line context and the first comment's text (the original
    finding), plus all replies in chronological order.  Status is the integer
    1/2/3/4/255 with a string label for readability.
    """
    ctx = getattr(t, "thread_context", None) or {}
    file_path = getattr(ctx, "file_path", None) if ctx else None
    right_start = getattr(ctx, "right_file_start", None) if ctx else None
    right_end = getattr(ctx, "right_file_end", None) if ctx else None

    def _line(rg):
        if rg is None:
            return None
        return getattr(rg, "line", None)

    status_int = getattr(t, "status", None)
    status_label = next((k for k, v in _THREAD_STATUS.items() if v == status_int), None)

    comments = []
    for c in (getattr(t, "comments", None) or []):
        author = getattr(c, "author", None)
        if isinstance(author, dict):
            author_name = author.get("displayName") or author.get("unique_name")
        else:
            author_name = getattr(author, "display_name", None) or getattr(author, "unique_name", None)
        comments.append({
            "id": getattr(c, "id", None),
            "author": author_name,
            "content": getattr(c, "content", None),
            "published_date": str(getattr(c, "published_date", "") or ""),
            "comment_type": getattr(c, "comment_type", None),
        })

    return {
        "id": getattr(t, "id", None),
        "status": status_int,
        "status_label": status_label,
        "is_deleted": getattr(t, "is_deleted", None),
        "file_path": file_path,
        "line_start": _line(right_start),
        "line_end": _line(right_end),
        "published_date": str(getattr(t, "published_date", "") or ""),
        "last_updated_date": str(getattr(t, "last_updated_date", "") or ""),
        "comments": comments,
    }


def cmd_threads(args) -> None:
    """List comment threads on a PR (review findings + replies)."""
    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_git_client()

    threads = client.get_threads(
        project=project,
        repository_id=args.repo,
        pull_request_id=args.pr_id,
    )

    rows = [_shorten_thread(t) for t in (threads or [])]

    if args.status:
        want = _THREAD_STATUS.get(args.status.lower())
        if want is None:
            ac.error(f"unknown --status '{args.status}'. Valid: {', '.join(_THREAD_STATUS)}")
        rows = [r for r in rows if r.get("status") == want]

    if not args.include_deleted:
        rows = [r for r in rows if not r.get("is_deleted")]

    ac.output(rows, args.format)


def cmd_approve(args) -> None:
    from azure.devops.v7_1.git.models import IdentityRefWithVote

    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_git_client()

    # SDK v7.1: add the calling user as a reviewer with vote=10 (approved).
    # The reviewer's `id` is required — we get it from the PR's created_by
    # (which on a fresh PR is the PAT owner). For more general use, this
    # could be replaced with a Graph lookup of any user.
    pr = client.get_pull_request(
        project=project,
        repository_id=args.repo,
        pull_request_id=args.pr_id,
    )
    if not pr or not getattr(pr, "created_by", None) or not pr.created_by.id:
        ac.error(
            f"could not determine the calling user's id for PR {args.pr_id}. "
            "Pass --user <id> to approve as a specific user."
        )
    voter = IdentityRefWithVote(id=pr.created_by.id, vote=10)
    result = client.create_pull_request_reviewer(
        reviewer=voter,
        repository_id=args.repo,
        pull_request_id=args.pr_id,
        reviewer_id=pr.created_by.id,
        project=project,
    )
    ac.output({
        "pr_id": args.pr_id,
        "vote": getattr(result, "vote", None),
        "reviewer": getattr(result, "display_name", None) or getattr(result, "unique_name", None),
    }, args.format)


def _short_identity_name(identity) -> str | None:
    return (
        getattr(identity, "display_name", None)
        or getattr(identity, "provider_display_name", None)
        or getattr(identity, "principal_name", None)
        or getattr(identity, "mail_address", None)
        or getattr(identity, "unique_name", None)
    )


def _resolve_reviewer(conn, query: str) -> dict:
    """Resolve an ADO user by identity id, email/principal name, or display name.

    Email/principal-name exact matches are preferred because display names are
    not unique in Azure DevOps organizations. Returns the IdentityRef fields
    needed by create_pull_request_reviewer().
    """
    q = query.strip()
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", q):
        return {"id": q, "display_name": None, "unique_name": None, "descriptor": None}

    graph_client = conn.clients.get_graph_client()
    identity_client = conn.clients.get_identity_client()

    users_page = graph_client.list_users()
    users = list(getattr(users_page, "graph_users", None) or getattr(users_page, "value", None) or [])
    q_lower = q.lower()

    email_matches = [
        u for u in users
        if q_lower in {
            str(getattr(u, "principal_name", "") or "").lower(),
            str(getattr(u, "mail_address", "") or "").lower(),
        }
    ]
    display_matches = [u for u in users if str(getattr(u, "display_name", "") or "").lower() == q_lower]
    contains_matches = [u for u in users if q_lower in str(getattr(u, "display_name", "") or "").lower()]

    matches = email_matches or display_matches or contains_matches
    if not matches:
        ac.error(f"could not resolve ADO user '{query}'. Prefer an email/principal name.")
    if len(matches) > 1:
        choices = []
        for u in matches:
            choices.append({
                "display_name": getattr(u, "display_name", None),
                "principal_name": getattr(u, "principal_name", None),
                "mail_address": getattr(u, "mail_address", None),
                "descriptor": getattr(u, "descriptor", None),
            })
        ac.error(f"user '{query}' matched multiple ADO identities; use an email/principal name. Matches: {choices}")

    user = matches[0]
    descriptor = getattr(user, "descriptor", None)
    identities = identity_client.read_identities(subject_descriptors=descriptor, query_membership="None") if descriptor else []
    identity = identities[0] if identities else None
    identity_id = getattr(identity, "id", None)
    if not identity_id:
        ac.error(f"resolved '{query}' but could not map graph descriptor to an ADO identity id")

    return {
        "id": identity_id,
        "display_name": getattr(user, "display_name", None) or _short_identity_name(identity),
        "unique_name": getattr(user, "principal_name", None) or getattr(user, "mail_address", None),
        "descriptor": descriptor,
    }


def cmd_assign(args) -> None:
    """Assign a PR to a user by adding them as a reviewer.

    Azure Repos PRs do not have a separate task-style assignee field. The
    closest native assignment is the reviewers list, so this command resolves
    the user and adds them as a reviewer with vote=0 (no vote yet).
    """
    from azure.devops.v7_1.git.models import IdentityRefWithVote

    conn = ac.get_connection()
    project = ac.get_project(args.project)
    client = conn.clients.get_git_client()

    pr = client.get_pull_request(
        project=project,
        repository_id=args.repo,
        pull_request_id=args.pr_id,
    )
    if not pr:
        ac.error(f"PR {args.pr_id} not found in repo {args.repo}")

    reviewer = _resolve_reviewer(conn, args.user)
    if args.resolve_only:
        ac.output({"resolved": True, "reviewer": reviewer}, args.format)
        return

    voter = IdentityRefWithVote(id=reviewer["id"], vote=0, is_required=args.required)
    try:
        result = client.create_pull_request_reviewer(
            reviewer=voter,
            repository_id=args.repo,
            pull_request_id=args.pr_id,
            reviewer_id=reviewer["id"],
            project=project,
        )
    except Exception as exc:
        ac.error(f"failed to assign reviewer to PR {args.pr_id}: {exc}")
        return
    ac.output({
        "assigned": True,
        "pr_id": args.pr_id,
        "repo": args.repo,
        "reviewer_id": reviewer["id"],
        "reviewer": getattr(result, "display_name", None) or reviewer.get("display_name"),
        "unique_name": getattr(result, "unique_name", None) or reviewer.get("unique_name"),
        "vote": getattr(result, "vote", None),
        "required": getattr(result, "is_required", None),
    }, args.format)


def _pr_artifact_url(pr) -> str:
    """Return the ArtifactLink URL Azure Boards expects for an ADO Git PR."""
    repo = getattr(pr, "repository", None)
    project_ref = getattr(repo, "project", None)
    project_id = getattr(project_ref, "id", None)
    repo_id = getattr(repo, "id", None)
    pr_id = getattr(pr, "pull_request_id", None)
    if not project_id or not repo_id or not pr_id:
        ac.error("could not build PR artifact URL: missing project id, repo id, or PR id")
    return f"vstfs:///Git/PullRequestId/{project_id}/{repo_id}/{pr_id}"


def cmd_link_work_item(args) -> None:
    """Link an Azure Repos PR to an Azure Boards work item.

    Azure Boards stores PR links as a work-item ArtifactLink relation. The URL
    is not the human/API PR URL; it must be the internal vstfs PR artifact URL:
    vstfs:///Git/PullRequestId/{projectId}/{repositoryId}/{pullRequestId}
    """
    from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation

    conn = ac.get_connection()
    project = ac.get_project(args.project)
    git_client = conn.clients.get_git_client()
    wit_client = conn.clients.get_work_item_tracking_client()

    pr = git_client.get_pull_request(
        project=project,
        repository_id=args.repo,
        pull_request_id=args.pr_id,
    )
    if not pr:
        ac.error(f"PR {args.pr_id} not found in repo {args.repo}")

    artifact_url = _pr_artifact_url(pr)

    wi = wit_client.get_work_item(id=args.work_item, project=project, expand="Relations")
    if not wi:
        ac.error(f"work item {args.work_item} not found")

    for rel in (getattr(wi, "relations", None) or []):
        if getattr(rel, "rel", None) == "ArtifactLink" and getattr(rel, "url", None) == artifact_url:
            ac.output({
                "linked": False,
                "already_linked": True,
                "pr_id": args.pr_id,
                "work_item": args.work_item,
                "repo": getattr(getattr(pr, "repository", None), "name", args.repo),
                "artifact_url": artifact_url,
            }, args.format)
            return

    patch = [JsonPatchOperation(
        op="add",
        path="/relations/-",
        value={
            "rel": "ArtifactLink",
            "url": artifact_url,
            "attributes": {"name": "Pull Request"},
        },
    )]
    result = wit_client.update_work_item(
        document=patch,
        id=args.work_item,
        project=project,
        validate_only=args.validate_only,
    )
    ac.output({
        "linked": not args.validate_only,
        "validated": bool(args.validate_only),
        "already_linked": False,
        "pr_id": args.pr_id,
        "work_item": args.work_item,
        "repo": getattr(getattr(pr, "repository", None), "name", args.repo),
        "artifact_url": artifact_url,
        "work_item_rev": getattr(result, "rev", None),
    }, args.format)


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", help="Override $AZDO_PROJECT")
    common.add_argument("--repo", help="Repository name or ID (required for show/create/comment/approve)")
    common.add_argument("--format", choices=["json", "table", "markdown"], default="json")

    p = argparse.ArgumentParser(
        prog="azdo prs",
        description="Azure DevOps PR CLI",
        parents=[common],
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List pull requests", parents=[common])
    p_list.add_argument("--status", choices=["active", "completed", "abandoned", "all"], default="active")
    p_list.add_argument("--source-branch", help="Filter by source branch (refs/heads/ optional)")
    p_list.add_argument("--target-branch", help="Filter by target branch (refs/heads/ optional)")
    p_list.add_argument("--created-by", help="Filter by creator display name")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show one PR", parents=[common])
    p_show.add_argument("pr_id", type=int)
    p_show.set_defaults(func=cmd_show)

    p_create = sub.add_parser("create", help="Create a PR", parents=[common])
    p_create.add_argument("--source", required=True, help="Source branch (refs/heads/ optional)")
    p_create.add_argument("--target", required=True, help="Target branch (refs/heads/ optional)")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--description")
    p_create.set_defaults(func=cmd_create)

    p_comment = sub.add_parser("comment", help="Add a comment to a PR", parents=[common])
    p_comment.add_argument("pr_id", type=int)
    p_comment.add_argument("--body", required=True)
    p_comment.set_defaults(func=cmd_comment)

    p_threads = sub.add_parser("threads", help="List comment threads on a PR (review findings + replies)", parents=[common])
    p_threads.add_argument("pr_id", type=int)
    p_threads.add_argument("--status", help="Filter by status: active|fixed|wontfix|closed|unknown")
    p_threads.add_argument("--include-deleted", action="store_true", help="Include deleted threads")
    p_threads.set_defaults(func=cmd_threads)

    p_approve = sub.add_parser("approve", help="Approve a PR", parents=[common])
    p_approve.add_argument("pr_id", type=int)
    p_approve.set_defaults(func=cmd_approve)

    p_assign = sub.add_parser("assign", help="Assign a PR by adding a reviewer", parents=[common])
    p_assign.add_argument("pr_id", type=int)
    p_assign.add_argument("--user", required=True, help="Reviewer email/principal name, display name, or ADO identity id")
    p_assign.add_argument("--required", action="store_true", help="Mark reviewer as required")
    p_assign.add_argument("--resolve-only", action="store_true", help="Resolve the user without changing the PR")
    p_assign.set_defaults(func=cmd_assign)

    p_link_wi = sub.add_parser("link-work-item", help="Link a PR to an Azure Boards work item", parents=[common])
    p_link_wi.add_argument("pr_id", type=int)
    p_link_wi.add_argument("--work-item", type=int, required=True, help="Work item ID to link to the PR")
    p_link_wi.add_argument("--validate-only", action="store_true", help="Validate the relation patch without saving it")
    p_link_wi.set_defaults(func=cmd_link_work_item)

    args = p.parse_args()
    if not getattr(args, "repo", None) and args.cmd in ("show", "create", "comment", "threads", "approve", "assign", "link-work-item"):
        p.error(f"--repo is required for `azdo prs {args.cmd}`")
    args.func(args)


if __name__ == "__main__":
    main()
