"""Contract pins for the surfaces OUT-OF-TREE consumers bind to (CLAWD-3388).

Authored by the independent tester for the v2026.7.30 Phase-1 merge candidate.
These do not test the merge's *intent*; they test the surfaces whose silent
breakage takes out the live fleet on deploy, because the consumer lives in a
different repo and no in-tree import would ever notice.

Consumers pinned here, all verified on disk 2026-08-03:

  * ``~/dev/hermes-mnemosyne-provider`` (``mnemosyne/__init__.py``) does
    ``from agent.memory_provider import MemoryProvider``, subclasses it, and is
    loaded through ``ctx.register_memory_provider(...)``.  This merge changed
    ``sync_turn`` on seven in-tree providers; the ABC and the manager's dispatch
    are what the out-of-tree subclass actually binds to.
  * ``~/dev/hermes-clawd-tools`` (``clawd_tools/__init__.py``) does
    ``ctx.register_tool(name=..., toolset=..., schema=..., handler=...,
    check_fn=..., requires_env=..., emoji=...)``.
  * agora binds through ``plugins/platforms/agora/adapter.py`` ->
    ``ctx.register_platform(...)`` (agora itself is SvelteKit and talks HTTP;
    the Python-side coupling is the platform adapter, not an import).

Deliberately dependency-free: ``tests/tools/test_send_message_tool.py`` skips
its whole module on ``pytest.importorskip("telegram")``, so its
``send_message`` assertions do not execute in a ``.[dev]`` venv.  Nothing here
may depend on an optional extra.
"""

import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The sibling checkout is a *host* fact, not a repo fact. Tests that need it
# say so and skip loudly; every contract below is ALSO pinned by an inline
# literal that runs unconditionally, so a missing sibling never turns a
# contract test into a silent pass.
MNEMOSYNE_PROVIDER = Path.home() / "dev" / "hermes-mnemosyne-provider" / "mnemosyne" / "__init__.py"
CLAWD_TOOLS = Path.home() / "dev" / "hermes-clawd-tools" / "clawd_tools" / "__init__.py"


def _params(func):
    return inspect.signature(func).parameters


def _ast_signature(source_path: Path, func_name: str, class_name: str | None = None):
    """Extract a function's parameter names from source, without importing it.

    Importing the sibling checkout would drag in its own dependency tree; the
    contract we care about is the call shape, which the AST gives us exactly.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    scope = tree
    if class_name is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                scope = node
                break
        else:
            pytest.fail(f"{class_name} not found in {source_path}")
    for node in ast.walk(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            args = node.args
            return {
                "positional": [a.arg for a in args.posonlyargs + args.args],
                "kwonly": [a.arg for a in args.kwonlyargs],
                "has_kwargs": args.kwarg is not None,
            }
    pytest.fail(f"{func_name} not found in {source_path}")


# ---------------------------------------------------------------------------
# 1. agent.memory_provider.MemoryProvider — the ABC hermes-mnemosyne-provider
#    subclasses from another checkout.
# ---------------------------------------------------------------------------


def test_memory_provider_abc_import_path_is_stable():
    """``from agent.memory_provider import MemoryProvider`` must keep working."""
    from agent.memory_provider import MemoryProvider

    assert inspect.isclass(MemoryProvider)
    module = sys.modules["agent.memory_provider"]
    assert Path(module.__file__).resolve().is_relative_to(REPO_ROOT), (
        f"agent.memory_provider resolved to {module.__file__}, outside {REPO_ROOT}"
    )


def test_memory_provider_abc_keeps_every_method_the_out_of_tree_provider_overrides():
    """Names MnemosyneMemoryProvider overrides; losing one silently orphans it."""
    from agent.memory_provider import MemoryProvider

    for name in (
        "initialize",
        "prefetch",
        "queue_prefetch",
        "sync_turn",
        "get_tool_schemas",
        "handle_tool_call",
        "on_session_end",
    ):
        assert hasattr(MemoryProvider, name), f"MemoryProvider lost .{name}()"


def test_memory_provider_sync_turn_signature_is_the_documented_v018_shape():
    """Pin the exact call shape. This merge rewrote sync_turn on 7 providers."""
    from agent.memory_provider import MemoryProvider

    params = _params(MemoryProvider.sync_turn)
    assert list(params) == [
        "self",
        "user_content",
        "assistant_content",
        "session_id",
        "messages",
    ]
    assert params["session_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["messages"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["session_id"].default == ""
    assert params["messages"].default is None


def test_manager_never_passes_conversation_id_that_merged_providers_dropped():
    """The merge deleted ``conversation_id`` from 7 sync_turn signatures.

    If any dispatch site still forwarded it, every one of those providers would
    raise TypeError on the first turn of every live gateway conversation.
    """
    from agent import memory_manager

    source = Path(memory_manager.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forwarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "sync_turn":
            forwarded.extend(kw.arg for kw in node.keywords if kw.arg)
    assert forwarded, "no sync_turn dispatch found in memory_manager — pin is stale"
    assert "conversation_id" not in forwarded, (
        f"memory_manager forwards conversation_id={forwarded}; the 7 merged "
        "providers no longer accept it"
    )


def test_every_in_tree_memory_provider_is_callable_through_the_manager_dispatch():
    """Behavioural: drive real MemoryManager.sync_all into each real subclass.

    Signature parity is not the contract — *being callable by the dispatcher*
    is. This binds each provider's real ``sync_turn`` through the real
    ``_provider_sync_accepts_messages`` branch, both with and without
    ``messages``, exactly as a live turn does.
    """
    from agent.memory_manager import MemoryManager

    providers = {}
    from plugins.memory.byterover import ByteRoverMemoryProvider
    from plugins.memory.hindsight import HindsightMemoryProvider
    from plugins.memory.holographic import HolographicMemoryProvider
    from plugins.memory.honcho import HonchoMemoryProvider
    from plugins.memory.mem0 import Mem0MemoryProvider
    from plugins.memory.openviking import OpenVikingMemoryProvider
    from plugins.memory.retaindb import RetainDBMemoryProvider
    from plugins.memory.supermemory import SupermemoryMemoryProvider

    providers = {
        "byterover": ByteRoverMemoryProvider,
        "hindsight": HindsightMemoryProvider,
        "holographic": HolographicMemoryProvider,
        "honcho": HonchoMemoryProvider,
        "mem0": Mem0MemoryProvider,
        "openviking": OpenVikingMemoryProvider,
        "retaindb": RetainDBMemoryProvider,
        "supermemory": SupermemoryMemoryProvider,
    }

    accepts = MemoryManager._provider_sync_accepts_messages
    for name, cls in providers.items():
        unbound = cls.sync_turn
        # Bind exactly what sync_all's two branches build.
        sig = inspect.signature(unbound)
        sig.bind(
            object(), "u", "a", session_id="s"
        )  # no-messages branch — must always bind
        probe = type("_Probe", (), {"sync_turn": unbound})()
        if accepts(probe):
            sig.bind(object(), "u", "a", session_id="s", messages=[])


def test_out_of_tree_memory_plugin_registration_path_still_collects_the_provider():
    """``register(ctx) -> ctx.register_memory_provider(...)`` end to end.

    NOTE: ``hermes_cli.plugins.PluginContext`` does NOT expose
    ``register_memory_provider`` (measured 2026-08-03). Memory plugins are
    loaded through a *different* context — ``plugins.memory._ProviderCollector``
    — so pinning PluginContext would have pinned the wrong object. This
    reproduces the exact shape of ~/dev/hermes-mnemosyne-provider's entry point.
    """
    from agent.memory_provider import MemoryProvider
    from plugins.memory import _ProviderCollector

    class _OutOfTreeProvider(MemoryProvider):
        name = "contract-probe"

        def is_available(self):
            return True

        def initialize(self, config, **kwargs):
            return True

        def prefetch(self, query, *, session_id=""):
            return ""

        def sync_turn(self, user_content, assistant_content, *, session_id="",
                      messages=None, conversation_id="", **_ignored):
            return None

        def get_tool_schemas(self):
            return []

        def handle_tool_call(self, name, args):
            return ""

    def register(ctx):  # verbatim shape of the sibling's entry point
        ctx.register_memory_provider(_OutOfTreeProvider())

    collector = _ProviderCollector()
    register(collector)
    assert isinstance(collector.provider, _OutOfTreeProvider), (
        "the memory-plugin registration path no longer collects the provider; "
        "hermes-mnemosyne-provider would load as a silent no-op"
    )


@pytest.mark.skipif(
    not MNEMOSYNE_PROVIDER.is_file(),
    reason=f"sibling checkout absent: {MNEMOSYNE_PROVIDER}",
)
def test_out_of_tree_mnemosyne_provider_still_binds_to_this_abc():
    """Drift detector against the real consumer's on-disk call shape.

    Environment-dependent by construction (it reads another checkout). The
    unconditional inline pin is
    ``test_memory_provider_sync_turn_signature_is_the_documented_v018_shape``.
    """
    from agent.memory_manager import MemoryManager

    consumer = _ast_signature(MNEMOSYNE_PROVIDER, "sync_turn", "MnemosyneMemoryProvider")
    # It must still be able to swallow every kwarg this tree's dispatcher sends.
    for kwarg in ("session_id", "messages"):
        assert kwarg in consumer["kwonly"] or consumer["has_kwargs"], (
            f"MnemosyneMemoryProvider.sync_turn cannot accept {kwarg}=; "
            f"this tree's MemoryManager.sync_all sends it"
        )
    assert "from agent.memory_provider import MemoryProvider" in (
        MNEMOSYNE_PROVIDER.read_text(encoding="utf-8")
    )
    assert hasattr(MemoryManager, "_provider_sync_accepts_messages")


# ---------------------------------------------------------------------------
# 2. PluginContext.register_tool — the surface hermes-clawd-tools calls.
# ---------------------------------------------------------------------------

# The literal call shape in ~/dev/hermes-clawd-tools/clawd_tools/__init__.py.
CLAWD_TOOLS_REGISTER_KWARGS = (
    "name",
    "toolset",
    "schema",
    "handler",
    "check_fn",
    "requires_env",
    "emoji",
)


def test_plugin_context_register_tool_accepts_the_clawd_tools_call_shape():
    from hermes_cli.plugins import PluginContext

    params = _params(PluginContext.register_tool)
    for kwarg in CLAWD_TOOLS_REGISTER_KWARGS:
        assert kwarg in params, f"PluginContext.register_tool lost {kwarg}="
        assert params[kwarg].kind is not inspect.Parameter.POSITIONAL_ONLY

    # Binding is the real check: it fails on a reorder-to-positional-only or a
    # newly-required parameter that clawd_tools does not pass.
    inspect.signature(PluginContext.register_tool).bind(
        object(),
        name="mail_compose",
        toolset="mail",
        schema={},
        handler=lambda **_: None,
        check_fn=lambda: True,
        requires_env=["CLAWD_API_AUTH_TOKEN"],
        emoji="📧",
    )


def test_tool_registry_register_accepts_the_same_shape():
    """register_tool delegates to registry.register — pin the far side too."""
    from tools.registry import ToolRegistry

    inspect.signature(ToolRegistry.register).bind(
        object(),
        name="mail_compose",
        toolset="mail",
        schema={},
        handler=lambda **_: None,
        check_fn=lambda: True,
        requires_env=["CLAWD_API_AUTH_TOKEN"],
        emoji="📧",
    )


def test_registering_a_plugin_tool_actually_lands_in_the_registry():
    """End-to-end: the registration path clawd_tools rides still works."""
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(
        name="_contract_probe_tool",
        toolset="mail",
        schema={"name": "_contract_probe_tool", "description": "probe"},
        handler=lambda **_: "ok",
        check_fn=lambda: True,
        requires_env=["CLAWD_API_AUTH_TOKEN"],
        emoji="📧",
    )
    entry = registry.get_entry("_contract_probe_tool")
    assert entry is not None
    assert registry.get_schema("_contract_probe_tool") is not None


@pytest.mark.skipif(
    not CLAWD_TOOLS.is_file(), reason=f"sibling checkout absent: {CLAWD_TOOLS}"
)
def test_out_of_tree_clawd_tools_passes_only_kwargs_this_tree_accepts():
    """Drift detector against the real consumer's on-disk call."""
    from hermes_cli.plugins import PluginContext

    tree = ast.parse(CLAWD_TOOLS.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_tool"
    ]
    assert calls, "clawd_tools no longer calls ctx.register_tool — pin is stale"
    accepted = set(_params(PluginContext.register_tool))
    for call in calls:
        assert not call.args, "clawd_tools passes register_tool args positionally"
        for kw in call.keywords:
            assert kw.arg in accepted, (
                f"clawd_tools passes {kw.arg}= which PluginContext.register_tool "
                f"no longer accepts"
            )


# ---------------------------------------------------------------------------
# 3. The agora platform adapter — the fleet's operator console surface.
# ---------------------------------------------------------------------------


def test_agora_platform_adapter_imports_and_keeps_its_registration_entry_point():
    import plugins.platforms.agora.adapter as agora

    assert Path(agora.__file__).resolve().is_relative_to(REPO_ROOT)
    assert callable(agora.register)
    assert inspect.isclass(agora.AgoraAdapter)
    # ``hasattr``/``getattr`` is NOT sufficient here: BasePlatformAdapter also
    # declares connect/disconnect/send/get_chat_info, so attribute lookup walks
    # the MRO and succeeds even when AgoraAdapter's own override has been
    # renamed away. Assert OWNERSHIP — the adapter must implement each one
    # itself, which is the thing the fleet's outbound path actually calls.
    for name in ("connect", "disconnect", "send", "get_chat_info"):
        assert name in agora.AgoraAdapter.__dict__, (
            f"AgoraAdapter no longer implements .{name}() itself; it would "
            f"inherit BasePlatformAdapter's and go silently inert"
        )
        assert callable(agora.AgoraAdapter.__dict__[name])
    for name in ("check_requirements", "validate_config", "is_connected"):
        assert callable(getattr(agora, name)), f"agora adapter lost {name}()"


def test_agora_register_supplies_every_kwarg_register_platform_requires():
    """The plugin loader calls register(ctx); pin the ctx surface it needs."""
    import plugins.platforms.agora.adapter as agora
    from hermes_cli.plugins import PluginContext

    assert hasattr(PluginContext, "register_platform")
    signature = inspect.signature(PluginContext.register_platform)
    accepted = set(signature.parameters)
    # register_platform ends in **entry_kwargs, so an unknown keyword is
    # swallowed rather than rejected. That means a *name* check proves nothing
    # by itself — bind() is the assertion with teeth, and the named-parameter
    # loop below only reports which kwargs are explicit vs. absorbed.
    absorbs_unknown = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    )

    tree = ast.parse(Path(agora.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_platform"
    ]
    assert calls, "agora adapter no longer calls ctx.register_platform"
    for call in calls:
        assert not call.args, "agora passes register_platform args positionally"
        passed = {kw.arg for kw in call.keywords if kw.arg}
        # The load-bearing check: the real call must BIND. This fails on a
        # renamed/removed explicit parameter, on a newly-required one agora
        # does not pass, and on a signature that stops absorbing extras.
        signature.bind(object(), **{name: None for name in passed})
        if not absorbs_unknown:
            unknown = passed - accepted
            assert not unknown, (
                f"agora passes register_platform({sorted(unknown)}) which "
                f"PluginContext.register_platform no longer accepts"
            )
    # Parameters agora depends on being handled, not merely swallowed. These
    # are named explicitly in the signature today; if one moves into
    # **entry_kwargs the adapter goes quiet instead of erroring.
    for required in ("name", "label", "adapter_factory", "check_fn"):
        assert required in accepted, (
            f"register_platform lost its explicit {required} parameter; agora "
            f"would be registered with it silently absorbed into **entry_kwargs"
        )


# ---------------------------------------------------------------------------
# 4. send_message transport — the merge de-registered the model tool but the
#    transport must stay callable for trusted in-process callers.
#    The staged assertion for this lives in a module that skips entirely when
#    python-telegram-bot is absent; this one has no optional dependency.
# ---------------------------------------------------------------------------


def test_send_message_transport_survives_deregistration_without_telegram_extra():
    from tools.registry import registry
    from tools.send_message_tool import _send_to_platform, send_message_tool

    assert callable(send_message_tool)
    assert callable(_send_to_platform)
    # De-registered from the model surface by CLAWD-3377 …
    assert registry.get_entry("send_message") is None
    assert registry.get_schema("send_message") is None
    # … and absent from every schema the model is actually shown.
    from toolsets import resolve_toolset

    for platform in ("hermes-cli", "hermes-telegram", "hermes-cron"):
        assert "send_message" not in resolve_toolset(platform)


def test_messaging_toolset_removal_is_complete_across_both_declaration_sites():
    """A half-removal leaves a picker entry resolving to a nonexistent tool.

    NOTE, measured 2026-08-03: ``"messaging" not in TOOLSETS`` is deliberately
    NOT asserted here. ``messaging`` was never a key in ``TOOLSETS`` — not in
    this tree and not at the merge base (b041b778) — so that assertion cannot
    fail and would read as coverage it does not provide. The two assertions
    below both go red when ``send_message`` is put back into
    ``_HERMES_CORE_TOOLS`` / ``CONFIGURABLE_TOOLSETS``.
    """
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
    from toolsets import _HERMES_CORE_TOOLS

    assert "send_message" not in _HERMES_CORE_TOOLS
    assert "messaging" not in {name for name, _, _ in CONFIGURABLE_TOOLSETS}


# ---------------------------------------------------------------------------
# 5. The merge's own green-gate guard, which arrives with no test of its own.
#    ``_main`` gained ``no_tests_ran_at_all`` -> banner + ``return 1``. Nothing
#    in tests/ mentions it (rg 'NO TESTS RAN|no_tests_ran|tests_collected'
#    over tests/ returns nothing), so a later edit could delete it silently and
#    a zero-collection run would go back to reporting a green gate.
# ---------------------------------------------------------------------------


def _stage_mini_suite(tmp_path: Path) -> Path:
    root = tmp_path / "mini"
    (root / "scripts").mkdir(parents=True)
    for name in ("run_tests.sh", "run_tests_parallel.py"):
        target = root / "scripts" / name
        target.write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
        target.chmod(0o755)
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_mini.py").write_text(
        "def test_mini_passes():\n    assert True\n", encoding="utf-8"
    )
    return root


def _run_parallel_runner(root: Path, extra: list[str]) -> subprocess.CompletedProcess:
    # Explicit narrow target and an explicit worker cap, always. The runner must
    # never be invoked bare or with --help as a probe.
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "run_tests_parallel.py"),
         "tests/test_mini.py", "-j", "1", *extra],
        capture_output=True, text=True, cwd=str(root),
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )


def test_zero_collection_is_not_reported_as_a_green_gate(tmp_path: Path):
    root = _stage_mini_suite(tmp_path)

    # Positive control FIRST: without the filter the same invocation is green.
    # Without this, the assertion below could pass because the runner is broken.
    ok = _run_parallel_runner(root, [])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "NO TESTS RAN" not in ok.stdout

    filtered = _run_parallel_runner(root, ["-k", "zzz_matches_no_test_name"])
    assert "NO TESTS RAN" in filtered.stdout, filtered.stdout + filtered.stderr
    assert filtered.returncode == 1, (
        "a run that collected zero tests exited "
        f"{filtered.returncode}; a zero-collection run must never be green"
    )


# ---------------------------------------------------------------------------
# 6. Prior-finding pin the staged suite does not cover: the FINAL exec.
#    The staged credential-isolation test probes only the pre-compile child.
# ---------------------------------------------------------------------------


def _stage_env_probe_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repo whose venv python dumps the environment it was exec'd with."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    for name in ("run_tests.sh", "run_tests_parallel.py"):
        target = root / "scripts" / name
        target.write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
        target.chmod(0o755)

    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (root / ".venv" / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    probe_out = tmp_path / "final-exec-env.txt"
    prefix_probe = "import os, sys; print(os.path.realpath(sys.prefix))"
    (venv_bin / "python").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ] && [ "$2" = "{prefix_probe}" ]; then\n'
        f"  printf '%s\\n' '{root / '.venv'}'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "-S" ] && [ "$2" = "-m" ]; then exit 0; fi\n'
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        f"env > '{probe_out}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (venv_bin / "python").chmod(0o755)
    return root, probe_out


def test_posix_run_does_not_forward_caller_TEMP_TMP_past_env_i(tmp_path: Path):
    """The WIN_ENV forwarding loop must stay gated on $SYSTEMROOT.

    TEMP/TMP are ordinary POSIX-settable variables. Ungated, the loop collects
    them on Linux and re-injects them PAST ``env -i`` — the exact boundary the
    surrounding hardening exists to enforce — and Python's tempfile honours
    TMPDIR, then TEMP, then TMP. A caller could therefore relocate the
    live-gateway guard module under a directory they control and have it placed
    on PYTHONPATH for every pytest child.

    Nothing in tests/ referenced WIN_ENV/SYSTEMROOT for this runner when this
    was written (rg over tests/ found only unrelated tools/ matches), so the
    gate shipped with no coverage.
    """
    root, probe_out = _stage_env_probe_repo(tmp_path)
    caller_dir = tmp_path / "caller-controlled"
    caller_dir.mkdir()

    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    env.pop("SYSTEMROOT", None)  # POSIX run, by construction
    env["TEMP"] = str(caller_dir)
    env["TMP"] = str(caller_dir)

    proc = subprocess.run(
        ["bash", str(root / "scripts" / "run_tests.sh")],
        capture_output=True, text=True, env=env, cwd=str(root),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert probe_out.is_file(), (
        "never reached the final exec, so this proved nothing: "
        + proc.stdout + proc.stderr
    )
    observed = dict(
        line.split("=", 1)
        for line in probe_out.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert "TEMP" not in observed, (
        f"caller TEMP={observed.get('TEMP')!r} crossed `env -i`; the guard "
        f"module would be materialized under a caller-chosen root"
    )
    assert "TMP" not in observed, f"caller TMP={observed.get('TMP')!r} crossed `env -i`"


def test_final_exec_drops_arbitrary_caller_credentials(tmp_path: Path):
    """A secret in the caller's env must not reach the test runner process."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    for name in ("run_tests.sh", "run_tests_parallel.py"):
        target = root / "scripts" / name
        target.write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
        target.chmod(0o755)

    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (root / ".venv" / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    probe_out = tmp_path / "final-exec-env.txt"
    prefix_probe = "import os, sys; print(os.path.realpath(sys.prefix))"
    (venv_bin / "python").write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ] && [ "$2" = "{prefix_probe}" ]; then\n'
        f"  printf '%s\\n' '{root / '.venv'}'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "-S" ] && [ "$2" = "-m" ]; then exit 0; fi\n'
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        f"env > '{probe_out}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (venv_bin / "python").chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["OPENAI_API_KEY"] = "synthetic-contract-probe-value"
    env["CLAWD_API_AUTH_TOKEN"] = "synthetic-contract-probe-token"
    env["AWS_SECRET_ACCESS_KEY"] = "synthetic-contract-probe-aws"

    proc = subprocess.run(
        ["bash", str(root / "scripts" / "run_tests.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert probe_out.is_file(), (
        "the runner never reached the final exec, so this test proved nothing: "
        + proc.stdout
        + proc.stderr
    )
    observed = probe_out.read_text(encoding="utf-8")
    assert "synthetic-contract-probe-value" not in observed
    assert "synthetic-contract-probe-token" not in observed
    assert "synthetic-contract-probe-aws" not in observed
    assert "OPENAI_API_KEY" not in observed
    assert "CLAWD_API_AUTH_TOKEN" not in observed
