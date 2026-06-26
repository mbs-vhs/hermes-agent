#!/usr/bin/env python3
"""Sync canonical persona name + role from the clawd roster SSOT into each
profile's ``profile.yaml`` (CLAWD-1828 P3).

The clawd ``agent_registry`` table (migration 189) is the single source of
truth for "PersonaName — Role" (e.g. profile ``engineer`` → "Quasimodo —
Engineer"). This script reads that roster over HTTP and writes
``display_name`` + ``role`` into ``<profile_dir>/profile.yaml`` via
``hermes_cli.profiles.write_profile_meta`` so downstream surfaces
(hermes-webui / chat.vhs.box) can render the canonical label instead of the
bare profile id.

SAFETY — dry-run by default. ``profile.yaml`` lives under each profile's
live ``HERMES_HOME`` (``~/.hermes/profiles/<id>/``); writing it is a live
runtime-state mutation (hermes-agent-fork CLAUDE.md Stop-condition (c)), so
this script PRINTS a plan and changes nothing unless ``--apply`` is passed.
Run ``--apply`` only in an operator-coordinated step.

Config (env):
  CLAWD_BASE_URL          clawd base (default http://127.0.0.1:8000)
  CLAWD_API_AUTH_TOKEN    bearer for the roster read (falls back to API_AUTH_TOKEN)

Usage:
  python scripts/sync_roster_to_profiles.py            # dry-run (default)
  python scripts/sync_roster_to_profiles.py --apply    # operator-gated write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Make the package importable when run as a bare script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli.profiles import (  # noqa: E402
    get_profile_dir,
    profile_exists,
    read_profile_meta,
    write_profile_meta,
)

_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_ROSTER_PATH = "/dashboard/agent-roster"


def _auth_token() -> str | None:
    return os.environ.get("CLAWD_API_AUTH_TOKEN") or os.environ.get("API_AUTH_TOKEN")


def fetch_roster(base_url: str, timeout: float = 15.0) -> list[dict]:
    """GET the clawd agent roster. Returns the list of agent rows.

    Each row carries at least ``agent_id`` plus ``display_name`` and a role
    field (``role`` or ``role_summary`` depending on the endpoint shape).

    Raises ``RuntimeError`` if the response shape is unexpected, or if the
    roster is empty AND the endpoint reports a degraded ``backend_status`` —
    a degraded clawd backend returns HTTP 200 with ``agents=[]``, and we must
    not let that masquerade as a genuinely-empty roster (which would silently
    "sync" nothing).
    """
    url = base_url.rstrip("/") + _ROSTER_PATH
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    token = _auth_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    backend_status: str | None = None
    if isinstance(payload, dict):
        agents = payload.get("agents")
        bs = payload.get("backend_status")
        backend_status = str(bs) if bs is not None else None
    else:
        agents = payload
    if not isinstance(agents, list):
        raise RuntimeError(f"unexpected roster shape from {url}: {type(payload).__name__}")
    if not agents and backend_status and backend_status.lower() not in {"ok", "healthy", "live"}:
        raise RuntimeError(
            f"clawd roster empty and backend_status={backend_status!r} "
            "(backend degraded, not a genuinely-empty roster)"
        )
    return agents


def _row_role(row: dict) -> str | None:
    for key in ("role", "role_summary", "role_label"):
        val = row.get(key)
        if val:
            return str(val).strip()
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write profile.yaml (default: dry-run, print the plan only). "
        "Operator-gated — mutates live runtime state under ~/.hermes/profiles/.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CLAWD_BASE_URL") or _DEFAULT_BASE_URL,
        help="clawd base URL (default %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        roster = fetch_roster(args.base_url)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError) as exc:
        print(f"ERROR: could not fetch roster from {args.base_url}{_ROSTER_PATH}: {exc}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] roster from {args.base_url}{_ROSTER_PATH}: {len(roster)} agent(s)")

    changes = 0
    skipped = 0
    for row in roster:
        if not isinstance(row, dict):
            print(f"  SKIP (non-dict row): {row!r}")
            skipped += 1
            continue
        agent_id = (row.get("agent_id") or row.get("id") or "").strip()
        display_name = (row.get("display_name") or "").strip() or None
        role = _row_role(row)
        if not agent_id:
            print(f"  SKIP (no agent_id): {row!r}")
            skipped += 1
            continue
        if not display_name and not role:
            print(f"  SKIP {agent_id}: roster row has no display_name/role")
            skipped += 1
            continue
        if not profile_exists(agent_id):
            print(f"  SKIP {agent_id}: no local profile directory")
            skipped += 1
            continue

        profile_dir = get_profile_dir(agent_id)
        current = read_profile_meta(profile_dir)
        cur_name, cur_role = current.get("display_name"), current.get("role")
        if cur_name == display_name and cur_role == role:
            print(f"  OK   {agent_id}: already '{display_name} — {role}'")
            continue

        print(
            f"  SET  {agent_id}: '{cur_name} — {cur_role}' -> '{display_name} — {role}'"
            + ("" if args.apply else "  (dry-run)")
        )
        if args.apply:
            write_profile_meta(profile_dir, display_name=display_name, role=role)
        changes += 1

    verb = "applied" if args.apply else "would apply"
    print(f"[{mode}] {verb} {changes} change(s); {skipped} skipped.")
    if not args.apply and changes:
        print("Re-run with --apply (operator-coordinated) to write profile.yaml.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
