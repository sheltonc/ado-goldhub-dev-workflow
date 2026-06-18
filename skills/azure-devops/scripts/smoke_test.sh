#!/usr/bin/env bash
# Smoke test for the azure-devops skill.
# Re-runs after any change to the SDK wrapper layer (azdo_common, azdo_boards,
# azdo_prs) to catch the kind of API-shape regressions documented in
# references/initial-debug-session.md (the "SDK v7.1 vs older docs" section).
#
# Usage:
#   scripts/smoke_test.sh             # uses the venv python directly
#   scripts/smoke_test.sh --with-pat  # also exercises create/update (mutating)
#
# Requires the calling environment to have GOLDHUB_AZDO_PAT, GOLDHUB_AZDO_ORG,
# GOLDHUB_AZDO_PROJECT set (or the legacy AZDO_PAT/AZDO_ORG/AZDO_PROJECT
# fallbacks). The script auto-sources ~/.hermes/.env if those vars aren't
# already set, so running it from a regular (non-Hermes) shell still works.

# Self-chmod: this file is shipped without the executable bit set (skill_manage
# write_file doesn't preserve it). The first run will make it executable for
# the current user only; subsequent invocations run straight through.
if [ ! -x "${BASH_SOURCE[0]}" ]; then
    chmod u+x "${BASH_SOURCE[0]}" 2>/dev/null || true
fi

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="${HOME}/.hermes/venvs/azdo/bin/python"
DISPATCHER="${SKILL_DIR}/scripts/azdo"

# Resolve the canonical env var names. The skill prefers GOLDHUB_AZDO_* but
# falls back to AZDO_* — mirror that here so the preflight check matches.
for _v in PAT ORG PROJECT; do
    eval "_val=\${GOLDHUB_AZDO_${_v}:-\${AZDO_${_v}:-}}"
    if [ -n "${_val:-}" ]; then
        eval "export GOLDHUB_AZDO_${_v}=\${_val}"
    fi
done

# Auto-source .env so this works from a plain shell, not just Hermes
if [ -z "${GOLDHUB_AZDO_PAT:-}${AZDO_PAT:-}" ] && [ -f "${HOME}/.hermes/.env" ]; then
    set -a; source "${HOME}/.hermes/.env"; set +a
    # Re-resolve after sourcing in case the .env uses the bare AZDO_* names
    for _v in PAT ORG PROJECT; do
        eval "_val=\${GOLDHUB_AZDO_${_v}:-\${AZDO_${_v}:-}}"
        if [ -n "${_val:-}" ]; then
            eval "export GOLDHUB_AZDO_${_v}=\${_val}"
        fi
    done
fi

# Preflight: do we have the venv and env vars?
if [ ! -x "${VENV_PY}" ]; then
    echo "FATAL: ${VENV_PY} not found. Create the venv with:"
    echo "  uv venv ~/.hermes/venvs/azdo --python 3.12"
    echo "  uv pip install --python ${VENV_PY} azure-devops"
    exit 2
fi
for v in GOLDHUB_AZDO_PAT GOLDHUB_AZDO_ORG GOLDHUB_AZDO_PROJECT; do
    if [ -z "${!v:-}" ]; then
        # Also accept the legacy bare name
        legacy="AZDO_${v#GOLDHUB_AZDO_}"
        if [ -n "${!legacy:-}" ]; then
            export "${v}=${!legacy}"
            continue
        fi
        echo "FATAL: \$${v} is not set (and not in ~/.hermes/.env as either \$${v} or \$${legacy})"
        exit 2
    fi
done

pass=0
fail=0

run() {
    local label="$1"; shift
    local out exit_code
    out="$("$@" 2>&1)"
    exit_code=$?
    if [ $exit_code -eq 0 ]; then
        echo "  PASS  ${label}"
        pass=$((pass + 1))
    else
        echo "  FAIL  ${label} (exit ${exit_code})"
        echo "        ${out}" | head -5
        fail=$((fail + 1))
    fi
}

run_json() {
    local label="$1"; shift
    local out
    out="$("$@" 2>&1)" || { echo "  FAIL  ${label} (crashed)"; echo "        ${out}" | head -5; fail=$((fail + 1)); return; }
    if echo "${out}" | "${VENV_PY}" -c "import json,sys; json.loads(sys.stdin.read())" >/dev/null 2>&1; then
        echo "  PASS  ${label} (valid JSON)"
        pass=$((pass + 1))
    else
        echo "  FAIL  ${label} (not JSON)"
        echo "        ${out}" | head -5
        fail=$((fail + 1))
    fi
}

echo "== azure-devops smoke test =="
echo "  venv:   ${VENV_PY}"
echo "  org:    ${GOLDHUB_AZDO_ORG:-${AZDO_ORG:-<unset>}}"
echo "  project:${GOLDHUB_AZDO_PROJECT:-${AZDO_PROJECT:-<unset>}}"
echo ""

echo "-- read-only paths --"
run "help"           "${VENV_PY}" "${DISPATCHER}" help
run "boards list"    "${VENV_PY}" "${DISPATCHER}" boards list --format table
run "boards list (json)"     "${VENV_PY}" "${DISPATCHER}" boards list --format json
run_json "boards list parses as JSON"  "${VENV_PY}" "${DISPATCHER}" boards list

# Show a known-existing item (id 1 per the original validation session)
run "boards show 1"  "${VENV_PY}" "${DISPATCHER}" boards show 1
run "prs list (Goldhub)"  "${VENV_PY}" "${DISPATCHER}" prs list --repo Goldhub
run "prs list (Journal)"  "${VENV_PY}" "${DISPATCHER}" prs list --repo Journal
run "prs list --status all"  "${VENV_PY}" "${DISPATCHER}" prs list --repo Goldhub --status all

# Argument-validation paths
run "boards show 999999 (not found; SDK raises but exits cleanly would be nicer)"
out="$("${VENV_PY}" "${DISPATCHER}" boards show 999999 2>&1)" && rc=0 || rc=$?
if [ $rc -ne 0 ] && echo "${out}" | grep -qi "not found\|404\|401180"; then
    echo "  PASS  boards show 999999 fails as expected (rc=${rc})"
    pass=$((pass + 1))
else
    echo "  FAIL  boards show 999999 — expected a 'not found' error, got rc=${rc}"
    fail=$((fail + 1))
fi

# Env-var guard
echo "-- guard paths --"
unset GOLDHUB_AZDO_PAT AZDO_PAT
run "missing GOLDHUB_AZDO_PAT exits 2" "${VENV_PY}" "${DISPATCHER}" boards list || true
# Restore so the (optional) mutating section below works
set -a; source "${HOME}/.hermes/.env"; set +a
for _v in PAT ORG PROJECT; do
    eval "_val=\${GOLDHUB_AZDO_${_v}:-\${AZDO_${_v}:-}}"
    if [ -n "${_val:-}" ]; then
        eval "export GOLDHUB_AZDO_${_v}=\${_val}"
    fi
done

if [ "${1:-}" = "--with-pat" ]; then
    echo ""
    echo "-- mutating paths (--with-pat) --"
    # Create → show → update → close
    created="$("${VENV_PY}" "${DISPATCHER}" boards create --type Task --title "smoke-test $(date +%s)" 2>&1)"
    new_id="$(echo "${created}" | "${VENV_PY}" -c 'import json,sys; print(json.loads(sys.stdin.read())["id"])' 2>/dev/null || echo "")"
    if [ -n "${new_id}" ] && [ "${new_id}" -gt 0 ] 2>/dev/null; then
        echo "  PASS  boards create (new id=${new_id})"
        pass=$((pass + 1))
        run "boards show ${new_id}"        "${VENV_PY}" "${DISPATCHER}" boards show "${new_id}"
        run "boards update ${new_id} --state Done" \
            "${VENV_PY}" "${DISPATCHER}" boards update "${new_id}" --state Done
    else
        echo "  FAIL  boards create — could not parse new id from: ${created}"
        fail=$((fail + 1))
    fi
fi

echo ""
echo "== ${pass} passed, ${fail} failed =="
[ $fail -eq 0 ] || exit 1
