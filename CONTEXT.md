# CONTEXT.md — hermes-agent-fork

Last regen: 2026-06-01
Schema: codebase-familiarity v1 (devops-process/skills/codebase-familiarity/references/schema.md)
Sibling: `.codebase-map.md` (the WHAT/HOW counterpart to this WHY file)
> DRAFT (2026-06-01) — VENDORED OSS fork. WHY/rationale needs operator ratification.

## L0 — Identity (why this repo exists)

We fork Hermes (rather than use upstream directly or build a runtime) because Minerva needs a **managed agent fleet** that upstream Hermes has no concept of: many always-on profiles drawing from a shared credential pool, a pluggable memory substrate (mnemosyne/clawd, ADR-058), fleet-safe operating discipline (ADR-059 gitleaks, stop-conditions), and supervised liveness (ADR-039). The fork is release-tag-pinned so we get upstream improvements deliberately, not continuously, and never drift unintentionally.

## L1 — Map (why this shape)

### Why these top-level dirs

| Dir | Why the local delta lives here |
|---|---|
| `agent/` (codex adapters, memory wiring) | The memory substrate is a fork-scoped concern (profile + user-identity-aware recall); upstream's agent loop has no mnemosyne notion |
| `gateway/` (`api_server.py`, `run.py` deltas) | 24/7 multi-profile operation needs idle-TTL eviction, crash recovery, and a REST surface for clawd writes — upstream assumes single-user, session-scoped |
| `CLAUDE.md` | The fork's operating contract (profiles, OAuth SPOF, rollout state) had to be local so fleet rules travel with the code |

### Why this state strategy

- **Per-profile `HERMES_HOME` isolation** so minerva (ops) can't see engineer (code) or librarian (research) sessions — independent session stores, plugins, memory scope.
- **One shared `~/.hermes/auth.json` OAuth pool** is a deliberate, *known* SPOF: a corrupted/revoked credential there takes down the whole fleet. It's gated by the ADR-058 rollout timeline rather than fixed inline. (See user-memory: a provider cap is not the same as a SPOF — don't conflate.)

### Why secrets live where they live

- Env + `auth.json` under `~/.hermes/` (mode 0600), outside the repo tree, so credentials never touch git; `.gitleaks.toml` (ADR-059) extends upstream rules as a pre-commit backstop.

## L2 — Anchors (why each abstraction exists)

- `MemoryProvider` ABC — the plugin seam exists so memory backends (mnemosyne, byterover) swap per-profile without core edits; mnemosyne ships as runtime state (not in-tree) so the fork stays mergeable with upstream
- Profile override (`_apply_profile_override`) — sets `HERMES_HOME` before imports because path resolution must be profile-scoped from the first line, or isolation leaks
- `gateway/platforms/api_server.py` — a fork-local REST surface was added because clawd memory writes + operator dashboard need an ingress upstream doesn't provide
- Gateway daemon eviction/recovery — exists because a 10-profile always-on fleet must survive individual crashes without cascading

## L3 — Deep dive pointers (where the rationale docs live)

- Fleet operating doctrine — this repo's `CLAUDE.md` (canonical local rules)
- mnemosyne memory-provider rollout (canary→staged) — minerva_vault `02 Systems/Architecture Decisions/058-*.md`
- gitleaks leak-guard — ADR-059; dual-file CLAUDE.md/AGENTS.md convention — ADR-012
- Engineer supervision — ADR-039 (`devops-process/supervisor/`)
- Upstream design — upstream `AGENTS.md` + Nous docs site (do not restate here)

## Ubiquitous language (terms with non-obvious meaning here)

| Term | Means here | Does NOT mean |
|---|---|---|
| Gateway | A long-lived systemd service bridging a messaging platform to agent sessions for one profile | An HTTP reverse proxy |
| Profile | An isolated `HERMES_HOME` (own config/auth/sessions/plugins/memory); one systemd unit each | A system user or "user profile" |
| Mnemosyne | The ADR-058 memory plugin (runtime-installed, not in-tree) | Anything in upstream Hermes |
| Clawd | The external evidence service (separate repo) that stores/recalls memory | Hermes core memory |
| OAuth pool | The shared `~/.hermes/auth.json` (a known SPOF) | A per-profile credential set |

## Decisions ledger (only the ones that shaped this repo)

| Date | Decision | ADR / ref | Why it matters |
|---|---|---|---|
| 2026-05-16 | Staged mnemosyne memory-provider rollout (research canary; finance/legal/marketing staged) | ADR-058 | Defines how/when profiles gain memory; gated on OAuth-cap regain |
| 2026-05-29 | gitleaks pre-commit leak-guard | ADR-059 | Fleet-safe secret hygiene |
| 2026-05-29 | Add fork `CLAUDE.md` (Minerva ops guide) | CLAWD-792 | Fleet rules travel with the code |
| — | Release-tag-pin vs branch-track upstream | (fork posture) | Deliberate, auditable upstream merges; no silent drift |

## What changed and why (recent)

- 2026-07-27 — **skills delta removed** (CLAWD-2834) — `skills/`, `tests/skills/` and `website/` are now byte-identical to the upstream merge-base. `xurl` is UPSTREAM's skill: the fork had only *recategorized* it (`social-media/` → `devops/`, R100) and skipped its test, so the fix was to revert, not delete — deleting would have removed upstream capability and *grown* the delta. `tufte-data-viz` (fork-local, 19 files) deleted; its live on-disk copies are handled separately (CLAWD-2835, operator-gated). Also removed two mis-pathed v0.18 merge-conflict artifacts under `skills/autonomous-ai-agents/hermes-agent/references/`. Runtime discovery is `$HERMES_HOME/skills` + `skills.external_dirs`, so no running process changes; already-synced profile copies are left in place by design (`tools/skills_sync.py` cleans the manifest, not the disk). Known accepted consequence: the relocation does not propagate to already-synced profiles, which keep a stale `devops/xurl` copy.
- 2026-05-29 — gitleaks pre-commit (ADR-059) — secret leak-guard
- 2026-05-29 — fork `CLAUDE.md` added (CLAWD-792) — document fleet operations
- 2026-05-27 — emit `session:end` from idle-expiry/auto-reset paths — correct lifecycle accounting for long-lived gateways
