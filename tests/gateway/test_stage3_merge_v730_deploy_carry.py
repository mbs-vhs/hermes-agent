"""STAGE 3 GATE — what the v2026.7.30 deploy actually carries to the fleet.

Written by the independent tester for the post-merge union run
(fork main = 4cc44689c).  These are NOT duplicates of the merge author's own
contract suites (``test_merge_v730_downstream_contracts.py`` /
``test_merge_v730_pause_resume_contract.py``).  Every test here exists because
an *import smoke test cannot reach it* — the CLAWD-3388 ``UnboundLocalError``
passed ``py_compile`` and passed an import smoke and still killed ``/v1/runs``
on all 11 gateways.  So each test below EXECUTES a path.

Three classes of deploy hazard are covered:

1. ``TestCreateAgentExecutes``   — the exact defect class that got through:
   a runtime name-binding error in ``APIServerAdapter._create_agent`` that is
   invisible to compilation.  Driven with the real call shape the fork's own
   ``/v1/runs`` handler builds.

2. ``TestClawdForwardedOverrides`` — the SILENT half of the merge.  clawd's
   ``chat_conversation_reply.py`` puts ``model`` / ``reasoning_effort`` /
   ``verbosity`` at the TOP LEVEL of the /v1/runs body.  The deployed 0.18.0
   tree validates and applies all three; the merged tree honours only
   ``model``, and reads the other two exclusively out of a nested
   ``model_options`` dict.  Nothing raises.  These tests pin the *measured*
   post-merge behaviour so the regression is visible in the suite instead of
   only in a commit message (CLAWD-3533).

3. ``TestMissingNemoRelayDegrades`` — ``nemo-relay`` became a NON-optional
   base dependency in this merge and is NOT installed in
   ``/opt/hermes-agent/venv``.  It is imported lazily, so a deploy that ships
   the tree without re-resolving dependencies fails *silently* rather than
   loudly.  Exercised by blocking the module and driving the real
   per-turn coordinator entry points ``run_agent.py`` calls on every turn.

No live gateway is contacted; no notifier is constructed; nothing under
``/opt`` or ``~/.hermes`` is written.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. _create_agent must EXECUTE, not merely compile
# ---------------------------------------------------------------------------

def _runtime_kwargs():
    return {
        "api_key": "test-key",
        "base_url": None,
        "provider": None,
        "api_mode": None,
        "command": None,
        "args": [],
    }


def _adapter():
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    return APIServerAdapter(PlatformConfig())


class TestCreateAgentExecutes:
    """CLAWD-3388: ``model`` was read before assignment at method-body top
    level — unconditional, so no call could avoid it, and neither
    ``py_compile`` nor ``import gateway.platforms.api_server`` sees it."""

    def test_create_agent_runs_with_the_bare_gateway_default(self):
        with patch("gateway.run._resolve_runtime_agent_kwargs") as kw, \
             patch("gateway.run._resolve_gateway_model") as gm, \
             patch("gateway.run._load_gateway_config") as cfg, \
             patch("run_agent.AIAgent") as agent_cls:
            kw.return_value = _runtime_kwargs()
            gm.return_value = "test/model"
            cfg.return_value = {}
            agent_cls.return_value = MagicMock()

            # The bug raised UnboundLocalError here, before any argument
            # handling — this bare call is the minimal reproduction.
            _adapter()._create_agent()

            assert agent_cls.called, "AIAgent was never constructed"

    def test_create_agent_runs_with_the_clawd_request_shape(self):
        """The shape ``/v1/runs`` builds from a clawd body carrying model +
        model_options.  Exercises ``_request_reasoning_config`` and
        ``_clean_request_string`` on the same call, i.e. the code upstream
        moved the fork's orphaned override block down into."""
        with patch("gateway.run._resolve_runtime_agent_kwargs") as kw, \
             patch("gateway.run._resolve_gateway_model") as gm, \
             patch("gateway.run._load_gateway_config") as cfg, \
             patch("run_agent.AIAgent") as agent_cls:
            kw.return_value = _runtime_kwargs()
            gm.return_value = "test/model"
            cfg.return_value = {}
            agent_cls.return_value = MagicMock()

            _adapter()._create_agent(
                session_id="stage3-session",
                requested_model="moonshotai/kimi-k2-thinking",
                requested_provider="openrouter",
                model_options={"reasoning_effort": "high", "verbosity": "low"},
            )

            assert agent_cls.called, "AIAgent was never constructed"

    def test_the_two_orphaned_override_names_are_fully_gone_from_the_body(self):
        """NOT a read-before-assignment detector — read the caveat.

        An earlier version of this test tried to find unbound locals by walking
        the AST and flagging a Load seen before a Store.  It was DECORATIVE:
        revert-validation R1 (restoring ``model = model or
        _resolve_gateway_model()``) left it GREEN, because ``ast.walk`` is
        breadth-first and visits the assignment TARGET before the value, so the
        Store was recorded first.  Detecting that class statically needs a CFG;
        the two execution tests above are what actually catch it.

        What this DOES pin is the narrower, checkable half: upstream moved the
        override handling out of ``_create_agent`` entirely, so the two names
        that were renamed alongside ``model`` must not appear in the body at
        all.  A resolver that re-introduces either — the exact shape of the
        CLAWD-3388 mistake — fails here even on a request that never supplies
        them, which the execution tests would not exercise.
        """
        import ast
        import inspect

        from gateway.platforms.api_server import APIServerAdapter

        src = inspect.cleandoc(inspect.getsource(APIServerAdapter._create_agent))
        fn = ast.parse(src).body[0]

        names = {
            n.id for n in ast.walk(fn) if isinstance(n, ast.Name)
        } | {
            n.arg for n in ast.walk(fn) if isinstance(n, ast.arg)
        } | {
            k.arg for n in ast.walk(fn) if isinstance(n, ast.keyword) for k in [n]
            if n.arg
        }

        resurrected = {"reasoning_effort", "verbosity"} & names
        assert not resurrected, (
            f"_create_agent references {sorted(resurrected)} again; upstream "
            "moved that handling to _request_reasoning_config(model_options). "
            "Re-introducing the fork's orphaned block is what produced the "
            "CLAWD-3388 UnboundLocalError."
        )


# ---------------------------------------------------------------------------
# 2. clawd's top-level overrides — the silent half
# ---------------------------------------------------------------------------

class TestClawdForwardedOverrides:
    """clawd/agents/service/app/slices/chat/routes/chat_conversation_reply.py
    builds its /v1/runs body as::

        if model is not None:            body["model"] = model
        if reasoning_effort is not None: body["reasoning_effort"] = ...
        if verbosity is not None:        body["verbosity"] = ...

    i.e. TOP LEVEL, not nested under ``model_options``.  The deployed 0.18.0
    gateway validated and applied all three.  This merge keeps only ``model``.
    These tests state that plainly rather than leaving it in a commit body."""

    def _overrides(self, body):
        from gateway.platforms.api_server import _request_agent_overrides

        return _request_agent_overrides(body, virtual_model="hermes-agent")

    def test_top_level_model_is_still_honoured(self):
        got = self._overrides({"model": "moonshotai/kimi-k2-thinking"})
        assert got.get("requested_model") == "moonshotai/kimi-k2-thinking"

    @pytest.mark.parametrize("key,value", [
        ("reasoning_effort", "high"),
        ("verbosity", "low"),
    ])
    def test_top_level_reasoning_and_verbosity_are_dropped_without_error(
        self, key, value
    ):
        """DELIBERATE post-merge behaviour (CLAWD-3533), pinned so a future
        change is visible: the key is neither honoured nor rejected.  clawd
        still sends it; after this deploy it has no effect and produces no
        diagnostic."""
        got = self._overrides({"model": "m", key: value})
        assert key not in got
        assert "model_options" not in got

    def test_nested_model_options_is_the_only_surviving_route(self):
        from gateway.platforms.api_server import _request_reasoning_config

        got = self._overrides(
            {"model": "m", "model_options": {"reasoning_effort": "high"}}
        )
        assert got["model_options"] == {"reasoning_effort": "high"}
        assert _request_reasoning_config(got["model_options"]) is not None

    def test_a_top_level_only_body_yields_no_reasoning_config(self):
        """End-to-end statement of the regression: the exact body clawd sends
        produces no reasoning config at all."""
        from gateway.platforms.api_server import _request_reasoning_config

        clawd_body = {
            "input": "hello",
            "model": "moonshotai/kimi-k2-thinking",
            "reasoning_effort": "high",
            "verbosity": "low",
        }
        overrides = self._overrides(clawd_body)
        assert _request_reasoning_config(overrides.get("model_options")) is None


# ---------------------------------------------------------------------------
# 3. nemo-relay: new NON-optional dep, lazily imported, absent from /opt venv
# ---------------------------------------------------------------------------

class _BlockNemoRelay:
    """Make ``import nemo_relay`` fail the way the deployed venv would."""

    def find_module(self, fullname, path=None):  # pragma: no cover - py<3.12
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nemo_relay" or fullname.startswith("nemo_relay."):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


@pytest.fixture
def nemo_relay_absent(monkeypatch):
    for name in [m for m in list(sys.modules) if m.split(".")[0] == "nemo_relay"]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    finder = _BlockNemoRelay()
    monkeypatch.setattr(sys, "meta_path", [finder] + list(sys.meta_path))
    return finder


class TestMissingNemoRelayDegrades:
    """``nemo-relay>=0.6.0,<0.7`` moved into ``[project] dependencies`` in this
    merge (platform-gated to the fleet's linux/x86_64) and is NOT present in
    ``/opt/hermes-agent/venv``.  ``agent/relay_runtime.py`` imports it inside
    ``_load_nemo_relay()``, so the failure mode is silence, not a crash — the
    dangerous kind.  Prove the degradation is actually graceful on the paths
    ``run_agent.py`` drives EVERY turn."""

    def test_the_block_fixture_actually_blocks(self, nemo_relay_absent):
        """Control.  If the fixture were inert every assertion below would be
        vacuously green against a venv that HAS nemo-relay installed."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("nemo_relay")

    def test_load_nemo_relay_raises_when_absent(self, nemo_relay_absent):
        from agent import relay_runtime

        with pytest.raises(ModuleNotFoundError):
            relay_runtime._load_nemo_relay()

    def test_per_turn_coordinator_degrades_to_noop_instead_of_raising(
        self, nemo_relay_absent
    ):
        """``run_agent.py`` calls acquire_conversation -> begin_turn on the
        forwarder to ``run_conversation``.  If a missing nemo_relay propagated
        out of here, every agent turn on every gateway would raise."""
        from agent import relay_runtime

        registry = type(relay_runtime.HOST_REGISTRY)()
        lease_host = registry.for_profile("stage3-profile")

        assert lease_host is not None
        assert isinstance(lease_host, relay_runtime.NoopRelayRuntime), (
            "a missing nemo_relay must degrade to NoopRelayRuntime; got "
            f"{type(lease_host).__name__}"
        )

    def test_acquire_and_begin_turn_complete_with_nemo_relay_absent(
        self, nemo_relay_absent
    ):
        from agent import relay_runtime

        coordinator = type(relay_runtime.SESSION_COORDINATOR)()
        lease = coordinator.acquire_conversation(
            profile_key="stage3-profile",
            session_id="stage3-session",
            platform="api_server",
            model="test/model",
        )
        turn = coordinator.begin_turn(
            lease, turn_id="stage3-turn", task_id="stage3-task"
        )
        assert lease is not None
        assert turn is not None
        coordinator.end_turn(turn, outcome="succeeded")
        coordinator.release_conversation(lease)

    def test_nemo_relay_is_a_base_dependency_not_an_extra(self):
        """The reason the above matters.  If nemo-relay were only an extra,
        a venv without it would be an ordinary supported configuration."""
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        data = tomllib.loads((root / "pyproject.toml").read_text())
        base = data["project"]["dependencies"]
        assert any(d.split(";")[0].strip().startswith("nemo-relay") for d in base), (
            "nemo-relay left the base dependency set; re-check whether the "
            "deployed venv still needs it"
        )


# ---------------------------------------------------------------------------
# 4. memory-provider module rename the deploy carries
# ---------------------------------------------------------------------------

class TestMemoryProviderModuleRename:
    """``hermes_cli/memory_providers.py`` exists in ``/opt/hermes-agent`` and
    is imported there by ``plugins/memory/__init__.py``.  At this ref the
    module is GONE and discovery lives in ``plugins.memory``.  A deploy that
    copies without ``--delete`` leaves the orphan importable but unused; a
    deploy WITH ``--delete`` removes it.  Either way the surface
    hermes-mnemosyne-provider is loaded through must be the new one."""

    def test_the_old_module_is_gone_at_this_ref(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        assert not (root / "hermes_cli" / "memory_providers.py").exists()

    def test_discovery_api_moved_to_plugins_memory(self):
        import plugins.memory as pm

        assert callable(getattr(pm, "discover_memory_providers", None))
        assert callable(getattr(pm, "load_memory_provider", None))

    def test_nothing_in_tree_still_imports_the_removed_module(self):
        """The orphan is only harmless while nothing references it.

        TWO TRAPS, both of which this test previously fell into. Independent
        review caught them; the notes stay so a future edit cannot reintroduce
        either one.

        1. `git grep` searches TRACKED files, so the moment this file is
           committed it matches its OWN argv and the test fails. It passed only
           while it sat untracked in a scratch worktree — the "38 passed" in the
           commit that added it was measured against an artifact that is not the
           one being shipped. Hence the `:(exclude)` pathspec below, and hence:
           REVERT-VALIDATE THIS FILE IN ITS TRACKED STATE, never untracked.

        2. `returncode` was unchecked. `git grep` exits 1 for "no matches" but
           128 when cwd is not a git repo — and this test's whole subject is the
           /opt/hermes-agent deploy tree, which HAS NO .git. There, it returned
           empty stdout and passed vacuously while a real reference sat in the
           tree. A search that cannot run must fail loudly, not report clean.
        """
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        rel = Path(__file__).resolve().relative_to(root).as_posix()
        out = subprocess.run(
            ["git", "grep", "-l", "-e", "hermes_cli.memory_providers",
             "-e", "from hermes_cli import memory_providers",
             "--", "*.py", f":(exclude){rel}"],
            cwd=root, capture_output=True, text=True,
        )
        # 0 = matches found, 1 = none. Anything else means the search did not
        # happen (128 = not a git repo) and the empty stdout below is meaningless.
        assert out.returncode in (0, 1), (
            f"git grep could not run (rc={out.returncode}) from {root} — this "
            f"assertion proves NOTHING here: {out.stderr.strip()[:200]}"
        )
        # rc=1 alone is not enough: an UNREADABLE tracked file makes git grep
        # exit 1 with empty stdout and "failed to stat: Permission denied" on
        # stderr — it SKIPPED the file that might hold the reference and called
        # that "no matches". Re-review reproduced it with chmod 000. Silence on
        # stderr is part of the evidence, not decoration.
        assert not out.stderr.strip(), (
            f"git grep ran but could not read part of the tree, so a clean "
            f"result proves nothing: {out.stderr.strip()[:300]}"
        )
        assert out.stdout.strip() == "", (
            "hermes_cli.memory_providers is still referenced: "
            f"{out.stdout.strip()}"
        )


# ---------------------------------------------------------------------------
# 5. Every bundled platform plugin, not just agora
# ---------------------------------------------------------------------------

def _bundled_platform_packages():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "plugins" / "platforms"
    return sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and (d / "__init__.py").exists()
    )


class TestEveryBundledPlatformRegisters:
    """The merge-author's contract file drives ``register(ctx)`` for agora only.
    The 11 live gateways run telegram/discord/slack/... — a ``PlatformEntry``
    field that only THOSE adapters pass would be dropped with agora green.
    Execute every bundled platform's real registration path."""

    @pytest.mark.parametrize("pkg_name", _bundled_platform_packages())
    def test_register_lands_a_constructible_entry(self, pkg_name):
        from gateway.platform_registry import platform_registry
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

        mod = importlib.import_module(f"plugins.platforms.{pkg_name}")
        register = getattr(mod, "register", None)
        if not callable(register):
            pytest.skip(f"{pkg_name} exposes no register()")

        manifest = PluginManifest(
            name=pkg_name, version="1.0.0", description=pkg_name,
            source="project", kind="platform", key=f"platforms/{pkg_name}",
        )
        # register_platform -> PlatformEntry(**entry_kwargs); a removed
        # dataclass field raises TypeError right here, at plugin-load time.
        # Deliberately NOT wrapped: the TypeError IS the failure signal.
        register(PluginContext(manifest, PluginManager()))

        # Every bundled plugin registers under its own directory name; that is
        # asserted rather than worked around, so a rename shows up here.
        entry = platform_registry.get(pkg_name)
        assert entry is not None, (
            f"{pkg_name} did not land in the platform registry under its own "
            "package name"
        )
        assert callable(entry.adapter_factory)
