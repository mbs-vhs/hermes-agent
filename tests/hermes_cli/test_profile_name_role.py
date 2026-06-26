"""Tests for the canonical persona ``display_name`` + ``role`` fields on the
profile.yaml metadata layer and the roster-sync script (CLAWD-1828 P3).

Covers:
  * read_profile_meta returns display_name/role None on missing file and on a
    legacy yaml without those keys.
  * write_profile_meta sets them (and strips whitespace).
  * Partial writes preserve the other fields in BOTH directions
    (describe-only must not clobber name/role, and name/role-only must not
    clobber description).
  * ProfileInfo carries the new fields; list_profiles surfaces them for the
    default profile AND for named profiles (the 10-profile mesh is all named).
  * scripts/sync_roster_to_profiles.py: _row_role precedence, fetch_roster
    payload-shape handling (urlopen mocked — no live GET), and the
    main() dry-run row -> (agent_id, display_name, role) mapping (no --apply).

Profile safety: every test operates on tempfile dirs only. The sync script is
exercised in DRY-RUN mode with a faked roster — no live GET, no --apply write
to ~/.hermes/profiles/.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hermes_cli import profiles as profiles_mod
from hermes_cli.profiles import ProfileInfo, list_profiles


# scripts/ is not an importable package — load the sync script by path, the
# same way tests/test_evidence_store.py loads a hyphenated script module.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYNC_PATH = _REPO_ROOT / "scripts" / "sync_roster_to_profiles.py"
_spec = importlib.util.spec_from_file_location("sync_roster_to_profiles", str(_SYNC_PATH))
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def profile_dir(tmp_path):
    """A bare, existing profile directory (write_profile_meta requires it)."""
    d = tmp_path / "prof"
    d.mkdir()
    return d


@pytest.fixture
def mesh_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a default profile + named-profiles root.

    Mirrors tests/hermes_cli/test_profiles.py::profile_env so list_profiles()
    resolves entirely inside tmp_path (never the live ~/.hermes).
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


# ---------------------------------------------------------------------------
# read_profile_meta — display_name/role default to None
# ---------------------------------------------------------------------------

def test_read_meta_missing_file_returns_none_name_role(profile_dir):
    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["display_name"] is None
    assert meta["role"] is None
    # Backward-compat keys still present and at their defaults.
    assert meta["description"] == ""
    assert meta["description_auto"] is False


def test_read_meta_legacy_yaml_without_name_role_keys(profile_dir):
    # A pre-CLAWD-1828 profile.yaml with only a description must read back
    # display_name/role as None, not raise / KeyError.
    (profile_dir / "profile.yaml").write_text("description: a researcher\n")
    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["description"] == "a researcher"
    assert meta["display_name"] is None
    assert meta["role"] is None


def test_read_meta_empty_string_name_role_coerced_to_none(profile_dir):
    # Blank/whitespace values are normalised to None (not "" ) so consumers can
    # fall back to the profile id cleanly.
    (profile_dir / "profile.yaml").write_text("display_name: ''\nrole: '   '\n")
    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["display_name"] is None
    assert meta["role"] is None


# ---------------------------------------------------------------------------
# write_profile_meta — sets display_name/role
# ---------------------------------------------------------------------------

def test_write_sets_display_name_and_role(profile_dir):
    profiles_mod.write_profile_meta(
        profile_dir, display_name="Quasimodo", role="Engineer"
    )
    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["display_name"] == "Quasimodo"
    assert meta["role"] == "Engineer"


def test_write_strips_whitespace(profile_dir):
    profiles_mod.write_profile_meta(
        profile_dir, display_name="  Quasimodo  ", role="  Engineer  "
    )
    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["display_name"] == "Quasimodo"
    assert meta["role"] == "Engineer"


# ---------------------------------------------------------------------------
# Partial-write preservation (both directions)
# ---------------------------------------------------------------------------

def test_partial_write_description_preserves_name_role(profile_dir):
    profiles_mod.write_profile_meta(
        profile_dir, display_name="Quasimodo", role="Engineer"
    )
    # A later describe-only write must NOT clobber the synced name/role.
    profiles_mod.write_profile_meta(profile_dir, description="writes code")
    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["description"] == "writes code"
    assert meta["display_name"] == "Quasimodo"
    assert meta["role"] == "Engineer"


def test_partial_write_name_role_preserves_description(profile_dir):
    profiles_mod.write_profile_meta(
        profile_dir, description="writes code", description_auto=True
    )
    # A later sync (name/role only) must NOT clobber the existing description.
    profiles_mod.write_profile_meta(
        profile_dir, display_name="Quasimodo", role="Engineer"
    )
    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["description"] == "writes code"
    assert meta["description_auto"] is True
    assert meta["display_name"] == "Quasimodo"
    assert meta["role"] == "Engineer"


def test_write_role_only_preserves_existing_display_name(profile_dir):
    profiles_mod.write_profile_meta(profile_dir, display_name="Quasimodo")
    profiles_mod.write_profile_meta(profile_dir, role="Engineer")
    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["display_name"] == "Quasimodo"
    assert meta["role"] == "Engineer"


# ---------------------------------------------------------------------------
# ProfileInfo carries the new fields
# ---------------------------------------------------------------------------

def test_profileinfo_name_role_default_none():
    info = ProfileInfo(
        name="x", path=Path("/tmp/x"), is_default=False, gateway_running=False
    )
    assert info.display_name is None
    assert info.role is None


def test_profileinfo_accepts_name_role():
    info = ProfileInfo(
        name="engineer",
        path=Path("/tmp/x"),
        is_default=False,
        gateway_running=False,
        display_name="Quasimodo",
        role="Engineer",
    )
    assert info.display_name == "Quasimodo"
    assert info.role == "Engineer"


# ---------------------------------------------------------------------------
# list_profiles surfaces display_name/role from profile.yaml
# ---------------------------------------------------------------------------

def test_list_profiles_default_surfaces_name_role(mesh_env):
    default_home = mesh_env / ".hermes"
    profiles_mod.write_profile_meta(
        default_home, display_name="Minerva", role="Chief of Staff"
    )
    infos = {p.name: p for p in list_profiles()}
    assert infos["default"].display_name == "Minerva"
    assert infos["default"].role == "Chief of Staff"


def test_list_profiles_named_surfaces_name_role(mesh_env):
    # The live 10-profile mesh (engineer, finance, legal, ...) is ALL named
    # profiles, never the bare default. Their profile.yaml display_name/role
    # MUST surface onto ProfileInfo so chat.vhs.box can render
    # "Quasimodo — Engineer" instead of the bare profile id.
    eng = mesh_env / ".hermes" / "profiles" / "engineer"
    eng.mkdir(parents=True)
    profiles_mod.write_profile_meta(eng, display_name="Quasimodo", role="Engineer")

    infos = {p.name: p for p in list_profiles()}
    assert "engineer" in infos
    assert infos["engineer"].display_name == "Quasimodo"
    assert infos["engineer"].role == "Engineer"


# ---------------------------------------------------------------------------
# sync_roster_to_profiles._row_role
# ---------------------------------------------------------------------------

class TestRowRole:
    def test_prefers_role(self):
        assert sync._row_role({"role": "Engineer", "role_summary": "x"}) == "Engineer"

    def test_falls_back_to_role_summary(self):
        assert sync._row_role({"role_summary": "Engineer"}) == "Engineer"

    def test_falls_back_to_role_label(self):
        assert sync._row_role({"role_label": "Engineer"}) == "Engineer"

    def test_none_when_absent(self):
        assert sync._row_role({"display_name": "Quasimodo"}) is None

    def test_strips_whitespace(self):
        assert sync._row_role({"role": "  Engineer  "}) == "Engineer"

    def test_empty_role_falls_through_to_summary(self):
        assert sync._row_role({"role": "", "role_summary": "Engineer"}) == "Engineer"


# ---------------------------------------------------------------------------
# sync_roster_to_profiles.fetch_roster — payload-shape handling (urlopen mocked)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, payload: bytes):
    monkeypatch.setattr(
        sync.urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload)
    )


class TestFetchRoster:
    def test_dict_with_agents_key(self, monkeypatch):
        _patch_urlopen(monkeypatch, b'{"agents": [{"agent_id": "engineer"}]}')
        assert sync.fetch_roster("http://x") == [{"agent_id": "engineer"}]

    def test_bare_list(self, monkeypatch):
        _patch_urlopen(monkeypatch, b'[{"agent_id": "finance"}]')
        assert sync.fetch_roster("http://x") == [{"agent_id": "finance"}]

    def test_unexpected_shape_raises(self, monkeypatch):
        _patch_urlopen(monkeypatch, b'{"not_agents": 1}')
        with pytest.raises(RuntimeError):
            sync.fetch_roster("http://x")


# ---------------------------------------------------------------------------
# sync_roster_to_profiles.main — DRY-RUN row mapping (no --apply, no live GET)
# ---------------------------------------------------------------------------

def test_main_dry_run_maps_rows_and_writes_nothing(monkeypatch, tmp_path, capsys):
    roster = [
        {"agent_id": "engineer", "display_name": "Quasimodo", "role": "Engineer"},
        # id fallback (no agent_id key) + role_summary fallback:
        {"id": "finance", "display_name": "Cromwell", "role_summary": "Finance"},
        {"display_name": "Ghost", "role": "Nobody"},          # no agent_id -> SKIP
        {"agent_id": "legal"},                                # no name/role -> SKIP
        {"agent_id": "unknown", "display_name": "X", "role": "Y"},  # no profile -> SKIP
    ]
    monkeypatch.setattr(sync, "fetch_roster", lambda *a, **k: roster)

    existing = {"engineer", "finance", "legal"}
    monkeypatch.setattr(sync, "profile_exists", lambda n: n in existing)

    created: dict[str, Path] = {}

    def _fake_get_dir(name):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        created[name] = d
        return d

    monkeypatch.setattr(sync, "get_profile_dir", _fake_get_dir)

    # Hard guard: a dry-run must NEVER write profile.yaml.
    def _boom(*a, **k):
        raise AssertionError("write_profile_meta called during dry-run")

    monkeypatch.setattr(sync, "write_profile_meta", _boom)

    rc = sync.main([])  # default = dry-run
    assert rc == 0

    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    # engineer: agent_id + display_name + role mapped.
    assert "Quasimodo" in out and "Engineer" in out
    # finance: id fallback + role_summary mapped.
    assert "Cromwell" in out and "Finance" in out
    # The three SKIP reasons all fire.
    assert "SKIP (no agent_id)" in out
    assert "SKIP legal" in out
    assert "SKIP unknown" in out
    # Two settable rows, three skipped.
    assert "would apply 2 change(s)" in out
    assert "3 skipped" in out

    # Nothing written to disk anywhere.
    for d in created.values():
        assert not (d / "profile.yaml").exists()
