#!/usr/bin/env python3
"""Generate a Hermes profile's ``config.yaml`` ``model:`` block from the
substrate-contract ``provider_policy`` manifest (ADR-072 P1b / CLAWD-2213).

Provider + preferred-model become *generated-from-manifest*, not hand-edited.
The single source of truth is ``substrate-contract/roster.yaml`` (per agent:
``provider_policy.default.{provider,model}`` + ``allowed_providers``). This tool
reads that policy via ``substrate_contract.provider_policy_for`` and surgically
merges ONLY ``model.default`` + ``model.provider`` (and, with ``--include-allowed``,
``model.allowed_providers``) into a target ``config.yaml`` — preserving every
other key, value, and comment via a **ruamel.yaml round-trip** (never a
whole-file ``safe_load`` / ``safe_dump``, which would reorder + strip comments).

Behaviour:
- **Governed-field scoped (ADR-072 §4).** Drift is judged on the *manifest-owned*
  fields only — ``model.provider`` + ``model.default`` (and, with
  ``--include-allowed``, ``model.allowed_providers``). config.yaml is only
  *partially* generated: toolsets, mcp_servers, ports, and secrets stay federated
  in Hermes, so a hand-edit to one of those must NOT read as provider drift. A
  ruamel round-trip may incidentally normalize such a federated field (e.g.
  un-fold a manually line-folded scalar); that cosmetic difference is deliberately
  NOT drift.
- **Idempotent.** When the governed fields already match the manifest nothing is
  written (no ``.bak`` churn), even if a whole-file re-render would differ
  cosmetically. A run with no governed change is a no-op.
- **Backup before write.** On a genuine governed change the original is copied to
  ``config.yaml.bak`` before the new content is written.
- **``--check``.** Reports governed drift and writes nothing; exits non-zero iff a
  governed field diverges from the manifest (the drift gate).

Targeting (so hermetic tests never touch the real ``~/.hermes``):
- ``--profile <id>`` selects the roster manifest entry (required).
- ``--config <path>`` targets an explicit ``config.yaml`` (tests / ad-hoc).
- ``--home <dir>`` targets ``<dir>/config.yaml``.
- With neither ``--config`` nor ``--home``, the LIVE profile config is resolved
  via the profile-safe helper ``hermes_cli.profiles.get_profile_dir(<id>)``.
  Writing a live profile is the ADR-072 **P1d** deploy step and is
  operator-coordinated (hermes-agent-fork CLAUDE.md Stop-condition (c)) — this
  tool supports it, but do not run it against live profiles outside that step.

Dependency note: ``substrate_contract`` is an OFFLINE build/tooling dependency,
installed editable out-of-band (``pip install -e ~/dev/substrate-contract``). It
is deliberately NOT a gateway-runtime dependency (ADR-058 venv decoupling); the
generator is an offline build tool, not a gateway import.

Usage:
  python scripts/generate_profile_provider.py --profile engineer --check
  python scripts/generate_profile_provider.py --profile engineer --config /path/config.yaml
"""
from __future__ import annotations

import argparse
import io
import sys
from dataclasses import dataclass
from pathlib import Path

# Make the package importable when run as a bare script from the repo root
# (mirrors scripts/sync_roster_to_profiles.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ruamel.yaml import YAML  # noqa: E402
from ruamel.yaml.comments import CommentedMap  # noqa: E402

try:
    from substrate_contract import ProviderPolicy, provider_policy_for
except ImportError as exc:  # pragma: no cover - dep-missing guidance path
    raise SystemExit(
        "generate_profile_provider requires the substrate-contract package "
        "(offline build/tooling dependency). Install it editable:\n"
        "    pip install -e /home/morganstempf/dev/substrate-contract"
    ) from exc

_MODEL_KEYS = ("provider", "default")


def _yaml() -> YAML:
    """A round-trip YAML that preserves comments, key order, and quoting.

    ``width`` is set wide so long scalars are never line-wrapped (a wrap would
    be a gratuitous diff on an otherwise-unchanged key).
    """
    yaml = YAML()  # round-trip mode
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def render_merged(text: str, policy: ProviderPolicy, *, include_allowed: bool) -> str:
    """Return ``text`` with the ``model:`` block's provider/model (and, when
    ``include_allowed``, ``allowed_providers``) merged from ``policy``.

    Every other key, value, and comment is preserved via ruamel round-trip.
    """
    yaml = _yaml()
    data = yaml.load(text)
    if not isinstance(data, dict):
        raise ValueError("config is empty or not a YAML mapping")

    model = data.get("model")
    if not isinstance(model, dict):
        # Rare: a scalar ``model: <name>`` (or absent). Replace with a mapping;
        # real profiles already use a mapping so this only affects edge configs.
        model = CommentedMap()
        data["model"] = model

    # Assigning to existing keys preserves their position + inline comments;
    # missing keys are appended in the order below.
    model["provider"] = policy.default.provider
    model["default"] = policy.default.model
    if include_allowed:
        model["allowed_providers"] = list(policy.allowed_providers)

    buf = io.StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def _model_values(text: str, *, include_allowed: bool = False) -> dict[str, object]:
    """Read the current governed ``model.*`` fields (for drift + reporting).

    Uses a throwaway round-trip load (never mutates the file). ``allowed_providers``
    is coerced to a plain ``list`` so equality against the manifest is stable
    (ruamel returns a ``CommentedSeq``).
    """
    try:
        data = _yaml().load(text)
    except Exception:
        return {}
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, dict):
        return {}
    keys = _MODEL_KEYS + (("allowed_providers",) if include_allowed else ())
    out: dict[str, object] = {}
    for k in keys:
        v = model.get(k)
        if k == "allowed_providers" and v is not None:
            v = list(v)
        out[k] = v
    return out


def _expected_model(policy: ProviderPolicy, *, include_allowed: bool) -> dict[str, object]:
    """The governed ``model.*`` values the manifest requires."""
    expected: dict[str, object] = {
        "provider": policy.default.provider,
        "default": policy.default.model,
    }
    if include_allowed:
        expected["allowed_providers"] = list(policy.allowed_providers)
    return expected


@dataclass(frozen=True)
class GenResult:
    config_path: Path
    changed: bool
    wrote: bool
    backup_path: Path | None
    before: str
    after: str


def apply_provider_policy(
    config_path: Path,
    policy: ProviderPolicy,
    *,
    include_allowed: bool = False,
    check: bool = False,
) -> GenResult:
    """Merge ``policy`` into ``config_path``'s ``model:`` block.

    In ``check`` mode nothing is written. Otherwise, on a genuine *governed*
    change (``model.provider`` / ``model.default`` / — when requested —
    ``model.allowed_providers`` diverges from the manifest) the original is backed
    up to ``<name>.bak`` before the merged text is written. When the governed
    fields already match, it is a no-op — even if a whole-file re-render would
    differ cosmetically (a federated field ruamel normalized) — so the tool never
    churns hand-federated config (ADR-072 §4).
    """
    original = config_path.read_text(encoding="utf-8")
    merged = render_merged(original, policy, include_allowed=include_allowed)
    # Drift is judged on the manifest-owned fields ONLY, not the whole-file render.
    current = _model_values(original, include_allowed=include_allowed)
    expected = _expected_model(policy, include_allowed=include_allowed)
    changed = current != expected

    if check or not changed:
        return GenResult(config_path, changed, False, None, original, merged)

    backup_path = config_path.with_name(config_path.name + ".bak")
    backup_path.write_text(original, encoding="utf-8")
    config_path.write_text(merged, encoding="utf-8")
    return GenResult(config_path, changed, True, backup_path, original, merged)


def _resolve_config_path(
    profile_id: str, *, config: Path | None, home: Path | None
) -> Path:
    if config is not None:
        return config
    if home is not None:
        return home / "config.yaml"
    # Live profile resolution — ADR-072 P1d territory (operator-gated). Local
    # import so tests passing --config/--home never pull the hermes_cli chain.
    from hermes_cli.profiles import get_profile_dir

    return get_profile_dir(profile_id) / "config.yaml"


def _report(result: GenResult, *, check: bool, include_allowed: bool = False) -> None:
    before = _model_values(result.before, include_allowed=include_allowed)
    after = _model_values(result.after, include_allowed=include_allowed)
    path = result.config_path
    keys = _MODEL_KEYS + (("allowed_providers",) if include_allowed else ())

    def _delta() -> str:
        parts = []
        for key in keys:
            if before.get(key) != after.get(key):
                parts.append(f"model.{key} {before.get(key)!r} -> {after.get(key)!r}")
        return "; ".join(parts)

    if not result.changed:
        print(f"OK    {path}: model block already matches manifest")
        return
    if check:
        print(f"DRIFT {path}: {_delta()}")
        return
    print(f"WROTE {path}: {_delta()} (backup: {result.backup_path})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, help="agent / profile id (roster manifest key)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and write nothing; non-zero exit if the target "
        "diverges from the manifest (the drift gate)",
    )
    parser.add_argument(
        "--include-allowed",
        action="store_true",
        help="also emit model.allowed_providers from the manifest",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--config",
        type=Path,
        help="explicit config.yaml path (hermetic testing / ad-hoc; bypasses "
        "live profile resolution)",
    )
    target.add_argument(
        "--home",
        type=Path,
        help="HERMES_HOME dir; config resolved as <home>/config.yaml",
    )
    args = parser.parse_args(argv)

    try:
        policy = provider_policy_for(args.profile)
    except KeyError:
        print(
            f"ERROR: no provider_policy for profile {args.profile!r} in the "
            "roster manifest",
            file=sys.stderr,
        )
        return 2

    config_path = _resolve_config_path(
        args.profile, config=args.config, home=args.home
    )
    if not config_path.is_file():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 2

    result = apply_provider_policy(
        config_path,
        policy,
        include_allowed=args.include_allowed,
        check=args.check,
    )
    _report(result, check=args.check, include_allowed=args.include_allowed)

    if args.check and result.changed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
