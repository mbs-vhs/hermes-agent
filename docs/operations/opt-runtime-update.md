# `/opt/hermes-agent` pinned-runtime update runbook

This runbook implements CLAWD-3507 and the ratified
`2026-07-27-opt-hermes-deploy-substrate.md` decision: keep the fleet source at
`/opt/hermes-agent`, keep it root-owned, make it a pinned Git checkout, and
advance it only to an exact reviewed commit.

It does not authorize a deploy by itself. Source merge, fresh backup, bootstrap,
source advance, one-profile canary, and the controlled fleet restart are separate
receipted gates. The updater never calls `systemctl`.

## Safety invariants

- The target is one lowercase 40-hex commit id reachable from fetched
  `origin/main`. `origin/main` is not accepted as a target.
- `/opt/hermes-agent` and `.git` remain root-owned and not group/other-writable.
- `init` and every apply require a backup receipt less than 24 hours old. The updater
  re-hashes both the local archive and the downloaded R2 round-trip artifact and
  requires the hashes to match. The files must be distinct resolved paths and
  distinct inodes; symlink and hardlink aliases are rejected before `.git` can
  be created.
- The cleanup command is fixed to `git clean -fd`. Never use `-fdx`:
  `venv/` is ignored and contains the interpreter all fleet gateways execute.
- A steady-state apply refuses any tracked or untracked drift from the current `HEAD`.
  It no longer refuses on *provenance* drift: under CLAWD-3655 readiness asks git only,
  because a gitignored path can never be cleared by `clean -fd` and `-x` is banned, so
  a provenance veto produced refusals with no operator remedy. `audit` still reports
  provenance in full — the report was kept, only its veto was dropped.
- The first a2 reconciliation additionally requires an exact frozen `audit`
  payload captured after a1. Its tree and venv fingerprints cover paths, object
  types, regular-file bytes, modes, uid/gid ownership, symlink targets, and
  xattr value hashes (including POSIX ACL xattrs where the kernel exposes them).
  Timestamps are intentionally not part of the contract. Any measured change
  blocks before mutation.
- Successful a2 writes both `refs/hermes-runtime/bootstrap-complete` and a
  fsynced `/var/lib/hermes-agent/runtime-transactions/bootstrap-complete.json`.
  `init` and `--initial-evidence` are permanently refused afterward; normal
  apply and rollback require both closure records.
- Before any reset, the updater fsyncs a complete before-state transaction
  journal. Interrupted clean-to-clean advances restore and verify the exact
  previous tree automatically. Interrupted initial a2 stays fail-closed until
  the verified archive is restored and `recover` confirms the before-state
  fingerprint. No success receipt is needed for recovery.
- A source advance does not restart anything. Canary and fleet restarts remain
  explicit operator actions.

## Install the reviewed source assets

Install from the merged, reviewed commit—not from a session worktree:

```bash
sudo install -d -o root -g root -m 0755 /usr/local/libexec/hermes-agent
sudo install -o root -g root -m 0755 \
  scripts/update_opt_hermes_runtime.py \
  scripts/opt_provenance_report.py \
  /usr/local/libexec/hermes-agent/
sudo install -d -o root -g root -m 0755 /usr/local/share/doc/hermes-agent
sudo install -o root -g root -m 0644 \
  docs/operations/opt-runtime-update.md \
  /usr/local/share/doc/hermes-agent/opt-runtime-update.md
sudo install -o root -g root -m 0644 \
  systemd/ai.hermes.opt-runtime-{update,audit}.service \
  systemd/ai.hermes.opt-runtime-audit.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
```

The private fork needs a root-scoped, least-authority read credential. Configure
its path in `/etc/hermes-agent/runtime-update.env` (mode `0600`, root-owned); do
not let the system unit inherit the operator's home or SSH agent. The file may
set a credential-free `HERMES_OPT_RUNTIME_REMOTE_URL` plus `GIT_SSH_COMMAND`
referencing a read-only deploy key under `/etc/hermes-agent/`. URL userinfo,
query strings, and fragments are refused, and Git diagnostics redact URL
userinfo defensively. Never put credential material in the target file,
receipts, journal, or repository.

The mutating update service deliberately has no `[Install]` section and no
timer. The only enabled timer is the read-only audit:

```bash
sudo systemctl enable --now ai.hermes.opt-runtime-audit.timer
```

## Select the exact target

Write only the reviewed, merged commit id. A moving ref, abbreviated SHA,
uppercase SHA, comment, or second line is rejected.

```bash
target=<reviewed-40-hex-origin-main-commit>
printf '%s\n' "$target" | sudo tee /etc/hermes-agent/runtime-target >/dev/null
sudo chown root:root /etc/hermes-agent/runtime-target
sudo chmod 0600 /etc/hermes-agent/runtime-target
```

## Fresh backup receipt

Immediately before a1/a2 or any steady update, create the numeric-owner/ACL/xattr
snapshot, verify its SHA locally, upload it to
`r2:clawd-substrate-backups/opt-hermes-agent/`, download it to a separate local
path, and verify the same SHA again. Store a root-owned mode-`0600` JSON receipt
at `/var/lib/hermes-agent/latest-backup.json`:

```json
{
  "runtime": "/opt/hermes-agent",
  "runtime_fingerprint": {
    "algorithm": "sha256-canonical-manifest-v2",
    "sha256": "64-lowercase-hex",
    "entry_count": 6236,
    "contract": ["...exact array, see below..."]
  },
  "created_at": "2026-08-03T00:00:00Z",
  "archive_profile": "gnu-tar-zstd-pax-numeric-owner-acl-xattr-v1",
  "archive_root": ".",
  "archive_path": "/path/to/opt-hermes-agent-UTC.tar.zst",
  "archive_sha256": "64-lowercase-hex",
  "remote_uri": "r2:clawd-substrate-backups/opt-hermes-agent/object.tar.zst",
  "roundtrip_path": "/separate/path/roundtrip.tar.zst",
  "roundtrip_sha256": "the-same-64-lowercase-hex"
}
```

**All ten fields are required.** An earlier revision of this section showed seven and
omitted `runtime_fingerprint`, `archive_profile` and `archive_root`, so an operator
following it hit `FATAL: backup receipt missing fields` and had no documented way to
produce the one that is not a plain string. As written the runbook was unexecutable.

**The revision that fixed that shipped two values the code still refuses**, found by
independent review: it printed `"archive_profile": "opt-hermes-agent-full"` and
`"archive_root": "/opt/hermes-agent"`, and the code accepts only the exact strings now
shown above. An operator following it stopped at the first gate of the first conversion
with `FATAL: backup receipt archive_profile is not approved`. The remedy had inherited
the disease. This block is now **mechanically checked** —
`tests/scripts/test_opt_runtime_runbook_receipt.py` parses this very JSON out of this
file and asserts the code's own validator accepts its constant fields, so the runbook
can no longer advertise a shape the tool rejects.

**Producing an archive that matches `archive_profile`.** The profile string is not
decorative — it names the exact `tar` invocation, and nothing but the test helper knew
it. `--directory` is what makes `archive_root` the canonical `"."`:

```sh
sudo tar --zstd \
         --format=pax \
         --numeric-owner \
         --acls \
         --xattrs --xattrs-include='*' \
         --directory /opt/hermes-agent \
         -cf /var/backups/opt-hermes-agent-$(date -u +%Y%m%dT%H%M%SZ).tar.zst .
```

`--numeric-owner`, `--acls` and `--xattrs` are load-bearing: the runtime is owned by
`root` and executed by eleven separate `hermes-*` uids, so an archive that resolves
names or drops xattrs does not restore the tree the fleet was running.

**Where `runtime_fingerprint` comes from — do not hand-compute it.** `audit` already
emits exactly this object, with the right `algorithm`, `entry_count` and `contract`
array. Take it verbatim:

```sh
sudo python3 scripts/update_opt_hermes_runtime.py audit \
     --runtime /opt/hermes-agent --target <40-hex> \
  | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["tree_fingerprint"]))'
```

It must be measured on the SAME tree state the archive captured — take the audit and
the backup without mutating the runtime in between, or the receipt will correctly
refuse to bind.

**What the receipt does and does not prove.** `remote_uri` is validated for shape
only. This tool performs **no network I/O**: it never contacts R2, and the
"round-trip artifact" is a local file whose digest must equal the archive's — a `cp`
satisfies it. That check is still worth having, because it catches a truncated or
swapped archive, but it is byte-identity, **not** remote durability. Emitted receipts
carry `"remote_verified": false` so a downstream reader cannot mistake one for the
other. Off-host durability is the backup producer's guarantee.


The updater validates the receipt and both local files; a claimed remote hash
without a downloaded round-trip artifact is not sufficient. The round-trip
path must not resolve to the archive and must not share its device/inode.

## First conversion: a1 then a2

Run a1 in a clean root environment that reads the root-only updater environment
file. `init` fingerprints every non-`.git` path, including the ignored live
venv, before and after Git initialization/fetch and `reset --mixed`. It fails if
a worktree path changes.

```bash
sudo /usr/bin/env -i PATH=/usr/bin:/bin /bin/bash -c '
  set -a
  . /etc/hermes-agent/runtime-update.env
  exec /usr/bin/python3.11 \
    /usr/local/libexec/hermes-agent/update_opt_hermes_runtime.py init \
    --runtime /opt/hermes-agent \
    --target-file /etc/hermes-agent/runtime-target \
    --backup-receipt /var/lib/hermes-agent/latest-backup.json \
    --remote-url "$HERMES_OPT_RUNTIME_REMOTE_URL"
'
```

Freeze the real target's exact provenance plus the exact `git clean -nd`
preview. `audit` is read-only and exits zero when measurement succeeds even
though the pre-a2 tree intentionally differs; `status` is the nonzero drift
gate.

```bash
sudo install -d -o root -g root -m 0700 /var/lib/hermes-agent/bootstrap
sudo /usr/bin/python3.11 \
  /usr/local/libexec/hermes-agent/update_opt_hermes_runtime.py audit \
  --runtime /opt/hermes-agent \
  --target-file /etc/hermes-agent/runtime-target \
  > /var/lib/hermes-agent/bootstrap/initial-audit.json
sudo chown root:root /var/lib/hermes-agent/bootstrap/initial-audit.json
sudo chmod 0600 /var/lib/hermes-agent/bootstrap/initial-audit.json
```

Review that evidence against the classified orphan ledger. If it differs by
one path or one provenance field, stop; do not edit the evidence to fit. Then
exercise the complete preflight without mutation:

```bash
sudo /usr/bin/python3.11 \
  /usr/local/libexec/hermes-agent/update_opt_hermes_runtime.py apply \
  --runtime /opt/hermes-agent \
  --target-file /etc/hermes-agent/runtime-target \
  --initial-evidence /var/lib/hermes-agent/bootstrap/initial-audit.json \
  --backup-receipt /var/lib/hermes-agent/latest-backup.json \
  --receipt-dir /var/lib/hermes-agent/runtime-receipts \
  --dry-run
```

Only after the dry run and the evidence review are green, remove `--dry-run`.
That is a2: `reset --hard` followed by `clean -fd -e /venv`, with a git-clean and
venv-preservation post-check. It writes a durable transaction journal and an
update receipt but restarts no unit.

### `target … tracks N path(s) under venv/` — what this refusal means

Before any mutation, apply runs `git ls-tree -r --name-only <target> -- venv`. If the
target COMMITS anything under `venv/`, apply refuses outright and nothing is touched.

This is not a hypothetical. The live interpreter is `/opt/hermes-agent/venv/bin/python`
and it is gitignored, so it survives `clean` only because of the `-e /venv` exclusion.
A target that both tracks `venv/…` **and** drops `venv/` from `.gitignore` would have
that exclusion stop applying, `reset --hard` would overwrite the interpreter and `clean`
would sweep the rest — with the eleven gateway units still pointed at it. Recovery then
fails too, because recovery needs an interpreter.

There is no override flag, and that is deliberate. If you hit this, the target is wrong:
find the commit that added tracked paths under `venv/` and fix it in the repo. Do not
work around it on the runtime.

## Steady-state source advance

The manual system service fetches, verifies the exact target file, rejects any
runtime drift, validates the fresh backup receipt, advances the source, and
writes a receipt:

```bash
sudo systemctl start ai.hermes.opt-runtime-update.service
sudo systemctl status ai.hermes.opt-runtime-update.service --no-pager
sudo journalctl -u ai.hermes.opt-runtime-update.service -n 100 --no-pager
sudo systemctl start ai.hermes.opt-runtime-audit.service
```

Do not enable or schedule the mutating service. The audit timer is intentionally
the only periodic unit. The services have no `ConditionPath*` shortcuts:
missing target or Git metadata is an explicit failed audit/update, never a
successful skipped invocation.

## ADR-091 gate before canary

Before restarting even one profile, record the CLAWD-3486 disposition.
`gateway/lifecycle_notifications.py` is source-only versus the pre-update live
tree and emits an operator notification through the conversational adapter. It
is not covered by ADR-091's permanent Hermes conversational exemption. The
canary must therefore wait for the notification-bus migration or the explicitly
reviewed interim attribution/test/allowlist guard owned by CLAWD-3486.

## Explicit canary and controlled fleet restart

After the source receipt and CLAWD-3486 gate are recorded, restart one
noncritical profile. Resolve its real uid; do not use the operator's masked
legacy gateway units:

```bash
profile=minerva
account="hermes-$profile"
uid="$(id -u "$account")"
sudo -u "$account" env XDG_RUNTIME_DIR="/run/user/$uid" \
  systemctl --user restart "ai.hermes.gateway-$profile.service"
sudo -u "$account" env XDG_RUNTIME_DIR="/run/user/$uid" \
  systemctl --user is-active "ai.hermes.gateway-$profile.service"
sudo journalctl _UID="$uid" -u "ai.hermes.gateway-$profile.service" \
  --since '-5 minutes' --no-pager
```

The canary receipt must include `active`, a real conversational smoke, provider
recall/writer behavior, and the ADR-091 lifecycle-notification outcome. Only then
restart the remaining discovered per-user gateway units one at a time, with an
`is-active` and journal check after each. Do not hardcode a profile count into
the updater; the live unit inventory is the authority.

## Rollback

For an interrupted first a2 conversion, rollback is the verified tar snapshot.
The updater leaves a `recovery_required` journal with the exact backup paths and
before-state fingerprint because the initial `reset --mixed` makes `HEAD` the
target before a2 and Git alone cannot reconstruct the pre-a2 file population.
Stop any canary, restore numeric owners/ACLs/xattrs from the named archive, then
consume the incomplete transaction without a success receipt:

```bash
sudo /usr/bin/python3.11 \
  /usr/local/libexec/hermes-agent/update_opt_hermes_runtime.py recover \
  --runtime /opt/hermes-agent \
  --transaction-dir /var/lib/hermes-agent/runtime-transactions
```

`recover` remains nonzero until the complete tree and ignored venv match the
fsynced before-state. A status audit also stays nonzero while any incomplete
transaction exists.

For later clean-to-clean advances, first snapshot the failed target and create a
new fresh backup receipt, then use the successful update receipt:

```bash
sudo /usr/bin/python3.11 \
  /usr/local/libexec/hermes-agent/update_opt_hermes_runtime.py rollback \
  --runtime /opt/hermes-agent \
  --update-receipt /var/lib/hermes-agent/runtime-receipts/<failed-update>.json \
  --backup-receipt /var/lib/hermes-agent/latest-backup.json \
  --receipt-dir /var/lib/hermes-agent/runtime-receipts \
  --dry-run
# Re-run without --dry-run, then repeat the explicit canary sequence.
```

Rollback also changes source only. It never restarts gateways automatically.
Both rollback and a normal apply refuse to run if either durable bootstrap
closure record is absent or an incomplete transaction remains.
