# MERGE STATE — upstream/main (v2026.7.20) into the fork

**Branch:** `merge/upstream-v2026.7.20-dryrun` · **UNPUSHED, UNLANDED, DO NOT PUSH.**
**Refs:** fork `3dfaac7cb` · `MERGE_HEAD` = `upstream/main` `96db67b8` · merge-base `7c1a029`

Conflict count: **17 files** — exactly what `git merge-tree --write-tree` predicted.

## ⚠ THE TREE DOES NOT IMPORT

`gateway/platforms/api_server.py` still carries **10 conflict markers**, deliberately.
While they remain, `import gateway.run` fails and **no test in the tree can collect** —
that is the marker state, not a regression. See **CLAWD-3092**.

## Resolved with per-hunk justification (15 of 17)

`gateway/run.py` — 8 regions: BOTH ×1, OURS ×4, MERGE ×2, plus a hand-merge of
`_send_home_channel_startup_notifications` (upstream's loop header + both fork skips
re-expressed, pinned skip relocated to use `transport.adapter`). Two regions were
cases where **neither side alone is correct** — keep-ours `NameError`s on
`platform_cfg` / `_cron_at_start`, keep-theirs silently drops fork behaviour.

| File | Decision |
|---|---|
| `gateway/platforms/qqbot/adapter.py` | THEIRS — docstring only, no fork behaviour |
| `plugins/platforms/wecom/callback_adapter.py` | THEIRS — `del is_reconnect`; without the kwarg the reconnect watcher dies with `TypeError` and the platform silently stays offline |
| `agent/conversation_loop.py` | h1 BOTH; h2 MERGE — took upstream's `compose_user_api_content` restructure and **threaded the fork's recent seed through it as a 4th param** (see below) |
| `agent/turn_context.py` *(not conflicted; edited to land the above)* | `compose_user_api_content(..., recent_seed="")` + prologue passes `agent._recent_seed_block` |
| `cli.py` | OURS ×2 — the fork deliberately REFUSES the `--global` persist (ADR-072, manifest-governed); theirs would silently re-enable config clobbering |
| `gateway/session.py` | h1 BOTH fields; h2 THEIRS' lock restructure **with the fork's `_prior_session_id_for_emit` capture re-applied**; h3 BOTH kwargs |
| `gateway/slash_commands.py` | BOTH ×2 — ADR-072 governance hook + notice alongside upstream's `is_once`/one-turn additions. Plus `floor = sorted(_ALWAYS_ALLOWED_FOR_USERS)` |
| `pyproject.toml` | MERGE — upstream's dev pins (starlette CVE, setuptools cap) + the fork's additive `pytest-timeout` + fork comment block |
| `scripts/release.py` | THEIRS restructure (`LEGACY_AUTHOR_MAP` + `contributors/emails/`, conflict-free by construction) with our mapping **re-applied through the new mechanism** (`contributors/emails/morgan-at-videotape-ai`) |
| `tests/gateway/test_channel_directory.py` | BOTH — add/add |
| `tests/gateway/test_model_picker_persist.py` | BOTH — add/add |
| `tests/hermes_cli/test_list_picker_providers.py` | BOTH — add/add |
| `tests/hermes_cli/test_model_switch_custom_providers.py` | BOTH — add/add |
| `tests/hermes_cli/test_web_server.py` | BOTH — add/add |
| `tests/gateway/test_model_command_flat_string_config.py` | OURS — its assertions CONTRADICT upstream's (refusal vs persist-happened); must match the `cli.py` policy kept above |

### The `compose_user_api_content` threading, because it is the subtlest decision

Upstream made that function the **single** composition point: the prologue stamps its
output as `api_content` and `conversation_loop` sends the same output, so the persisted
sidecar can never drift from the bytes on the wire. The fork's cross-surface recent
seed (ADR-065 / CLAWD-1542) previously rode a separate `_injections` list that upstream
retired. Appending the seed at only one call site would break that prompt-cache
invariant, so the seed is threaded **through the function** and both call sites pass it.

## NOT resolved (2)

1. **`gateway/platforms/api_server.py`** — 10 hunks, left in conflict state on purpose.
   Upstream refactor (renamed request surface, extracted `active_agent_work_count` /
   `_profile_scope` / `_http_route_table` / `unregister_gateway_notify` /
   `_stopping_run_ids` / `_resolve_route`) over a +283/-15 fork delta.
   **`clarify_gateway` (13 refs) and `_run_clarify_sessions` (6 refs) are fork-only —
   zero upstream** — and 3 hunks touch them. → **CLAWD-3092** (all 10 enumerated).
2. **`tests/gateway/test_session_boundary_hooks.py`** — resolved as OURS ×6, which was
   **wrong**: the file auto-merged around the hunks to upstream's structure, so 8 tests
   fail. Needs the same THEIRS-restructure + re-apply treatment `session.py` got, and is
   **downstream of** that reconciliation. → **CLAWD-3093**.

## Verified before api_server blocked the tree

- 17-file conflict count matched the prediction exactly.
- All three merge behavioural contracts (`tests/gateway/test_upstream_merge_behavioural_contracts.py`)
  passed **11/11** post-merge, mutation-verified non-vacuous on both sides.
- The cross-file `slash_access` / `slash_commands` split regression was caught by the
  CLAWD-2839 floor guard and fixed (42 passed).
- Bounded lifecycle cone: 9 files, 200 passed, 0 failed.

## Decision recorded: MERGE, NEVER REBASE

Rebase replays all 75 non-merge commits **including ones already reverted** — it stopped
on `move xurl skill to devops` and `add tufte data visualization skill`, both reverted
to zero by CLAWD-2834, and charged `website/sidebars.ts` conflicts for them. Two driver
attempts could not pass add/add and `directory rename split` conflict classes. Do not
build a rebase driver. If linear history is wanted, squash first, then rebase.
