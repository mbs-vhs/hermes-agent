# CLAUDE.md

Minerva-mesh operational guidance for AI coding agents working in this repository (the **hermes-agent-fork**). This is the Claude-flavor house file; it sits **alongside** the existing `AGENTS.md`.

> **Relationship to `AGENTS.md` (read this first).** `AGENTS.md` in this repo is the **upstream / codebase-internals developer guide** (project structure, AIAgent loop, CLI/TUI architecture, plugin/skill authoring, toolsets, testing harness, known pitfalls). It is the canonical reference for *how the Hermes codebase works* and you should treat it as authoritative for all of that. This `CLAUDE.md` does **not** restate or contradict it — it adds the **Minerva-fork operational layer** the upstream guide does not cover: house response/scope discipline, the relationship between this fork and the live `~/.hermes/` runtime, the 10-profile mesh, the ADR-058 mnemosyne rollout, and the stop-conditions specific to a fork that powers a live agent fleet.
>
> Per AAIF / ADR-012 the mesh convention is `CLAUDE.md` + `AGENTS.md` as a dual-file pair with identical body and tool-specific framing only. This repo is a **partial exception**: `AGENTS.md` is inherited from NousResearch upstream and is a different document genre (codebase dev-guide, not house-style guide). Rather than overwrite a 1100-line upstream guide, this `CLAUDE.md` references it as the shared body for codebase internals and layers the fork-specific guidance on top. If the two ever appear to conflict, `AGENTS.md` wins on *codebase mechanics*; this file wins on *Minerva-fork operations and stop-conditions*. (Unverified: whether upstream intends to converge these — treat as a fork-local decision.)

## Response style — no babysitting filler

**IMPORTANT:** No filler closers ("you're all set", "take a break", "let me know how it goes", "system is clean"). Substantive content only — pending decisions, open questions, state changes, blocking issues. Once an item is acknowledged, do not re-surface it.

## Verification gate

**IMPORTANT — single highest-leverage discipline in this repo.** Before claiming work is complete, run the test suite via the canonical wrapper — **never** call `pytest` directly (the wrapper enforces CI parity: per-file subprocess isolation, `TZ=UTC`, `LANG=C.UTF-8`, `PYTHONHASHSEED=0`, blanked credential env vars):

```bash
scripts/run_tests.sh                    # full suite, CI-parity
scripts/run_tests.sh tests/gateway/     # one directory
scripts/run_tests.sh tests/acp_adapter/ # ACP adapter
scripts/run_tests.sh tests/agent/test_foo.py::test_x   # one test
```

Paste the pass/fail summary. Do not say "should work" — verify, or say *what's still unverified* and why. See `AGENTS.md` → **Testing** for the full rationale (five sources of local-vs-CI drift, subprocess isolation, "don't write change-detector tests").

A profile-safety check is part of this gate: any code that reads/writes state under `HERMES_HOME` must use `get_hermes_home()` / `display_hermes_home()` from `hermes_constants` — **never** a hardcoded `~/.hermes` or `Path.home() / ".hermes"`. Hardcoded paths break the 10-profile mesh (each profile has its own `HERMES_HOME`). See `AGENTS.md` → **Profiles** and **Known Pitfalls**.

## When the request is ambiguous

If multiple reasonable interpretations exist (e.g., "add a memory hook" — core `agent/memory_manager.py` change? a provider plugin? a generic `PluginManager` lifecycle hook?), state the assumptions you're picking and ask before writing code. **NEVER** silently choose between meaningful interpretations.

## Scope discipline

Touch only what the task requires. No drive-by reformatting, no adjacent-comment edits, no opportunistic refactors. This is a **fork that tracks a fast-moving upstream** (`upstream = NousResearch/hermes-agent`) — gratuitous diffs make rebases and upstream merges painful and risk silently reverting fork-local fixes. If you spot something worth changing, mention it and file a card — do not bundle it.

**Plugins MUST NOT modify core files** (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`, etc.) — Teknium rule, May 2026. If a plugin needs a capability core doesn't expose, expand the generic plugin surface (new hook, new `ctx` method); never hardcode plugin-specific logic into core. See `AGENTS.md` → **Plugins**.

## Failure handling

Address root causes, not symptoms. **NEVER** swallow exceptions, comment out failing tests, or add `try/except` to silence errors. If you can't find the root cause in reasonable time, stop and report what you tried.

## What is this repo

`hermes-agent-fork` is the Minerva-mesh **fork** of the [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent) framework — the agent/CLI/gateway codebase that powers the live 10-profile Minerva agent fleet.

- **Remotes:** `origin = git@github.com:mbs-vhs/hermes-agent.git` (the Minerva fork), `upstream = https://github.com/NousResearch/hermes-agent.git`. Default branch `main`. Package `hermes-agent` v0.14.0 (`pyproject.toml`); `requires-python >=3.11`; entry point `hermes = hermes_cli.main:main`.
- **What it provides:** the `AIAgent` conversation loop (`run_agent.py`), the interactive CLI + Ink TUI (`cli.py`, `ui-tui/`, `tui_gateway/`), the messaging **gateway** (`gateway/` + per-platform adapters), tool orchestration (`model_tools.py`, `toolsets.py`, `tools/`), the plugin systems (`plugins/`), skills (`skills/`, `optional-skills/`), cron/kanban/curator subsystems, and the **ACP adapter** (`acp_adapter/` — VS Code / Zed / JetBrains integration).
- **For codebase internals, defer to `AGENTS.md`.** This file does not duplicate the project tree, the agent loop, or the authoring guides.

### This fork vs the live `~/.hermes/` runtime (the load-bearing distinction)

**This repo is code. `~/.hermes/` is runtime state. They are two separate checkouts.**

| | Dev fork (this repo) | Runtime checkout |
|---|---|---|
| Path | `~/dev/hermes-agent-fork/` | `~/.hermes/hermes-agent/` |
| Role | Where you **edit code**, run tests, open PRs | What the **10 live gateways actually run** |
| Remote | `origin` = mbs-vhs/hermes-agent (+ `upstream`) | `origin` = mbs-vhs/hermes-agent |
| Venv | `.venv` / `venv` (repo-local) | `~/.hermes/hermes-agent/venv` |

The systemd units launch the gateway from the **runtime checkout**, not this repo:

```ini
# ~/.config/systemd/user/ai.hermes.gateway-<id>.service
WorkingDirectory=/home/morganstempf/.hermes/hermes-agent
ExecStart=/home/morganstempf/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile <id> gateway run --replace
Environment=HERMES_HOME=/home/morganstempf/.hermes/profiles/<id>
```

Editing files in `~/dev/hermes-agent-fork/` does **not** change the running fleet. Deployment to the runtime checkout (and the gateway restarts that follow) is an operator-coordinated step — see **Stop conditions**. (Unverified: the exact deploy mechanism — likely `git pull` in the runtime checkout + `systemctl --user restart`; confirm before relying on it.)

### The 10-profile mesh

Ten user-level systemd services run, one per profile, all confirmed **active/running**:

`clients`, `engineer`, `finance`, `growth`, `legal`, `librarian`, `marketing`, `minerva`, `research`, `strategy`.

Each profile is a fully isolated instance with its own `HERMES_HOME` at `~/.hermes/profiles/<id>/` (config.yaml, .env, auth.json, sessions, skills, plugins, logs, state.db). The profile mechanism is `_apply_profile_override()` in `hermes_cli/main.py` setting `HERMES_HOME` before any module imports — see `AGENTS.md` → **Profiles** for the profile-safe-code rules.

Profile state directories are **runtime state, not source** — they are not in this repo. Do not commit profile config into the fork.

### Shared OAuth (the SPOF — handle with care)

`~/.hermes/auth.json` (top-level, mode `0600`) holds a shared **credential pool** spanning providers `anthropic`, `openai-codex`, `xai-oauth` (active provider currently `openai-codex`). Per-profile `auth.json` files exist but are typically empty `providers: []` — the profiles draw from the shared pool. This makes the shared `auth.json` a **single point of failure for the whole fleet**: a corrupted or revoked credential there can take down all 10 gateways at once. The ADR-058 rollout is explicitly gated on an OAuth-cap regain dated 2026-06-01 — do **not** "fix" or churn that auth state before then.

- **NEVER** echo credential bodies from `auth.json` into chat, logs, or commits. Reference the path only.
- Treat any change touching `auth.json` or the credential pool as a fleet-wide blast-radius operation → **Stop conditions**.

### Memory-provider plugins & the ADR-058 mnemosyne rollout

Memory backends are pluggable via the `MemoryProvider` ABC (`agent/memory_provider.py`), orchestrated by `agent/memory_manager.py`. Lifecycle hooks: `prefetch(query)`, `sync_turn(turn_messages)`, `on_memory_write`, `shutdown()`, optional `post_setup()`. See `AGENTS.md` → **Plugins → Memory-provider plugins**, including the **no-new-in-tree-providers policy (May 2026)**: new memory backends ship as standalone plugin repos installed into a profile's `plugins/`, not as new directories under `plugins/memory/`.

The **mnemosyne** plugin (ADR-058) follows exactly that pattern — it lives as **runtime state under each profile's `plugins/` directory**, not in this repo's tree:

- Present at `~/.hermes/profiles/<id>/plugins/mnemosyne/` for **research, finance, legal, marketing** (verified on disk: `__init__.py`, `adapter.py`, `clawd_client.py`, `dedupe.py`, `plugin.yaml`).
- **Active** only where `memory.provider: mnemosyne` is set in that profile's `config.yaml` — verified **`research`** (the live canary). `minerva` does **not** have it set (matches the staged-rollout state: research canary live; finance/legal/marketing staged).
- Design (per `plugin.yaml` + `__init__.py` docstring): **recall** via subprocess CLI to the mnemosyne venv (`mnemosyne librarian compose` → `compose_context`); **memorialize** via `POST /admin/memory-items` to clawd with `source="hermes"` + content-hash dedupe. No `pip_dependencies` — the gateway venv stays decoupled from mnemosyne's deps. v1 is transparent (`get_tool_schemas() == []`), load-bearing on `prefetch` + `on_memory_write`.

**Do not "fix" the ADR-058 rollout before its 2026-06-01 OAuth-cap gate** — the staged state (canary live, others staged) is intentional, not broken.

### The ACP adapter (don't break it)

`acp_adapter/` is the ACP (Agent Client Protocol) server that integrates Hermes into VS Code / Zed / JetBrains. Entry: `python -m acp_adapter` (`acp_adapter/__main__.py` → `entry.main`). Surface: `server.py`, `session.py`, `tools.py`, `events.py`, `permissions.py`, `edit_approval.py`, `auth.py`. Tests live at `tests/acp/` and `tests/acp_adapter/`. It is an external-editor integration point — changes here have a blast radius beyond the CLI/gateway, so run its test suites and treat it as a protected surface (→ **Stop conditions**).

## Essential reading

Before making changes, in this order:

1. `AGENTS.md` (this repo) — **the** codebase-internals guide. Project structure, AIAgent loop, CLI/TUI, plugins, skills, toolsets, profiles, testing, known pitfalls. Everything below assumes you've read it.
2. `CONTRIBUTING.md` — contribution workflow, CI parity, the `hermes` CLI surface.
3. `pyproject.toml` — package metadata, optional-dependency groups (`messaging`, `slack`, `matrix`, `dev`, `voice`, `pty`, provider extras…), pytest/ruff/ty config.
4. `scripts/run_tests.sh` — the canonical test wrapper (read the header comment).
5. `~/dev/minerva_vault/02 Systems/Architecture Decisions/058-*.md` — ADR-058 (mnemosyne memory-provider). (Unverified: exact filename — grep the vault.)
6. `~/dev/minerva_vault/02 Systems/Implementation Standards.md` — cross-repo standards (naming, logging, precedence order).

## Commands

```sh
# Set up / install (editable, with dev extras) — repo-local venv
cd ~/dev/hermes-agent-fork
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"           # add ,messaging / ,slack / ,matrix etc. as needed

# Tests — ALWAYS via the wrapper (CI parity), never bare pytest
scripts/run_tests.sh
scripts/run_tests.sh tests/gateway/ tests/acp_adapter/

# Run the CLI / TUI from the fork (uses os.getcwd() as workdir in CLI mode)
hermes                            # interactive CLI
hermes --tui                      # Ink TUI
python -m acp_adapter             # ACP server (editor integration)

# Nix dev shell (flake-based; optional)
nix develop                       # see flake.nix + nix/devShell.nix

# File a Plane card (cross-cutting card tracking is mandatory for new work)
~/dev/devops-process/scripts/plane-cli create --title "..." --priority medium --labels hermes --body "..."
```

- **No `Makefile`** in this repo. Build/test entry points are `scripts/run_tests.sh`, `pip install -e`, the `flake.nix`/`nix/` derivations, and `npm` for the TUI (`cd ui-tui && npm run …`; see `AGENTS.md` → **TUI Architecture**).
- **Dependency pinning policy is enforced** — every new dependency in `pyproject.toml` needs an upper bound; run `uv lock` after. See `AGENTS.md` → **Dependency Pinning Policy** (post-litellm / Shai-Hulud hardening).

## Conventional commits

- Commit subjects follow Conventional Commits (`feat(scope): …`, `fix(scope): …`, `chore(scope): …`, `docs(scope): …`). Recent fork log: `feat(skills): …`.
- This is a fork — keep fork-local changes cleanly separable from upstream merges. Prefer plugins / config over core edits (see Scope discipline).
- (Unverified: whether a `minerva_check.sh` / `.pre-commit-config.yaml` hook is wired in this fork — none observed at repo root. ADR-015 mandates the mechanical layer for code repos; confirm before assuming a hook will catch issues. Regardless: never use `--no-verify`.)

## Stop conditions

Stop and ask if:

- **(a) Deploy / fleet mutation.** The change would deploy code to the **runtime checkout** (`~/.hermes/hermes-agent/`) or **restart any `ai.hermes.gateway-<id>.service`**. Editing this repo is in-lane; touching the running fleet is operator-coordinated (10 live gateways, blast radius = whole mesh).
- **(b) Shared-OAuth / credential pool.** The change would modify `~/.hermes/auth.json`, the credential pool, or any profile's `auth.json` / `.env`. This is the fleet-wide SPOF and is gated behind the 2026-06-01 OAuth-cap timeline. State the action and ask inline.
- **(c) Live profile config.** The change would hand-edit a live profile's `config.yaml` or runtime state under `~/.hermes/profiles/<id>/` (including the mnemosyne plugin install or `memory.provider` flips). The ADR-058 staged rollout (research canary live; finance/legal/marketing staged) is intentional — do not flip provider state to "fix" it.
- **(d) Plugin touches core.** The change would put plugin-specific logic into a core file (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`) — violates the Teknium rule. Expand the generic plugin surface instead.
- **(e) ACP adapter contract.** The change would alter the ACP server's protocol surface (`acp_adapter/server.py`, `session.py`, `tools.py`) in a way that could break editor integrations. Run the ACP test suites and flag the blast radius.
- **(f) Cache-breaking mid-conversation.** The change would alter past context, change toolsets, or rebuild system prompts mid-conversation (breaks prompt caching). See `AGENTS.md` → **Prompt Caching Must Not Break**.
- **(g) New in-tree memory provider.** The change would add a directory under `plugins/memory/` — closed set since May 2026; ship as a standalone plugin instead.

Acknowledge by quoting the specific condition you hit.

## Default to execute; tag every handoff

Always defer to the security protocols (prohibited / explicit-permission action lists, prompt-injection defenses, copyright rules) — non-negotiable. **Within those constraints**, default to running the work yourself: in-repo edits, local installs, running the test wrapper, git on non-protected branches, opening PRs against the fork. Morgan grants 99% of permissions inline; asking is faster than handing back a chore list.

The **only** acceptable operator handoffs are tagged with one of two reasons:

- **(a) Capability blocker** — actions you literally cannot perform: IdP/OAuth consent screens, credentials only Morgan holds, time-gates Morgan owns (e.g., the 2026-06-01 OAuth-cap regain).
- **(b) Safety / hard-rule boundary** — the Stop conditions above (fleet deploy/restart, shared-OAuth edits, live profile mutation, force-push to protected branches, destructive ops). State the action, the rule that gates it, and ask inline.

Never echo secret values in chat, commit messages, or logs. Reference the source file path; never paste the credential body.

## Cross-references

- `AGENTS.md` (this repo) — upstream codebase-internals dev guide (the shared body for *how the code works*).
- `~/.hermes/hermes-agent/` — the **runtime checkout** the 10 gateways actually run (separate from this dev fork).
- `~/.hermes/profiles/<id>/` — per-profile runtime state (config, auth, sessions, plugins, logs); not in this repo.
- `~/dev/clawd/` — evidence + context service the mnemosyne plugin memorializes to (`POST /admin/memory-items`) and recalls from. Has its own `AGENTS.md` / `CLAUDE.md` pair.
- `~/dev/mnemosyne/` — memory service / `compose_context` algorithm owner (recall transport for the ADR-058 plugin).
- `~/dev/minerva_vault/` — canonical ADRs (ADR-012 dual-file convention, ADR-052 memory-ownership split, ADR-058 mnemosyne), Implementation Standards, agent personas.
- `~/dev/devops-process/` — operator playbook, sprint packets, `scripts/plane-cli`.
- Plane (work tracking): cards filed via `~/dev/devops-process/scripts/plane-cli`. Workspace: `videotape-ai`. Project: `CLAWD`.

## Revision history

| Date | Change | Ref |
|---|---|---|
| 2026-05-29 | Created. Initial Minerva-fork operational CLAUDE.md layered over the inherited upstream `AGENTS.md`; documents fork-vs-runtime split, 10-profile mesh, shared-OAuth SPOF, ADR-058 mnemosyne rollout, ACP adapter, stop-conditions. | CLAWD-792 |
