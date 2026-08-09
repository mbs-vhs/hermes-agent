"""CLAWD-3507 — a source-only advance must notice the venv cannot run the new code.

THE GAP. This tool advances SOURCE ONLY and deliberately preserves the venv — the
venv is the interpreter all 11 gateways execute, so not touching it is correct. But
it means new code runs against the OLD environment, and the module had ZERO
dependency awareness: `grep -ci 'pyproject|dependenc|pip|requirement'` returned 0.

Measured on the live fleet: `/opt/hermes-agent/pyproject.toml` pins
`nemo-relay==0.3` as an OPTIONAL EXTRA, while fork `origin/main` declares
`nemo-relay>=0.6.0` as a MAIN dependency. Advancing source alone therefore produces
an import-time failure across every gateway, at the moment the tool reports success.

The check is local — it asks the runtime's own interpreter what it has — and
degrades rather than blocks when `packaging` is unavailable, because a missing
helper library must not make every apply impossible.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SUBJECT = REPO / "scripts" / "update_opt_hermes_runtime.py"


def _subject():
    import importlib.util

    spec = importlib.util.spec_from_file_location("subject", SUBJECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *a], check=True,
                          capture_output=True, text=True).stdout


def _runtime_with(
    tmp_path: Path,
    deps: list[str],
    installed: dict[str, str] | None = None,
    with_packaging: bool = True,
) -> tuple[Path, str]:
    """A runtime whose TARGET commit declares `deps` as main dependencies."""
    rt = tmp_path / "rt"
    rt.mkdir()
    _git(rt.parent, "init", "-q", str(rt)) if False else None
    subprocess.run(["git", "init", "-q", "-b", "main", str(rt)], check=True)
    (rt / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0'\ndependencies = "
        + json.dumps(deps)
        + "\n"
    )
    _git(rt, "add", "-A")
    _git(rt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "t")
    (rt / "venv" / "bin").mkdir(parents=True)
    (rt / "venv" / "bin" / "python").symlink_to(sys.executable)
    # A bare symlink is NOT a venv: python derives sys.prefix from the symlink's own
    # location, so `<rt>/venv` becomes the prefix and there is no site-packages behind
    # it. The old probe only appeared to work here because it inherited the caller's
    # environment. The probe now runs under a sanitised env — deliberately, because a
    # leaked PYTHONPATH could report a dependency SATISFIED that the gateway, running
    # under a clean systemd environment, will not find; that is a false negative on the
    # one check whose whole purpose is preventing a fleet-wide import failure.
    #
    # So the fixture gains what makes a real venv real. This models production (the
    # live /opt venv has a pyvenv.cfg); it does not weaken the assertion.
    (rt / "venv" / "pyvenv.cfg").write_text(
        f"home = {Path(sys.base_prefix) / 'bin'}\n"
        f"include-system-site-packages = false\n"
        f"version = {sys.version.split()[0]}\n"
    )
    # `installed` makes a distribution genuinely PRESENT to the probe. The probe asks
    # importlib.metadata.version(name), which reads dist-info off sys.path — so writing
    # real dist-info is the faithful way to model an installed package, with no network
    # and no pip. Pointing the fixture at the caller's site-packages would NOT model
    # production: the live /opt venv is self-contained, and the gateway that must import
    # these packages runs under a clean systemd environment.
    site = rt / "venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for name, ver in (installed or {}).items():
        info = site / f"{name.replace('-', '_')}-{ver}.dist-info"
        info.mkdir(exist_ok=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n")
        (info / "RECORD").write_text("")
    if with_packaging:
        # The live /opt venv has packaging 26.0 (measured), so the version-specifier arm
        # IS reachable in production and must be tested. Symlinked rather than copied:
        # this only needs to be importable.
        import packaging as _pkg

        src = Path(_pkg.__file__).parent
        (site / "packaging").symlink_to(src, target_is_directory=True)
    return rt, _git(rt, "rev-parse", "HEAD").strip()


def test_a_missing_main_dependency_is_reported(tmp_path: Path):
    """The nemo-relay case: declared by the target, absent from the venv."""
    rt, head = _runtime_with(tmp_path, ["definitely-not-a-real-package-xyz>=1.0"])
    skew = _subject()._dependency_skew(rt, head)
    assert skew, "a dependency the venv does not have was not reported"
    assert "definitely-not-a-real-package-xyz" in skew[0]
    assert "NOT INSTALLED" in skew[0]


def test_a_satisfied_dependency_is_not_reported(tmp_path: Path):
    """Negative control: no false positives on something actually installed.

    The package is installed INTO THE FIXTURE'S OWN VENV rather than borrowed from the
    caller's. Previously this passed only because the probe inherited the test runner's
    environment; the probe now runs sanitised, so a package the fixture venv does not
    actually have is correctly reported missing. Modelling it properly is the point —
    the gateway imports under a clean systemd environment too.
    """
    rt, head = _runtime_with(tmp_path, ["pytest"], installed={"pytest": "9.0.2"})
    assert _subject()._dependency_skew(rt, head) == []


def test_a_dependency_present_but_TOO_OLD_is_reported(tmp_path: Path):
    """The version-specifier arm, distinct from the absent arm.

    This is the `cryptography 46.0.7 -> 48.0.1` shape measured on the real fleet: the
    package IS installed, so an existence-only check passes it, and the gateway still
    imports the wrong version.
    """
    rt, head = _runtime_with(
        tmp_path, ["cryptography==48.0.1"], installed={"cryptography": "46.0.7"}
    )
    skew = _subject()._dependency_skew(rt, head)
    assert len(skew) == 1, skew
    assert "cryptography" in skew[0] and "46.0.7" in skew[0]


def test_no_declared_dependencies_is_not_skew(tmp_path: Path):
    rt, head = _runtime_with(tmp_path, [])
    assert _subject()._dependency_skew(rt, head) == []


def test_the_target_pyproject_is_read_from_git_not_the_worktree(tmp_path: Path):
    """At check time the worktree is still at the OLD commit.

    Reading the worktree would consult exactly the pyproject whose staleness is the
    problem. This asserts the target commit's content is what is used.
    """
    rt, head = _runtime_with(tmp_path, ["definitely-not-a-real-package-xyz>=1.0"])
    # Make the WORKTREE claim no dependencies at all.
    (rt / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\ndependencies = []\n")
    skew = _subject()._dependency_skew(rt, head)
    assert skew, "skew was read from the worktree, not from the target commit"


def test_the_override_flag_is_absent_from_the_parser(tmp_path: Path):
    """The override is gone, asserted against the PARSER.

    RENAMED after review: this was called `..._dry_run_PRINTS_the_skew_and_then_refuses`
    and its docstring claimed "behaviour, not a grep" — but it never invokes apply, never
    passes --dry-run and never builds a plan. That behaviour IS covered, by
    `test_PIN_dry_run_plan_carries_the_real_dependency_skew` in the adversarial file,
    which review confirmed goes red when the dry-run refusal is deleted. This test does
    one honest thing: pin the flag's absence.

    This replaces a signature test that asserted `'"dependency_skew": skew' in source`
    and `"--accept-dependency-skew" in source`. Two problems with that: the file's own
    header forbids signature tests ("that is a signature, and a signature is not a
    property"), and a source grep passes whether or not the field is ever reached.

    THE FLAG IS DELIBERATELY GONE. The old contract let an operator wave skew through.
    This branch shipped two advertised-but-inert `--accept-*` flags already, and the
    honest remedy for skew is to resync the venv, not to override the finding on the
    way to breaking eleven gateways. Asserted here so the removal is a decision on the
    record rather than an omission.
    """
    mod = _subject()
    # REMOVED: `assert not hasattr(mod, "_accept_dependency_skew")`. Independent review
    # showed `git log --all -S` finds that attribute in no revision — the flag was always
    # `--accept-dependency-skew` / `args.accept_dependency_skew`, so the assertion was
    # vacuously true forever, including if the override came back. The parser assertion
    # below is the one that can actually fail.
    parser_choices = mod._parser()
    opts = {s for a in parser_choices._actions for s in a.option_strings}
    assert "--accept-dependency-skew" not in opts, (
        "an --accept-dependency-skew flag is advertised again; if it is genuinely "
        "wanted it needs wiring AND a test that it changes behaviour"
    )


def test_an_unmeasurable_target_RAISES_rather_than_returning_empty(tmp_path: Path):
    """`[]` must mean 'asked and clean', never 'could not ask'.

    RENAMED: this was `test_the_plan_carries_a_MEASURED_skew_value`, which over-claimed —
    it never builds a plan. The plan field is pinned by the adversarial dry-run test.
    What this actually asserts is the measured-vs-unmeasurable split at the source.
    """
    rt, head = _runtime_with(tmp_path, ["pytest"], installed={"pytest": "9.0.2"})
    mod = _subject()
    assert mod._dependency_skew(rt, head) == []
    # And the unmeasurable case raises rather than yielding the same [].
    subprocess.run(["git", "-C", str(rt), "update-ref", "refs/heads/broken", head], check=True)
    try:
        mod._dependency_skew(rt, "0" * 40)
    except mod.UpdateError:
        pass
    else:
        raise AssertionError("an unresolvable target returned a value instead of raising")


# REMOVED: test_the_importable_orphan_list_stays_in_the_plan. It grepped the subject's
# source for '"importable_orphans_to_remove": importable_orphans' — a signature test, in
# a file whose header forbids them, and (as review noted) MORE brittle than the string it
# replaced. The property is covered behaviourally by
# test_PIN_the_conversion_plan_names_the_importable_orphans_it_will_delete, which builds
# a real plan and reads the field out of real JSON.


def test_an_UNPARSEABLE_requirement_is_reported_not_silently_dropped(tmp_path: Path):
    """B2 from independent review: `except Exception: continue` returned [].

    The docstring claimed "every path either measures or raises". This one did neither
    — it dropped the requirement, and `[]` is then written into the plan under a comment
    asserting it means "asked, and the venv satisfies the target". Same value, two
    meanings: the exact CLAWD-3655 shape this work exists to remove.

    It now surfaces as a finding, so the apply refuses rather than proceeding on a
    requirement nobody could evaluate.
    """
    rt, head = _runtime_with(tmp_path, ["definitely-not-real-xyz >= = 1.0"])
    skew = _subject()._dependency_skew(rt, head)
    assert skew, "an unparseable requirement vanished into a 'measured clean' []"
    assert "unparseable requirement" in skew[0], skew


# test_ROLLBACK_is_never_refused_for_dependency_skew moved to the adversarial file as an
# end-to-end test. It was pinned here by grepping `_apply`'s source, which is a signature
# test for the severest finding in this branch — not good enough.


def test_WITHOUT_packaging_the_probe_refuses_instead_of_silently_passing(tmp_path: Path):
    """B3 from independent review, and the reason `with_packaging` existed.

    That knob was added to both fixtures and never once passed False — a dead knob is
    not coverage, which is how the fail-open below survived being written down as a
    benign degradation.

    Measured by review: without `packaging` the fallback stops evaluating version
    specifiers, so `cryptography==48.0.1` against an installed 46.0.7 came back CLEAN —
    the exact case this check exists for. It now refuses instead.
    """
    rt, head = _runtime_with(
        tmp_path, ["cryptography==48.0.1"],
        installed={"cryptography": "46.0.7"}, with_packaging=False,
    )
    skew = _subject()._dependency_skew(rt, head)
    assert skew, "a version specifier went unevaluated and reported CLEAN"
    assert "packaging is not installed" in skew[0], skew


def test_WITH_packaging_the_same_case_reports_the_real_version_skew(tmp_path: Path):
    """Control for the above: the normal production path names the actual mismatch."""
    rt, head = _runtime_with(
        tmp_path, ["cryptography==48.0.1"],
        installed={"cryptography": "46.0.7"}, with_packaging=True,
    )
    skew = _subject()._dependency_skew(rt, head)
    assert len(skew) == 1 and "46.0.7" in skew[0], skew


def test_a_pyproject_whose_project_is_not_a_table_RAISES_cleanly(tmp_path: Path):
    """Was an unhandled AttributeError: raw traceback, exit 1, not UNMEASURED_EXIT(3)."""
    rt, _ = _runtime_with(tmp_path, [])
    # Root-level, not under [tool]: an earlier draft nested it and the guard never fired.
    (rt / "pyproject.toml").write_text("project = 'not-a-table'\n")
    _git(rt, "add", "-A")
    _git(rt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bad")
    bad = _git(rt, "rev-parse", "HEAD").strip()
    mod = _subject()
    try:
        mod._target_main_dependencies(rt, bad)
    except mod.UpdateError as exc:
        assert "not a table" in str(exc)
    else:
        raise AssertionError("a non-table [project] did not raise UpdateError")


def test_non_string_dependency_entries_REFUSE_rather_than_being_dropped(tmp_path: Path):
    """Silently dropping them under-counts the very thing being measured."""
    rt, _ = _runtime_with(tmp_path, [])
    (rt / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='0'\ndependencies = ['ok-pkg', 123]\n"
    )
    _git(rt, "add", "-A")
    _git(rt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "mixed")
    head = _git(rt, "rev-parse", "HEAD").strip()
    mod = _subject()
    try:
        mod._target_main_dependencies(rt, head)
    except mod.UpdateError as exc:
        assert "non-string" in str(exc)
    else:
        raise AssertionError("a non-string dependency entry was silently dropped")
