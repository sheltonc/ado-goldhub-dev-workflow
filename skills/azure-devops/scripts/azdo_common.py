"""Azure DevOps skill — common helpers.

Auth: PAT via $GOLDHUB_AZDO_PAT (falls back to $AZDO_PAT),
org URL via $GOLDHUB_AZDO_ORG (falls back to $AZDO_ORG),
default project via $GOLDHUB_AZDO_PROJECT (falls back to $AZDO_PROJECT).

The GOLDHUB_AZDO_* prefix is preferred because it's project-scoped (one ADO
project per Hermes profile is the goal — e.g. a `coder-goldhub` profile and a
`designer-goldhub` profile each carry their own org/pat/project). The bare
AZDO_* names are accepted as a fallback so legacy callers keep working.

All three are loaded from the agent profile's environment. There is no fallback
to config files or CLI flags — env-only by design (see SKILL.md).

Usage from feature scripts:
    from azdo_common import get_connection, get_project, output, error
    conn = get_connection()
    project = get_project()  # or pass explicit override
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Iterable

try:
    from azure.devops.connection import Connection
    from msrest.authentication import BasicAuthentication
except ImportError as e:  # pragma: no cover
    sys.stderr.write(
        "ERROR: azure-devops SDK not installed. Run:\n"
        "  uv pip install --python ~/.hermes/venvs/azdo/bin/python azure-devops\n"
        "or activate the venv before running azdo commands.\n"
        f"  ({e})\n"
    )
    sys.exit(2)


# --- Configuration ---------------------------------------------------------

# Project-scoped env names (preferred). The bare AZDO_* names are kept as
# fallbacks so legacy profiles and the smoke test (which may run outside any
# Hermes profile) keep working.
_ENV_PROJECT = "GOLDHUB_AZDO_PROJECT"
_ENV_ORG = "GOLDHUB_AZDO_ORG"
_ENV_PAT = "GOLDHUB_AZDO_PAT"


def _resolve(name: str) -> str | None:
    """Return the project-scoped value if set, else the bare value, else None.

    The project-scoped name is GOLDHUB_AZDO_<NAME>, the bare fallback is
    AZDO_<NAME>. Both are checked in that order.
    """
    return os.environ.get(f"GOLDHUB_AZDO_{name}") or os.environ.get(f"AZDO_{name}")


def _require_env(name: str) -> str:
    val = _resolve(name)
    if not val:
        sys.stderr.write(
            f"ERROR: environment variable GOLDHUB_AZDO_{name} (or fallback "
            f"AZDO_{name}) is not set.\n"
            f"This skill requires it to be defined in the agent profile that "
            f"is invoking the azdo command.\n"
        )
        sys.exit(2)
    return val


def get_connection() -> Connection:
    """Build a Connection from $GOLDHUB_AZDO_PAT + $GOLDHUB_AZDO_ORG.
    Exits 2 if missing or malformed."""
    pat = _require_env("PAT")
    org = _require_env("ORG")
    if not org.startswith(("http://", "https://")):
        sys.stderr.write(
            f"ERROR: $GOLDHUB_AZDO_ORG must be a full URL including scheme, e.g.\n"
            f"  https://dev.azure.com/yourorg\n"
            f"Got: {org!r}\n"
            f"(Use a bare org name only if you're on Azure DevOps Server "
            f"and have set up the URL yourself.)\n"
        )
        sys.exit(2)
    return Connection(
        base_url=org.rstrip("/"),
        creds=BasicAuthentication("", pat),
    )


def get_project(explicit: str | None = None) -> str:
    """Resolve project name. Explicit arg wins, else $GOLDHUB_AZDO_PROJECT."""
    if explicit:
        return explicit
    return _require_env("PROJECT")


# --- Output formatting -----------------------------------------------------

def output(data: Any, fmt: str = "json") -> None:
    """Emit result to stdout. fmt in {json, table, markdown}."""
    if fmt == "json":
        print(json.dumps(data, indent=2, default=str))
        return

    if not isinstance(data, list):
        data = [data]

    if fmt == "table":
        if not data:
            print("(no results)")
            return
        keys = sorted({k for row in data for k in row.keys()})
        widths = {k: max(len(k), *(len(str(row.get(k, ""))) for row in data)) for k in keys}
        print(" | ".join(k.ljust(widths[k]) for k in keys))
        print("-+-".join("-" * widths[k] for k in keys))
        for row in data:
            print(" | ".join(str(row.get(k, "")).ljust(widths[k]) for k in keys))
        return

    if fmt == "markdown":
        if not data:
            print("_no results_")
            return
        keys = sorted({k for row in data for k in row.keys()})
        print("| " + " | ".join(keys) + " |")
        print("|" + "|".join("---" for _ in keys) + "|")
        for row in data:
            cells = [str(row.get(k, "")).replace("|", "\\|").replace("\n", " ") for k in keys]
            print("| " + " | ".join(cells) + " |")
        return

    sys.stderr.write(f"ERROR: unknown format: {fmt}\n")
    sys.exit(2)


def error(msg: str) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.exit(1)


# --- WIQL / pagination helpers --------------------------------------------

def flatten_paged(responses: Iterable) -> list:
    """Consume SDK paged responses OR plain lists, return a flat list.

    Handles three shapes returned across SDK versions and clients:
      1. Plain list (e.g. CoreClient.get_projects)
      2. Paged response with `.value` attribute (e.g. get_work_items)
      3. None (treated as empty)
    """
    out = []
    for resp in responses:
        if resp is None:
            continue
        if isinstance(resp, list):
            out.extend(resp)
            continue
        val = getattr(resp, "value", None)
        if val is not None:
            out.extend(val)
            continue
        # Last resort: single object returned, wrap it.
        out.append(resp)
    return out


def wiql(client, project: str, query: str) -> list:
    """Run a WIQL query and return a flat list of WorkItemReference objects.

    Note: SDK v7.1 query_by_wiql does not take a project kwarg — the project
    is filtered inside the WIQL via [System.TeamProject].
    """
    from azure.devops.v7_1.work_item_tracking.models import Wiql

    wiql_obj = Wiql(query=query)
    result = client.query_by_wiql(wiql_obj)
    if result is None:
        return []
    return list(getattr(result, "work_items", None) or [])
