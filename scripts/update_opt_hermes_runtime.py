#!/usr/bin/env python3
"""Advance the root-owned Hermes fleet runtime to an exact, reviewed commit.

This is the source half of CLAWD-3507.  It deliberately does not restart a
gateway: source advance, canary restart, and fleet restart are separate,
operator-visible phases.  The mutating commands are fixed to ``reset --hard``
followed by ``clean -fd``.  ``clean -fdx`` is never accepted or constructed,
because ``/opt/hermes-agent/venv`` is ignored and is the interpreter used by
the fleet.

The first conversion of the existing non-git tree is also split:

* ``init`` creates/fetches Git metadata and uses ``reset --mixed`` to seed the
  index without changing a worktree file (proposal step a1).
* ``audit`` freezes the exact provenance and ``git clean -nd`` preview.
* ``apply --initial-evidence ...`` requires that frozen evidence to match
  byte-for-byte before it performs proposal step a2.

Every apply additionally requires a recent local backup and an R2 round-trip
receipt.  READ THE NEXT SENTENCE BEFORE RELYING ON THAT.  This tool performs NO
NETWORK I/O.  It cannot and does not confirm that any byte reached R2: `remote_uri`
is validated for shape only, and the "round-trip artifact" is a local file whose
digest must equal the archive's.  So the receipt is an ATTESTATION by whatever
produced it, plus a local byte-identity check.  A `cp` of the archive satisfies it.
Remote durability is the backup producer's guarantee, not this tool's, and the
emitted receipt says so in `remote_verified`.  A steady-state update refuses dirty or untracked runtime
state.  Targets are full 40-hex commit ids, never moving branch names.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence

try:  # Works both from the repo and when both files are installed in libexec.
    from scripts import opt_provenance_report as provenance
except ImportError:  # pragma: no cover - exercised by the installed layout.
    import opt_provenance_report as provenance  # type: ignore[no-redef]


DEFAULT_RUNTIME = Path("/opt/hermes-agent")
DEFAULT_TARGET_FILE = Path("/etc/hermes-agent/runtime-target")
DEFAULT_LOCK_FILE = Path("/run/lock/hermes-opt-runtime-update.lock")
DEFAULT_RECEIPT_DIR = Path("/var/lib/hermes-agent/runtime-receipts")
DEFAULT_TRANSACTION_DIR = Path("/var/lib/hermes-agent/runtime-transactions")
# The live virtualenv is runtime state this tool must NEVER remove, independent of
# git's opinion about it. Anchored with a leading slash so it matches only the
# runtime root, not a nested vendor directory.
VENV_EXCLUDE = "/venv"

EXACT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BACKUP_MAX_AGE = dt.timedelta(hours=24)
BACKUP_ARCHIVE_PROFILE = "gnu-tar-zstd-pax-numeric-owner-acl-xattr-v1"
BOOTSTRAP_REF = "refs/hermes-runtime/bootstrap-complete"
BOOTSTRAP_STATE = "bootstrap-complete.json"
INCOMPLETE_TRANSACTION_STATES = {
    "prepared",
    "resetting",
    "reset_done",
    "cleaning",
    "clean_done",
    "verified",
    "bootstrap_marking",
    "bootstrap_marked",
    "receipting",
    "publication_ready",
    "recovery_required",
}
TERMINAL_TRANSACTION_STATES = {
    "complete",
    "recovered",
    "recovered_external",
    "recovered_after_failure",
}
ALL_TRANSACTION_STATES = INCOMPLETE_TRANSACTION_STATES | TERMINAL_TRANSACTION_STATES
DRIFT_EXIT = 2
UNMEASURED_EXIT = 3


class UpdateError(RuntimeError):
    """A fail-closed updater refusal."""


def _redact(value: str) -> str:
    """Remove URL userinfo from diagnostics before it reaches a journal."""
    return re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1<redacted>@", value)


def _validate_remote_url(value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise UpdateError("origin URL contains a control character")
    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            raise UpdateError("origin URL is malformed")
        allowed_ssh_user = parsed.scheme == "ssh" and parsed.username == "git"
        if parsed.password is not None or (
            parsed.username is not None and not allowed_ssh_user
        ):
            raise UpdateError("origin URL must not contain embedded credentials")
        if parsed.query or parsed.fragment:
            raise UpdateError("origin URL must not contain a query or fragment")
    elif "@" in value and not value.startswith(("/", "./", "../")):
        # The only accepted SCP-style userinfo is the conventional non-secret
        # git@host:path form.  Tokens and arbitrary usernames are refused.
        if not re.fullmatch(r"git@[^/:\s]+:.+", value):
            raise UpdateError("origin URL contains unsupported userinfo")
    return value


def _canonical_r2_uri(value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise UpdateError("backup remote_uri contains a control character")
    match = re.fullmatch(
        r"r2:([a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?)/"
        r"([A-Za-z0-9](?:[A-Za-z0-9._/-]{0,1022}[A-Za-z0-9])?)",
        value,
    )
    if match is None:
        raise UpdateError("backup remote_uri is not a canonical credential-free r2 location")
    bucket, key = match.groups()
    segments = key.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise UpdateError("backup remote_uri contains a noncanonical object key")
    if ".." in bucket:
        raise UpdateError("backup remote_uri bucket is noncanonical")
    return f"r2:{bucket}/{key}"


def _git(
    runtime: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/var/empty",
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
    }
    scoped_ssh = os.environ.get("HERMES_OPT_RUNTIME_GIT_SSH_COMMAND")
    if scoped_ssh:
        if any(character in scoped_ssh for character in ("\n", "\r", "\x00")):
            raise UpdateError("scoped Git SSH command contains a control character")
        env["GIT_SSH_COMMAND"] = scoped_ssh
    proc = subprocess.run(
        ["git", "-C", str(runtime), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        rendered_args = _redact(" ".join(args))
        raise UpdateError(f"git {rendered_args} failed: {_redact(detail)}")
    return proc


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2) + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise UpdateError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _safe_owned_file(path: Path, description: str) -> None:
    if path.is_symlink():
        raise UpdateError(f"{description} must not be a symlink: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise UpdateError(f"cannot stat {description} {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise UpdateError(f"{description} is not a regular file: {path}")
    if info.st_uid != os.geteuid():
        raise UpdateError(
            f"{description} owner uid {info.st_uid} does not match updater uid {os.geteuid()}: {path}"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UpdateError(f"{description} is group/other-writable: {path}")


def _runtime_safety(runtime: Path, *, require_git: bool) -> None:
    if runtime.is_symlink():
        raise UpdateError(f"runtime must not be a symlink: {runtime}")
    try:
        info = runtime.stat()
    except OSError as exc:
        raise UpdateError(f"cannot stat runtime {runtime}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise UpdateError(f"runtime is not a directory: {runtime}")
    if info.st_uid != os.geteuid():
        raise UpdateError(
            f"runtime owner uid {info.st_uid} does not match updater uid {os.geteuid()}"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UpdateError(f"runtime is group/other-writable: {runtime}")
    if runtime.resolve() == DEFAULT_RUNTIME and os.geteuid() != 0:
        raise UpdateError(f"{DEFAULT_RUNTIME} may only be advanced by root")
    if require_git:
        git_dir = runtime / ".git"
        if not git_dir.is_dir():
            raise UpdateError(
                f"runtime is not initialized as a Git checkout: {runtime}"
            )
        if git_dir.stat().st_uid != os.geteuid():
            raise UpdateError("runtime .git ownership does not match the updater uid")


@contextlib.contextmanager
def _exclusive_lock(lock_file: Path) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            lock_file,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise UpdateError(f"cannot open lock file safely {lock_file}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise UpdateError(
                    f"lock file is not a regular file owned by the updater uid: {lock_file}"
                )
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateError(f"another updater holds {lock_file}") from exc
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            yield
    finally:
        # The file is intentionally retained in /run; deleting a lock file permits
        # two processes to lock different inodes during the unlink/create race.
        pass


def _target_value(target: str | None, target_file: Path | None) -> str:
    if bool(target) == bool(target_file):
        raise UpdateError("provide exactly one of --target or --target-file")
    if target_file is not None:
        _safe_owned_file(target_file, "target file")
        try:
            lines = target_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise UpdateError(f"cannot read target file {target_file}: {exc}") from exc
        if len(lines) != 1:
            raise UpdateError("target file must contain exactly one line")
        target = lines[0]
    assert target is not None
    if not EXACT_SHA_RE.fullmatch(target):
        raise UpdateError("target must be one lowercase 40-hex commit id")
    return target


def _remote_url(runtime: Path) -> str:
    return _validate_remote_url(
        _git(runtime, "remote", "get-url", "origin").stdout.strip()
    )


def _fetch_and_verify(runtime: Path, target: str, expected_remote: str | None) -> None:
    current_remote = _remote_url(runtime)
    if expected_remote is not None:
        expected_remote = _validate_remote_url(expected_remote)
        if current_remote != expected_remote:
            raise UpdateError("origin URL does not match the approved remote")
    _git(
        runtime,
        "fetch",
        "--prune",
        "--tags",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    _verify_target(runtime, target)


def _verify_target(runtime: Path, target: str) -> None:
    resolved = _git(
        runtime, "rev-parse", "--verify", f"{target}^{{commit}}"
    ).stdout.strip()
    if resolved != target:
        raise UpdateError(
            f"target did not resolve exactly: requested {target}, resolved {resolved}"
        )
    ancestor = _git(
        runtime,
        "merge-base",
        "--is-ancestor",
        target,
        "refs/remotes/origin/main",
        check=False,
    )
    if ancestor.returncode != 0:
        raise UpdateError(f"target {target} is not reachable from fetched origin/main")


def _head(runtime: Path) -> str | None:
    proc = _git(runtime, "rev-parse", "--verify", "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _ref_exists(runtime: Path, ref: str) -> bool:
    return _git(runtime, "rev-parse", "--verify", ref, check=False).returncode == 0


def _xattr_fingerprint(path: Path) -> list[dict[str, str]]:
    """Hash xattr values, including kernel-exposed POSIX ACL xattrs."""
    try:
        names = sorted(os.listxattr(path, follow_symlinks=False))
    except OSError as exc:
        if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            return []
        raise UpdateError(f"cannot list xattrs for {path}: {exc}") from exc
    result: list[dict[str, str]] = []
    for name in names:
        try:
            value = os.getxattr(path, name, follow_symlinks=False)
        except OSError as exc:
            raise UpdateError(f"cannot read xattr {name!r} for {path}: {exc}") from exc
        result.append({"name": name, "sha256": hashlib.sha256(value).hexdigest()})
    return result


def _manifest_fingerprint(root: Path, *, exclude_git: bool) -> dict[str, Any]:
    """Digest a canonical path/type/content/ownership/mode/xattr manifest.

    The digest contract intentionally excludes timestamps.  It includes the
    root and every descendant without following symlinks.  POSIX ACLs are
    covered where the filesystem exposes them as system.posix_acl_* xattrs.
    """
    records: list[dict[str, Any]] = []

    def record(path: Path, relative: str) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise UpdateError(f"cannot stat fingerprint path {path}: {exc}") from exc
        entry: dict[str, Any] = {
            "path": relative,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "xattrs": _xattr_fingerprint(path),
        }
        if stat.S_ISLNK(info.st_mode):
            entry.update({"type": "symlink", "target": os.readlink(path)})
        elif stat.S_ISREG(info.st_mode):
            entry.update({
                "type": "file",
                "size": info.st_size,
                "sha256": _sha256(path),
            })
        elif stat.S_ISDIR(info.st_mode):
            entry["type"] = "directory"
        else:
            entry.update({"type": "special", "rdev": info.st_rdev})
        records.append(entry)

    def walk(directory: Path, relative: str) -> None:
        record(directory, relative)
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise UpdateError(
                f"cannot enumerate fingerprint directory {directory}: {exc}"
            ) from exc
        for child in children:
            if exclude_git and relative == "." and child.name == ".git":
                continue
            child_path = Path(child.path)
            child_relative = (
                child.name if relative == "." else f"{relative}/{child.name}"
            )
            try:
                is_directory = child.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise UpdateError(
                    f"cannot inspect fingerprint path {child_path}: {exc}"
                ) from exc
            if is_directory:
                walk(child_path, child_relative)
            else:
                record(child_path, child_relative)

    walk(root, ".")
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    return {
        "algorithm": "sha256-canonical-manifest-v2",
        "contract": [
            "path",
            "type",
            "content",
            "mode",
            "uid",
            "gid",
            "symlink-target",
            "xattrs",
        ],
        "entry_count": len(records),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _tree_fingerprint(runtime: Path) -> dict[str, Any]:
    """Fingerprint every non-.git path, including the ignored live venv."""
    return _manifest_fingerprint(runtime, exclude_git=True)


def _clean_preview(runtime: Path) -> list[str]:
    proc = _git(runtime, "clean", "-nd", "-e", VENV_EXCLUDE)
    return proc.stdout.splitlines()


def _status_lines(runtime: Path) -> list[str]:
    proc = _git(runtime, "status", "--porcelain=v1", "--untracked-files=all")
    return proc.stdout.splitlines()


def _venv_guard(runtime: Path) -> dict[str, Any]:
    venv = runtime / "venv"
    python = venv / "bin" / "python"
    if not venv.is_dir() or not python.exists():
        raise UpdateError(f"live ignored venv is missing or incomplete: {python}")
    ignored = _git(runtime, "check-ignore", "-q", "--", "venv", check=False)
    if ignored.returncode != 0:
        raise UpdateError("runtime/venv is not ignored; clean -fd is unsafe")
    return {
        "path": str(venv),
        "fingerprint": _manifest_fingerprint(venv, exclude_git=False),
    }


def _build_audit(runtime: Path, target: str) -> dict[str, Any]:
    tree_before = _tree_fingerprint(runtime)
    venv = _venv_guard(runtime)
    try:
        report = provenance.build_report(
            runtime,
            runtime,
            target,
            provenance.DEFAULT_EXCLUDES,
            python=str(runtime / "venv" / "bin" / "python"),
        )
    except provenance.ProbeFailure as exc:
        raise UpdateError(f"provenance probe is unmeasured: {exc}") from exc
    status = _status_lines(runtime)
    clean_preview = _clean_preview(runtime)
    tree_after = _tree_fingerprint(runtime)
    if tree_before != tree_after:
        raise UpdateError("runtime tree changed while the audit was being measured")
    return {
        "schema": 1,
        "runtime": str(runtime.resolve()),
        "target": target,
        "head": _head(runtime),
        "origin": _remote_url(runtime),
        "status": status,
        "clean_preview": clean_preview,
        "tree_fingerprint": tree_before,
        "provenance": report,
        "venv": venv,
    }


def _git_ignored(runtime: Path, paths: list[str]) -> set[str]:
    """Which of `paths` git actually ignores in `runtime`.

    Uses check-ignore's batch stdin form so one process settles the whole list.
    A non-zero exit just means "none matched" and is not an error here.
    """
    if not paths:
        return set()
    proc = subprocess.run(
        ["git", "-C", str(runtime), "check-ignore", "--stdin"],
        input="\n".join(paths),
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _provenance_is_exact(report: dict[str, Any], runtime: Path | None = None) -> bool:
    """Readiness against what `clean -fd` can ACTUALLY remove.

    The provenance walk is deliberately gitignore-BLIND — that is correct for a
    census, and `opt_provenance_report.py` stays that way. But readiness was ANDing
    that blind count with a gitignore-AWARE clean check, so any path git ignores and
    the walk does not exclude became permanently un-clearable: `clean -fd` will never
    touch it and `-x` is banned outright because it would delete the venv.

    That is not hypothetical. The fork's .gitignore covers `logs/`, `data/`, `.env` —
    files the gateway writes AT LAUNCH. So the first gateway start after a successful
    conversion would jam every later `apply`, and `rollback` (which shares this
    preflight) would refuse in exactly the incident it exists for. The tool would have
    solved "a merged fix cannot reach the fleet" approximately once.

    Ignored orphans are therefore excluded from the readiness verdict — but only for
    `only_in_tree`. An IMPORTABLE orphan still fails even when ignored: a stray module
    on the import path can shadow real code, and one the tool cannot remove is a
    finding an operator must see rather than a state it may proceed from.
    """
    counts = dict(report["counts"])
    if runtime is not None:
        stray = [str(x) for x in report.get("only_in_tree", [])]
        ignored = _git_ignored(runtime, stray)
        if ignored:
            counts["only_in_tree"] = len([x for x in stray if x not in ignored])
    return all(
        counts[name] == 0
        for name in (
            "only_in_tree",
            "only_in_ref",
            "differing",
            "only_in_tree_importable",
            "unreadable",
        )
    )


def _ready(audit: dict[str, Any]) -> bool:
    return (
        audit["head"] == audit["target"]
        and not audit["status"]
        and not audit["clean_preview"]
        and not audit.get("incomplete_transactions", [])
        and audit.get("bootstrap_closure_valid", True) is True
        and _provenance_is_exact(audit["provenance"], Path(audit["runtime"]))
    )


def _load_json(path: Path, description: str) -> dict[str, Any]:
    _safe_owned_file(path, description)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"cannot read {description} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError(f"{description} must contain a JSON object")
    return payload


def _owned_directory(path: Path, description: str) -> None:
    if path.is_symlink():
        raise UpdateError(f"{description} must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.stat()
    except OSError as exc:
        raise UpdateError(f"cannot stat {description} {path}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise UpdateError(f"{description} must be a directory owned by the updater uid")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UpdateError(f"{description} is group/other-writable: {path}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json_write(path: Path, payload: dict[str, Any], description: str) -> Path:
    _owned_directory(path.parent, f"{description} directory")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise UpdateError(f"cannot persist {description} {path}: {exc}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return path


def _parse_utc(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise UpdateError("backup receipt created_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise UpdateError("backup receipt created_at must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _valid_tree_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("algorithm") == "sha256-canonical-manifest-v2"
        and value.get("contract")
        == [
            "path",
            "type",
            "content",
            "mode",
            "uid",
            "gid",
            "symlink-target",
            "xattrs",
        ]
        and isinstance(value.get("entry_count"), int)
        and value["entry_count"] > 0
        and isinstance(value.get("sha256"), str)
        and bool(SHA256_RE.fullmatch(value["sha256"]))
    )


def _archive_restored_fingerprint(path: Path) -> dict[str, Any]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/var/empty",
        "LC_ALL": "C",
        "LANG": "C",
    }
    listing = subprocess.run(
        ["/usr/bin/tar", "--zstd", "--list", "--file", str(path)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if listing.returncode != 0:
        raise UpdateError(f"backup archive is not readable tar+zstd: {_redact(listing.stderr)}")
    names = listing.stdout.splitlines()
    if "./" not in names:
        raise UpdateError("backup archive has no canonical ./ root entry")
    for name in names:
        if name == "./":
            continue
        if not name.startswith("./") or "//" in name:
            raise UpdateError("backup archive contains a noncanonical member path")
        parts = name[2:].rstrip("/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise UpdateError("backup archive contains an unsafe member path")
    # dir= is deliberate. The default temp root is /tmp, which is tmpfs on this
    # host, and this block extracts a FULL copy of the runtime — venv included —
    # to re-hash it. Doing that in RAM on the box running the fleet is a memory
    # event, and the unit's PrivateTmp=yes does not change where /tmp lives. Verify
    # next to the receipt instead, which StateDirectory puts on disk.
    with tempfile.TemporaryDirectory(
        prefix="hermes-backup-verify-", dir=_verify_scratch_dir(path)
    ) as temporary:
        restored = Path(temporary)
        extraction = subprocess.run(
            [
                "/usr/bin/tar",
                "--zstd",
                "--extract",
                "--same-owner",
                "--same-permissions",
                "--acls",
                "--xattrs",
                "--xattrs-include=*",
                "--file",
                str(path),
                "--directory",
                str(restored),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        if extraction.returncode != 0:
            detail = extraction.stderr.strip() or extraction.stdout.strip()
            raise UpdateError(f"backup archive fidelity restore failed: {_redact(detail)}")
        return _tree_fingerprint(restored)



def _verify_scratch_dir(archive_path: Path) -> str:
    """A disk-backed directory for whole-tree extraction, never tmpfs.

    Prefers the archive's own directory (StateDirectory => /var/lib, on disk).
    Falls back to the default only if that is unusable, because refusing to verify
    a backup is worse than verifying it slowly.
    """
    try:
        candidate = Path(archive_path).resolve().parent
        if candidate.is_dir() and os.access(candidate, os.W_OK):
            return str(candidate)
    except OSError:
        pass
    return tempfile.gettempdir()

def _validate_backup_receipt(path: Path, runtime: Path) -> dict[str, Any]:
    receipt = _load_json(path, "backup receipt")
    required = {
        "runtime",
        "runtime_fingerprint",
        "created_at",
        "archive_profile",
        "archive_root",
        "archive_path",
        "archive_sha256",
        "remote_uri",
        "roundtrip_path",
        "roundtrip_sha256",
    }
    missing = sorted(required - receipt.keys())
    if missing:
        raise UpdateError(f"backup receipt missing fields: {', '.join(missing)}")
    string_fields = required - {"runtime_fingerprint"}
    if any(not isinstance(receipt[field], str) for field in string_fields):
        raise UpdateError("backup receipt string fields have invalid types")
    if not _valid_tree_fingerprint(receipt["runtime_fingerprint"]):
        raise UpdateError("backup receipt runtime_fingerprint is invalid")
    if Path(receipt["runtime"]).resolve() != runtime.resolve():
        raise UpdateError("backup receipt runtime does not match --runtime")
    created = _parse_utc(receipt["created_at"])
    age = dt.datetime.now(dt.timezone.utc) - created
    if age < dt.timedelta(minutes=-5) or age > BACKUP_MAX_AGE:
        raise UpdateError(f"backup receipt is not fresh (age {age})")
    if receipt["archive_profile"] != BACKUP_ARCHIVE_PROFILE:
        raise UpdateError("backup receipt archive_profile is not approved")
    if receipt["archive_root"] != ".":
        raise UpdateError("backup receipt archive_root must be canonical '.'")
    archive_sha = receipt["archive_sha256"]
    roundtrip_sha = receipt["roundtrip_sha256"]
    if not SHA256_RE.fullmatch(str(archive_sha)) or archive_sha != roundtrip_sha:
        raise UpdateError(
            "backup and R2 round-trip SHA-256 values are invalid or differ"
        )
    archive_path = Path(receipt["archive_path"])
    # Stated where a reader will hit it, not only in the module docstring: nothing
    # below contacts R2. An earlier docstring called this "a verified R2 round-trip
    # receipt", which reads as proof the object is durable off-host. It is not. The
    # checks are: the URI is well-formed, the round-trip artifact is a distinct
    # owned file, and its digest equals the archive's. All three are satisfiable
    # with `cp`. That is still worth doing — it catches a truncated or swapped
    # archive — but it is byte-identity, not remote durability.
    roundtrip_path = Path(receipt["roundtrip_path"])
    _safe_owned_file(archive_path, "local backup archive")
    _safe_owned_file(roundtrip_path, "R2 round-trip artifact")
    try:
        archive_resolved = archive_path.resolve(strict=True)
        roundtrip_resolved = roundtrip_path.resolve(strict=True)
        archive_info = archive_path.stat()
        roundtrip_info = roundtrip_path.stat()
    except OSError as exc:
        raise UpdateError(f"cannot resolve backup artifacts: {exc}") from exc
    if archive_resolved == roundtrip_resolved:
        raise UpdateError("backup and R2 round-trip artifacts must be separate paths")
    if (archive_info.st_dev, archive_info.st_ino) == (
        roundtrip_info.st_dev,
        roundtrip_info.st_ino,
    ):
        raise UpdateError("backup and R2 round-trip artifacts must be separate files")
    if _sha256(archive_path) != archive_sha:
        raise UpdateError("local backup archive hash does not match its receipt")
    if _sha256(roundtrip_path) != roundtrip_sha:
        raise UpdateError("R2 round-trip artifact hash does not match its receipt")
    receipt["remote_uri"] = _canonical_r2_uri(receipt["remote_uri"])
    current_before = _tree_fingerprint(runtime)
    if current_before != receipt["runtime_fingerprint"]:
        raise UpdateError("backup receipt does not bind the exact current runtime tree")
    restored = _archive_restored_fingerprint(roundtrip_path)
    current_after = _tree_fingerprint(runtime)
    if current_after != current_before:
        raise UpdateError("runtime changed while the backup was being verified")
    if restored != receipt["runtime_fingerprint"]:
        raise UpdateError("backup archive restore does not reproduce the runtime fingerprint")
    return receipt


def _write_receipt(directory: Path, payload: dict[str, Any]) -> Path:
    _owned_directory(directory, "receipt directory")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    final = directory / f"{stamp}-{payload['target']}.json"
    fd, temporary = tempfile.mkstemp(prefix=".runtime-receipt-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
        _fsync_directory(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return final


def _ref_value(runtime: Path, ref: str) -> str | None:
    proc = _git(runtime, "rev-parse", "--verify", ref, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _bootstrap_state_path(transaction_dir: Path) -> Path:
    return transaction_dir / BOOTSTRAP_STATE


def _bootstrap_closed(runtime: Path, transaction_dir: Path) -> bool:
    state_path = _bootstrap_state_path(transaction_dir)
    if not _ref_exists(runtime, BOOTSTRAP_REF) or not state_path.exists():
        return False
    state = _load_json(state_path, "bootstrap closure")
    initial_target = str(state.get("initial_target", ""))
    ref_target = _ref_value(runtime, BOOTSTRAP_REF)
    return (
        state.get("schema") == 1
        and state.get("runtime") == str(runtime.resolve())
        and bool(EXACT_SHA_RE.fullmatch(initial_target))
        and ref_target == initial_target
    )


def _require_bootstrap_closed(runtime: Path, transaction_dir: Path) -> None:
    if not _bootstrap_closed(runtime, transaction_dir):
        raise UpdateError(
            "durable bootstrap closure is missing; normal apply and rollback are blocked"
        )


def _write_bootstrap_closure(runtime: Path, transaction_dir: Path, target: str) -> None:
    _git(runtime, "update-ref", BOOTSTRAP_REF, target)
    try:
        _atomic_json_write(
            _bootstrap_state_path(transaction_dir),
            {
                "schema": 1,
                "runtime": str(runtime.resolve()),
                "initial_target": target,
                "completed_at": dt.datetime
                .now(dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            "bootstrap closure",
        )
    except Exception:
        _git(runtime, "update-ref", "-d", BOOTSTRAP_REF, check=False)
        raise


def _remove_bootstrap_closure(runtime: Path, transaction_dir: Path) -> None:
    _git(runtime, "update-ref", "-d", BOOTSTRAP_REF, check=False)
    state_path = _bootstrap_state_path(transaction_dir)
    with contextlib.suppress(FileNotFoundError):
        state_path.unlink()
        _fsync_directory(state_path.parent)


def _transaction_path(transaction_dir: Path, transaction_id: str) -> Path:
    return transaction_dir / f"{transaction_id}.json"


def _persist_transaction(path: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = (
        dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    )
    _atomic_json_write(path, payload, "runtime transaction")


def _incomplete_transactions(
    transaction_dir: Path,
) -> list[tuple[Path, dict[str, Any]]]:
    if not transaction_dir.exists():
        return []
    _owned_directory(transaction_dir, "transaction directory")
    results: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(transaction_dir.glob("*.json")):
        if path.name == BOOTSTRAP_STATE:
            continue
        payload = _load_json(path, "runtime transaction")
        if payload.get("schema") != 1 or payload.get("transaction_id") != path.stem:
            raise UpdateError(f"runtime transaction identity is invalid: {path.name}")
        state = payload.get("state")
        if not isinstance(state, str) or state not in ALL_TRANSACTION_STATES:
            raise UpdateError(f"runtime transaction state is unknown: {path.name}")
        if state in INCOMPLETE_TRANSACTION_STATES:
            results.append((path, payload))
    return results


def _assert_no_incomplete(transaction_dir: Path) -> None:
    pending = _incomplete_transactions(transaction_dir)
    if pending:
        ids = ", ".join(
            str(payload.get("transaction_id", path.stem)) for path, payload in pending
        )
        raise UpdateError(f"incomplete runtime transaction requires recover: {ids}")


def _begin_transaction(
    args: argparse.Namespace,
    *,
    action: str,
    target: str,
    before_head: str,
    before_tree: dict[str, Any],
    venv_before: dict[str, Any],
    backup: dict[str, Any],
    clean_preview: list[str],
    initial: bool,
) -> tuple[Path, dict[str, Any]]:
    transaction_id = str(uuid.uuid4())
    path = _transaction_path(args.transaction_dir, transaction_id)
    previous_ref = _ref_value(args.runtime, "refs/hermes-runtime/previous")
    payload: dict[str, Any] = {
        "schema": 1,
        "transaction_id": transaction_id,
        "state": "prepared",
        "action": action,
        "runtime": str(args.runtime.resolve()),
        "before_head": before_head,
        "before_tree": before_tree,
        "before_venv": venv_before,
        "previous_ref": previous_ref,
        "target": target,
        "initial": initial,
        "clean_preview": clean_preview,
        "backup": {
            "archive_path": backup["archive_path"],
            "archive_sha256": backup["archive_sha256"],
            "remote_uri": backup["remote_uri"],
            "roundtrip_path": backup["roundtrip_path"],
            "roundtrip_sha256": backup["roundtrip_sha256"],
            # This tool performs no network I/O. The digests above prove the local
            # round-trip artifact is byte-identical to the archive; nothing here
            # establishes the object exists in R2. Emitted so a consumer of this
            # receipt cannot read byte-identity as remote durability.
            "remote_verified": False,
            "remote_verified_by": "backup producer attestation (not checked here)",
        },
        "created_at": dt.datetime
        .now(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    _persist_transaction(path, payload)
    return path, payload


def _restore_previous_ref(runtime: Path, payload: dict[str, Any]) -> None:
    previous = payload.get("previous_ref")
    if previous is None:
        _git(runtime, "update-ref", "-d", "refs/hermes-runtime/previous", check=False)
    elif EXACT_SHA_RE.fullmatch(str(previous)):
        _git(runtime, "update-ref", "refs/hermes-runtime/previous", str(previous))
    else:
        raise UpdateError("transaction previous_ref is invalid")



def _clean_runtime(runtime: Path) -> None:
    """Sweep untracked files, ALWAYS sparing the live virtualenv.

    Every mutating clean in this module goes through here. It exists because the
    venv exclusion was previously spelled out at each call site, which meant a test
    could only re-implement the invariant instead of exercising it — and a
    re-implementation passes even when the production line has lost the exclusion.

    The exclusion is load-bearing: callers run this AFTER `git reset --hard <target>`
    has replaced .gitignore with the target commit's copy. If that commit stopped
    listing venv/, the live interpreter for all 11 gateways is untracked-and-not-
    ignored and a bare `clean -fd` deletes it. Git cannot restore it; it was never
    tracked.
    """
    _git(runtime, "clean", "-fd", "-e", VENV_EXCLUDE)

def _restore_steady_transaction(runtime: Path, payload: dict[str, Any]) -> None:
    before_head = str(payload.get("before_head", ""))
    if not EXACT_SHA_RE.fullmatch(before_head):
        raise UpdateError("transaction before_head is invalid")
    _git(runtime, "reset", "--hard", "-q", before_head)
    # Same hazard on the recovery path: before_head's .gitignore may differ from the
    # tree's current one, and this clean runs while the fleet is already degraded.
    _clean_runtime(runtime)
    _restore_previous_ref(runtime, payload)
    if _tree_fingerprint(runtime) != payload.get("before_tree"):
        raise UpdateError(
            "automatic recovery did not restore the exact before-tree fingerprint"
        )
    if _venv_guard(runtime) != payload.get("before_venv"):
        raise UpdateError("automatic recovery did not restore the exact live venv")


def _abort_receipt(receipt_path: Path | None) -> None:
    if receipt_path is None or not receipt_path.exists():
        return
    aborted = receipt_path.with_suffix(receipt_path.suffix + ".aborted")
    os.replace(receipt_path, aborted)
    _fsync_directory(receipt_path.parent)


def _validate_update_receipt(path: Path) -> dict[str, Any]:
    receipt = _load_json(path, "update receipt")
    journal_value = receipt.get("transaction_journal")
    transaction_id = receipt.get("transaction_id")
    if not isinstance(journal_value, str) or not isinstance(transaction_id, str):
        raise UpdateError("update receipt has no durable transaction identity")
    journal = _load_json(Path(journal_value), "update transaction journal")
    if (
        journal.get("state") != "complete"
        or journal.get("transaction_id") != transaction_id
        or journal.get("receipt") != str(path)
    ):
        raise UpdateError("update receipt transaction is not durably complete")
    return receipt


def _maybe_inject_failure(args: argparse.Namespace, stage: str) -> None:
    if args.inject_failure_after != stage:
        return
    if (
        os.environ.get("HERMES_UPDATER_TESTING") != "1"
        or args.runtime.resolve() == DEFAULT_RUNTIME
    ):
        raise UpdateError("failure injection is restricted to non-production tests")
    raise UpdateError(f"injected failure after {stage}")


def _handle_failed_transaction(
    args: argparse.Namespace,
    transaction_path: Path,
    transaction: dict[str, Any],
    exc: Exception,
    receipt_path: Path | None,
) -> UpdateError:
    try:
        _abort_receipt(receipt_path)
    except Exception as abort_exc:
        transaction["receipt_abort_failure"] = _redact(str(abort_exc))
    transaction["failure"] = _redact(str(exc))
    if transaction["initial"]:
        _remove_bootstrap_closure(args.runtime, args.transaction_dir)
        transaction["state"] = "recovery_required"
        transaction["recovery"] = (
            "restore the verified archive with numeric owners, ACLs, and xattrs; "
            "then run recover to verify the exact before-tree fingerprint"
        )
        _persist_transaction(transaction_path, transaction)
        return UpdateError(
            f"initial reconciliation interrupted; explicit backup recovery required "
            f"for transaction {transaction['transaction_id']}: {_redact(str(exc))}"
        )
    try:
        _restore_steady_transaction(args.runtime, transaction)
    except Exception as recovery_exc:
        transaction["state"] = "recovery_required"
        transaction["recovery_failure"] = _redact(str(recovery_exc))
        _persist_transaction(transaction_path, transaction)
        return UpdateError(
            f"source transaction failed and automatic recovery failed; run recover "
            f"for {transaction['transaction_id']}"
        )
    transaction["state"] = "recovered_after_failure"
    _persist_transaction(transaction_path, transaction)
    return UpdateError(
        f"source transaction failed and the exact before state was restored: {_redact(str(exc))}"
    )


def _recover(args: argparse.Namespace) -> int:
    _runtime_safety(args.runtime, require_git=True)
    if getattr(args, "dry_run", False):
        # `recover` is the verb an operator reaches for MID-INCIDENT, on a live tree,
        # and --dry-run is what a careful one types first. It previously fell straight
        # through to a real reset --hard + clean -fd, rolled the runtime back
        # unannounced, and CONSUMED the transaction — reporting success. Every other
        # mutating verb honoured --dry-run; this one did not, and it is the one where
        # being wrong costs the most.
        pending_preview = _incomplete_transactions(args.transaction_dir)
        if args.transaction_id is not None:
            pending_preview = [
                item for item in pending_preview
                if item[1].get("transaction_id") == args.transaction_id
            ]
        print(
            _canonical_json({
                "action": "recover",
                "dry_run": True,
                "state": "preview",
                "runtime": str(args.runtime),
                "would_recover": [
                    item[1].get("transaction_id") for item in pending_preview
                ],
                "restart_performed": False,
            })
        )
        return 0
    pending = _incomplete_transactions(args.transaction_dir)
    if args.transaction_id is not None:
        pending = [
            item
            for item in pending
            if item[1].get("transaction_id") == args.transaction_id
        ]
    if not pending:
        raise UpdateError("no matching incomplete runtime transaction")
    if len(pending) != 1:
        raise UpdateError(
            "multiple incomplete transactions exist; select --transaction-id"
        )
    path, transaction = pending[0]
    if transaction.get("runtime") != str(args.runtime.resolve()):
        raise UpdateError("transaction runtime does not match --runtime")
    if transaction.get("initial"):
        _remove_bootstrap_closure(args.runtime, args.transaction_dir)
        if _tree_fingerprint(args.runtime) != transaction.get("before_tree"):
            raise UpdateError(
                "initial before-tree is not restored; restore the transaction backup and rerun recover"
            )
        if _venv_guard(args.runtime) != transaction.get("before_venv"):
            raise UpdateError(
                "restored initial tree does not contain the exact pre-a2 venv"
            )
        transaction["state"] = "recovered_external"
    else:
        _restore_steady_transaction(args.runtime, transaction)
        transaction["state"] = "recovered"
    _persist_transaction(path, transaction)
    print(
        _canonical_json({
            "action": "recover",
            "transaction_id": transaction["transaction_id"],
            "state": transaction["state"],
            "restart_performed": False,
        }),
        end="",
    )
    return 0


def _init(args: argparse.Namespace, target: str) -> int:
    runtime = args.runtime
    _runtime_safety(runtime, require_git=False)
    if not args.remote_url:
        raise UpdateError("init requires --remote-url")
    approved_remote = _validate_remote_url(args.remote_url)
    _validate_backup_receipt(args.backup_receipt, runtime)
    _assert_no_incomplete(args.transaction_dir)
    before = _tree_fingerprint(runtime)
    if args.dry_run:
        print(
            _canonical_json({
                "action": "init",
                "dry_run": True,
                "runtime": str(runtime),
                "target": target,
            })
        )
        return 0

    if not (runtime / ".git").exists():
        _git(runtime, "init", "-q", "-b", "main")
        _git(runtime, "remote", "add", "origin", approved_remote)
    else:
        _runtime_safety(runtime, require_git=True)
        if (
            _ref_exists(runtime, BOOTSTRAP_REF)
            or _bootstrap_state_path(args.transaction_dir).exists()
        ):
            raise UpdateError(
                "runtime bootstrap is already complete; init cannot be reused"
            )
        if _remote_url(runtime) != approved_remote:
            raise UpdateError("existing origin URL does not match --remote-url")
    _fetch_and_verify(runtime, target, approved_remote)
    # Seed HEAD + index without changing a worktree file.  This makes `clean -nd`
    # enumerate only true untracked/orphan paths instead of the whole live tree.
    _git(runtime, "reset", "--mixed", "-q", target)
    after = _tree_fingerprint(runtime)
    if before != after:
        raise UpdateError(
            "init changed a non-.git runtime path; stop and restore the backup"
        )
    print(
        _canonical_json({
            "action": "init",
            "runtime": str(runtime),
            "target": target,
            "worktree_unchanged": True,
        })
    )
    return 0


def _audit(args: argparse.Namespace, target: str, *, status: bool) -> int:
    _runtime_safety(args.runtime, require_git=True)
    _verify_target(args.runtime, target)
    audit = _build_audit(args.runtime, target)
    if status:
        audit["incomplete_transactions"] = [
            str(payload.get("transaction_id", path.stem))
            for path, payload in _incomplete_transactions(args.transaction_dir)
        ]
        audit["bootstrap_closure_valid"] = _bootstrap_closed(
            args.runtime, args.transaction_dir
        )
        audit["ready"] = _ready(audit)
    print(_canonical_json(audit), end="")
    return 0 if not status or audit["ready"] else DRIFT_EXIT


def _steady_preflight(runtime: Path) -> None:
    current = _head(runtime)
    if current is None:
        raise UpdateError("runtime has no current HEAD")
    current_audit = _build_audit(runtime, current)
    if not _ready(current_audit):
        raise UpdateError(
            "runtime is dirty, untracked, or provenance-divergent from current HEAD"
        )


def _apply(
    args: argparse.Namespace,
    target: str,
    *,
    rollback_from: dict[str, Any] | None = None,
) -> int:
    runtime = args.runtime
    _runtime_safety(runtime, require_git=True)
    _assert_no_incomplete(args.transaction_dir)
    backup = _validate_backup_receipt(args.backup_receipt, runtime)
    if args.fetch:
        _fetch_and_verify(runtime, target, args.remote_url)
    else:
        _verify_target(runtime, target)
        if args.remote_url is not None and _remote_url(runtime) != args.remote_url:
            raise UpdateError("origin URL does not match --remote-url")

    before_head = _head(runtime)
    if before_head is None:
        raise UpdateError("runtime has no current HEAD")

    if rollback_from is not None:
        _require_bootstrap_closed(runtime, args.transaction_dir)
        if rollback_from.get("target") != before_head:
            raise UpdateError(
                "rollback receipt target is not the runtime's current HEAD"
            )
        if rollback_from.get("before_head") != target:
            raise UpdateError("rollback target does not match receipt before_head")
        if target == before_head:
            raise UpdateError("rollback receipt has no distinct previous commit")
        _steady_preflight(runtime)
    elif args.initial_evidence is not None:
        if (
            _ref_exists(runtime, BOOTSTRAP_REF)
            or _bootstrap_state_path(args.transaction_dir).exists()
        ):
            raise UpdateError(
                "initial-evidence mode is permanently closed after bootstrap"
            )
        expected = _load_json(args.initial_evidence, "initial evidence")
        actual = _build_audit(runtime, target)
        if _canonical_json(actual) != _canonical_json(expected):
            raise UpdateError(
                "initial evidence does not exactly match the current runtime audit"
            )
        if actual["head"] != target:
            raise UpdateError(
                "initial evidence was not captured after init/reset --mixed"
            )
        if _ready(actual):
            raise UpdateError(
                "initial evidence describes an already-clean runtime; use steady apply"
            )
    else:
        _require_bootstrap_closed(runtime, args.transaction_dir)
        _steady_preflight(runtime)
        forward = _git(
            runtime,
            "merge-base",
            "--is-ancestor",
            before_head,
            target,
            check=False,
        )
        if forward.returncode != 0:
            raise UpdateError(
                "normal apply would move HEAD backward or sideways; use rollback with a successful update receipt"
            )

    before_tree = _tree_fingerprint(runtime)
    venv_before = _venv_guard(runtime)
    clean_preview = _clean_preview(runtime)
    plan = {
        "action": "rollback" if rollback_from is not None else "apply",
        "runtime": str(runtime),
        "before_head": before_head,
        "target": target,
        "clean_preview": clean_preview,
        "backup_receipt": str(args.backup_receipt),
        "restart_performed": False,
    }
    if args.dry_run:
        plan["dry_run"] = True
        print(_canonical_json(plan), end="")
        return 0

    transaction_path, transaction = _begin_transaction(
        args,
        action=str(plan["action"]),
        target=target,
        before_head=before_head,
        before_tree=before_tree,
        venv_before=venv_before,
        backup=backup,
        clean_preview=clean_preview,
        initial=args.initial_evidence is not None,
    )
    receipt_path: Path | None = None
    try:
        _git(runtime, "update-ref", "refs/hermes-runtime/previous", before_head)
        transaction["state"] = "resetting"
        _persist_transaction(transaction_path, transaction)
        _git(runtime, "reset", "--hard", "-q", target)
        transaction["state"] = "reset_done"
        _persist_transaction(transaction_path, transaction)
        _maybe_inject_failure(args, "reset")

        transaction["state"] = "cleaning"
        _persist_transaction(transaction_path, transaction)
        # Fixed, intentional command: preserves ignored venv. Never add -x.
        # -e /venv is LOAD-BEARING, not defensive. The venv guard above ran against
        # the .gitignore that `reset --hard` on the previous line has just REPLACED
        # with the target commit's. If the target stops listing venv/ — an ordinary
        # tidy-up commit — the live interpreter for all 11 gateways becomes
        # untracked-and-not-ignored and this clean deletes it. No -x required, and
        # git cannot restore it because it was never tracked. Excluding it here makes
        # the invariant independent of what any target commit happens to ignore.
        _clean_runtime(runtime)
        transaction["state"] = "clean_done"
        _persist_transaction(transaction_path, transaction)
        _maybe_inject_failure(args, "clean")

        venv_after = _venv_guard(runtime)
        if venv_after != venv_before:
            raise UpdateError("live venv changed during source advance")
        after_audit = _build_audit(runtime, target)
        if not _ready(after_audit):
            raise UpdateError("post-advance runtime is not clean and exact at target")
        transaction["state"] = "verified"
        transaction["after_tree"] = after_audit["tree_fingerprint"]
        _persist_transaction(transaction_path, transaction)
        _maybe_inject_failure(args, "post-audit")

        if args.initial_evidence is not None:
            transaction["state"] = "bootstrap_marking"
            _persist_transaction(transaction_path, transaction)
            _write_bootstrap_closure(runtime, args.transaction_dir, target)
            transaction["state"] = "bootstrap_marked"
            _persist_transaction(transaction_path, transaction)
            _maybe_inject_failure(args, "bootstrap-ref")

        completed_at = (
            dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        )
        receipt_payload = {
            **plan,
            "transaction_id": transaction["transaction_id"],
            "transaction_journal": str(transaction_path),
            "completed_at": completed_at,
            "backup": {
                "archive_sha256": backup["archive_sha256"],
                "remote_uri": backup["remote_uri"],
                "roundtrip_sha256": backup["roundtrip_sha256"],
                "remote_verified": False,
                "remote_verified_by": "backup producer attestation (not checked here)",
            },
            "post_tree_fingerprint": after_audit["tree_fingerprint"],
            "post_provenance_counts": after_audit["provenance"]["counts"],
        }
        transaction["state"] = "receipting"
        _persist_transaction(transaction_path, transaction)
        receipt_path = _write_receipt(args.receipt_dir, receipt_payload)
        transaction["receipt"] = str(receipt_path)
        _maybe_inject_failure(args, "receipt")
        transaction["state"] = "complete"
        _persist_transaction(transaction_path, transaction)
    except Exception as exc:
        raise _handle_failed_transaction(
            args, transaction_path, transaction, exc, receipt_path
        ) from exc

    receipt_payload["receipt"] = str(receipt_path)
    receipt_payload["next_gate"] = (
        "record CLAWD-3486 disposition, then restart one canary profile"
    )
    print(_canonical_json(receipt_payload), end="")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("init", "audit", "status", "apply", "rollback", "recover")
    )
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    target_group = parser.add_mutually_exclusive_group(required=False)
    target_group.add_argument("--target")
    target_group.add_argument("--target-file", type=Path)
    parser.add_argument(
        "--remote-url", default=os.environ.get("HERMES_OPT_RUNTIME_REMOTE_URL")
    )
    parser.add_argument(
        "--fetch", action="store_true", help="fetch origin before apply/rollback"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--backup-receipt", type=Path)
    parser.add_argument("--initial-evidence", type=Path)
    parser.add_argument(
        "--update-receipt", type=Path, help="successful apply receipt to roll back"
    )
    parser.add_argument("--receipt-dir", type=Path, default=DEFAULT_RECEIPT_DIR)
    parser.add_argument("--transaction-dir", type=Path, default=DEFAULT_TRANSACTION_DIR)
    parser.add_argument("--transaction-id")
    parser.add_argument(
        "--inject-failure-after",
        dest="inject_failure_after",
        choices=("reset", "clean", "post-audit", "bootstrap-ref", "receipt"),
        help=argparse.SUPPRESS,
    )
    return parser



class _Interrupted(UpdateError):
    """SIGTERM/SIGINT arrived. Raised so the normal failure path runs."""


def _install_signal_handlers() -> None:
    """Turn a systemd stop into an exception, not a corpse.

    `import signal` was present and unused. Without a handler, systemd's
    TimeoutStartSec expiry SIGKILLs this process outright: no except block, no
    _handle_failed_transaction, no rollback — and KillMode=control-group takes the
    running `git clean` down with it, leaving a half-swept live runtime that every
    recovery verb then refuses. Reproduced by SIGKILL mid-clean: 11,400 of 20,000
    orphans removed, journal stuck at `cleaning`, both recover and apply refusing.

    Raising instead means the existing transaction machinery unwinds and records a
    recoverable state. SIGKILL still cannot be caught — that is why the unit also
    sets TimeoutStopSec, so the handler gets a window before escalation.
    """

    def _raise(signum: int, _frame: Any) -> None:
        raise _Interrupted(f"interrupted by signal {signum}; transaction unwound")

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _raise)
        except (ValueError, OSError):
            # Not the main thread, or a platform without it. Never fatal.
            pass

def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _install_signal_handlers()
    try:
        with _exclusive_lock(args.lock_file):
            if args.command == "recover":
                return _recover(args)
            if args.command == "rollback":
                if args.update_receipt is None:
                    raise UpdateError("rollback requires --update-receipt")
                rollback_from = _validate_update_receipt(args.update_receipt)
                target = str(rollback_from.get("before_head", ""))
                if not EXACT_SHA_RE.fullmatch(target):
                    raise UpdateError(
                        "update receipt before_head is not an exact commit"
                    )
                if args.target or args.target_file:
                    requested = _target_value(args.target, args.target_file)
                    if requested != target:
                        raise UpdateError(
                            "explicit rollback target disagrees with update receipt"
                        )
                if args.backup_receipt is None:
                    raise UpdateError("rollback requires --backup-receipt")
                return _apply(args, target, rollback_from=rollback_from)

            target = _target_value(args.target, args.target_file)
            if args.command == "init":
                if args.backup_receipt is None:
                    raise UpdateError("init requires --backup-receipt")
                return _init(args, target)
            if args.command == "audit":
                return _audit(args, target, status=False)
            if args.command == "status":
                return _audit(args, target, status=True)
            if args.backup_receipt is None:
                raise UpdateError("apply requires --backup-receipt")
            return _apply(args, target)
    except UpdateError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return UNMEASURED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
