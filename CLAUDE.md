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

## Operator notifications — read the standard before you touch one

**Normative:** `devops-process/standards/operator-notifications.md`. Canon: **ADR-091**
(one egress) extending **ADR-078** (the cross-surface operator-action bus). Read it
before editing a notification, adding or changing a button, or investigating why the
operator received a message. This pointer is a **summary** — the standard is the sole
normative source.

**Senders in this repo, and the distinction that matters.** The **conversational**
send path (`plugins/platforms/telegram/adapter.py`, `tools/send_message_tool.py`) is
**out of scope** — the operator talking to an agent is not the mesh notifying the
operator, and ADR-078 forbids Hermes-core edits.

But `gateway/lifecycle_notifications.py` — which emits "Gateway offline — Hermes is
restarting" — **is** an operator notification riding that adapter, and **is** a
migration target (CLAWD-3486). Do not read the conversational exemption as covering it.

(**Which files carry this, and why they differ.** `AGENTS.md` here is the **vendored
upstream** guide — every commit touching it is by an upstream Nous author — so
minimal-delta applies and it is deliberately left alone; see the de-vendoring epic,
CLAWD-2832. `GROK.md` is fork-authored and is **GENERATED** from this file's shared
body by `devops-process/scripts/gen-agent-guides.sh`, so it carries this section too —
never hand-edit it; edit `CLAUDE.md` and regenerate.)

**Three things that bite, stated here so a reader who never opens the standard gets them:**

1. **Tier decides whether the operator is INTERRUPTED.** ADR-078 Amendment 2 ratifies
   `record` as **pull-only on every surface — never an interrupt**; `confirm` (a
   *reversible* event where silence is consent) pushes **on telegram, and is pull-only
   on agora and control**. `record` is the DEFAULT tier — Amendment 2 calls it
   "deliberately the annoying default", so an action with no declared tier does not
   interrupt. Choosing `record`
   does not change how a message is sent — it stops it being sent. Do not then claim
   `confirm` for everything that pushes today: that is push-unless-told-otherwise
   relabelled, and it destroys the demotion-on-evidence the tier model runs on.
   Migration is per-producer triage, and some messages honestly go quiet — that is the
   intended outcome.
2. **A test run must never DM the operator, and the guard belongs at the NETWORK
   BOUNDARY**, not the call site you happen to be looking at. The `devops-process`
   hot-lane gate was measured sending two real DMs per gate run from a fixture
   (CLAWD-3475). Reference impl: `devops-process/scripts/hot-lane/hl_notify.py`.
   Mock the notifier in your test anyway — the boundary guard is a safety net, not the
   contract.
3. **Most of the egress is a TARGET, not built.** `tier` and producer identity are
   absent from the live schema and `OperatorActionCreate` is `extra="forbid"`, so
   posting them returns **422** today. The standard opens with a build-state table.
   Epic: **CLAWD-3479**.

## When the request is ambiguous

If multiple reasonable interpretations exist (e.g., "add a memory hook" — core `agent/memory_manager.py` change? a provider plugin? a generic `PluginManager` lifecycle hook?), state the assumptions you're picking and ask before writing code. **NEVER** silently choose between meaningful interpretations.

## Scope discipline

Touch only what the task requires. No drive-by reformatting, no adjacent-comment edits, no opportunistic refactors. This is a **fork that tracks a fast-moving upstream** (`upstream = NousResearch/hermes-agent`) — gratuitous diffs make rebases and upstream merges painful and risk silently reverting fork-local fixes. If you spot something worth changing, mention it and file a card — do not bundle it.

**Plugins MUST NOT modify core files** (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`, etc.) — Teknium rule, May 2026. If a plugin needs a capability core doesn't expose, expand the generic plugin surface (new hook, new `ctx` method); never hardcode plugin-specific logic into core. See `AGENTS.md` → **Plugins**.

## Failure handling

Address root causes, not symptoms. **NEVER** swallow exceptions, comment out failing tests, or add `try/except` to silence errors. If you can't find the root cause in reasonable time, stop and report what you tried.

## What is this repo

`hermes-agent-fork` is the Minerva-mesh **fork** of the [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent) framework — the agent/CLI/gateway codebase that powers the live Minerva agent fleet (11 gateway units across 12 per-user accounts).

- **Remotes:** `origin = git@github.com:mbs-vhs/hermes-agent.git` (the Minerva fork), `upstream = https://github.com/NousResearch/hermes-agent.git`. Default branch `main`. Package `hermes-agent` v0.18.0 (`pyproject.toml`); `requires-python >=3.11`; entry point `hermes = hermes_cli.main:main`.
- **What it provides:** the `AIAgent` conversation loop (`run_agent.py`), the interactive CLI + Ink TUI (`cli.py`, `ui-tui/`, `tui_gateway/`), the messaging **gateway** (`gateway/` + per-platform adapters), tool orchestration (`model_tools.py`, `toolsets.py`, `tools/`), the plugin systems (`plugins/`), skills (`skills/`, `optional-skills/`), cron/kanban/curator subsystems, and the **ACP adapter** (`acp_adapter/` — VS Code / Zed / JetBrains integration).
- **For codebase internals, defer to `AGENTS.md`.** This file does not duplicate the project tree, the agent loop, or the authoring guides.

### This fork vs the live runtimes (the load-bearing distinction)

> **TOPOLOGY RE-BASELINED 2026-07-25 (CLAWD-2792) — read this before trusting the table below.**
> There are now **TWO live Hermes populations**, not one:
>
> | Population | Runs from | Managed by | Serves |
> |---|---|---|---|
> | **The fleet** (12 profiles) | `/opt/hermes-agent/venv` — an **installed package** (`hermes-agent 0.18.0`), *not* a checkout | `ai.hermes.gateway-<profile>.service` under **each per-user manager** (`/home/hermes-<profile>`) | the agent fleet |
> | **The `~/.hermes` checkout** | `~/.hermes/hermes-agent` + its venv (hermes-agent installed **editable**) | the two OAuth-refresh timers under the operator's manager | OAuth refresh + research (**not** `chat.vhs.box` — decommissioned 2026-07-28, CLAWD-2803) |
>
> The 11 `ai.hermes.gateway-*.service` units under the **operator's own** manager are **legacy leftovers and are all MASKED**. The older claim "the 10 gateways run from `~/.hermes/hermes-agent`" is **no longer true** and, left uncorrected, made `scripts/deploy-to-runtime.sh` fail *dangerous* (it advanced the live chat runtime, then errored about gateways it could never restart). That script now validates restart targets **before** mutating.
>
> Because the `~/.hermes` venv installs hermes-agent **editable**, advancing that checkout changes the code its consumers execute **immediately** — treat it as a live mutation, not a staged one.
>
> **⚠ `~/.hermes` IS STILL LOAD-BEARING — do not read the chat.vhs.box teardown as permission to retire it (CLAWD-2803).** The old justification written here made `chat.vhs.box` the *sole* stated reason `~/.hermes` was protected. That surface is gone; the directory is not retirable. Nine independent live consumers, all verified on the host 2026-07-25:
>
> 1. `ai.hermes.oauth-refresh.service`/`.timer` (every 6h) — writes the shared `auth.json`
> 2. `ai.hermes.codex-refresh.service`/`.timer` (daily)
> 3. `ai.hermes.dashboard.service` — serves `hermes.vhs.box` on `127.0.0.1:9119`
> 4. `clawd-app` — bind-mounts `~/.hermes/profiles` read-only (`HERMES_PROFILE_ROOT`)
> 5. `clawd-alloy` — bind-mounts `~/.hermes/profiles` read-only; ships 5 log files x 11 profiles to Loki
> 6. `hermes-profile-backup.timer`, `hermes-kanban-backup.timer`, `hermes-kanban-autoheal.timer`
> 7. `dev-preflight.sh` — monitors it as a LIVE runtime checkout
> 8. `~/.hermes/skills/` — the curator skills dir that per-profile `external_dirs` resolve through
> 9. `~/.hermes/cron/jobs.json` — the hermes cron ticker
>
> Retiring `~/.hermes` is a separate, larger decision (it would require migrating the OAuth refresh path and re-homing `hermes.vhs.box`), not a follow-on of the chat teardown.

**This repo is code. `~/.hermes/` is runtime state. They are two separate checkouts.**

| | Dev fork (this repo) | Runtime checkout |
|---|---|---|
| Path | `~/dev/hermes-agent-fork/` | `~/.hermes/hermes-agent/` |
| Role | Where you **edit code**, run tests, open PRs | Serves the OAuth-refresh timers + research (**not** the fleet, and **not** `chat.vhs.box` — decommissioned CLAWD-2803) |
| Remote | `origin` = mbs-vhs/hermes-agent (+ `upstream`) | `origin` = mbs-vhs/hermes-agent |
| Venv | `.venv` / `venv` (repo-local) | `~/.hermes/hermes-agent/venv` |

**SUPERSEDED 2026-07-25 — the unit shown below is a MASKED legacy leftover, not how the fleet runs.** All 11 `ai.hermes.gateway-*.service` units under the *operator's* manager are masked. Kept only to show what the old topology looked like:

```ini
# ~/.config/systemd/user/ai.hermes.gateway-<id>.service   ← MASKED / LEGACY
WorkingDirectory=/home/morganstempf/.hermes/hermes-agent
ExecStart=/home/morganstempf/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile <id> gateway run --replace
Environment=HERMES_HOME=/home/morganstempf/.hermes/profiles/<id>
```

The **live** fleet units live under each per-user manager and exec from `/opt`:

```ini
# /home/hermes-<id>/.config/systemd/user/ai.hermes.gateway-<id>.service   ← LIVE
ExecStart=/opt/hermes-agent/venv/bin/python ...
```

Editing files in `~/dev/hermes-agent-fork/` does **not** change either running population. Deployment is an operator-coordinated step — see **Stop conditions**. `scripts/deploy-to-runtime.sh` serves **only** the `~/.hermes` population and validates its restart targets before mutating anything; it cannot reach the `/opt` fleet.

### The profile mesh

Eleven gateway units run (one per profile; the 12th account `hermes-cc` has none), each under its own per-user systemd manager:

`clients`, `engineer`, `finance`, `legal`, `librarian`, `marketing`, `minerva`, `research`, `sales`, `strategy` (the former `growth` profile was converted to `sales`).

Each profile is a fully isolated instance with its own `HERMES_HOME` at `~/.hermes/profiles/<id>/` (config.yaml, .env, auth.json, sessions, skills, plugins, logs, state.db). The profile mechanism is `_apply_profile_override()` in `hermes_cli/main.py` setting `HERMES_HOME` before any module imports — see `AGENTS.md` → **Profiles** for the profile-safe-code rules.

Profile state directories are **runtime state, not source** — they are not in this repo. Do not commit profile config into the fork.

### Shared OAuth (the SPOF — handle with care)

`~/.hermes/auth.json` (top-level, mode `0600`) holds a shared **credential pool** spanning providers `anthropic`, `openai-codex`, `xai-oauth` (active provider currently `openai-codex`). Per-profile `auth.json` files exist but are typically empty `providers: []` — the profiles draw from the shared pool. This makes the shared `auth.json` a **single point of failure for the whole fleet**: a corrupted or revoked credential there can take down all 10 gateways at once. The ADR-058 rollout is explicitly gated on an OAuth-cap regain dated 2026-06-01 — do **not** "fix" or churn that auth state before then.

- **NEVER** echo credential bodies from `auth.json` into chat, logs, or commits. Reference the path only.
- Treat any change touching `auth.json` or the credential pool as a fleet-wide blast-radius operation → **Stop conditions**.

### Memory-provider plugins & the ADR-058 mnemosyne rollout

Memory backends are pluggable via the `MemoryProvider` ABC (`agent/memory_provider.py`), orchestrated by `agent/memory_manager.py`. Lifecycle hooks: `prefetch(query)`, `sync_turn(turn_messages)`, `on_memory_write`, `shutdown()`, optional `post_setup()`. See `AGENTS.md` → **Plugins → Memory-provider plugins**, including the **no-new-in-tree-providers policy (May 2026)**: new memory backends ship as standalone plugin repos installed into a profile's `plugins/`, not as new directories under `plugins/memory/`.

The **mnemosyne** plugin (ADR-058) follows exactly that pattern — it lives as **runtime state under each profile's `plugins/` directory**, not in this repo's tree:

- Present at `~/.hermes/profiles/<id>/plugins/mnemosyne/` for **all 10 profiles** (`__init__.py`, `adapter.py`, `clawd_client.py`, `dedupe.py`, `plugin.yaml`) — ADR-058 rollout complete 2026-06-02.
- **Active** in **all 10 profiles** — `memory.provider: mnemosyne` is set in every profile's `config.yaml`; recall **and** auto-capture (the full 2-way bridge) are live fleet-wide (verified 2026-06-02).
- Design (per `plugin.yaml` + `__init__.py` docstring): **recall** via subprocess CLI to the mnemosyne venv (`mnemosyne librarian compose` → `compose_context`); **memorialize** via `POST /admin/memory-items` to clawd with `source="hermes"` + content-hash dedupe. No `pip_dependencies` — the gateway venv stays decoupled from mnemosyne's deps. v1 is transparent (`get_tool_schemas() == []`), load-bearing on `prefetch` + `on_memory_write`.

**The ADR-058 rollout is complete** (2026-06-02, all 10 profiles live) — there is no staged remainder to "finish".

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

- **(a) Deploy / fleet mutation.** The change would deploy code to the **runtime checkout** (`~/.hermes/hermes-agent/`) or **restart any `ai.hermes.gateway-<id>.service`**. Editing this repo is in-lane; touching either live population is operator-coordinated (11 live gateway units, blast radius = whole mesh).
- **(b) Shared-OAuth / credential pool.** The change would modify `~/.hermes/auth.json`, the credential pool, or any profile's `auth.json` / `.env`. This is the fleet-wide SPOF and is gated behind the 2026-06-01 OAuth-cap timeline. State the action and ask inline.
- **(c) Live profile config.** The change would hand-edit a live profile's `config.yaml` or runtime state under `~/.hermes/profiles/<id>/` (including the mnemosyne plugin install or `memory.provider` flips). The ADR-058 rollout is complete (2026-06-02, all 10 profiles live); provider state remains operator-gated — do not flip it from a doc-driven session.
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
- `~/.hermes/hermes-agent/` — the **runtime checkout** serving the OAuth-refresh timers + research (`chat.vhs.box` was decommissioned 2026-07-28, CLAWD-2803). NOT the fleet: the 11 fleet gateway units exec from `/opt/hermes-agent/venv` (see the topology re-baseline above).
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
| 2026-07-25 | **Topology re-baseline.** Corrected the falsified "the 10 gateways run from `~/.hermes/hermes-agent`" claim: there are now TWO live populations — the fleet (11 gateway units across 12 per-user accounts, exec from `/opt/hermes-agent/venv`, an editable install on a non-git tree) and the `~/.hermes` checkout (serves `chat.vhs.box` + research; hermes-webui *spawns* agents from its venv, so a deploy lands on the next spawned agent). The operator-manager `ai.hermes.gateway-*` units are masked legacy. This staleness had made `scripts/deploy-to-runtime.sh` fail *dangerous* — it advanced the live chat runtime 4,547 commits, then errored on gateways it could never restart; that script now validates restart targets **before** mutating. | CLAWD-2792 |
| 2026-07-27 | **`/opt/hermes-agent` made measurable; stale version corrected.** Fixed the `v0.14.0` package version (it is `0.18.0`; `0.14.0` is the *`~/.hermes`* venv, a different population). Added read-only `scripts/opt_provenance_report.py` + tests: the fleet runtime has no `.git`, so this is the only way to answer "what is running?" / "has it drifted?". Measured 251 files present in `/opt` and absent from `HEAD`, 33 still resolvable as importable modules — every one traced to a named deleting commit, so a `--delete` deploy would have removed live-tree code with no record of why. Both deploy-script refusal sites now point at the tool and the substrate proposal. GROK.md additionally received the 2026-07-25 topology re-baseline it never got — it was still asserting the falsified single-runtime topology. | CLAWD-2833 |
| 2026-07-28 | **`chat.vhs.box` decommissioned; `~/.hermes` protection re-grounded.** hermes-webui and its two units were torn down (it ran as uid 1000 with read of the operator credential pool, while the fleet runs contained as per-user `hermes-*` uids). Two consequences recorded here: (1) `scripts/deploy-to-runtime.sh` no longer has chat-ui as its consumer — the `~/.hermes` venv's consumers are now the two OAuth-refresh timers, which are oneshots and pick up new code on their next firing; (2) the *stated reason* `~/.hermes` was protected evaporated with that surface, so the nine real consumers are now enumerated inline above. Also removed the `HERMES_OPERATOR_WEBUI` branch in `gateway/person_identity.py`; its test suite was REPLACED with removal guards, not deleted. **Correction (same day, from independent review):** this was first described as readers-without-writers with "no producer anywhere" — that was FALSE. Producers exist in `~/.hermes/profiles/minerva/.env` and `/home/hermes-minerva/.hermes/profiles/minerva/.env`; the original search covered only systemd units and `/etc/chat-ui/.env` (already deleted, so vacuously empty) and never looked at profile `.env` files. The removal is still safe — `_operator_person_id()` short-circuits on the explicit `HERMES_OPERATOR_PERSON_ID` that minerva sets, and the predicate was reachable only via `platform="webui"` which only hermes-webui emitted — but the two `.env` lines are now orphan writers-without-readers, left for an HR7-gated follow-up. | CLAWD-2803 |
