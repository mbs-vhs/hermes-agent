"""Invariants for scripts/run_tests.sh's interpreter selection.

Context (CLAWD-3136): the wrapper used to probe a third venv candidate,
``$HOME/.hermes/hermes-agent/venv``.  That path is a *different* checkout's live
runtime, on its own release cadence, and it can never be the correct interpreter
for this repo:

  * The checkout it belongs to can never select it *as* the fallback — that
    checkout's ``REPO_ROOT`` is ``~/.hermes/hermes-agent``, so ``$REPO_ROOT/venv``
    matches first.  The fallback therefore only ever fired from some *other*
    checkout: this dev fork, or one of its worktrees.
  * Worktrees historically had no venv of their own, so the fallback fired
    every time — silently running the gate against an
    interpreter that did not have this repo's declared dependencies installed
    (it was hermes-agent 0.14.0 with no ``Markdown`` while this repo was 0.18.0
    and declares ``Markdown==3.10.2``).  Import-guarded code then took its
    degraded branch, and the gate reported a failure that existed nowhere but
    the harness.

The invariant that matters is behavioural, not textual: **with no repo-local
venv, the runner must fail rather than borrow an interpreter from outside
REPO_ROOT** — even when a plausible-looking one exists under ``$HOME``.  These
tests stage a throwaway repo root plus a decoy ``$HOME/.hermes`` venv and assert
the runner refuses, then assert it *does* select each repo-local candidate in
priority order.
"""

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts import run_tests_parallel as parallel_runner

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "run_tests.sh"
PARALLEL_RUNNER = REPO_ROOT / "scripts" / "run_tests_parallel.py"
PREFIX_PROBE = "import os, sys; print(os.path.realpath(sys.prefix))"

LAUNCH_GATE_HARNESS = r"""
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from scripts import run_tests_parallel as runner

phase = sys.argv[1]
state = Path(sys.argv[2])
registry = runner._ProcessRegistry()
worker = None
capture_original = runner._capture_process_group
exit_code = 99

child_code = (
    "import sys, time; from pathlib import Path; "
    "Path(sys.argv[1]).write_text('started', encoding='utf-8'); time.sleep(300)"
)
child_cmd = [sys.executable, "-c", child_code, str(state / "child-started")]
post_cmd = [sys.executable, "-c", child_code, str(state / "post-started")]


def watch_shutdown():
    registry._shutdown.wait()
    (state / "shutdown-observed").write_text("set", encoding="utf-8")


def run_child():
    proc = None
    try:
        proc, pgid = registry.launch(
            child_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        (state / "published").write_text(
            f"{proc.pid}\n{pgid}", encoding="utf-8"
        )
        proc.wait()
    except runner._ShutdownRequested:
        (state / "worker-rejected").write_text("rejected", encoding="utf-8")
    finally:
        if proc is not None:
            registry.finish(proc)


threading.Thread(target=watch_shutdown, daemon=True).start()
with runner._live_guard_environment() as guard_dir:
    (state / "guard-dir").write_text(str(guard_dir), encoding="utf-8")
    previous = runner._install_signal_handlers(registry)
    try:
        if phase == "before":
            (state / "ready").write_text("ready", encoding="utf-8")
        else:
            if phase == "during":
                def capture_during_publication(proc):
                    (state / "publication-open").write_text(
                        str(proc.pid), encoding="utf-8"
                    )
                    while not (state / "release-publication").exists():
                        time.sleep(0.001)
                    pgid = capture_original(proc)
                    (state / "pgid").write_text(str(pgid), encoding="utf-8")
                    return pgid

                runner._capture_process_group = capture_during_publication
            worker = threading.Thread(target=run_child)
            worker.start()
        while True:
            time.sleep(0.01)
    except runner._SignalExit as interrupted:
        exit_code = interrupted.exit_code
        try:
            late, _ = registry.launch(
                post_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except runner._ShutdownRequested:
            (state / "post-rejected").write_text("rejected", encoding="utf-8")
        else:
            registry.finish(late)
    finally:
        registry.request_shutdown()
        if worker is not None:
            worker.join(timeout=5)
        runner._capture_process_group = capture_original
        runner._restore_signal_handlers(previous)

sys.exit(exit_code)
"""

INSTALL_RACE_HARNESS = r"""
import signal
import sys
import time
from pathlib import Path

from scripts import run_tests_parallel as runner

phase = sys.argv[1]
state = Path(sys.argv[2])
real_signal = runner.signal.signal
install_calls = 0


def pause_installation():
    (state / "install-paused").write_text("paused", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not (state / "release-install").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting to release handler installation")
        time.sleep(0.001)


def controlled_signal(signum, handler):
    global install_calls
    is_install = getattr(handler, "__name__", "") == "handle_signal"
    if is_install:
        install_calls += 1
        if (phase == "before-first" and install_calls == 1) or (
            phase == "between" and install_calls == 2
        ):
            pause_installation()
    previous = real_signal(signum, handler)
    if is_install and phase == "after-second" and install_calls == 2:
        pause_installation()
    return previous


def forbidden_main(_registry):
    (state / "main-entered").write_text("entered", encoding="utf-8")
    return 91


runner.signal.signal = controlled_signal
runner._main = forbidden_main
sys.exit(runner.main())
"""

GUARD_RACE_HARNESS = r"""
import sys
import time
from pathlib import Path

from scripts import run_tests_parallel as runner

phase = sys.argv[1]
state = Path(sys.argv[2])
real_tempdir = runner.tempfile.TemporaryDirectory
real_copy2 = runner.shutil.copy2


def pause_guard_materialization():
    (state / "guard-paused").write_text("paused", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not (state / "release-guard").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out waiting to release guard materialization")
        time.sleep(0.001)


def controlled_tempdir(*args, **kwargs):
    temporary = real_tempdir(*args, **kwargs)
    if phase == "mkdir":
        pause_guard_materialization()
    return temporary


def controlled_copy2(source, destination, *args, **kwargs):
    if phase == "copy":
        pause_guard_materialization()
    return real_copy2(source, destination, *args, **kwargs)


def forbidden_main(_registry):
    (state / "main-entered").write_text("entered", encoding="utf-8")
    return 91


runner.tempfile.TemporaryDirectory = controlled_tempdir
runner.shutil.copy2 = controlled_copy2
runner._main = forbidden_main
sys.exit(runner.main())
"""


def _fake_python(
    path: Path,
    prefix: Path,
    marker: str,
    *,
    exit_code: int = 0,
    compile_log: Path | None = None,
) -> Path:
    """Write an interpreter shim that exposes its prefix and final environment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    compile_probe = ""
    if compile_log is not None:
        compile_probe = (
            'if [ "$1" = "-S" ] && [ "$2" = "-m" ] '
            '&& [ "$3" = "compileall" ]; then\n'
            f"  printf 'ARGS=%s\\n' \"$*\" > {shlex.quote(str(compile_log))}\n"
            f"  printf 'PYTHONPATH=%s\\n' \"${{PYTHONPATH-}}\" >> {shlex.quote(str(compile_log))}\n"
            f"  printf 'OPENAI_API_KEY=%s\\n' \"${{OPENAI_API_KEY-}}\" >> {shlex.quote(str(compile_log))}\n"
            "  exit 0\n"
            "fi\n"
        )
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ] && [ "$2" = {shlex.quote(PREFIX_PROBE)} ]; then\n'
        f"  printf '%s\\n' {shlex.quote(str(prefix.resolve()))}\n"
        "  exit 0\n"
        "fi\n"
        f"{compile_probe}"
        'if [ "$1" = "-c" ]; then\n'
        f"  exec {shlex.quote(sys.executable)} \"$@\"\n"
        "fi\n"
        f"printf '%s\\n' {shlex.quote(marker)}\n"
        "printf 'OBSERVED_PATH=%s\\n' \"$PATH\"\n"
        "printf 'OBSERVED_VIRTUAL_ENV=%s\\n' \"${VIRTUAL_ENV-}\"\n"
        "printf 'OBSERVED_PYTHONPATH=%s\\n' \"${PYTHONPATH-}\"\n"
        "printf 'OBSERVED_PYTEST_PLUGINS=%s\\n' \"${PYTEST_PLUGINS-}\"\n"
        "printf 'OBSERVED_GUARD_SOURCE=%s\\n' "
        '"${HERMES_PYTEST_LIVE_GUARD_SOURCE-}"\n'
        "if [ -n \"${PYTHONPATH-}\" ] && "
        "[ -f \"$PYTHONPATH/pytest_live_guard.py\" ]; then\n"
        "  printf 'OBSERVED_GUARD_ONLY=%s\\n' \"pytest_live_guard.py\"\n"
        "fi\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_venv(path: Path, marker: str) -> Path:
    """A directory that looks like a venv to the runner's probe.

    ``bin/python`` echoes ``marker`` and exits 0, so a run that gets as far as
    the final ``exec`` reveals *which* venv was chosen.
    """
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    _fake_python(path / "bin" / "python", path, marker)
    return path


def _real_python_shim(path: Path, prefix: Path) -> Path:
    """Expose a fake local prefix, then run the staged harness with real Python."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ] && [ "$2" = {shlex.quote(PREFIX_PROBE)} ]; then\n'
        f"  printf '%s\\n' {shlex.quote(str(prefix.resolve()))}\n"
        "  exit 0\n"
        "fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    """A throwaway repo root holding a real copy of the runner, and a fake HOME.

    ``REPO_ROOT`` is derived from ``BASH_SOURCE``, so relocating the script is
    the only way to control it.  ``$HOME/.hermes/hermes-agent/venv`` is created
    as a decoy: it is exactly what the removed fallback pointed at.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(RUNNER, root / "scripts" / "run_tests.sh")
    (root / "scripts" / "run_tests.sh").chmod(0o755)
    shutil.copy2(
        REPO_ROOT / "scripts" / "run_tests_parallel.py",
        root / "scripts" / "run_tests_parallel.py",
    )

    home = tmp_path / "home"
    _fake_venv(home / ".hermes" / "hermes-agent" / "venv", "DECOY-RUNTIME-VENV")
    return root


def _run(
    root: Path, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(root.parent / "home")
    tmpdir = root.parent / "tmp"
    tmpdir.mkdir(exist_ok=True)
    env["TMPDIR"] = str(tmpdir)
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(root / "scripts" / "run_tests.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
    )


def _install_live_guard(root: Path) -> Path:
    guard = root.parent / "home" / ".hermes" / "pytest_live_guard.py"
    guard.write_text("GUARD_MARKER = 'isolated-guard'\n", encoding="utf-8")
    return guard


def _observed_value(proc: subprocess.CompletedProcess, name: str) -> str:
    prefix = f"{name}="
    return next(
        line.removeprefix(prefix)
        for line in proc.stdout.splitlines()
        if line.startswith(prefix)
    )


def _safe_system_path() -> str:
    paths = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    paths.extend(
        path
        for path in ("/opt/homebrew/bin", "/opt/homebrew/sbin")
        if Path(path).is_dir()
    )
    return os.pathsep.join(paths)


def _pgid_exists(pgid: int) -> bool:
    ps = shutil.which("ps", path=_safe_system_path())
    assert ps is not None
    proc = subprocess.run(
        [ps, "-eo", "pgid=,stat="],
        capture_output=True,
        text=True,
    )
    return any(
        int(fields[0]) == pgid and not fields[1].startswith("Z")
        for line in proc.stdout.splitlines()
        if len(fields := line.split()) == 2
    )


def _force_kill_group(pgid: int) -> None:
    kill = shutil.which("kill", path=_safe_system_path())
    assert kill is not None
    subprocess.run(
        [kill, "-KILL", f"-{pgid}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _wait_for_path(
    path: Path, proc: subprocess.Popen | None = None, timeout: float = 5
) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            pytest.fail(
                f"process exited {proc.returncode} before {path}: {proc.stderr.read()}"
            )
        time.sleep(0.005)
    assert path.exists(), f"timed out waiting for {path}"


def test_refuses_to_borrow_an_interpreter_from_outside_the_repo(staged: Path):
    """No repo-local venv + a runtime venv under $HOME => fail, don't borrow."""
    proc = _run(staged)

    assert proc.returncode != 0, (
        "runner must fail when the repo has no venv; instead it exited 0 "
        f"with stdout={proc.stdout!r}"
    )
    # The decisive assertion: it never reached the exec, so it never ran the
    # foreign interpreter.
    assert "DECOY-RUNTIME-VENV" not in proc.stdout
    assert "no virtualenv found" in proc.stderr


def test_error_names_only_repo_local_candidates(staged: Path):
    """The failure must tell you where it looked — and it looked only in-repo."""
    proc = _run(staged)

    assert str(staged / ".venv") in proc.stderr
    assert str(staged / "venv") in proc.stderr
    # If a future edit re-adds a $HOME candidate, it would have to appear here.
    assert ".hermes" not in proc.stderr


def test_runner_remains_bash_32_compatible():
    """Guard against post-macOS-Bash-3.2 builtins entering the wrapper."""
    source = RUNNER.read_text(encoding="utf-8")

    for unsupported in ("mapfile", "readarray", "wait -n", "coproc"):
        assert unsupported not in source
    proc = subprocess.run(
        ["bash", "--posix", "-n", str(RUNNER)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_shell_keeps_final_exec_and_passes_only_guard_source():
    source = RUNNER.read_text(encoding="utf-8")

    assert "exec env -i" in source
    assert "HERMES_PYTEST_LIVE_GUARD_SOURCE=" in source
    assert "RUNNER_PID" not in source
    assert "trap " not in source


def test_rejects_symlinked_dot_venv(staged: Path):
    """A worktree must own its environment, not link another checkout's."""
    shared = _fake_venv(staged.parent / "shared-venv", "SHARED-VENV")
    (staged / ".venv").symlink_to(shared, target_is_directory=True)

    proc = _run(staged)

    assert proc.returncode != 0
    assert "SHARED-VENV" not in proc.stdout
    assert "refusing symlinked virtualenv" in proc.stderr
    assert str(staged / ".venv") in proc.stderr


def test_rejects_bin_directory_resolving_outside_environment(staged: Path):
    """A real local shell directory cannot hide a shared external venv bin."""
    shared = _fake_venv(staged.parent / "shared-venv", "SHARED-VENV")
    (staged / ".venv").mkdir()
    (staged / ".venv" / "bin").symlink_to(shared / "bin", target_is_directory=True)

    proc = _run(staged)

    assert proc.returncode != 0
    assert "SHARED-VENV" not in proc.stdout
    assert "bin directory resolves outside its environment" in proc.stderr
    assert str(shared / "bin") in proc.stderr


def test_rejects_interpreter_symlink_with_foreign_prefix(staged: Path):
    """A local bin directory cannot borrow another venv's interpreter state."""
    shared = _fake_venv(staged.parent / "shared-venv", "SHARED-VENV")
    local = staged / ".venv"
    (local / "bin").mkdir(parents=True)
    (local / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    (local / "bin" / "python").symlink_to(shared / "bin" / "python")

    proc = _run(staged)

    assert proc.returncode != 0
    assert "SHARED-VENV" not in proc.stdout
    assert "interpreter reports a foreign sys.prefix" in proc.stderr
    assert str(shared) in proc.stderr


def test_accepts_interpreter_symlink_with_local_prefix(staged: Path):
    """Normal venv interpreter symlinks remain valid when sys.prefix is local."""
    local = staged / ".venv"
    (local / "bin").mkdir(parents=True)
    (local / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    base_python = _fake_python(staged.parent / "base-python", local, "SELECTED-SYMLINK")
    (local / "bin" / "python").symlink_to(base_python)

    proc = _run(staged)

    assert proc.returncode == 0, proc.stderr
    assert "SELECTED-SYMLINK" in proc.stdout


def test_selected_venv_overrides_hostile_path_and_virtual_env(staged: Path):
    """Child commands inherit the selected environment, not the caller's venv."""
    local = _fake_venv(staged / ".venv", "SELECTED-LOCAL")
    hostile = _fake_venv(staged.parent / "hostile-venv", "HOSTILE-VENV")
    sentinel_marker = staged.parent / "hostile-utility-ran.txt"
    for utility in ("dirname", "env", "mktemp", "cp", "chmod", "ps", "sleep", "rm"):
        sentinel = hostile / "bin" / utility
        sentinel.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' {shlex.quote(utility)} >> "
            f"{shlex.quote(str(sentinel_marker))}\n"
            "exit 97\n",
            encoding="utf-8",
        )
        sentinel.chmod(0o755)
    inherited_path = f"{hostile / 'bin'}:{os.environ['PATH']}"

    proc = _run(
        staged,
        {"PATH": inherited_path, "VIRTUAL_ENV": str(hostile)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "SELECTED-LOCAL" in proc.stdout
    assert "HOSTILE-VENV" not in proc.stdout
    observed_path = _observed_value(proc, "OBSERVED_PATH")
    assert observed_path == f"{local / 'bin'}:{_safe_system_path()}"
    assert str(hostile / "bin") not in observed_path.split(os.pathsep)
    assert f"OBSERVED_VIRTUAL_ENV={local}" in proc.stdout
    assert not sentinel_marker.exists()


def test_shell_passes_guard_source_without_import_paths(staged: Path):
    guard_source = _install_live_guard(staged)
    _fake_venv(staged / ".venv", "SELECTED-LOCAL")

    proc = _run(
        staged,
        {
            "PYTHONPATH": str(staged.parent / "hostile-pythonpath"),
            "PYTEST_PLUGINS": "hostile_plugin",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert _observed_value(proc, "OBSERVED_GUARD_SOURCE") == str(guard_source)
    assert _observed_value(proc, "OBSERVED_PYTHONPATH") == ""
    assert _observed_value(proc, "OBSERVED_PYTEST_PLUGINS") == ""


def test_precompile_is_credential_isolated_and_worker_bounded(staged: Path):
    """Compile startup gets no caller env and never exceeds four workers."""
    local = staged / ".venv"
    (local / "bin").mkdir(parents=True)
    (local / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    compile_log = staged.parent / "compile-env.txt"
    _fake_python(
        local / "bin" / "python",
        local,
        "SELECTED-LOCAL",
        compile_log=compile_log,
    )

    proc = _run(
        staged,
        {
            "PYTHONPATH": str(staged.parent / "hostile-pythonpath"),
            "OPENAI_API_KEY": "synthetic-test-value",
        },
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    observed = compile_log.read_text(encoding="utf-8")
    assert "ARGS=-S -m compileall -q -j 4 --" in observed
    assert "PYTHONPATH=\n" in observed
    assert "OPENAI_API_KEY=\n" in observed


def test_precompile_cannot_execute_hostile_pythonpath_sitecustomize(staged: Path):
    """Caller-controlled sitecustomize is inert during every wrapper phase."""
    local = staged / ".venv"
    (local / "bin").mkdir(parents=True)
    (local / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    _real_python_shim(local / "bin" / "python", local)

    marker = staged.parent / "sitecustomize-executed.txt"
    hostile = staged.parent / "hostile-pythonpath"
    hostile.mkdir()
    (hostile / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    tests_dir = staged / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_safe.py").write_text(
        "def test_safe():\n    assert True\n",
        encoding="utf-8",
    )

    proc = _run(staged, {"PYTHONPATH": str(hostile)})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not marker.exists()


def test_guard_uses_one_module_path_and_worktree_imports_win(staged: Path):
    """The optional guard cannot expose either sibling-source shadow route."""
    _install_live_guard(staged)
    local = staged / ".venv"
    (local / "bin").mkdir(parents=True)
    (local / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    _real_python_shim(local / "bin" / "python", local)

    (staged / "agent").mkdir()
    (staged / "agent" / "__init__.py").write_text(
        "SOURCE_MARKER = 'worktree'\n", encoding="utf-8"
    )
    sibling_root = staged.parent / "home" / ".hermes" / "hermes-agent"
    (sibling_root / "agent").mkdir()
    (sibling_root / "agent" / "__init__.py").write_text(
        "SOURCE_MARKER = 'sibling'\n", encoding="utf-8"
    )
    (staged.parent / "home" / ".hermes" / "sibling_source.py").write_text(
        "SOURCE_MARKER = 'whole-home'\n", encoding="utf-8"
    )

    tests_dir = staged / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_import_provenance.py").write_text(
        "import importlib\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import agent\n"
        "import pytest_live_guard\n"
        "def test_import_provenance():\n"
        "    root = Path.cwd().resolve()\n"
        "    sibling = (Path.home() / '.hermes' / 'hermes-agent').resolve()\n"
        "    whole_home = (Path.home() / '.hermes').resolve()\n"
        "    guard_dir = Path(pytest_live_guard.__file__).resolve().parent\n"
        "    assert agent.SOURCE_MARKER == 'worktree'\n"
        "    assert Path(agent.__file__).resolve().is_relative_to(root)\n"
        "    assert pytest_live_guard.GUARD_MARKER == 'isolated-guard'\n"
        "    assert os.environ['PYTHONPATH'] == str(guard_dir)\n"
        "    assert 'HERMES_PYTEST_LIVE_GUARD_SOURCE' not in os.environ\n"
        "    assert {p.name for p in guard_dir.iterdir()} == {'pytest_live_guard.py'}\n"
        "    resolved_sys_path = {Path(p).resolve() for p in sys.path}\n"
        "    assert sibling not in resolved_sys_path\n"
        "    assert whole_home not in resolved_sys_path\n"
        "    try:\n"
        "        importlib.import_module('sibling_source')\n"
        "    except ModuleNotFoundError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('whole-home sibling source was importable')\n"
        "    (root / 'guard-path.txt').write_text(str(guard_dir), encoding='utf-8')\n",
        encoding="utf-8",
    )
    (tests_dir / "test_guard_second_child.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import pytest_live_guard\n"
        "def test_guard_in_second_child():\n"
        "    guard_dir = Path(pytest_live_guard.__file__).resolve().parent\n"
        "    assert pytest_live_guard.GUARD_MARKER == 'isolated-guard'\n"
        "    assert os.environ['PYTHONPATH'] == str(guard_dir)\n"
        "    assert 'HERMES_PYTEST_LIVE_GUARD_SOURCE' not in os.environ\n"
        "    (Path.cwd() / 'guard-path-second.txt').write_text(\n"
        "        str(guard_dir), encoding='utf-8'\n"
        "    )\n",
        encoding="utf-8",
    )

    hostile_pythonpath = os.pathsep.join(
        [str(staged.parent / "home" / ".hermes"), str(sibling_root)]
    )
    proc = _run(staged, {"PYTHONPATH": hostile_pythonpath})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    guard_dir = Path((staged / "guard-path.txt").read_text(encoding="utf-8"))
    second_guard_dir = Path(
        (staged / "guard-path-second.txt").read_text(encoding="utf-8")
    )
    assert second_guard_dir == guard_dir
    assert not guard_dir.exists()


def test_tracked_anthropic_test_cannot_import_controlled_sibling(tmp_path: Path):
    """The former sys.path insertion must not reach a controlled fake HOME."""
    controlled_home = tmp_path / "controlled-home"
    sibling_agent = controlled_home / ".hermes" / "hermes-agent" / "agent"
    sibling_agent.mkdir(parents=True)
    (sibling_agent / "__init__.py").write_text("", encoding="utf-8")
    import_marker = tmp_path / "sibling-imported.txt"
    (sibling_agent / "anthropic_adapter.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
        "def _sanitize_replay_block(value): return value\n"
        "def _convert_content_part_to_anthropic(value): return value\n"
        "def _convert_assistant_message(value): return value\n",
        encoding="utf-8",
    )

    tracked_test = REPO_ROOT / "tests" / "agent" / "test_anthropic_output_field_leak.py"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTEST_PLUGINS", None)
    env.update(
        {
            "HOME": str(controlled_home),
            "HERMES_HOME": str(controlled_home / ".hermes"),
        }
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1], run_name='tracked_probe')",
            str(tracked_test),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not import_marker.exists(), "tracked test imported controlled sibling source"


def test_guard_temp_path_cleaned_after_nonzero(staged: Path):
    """The Python owner removes the private guard path after a red test file."""
    _install_live_guard(staged)
    local = staged / ".venv"
    (local / "bin").mkdir(parents=True)
    (local / "bin" / "activate").write_text("# fake\n", encoding="utf-8")
    _real_python_shim(local / "bin" / "python", local)
    tests_dir = staged / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_guard_failure.py").write_text(
        "from pathlib import Path\n"
        "import pytest_live_guard\n"
        "def test_failure():\n"
        "    guard_dir = Path(pytest_live_guard.__file__).resolve().parent\n"
        "    (Path.cwd() / 'failed-guard-path.txt').write_text(\n"
        "        str(guard_dir), encoding='utf-8'\n"
        "    )\n"
        "    assert False\n",
        encoding="utf-8",
    )

    proc = _run(staged)

    assert proc.returncode == 1
    guard_dir = Path(
        (staged / "failed-guard-path.txt").read_text(encoding="utf-8")
    )
    assert not guard_dir.exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(signal, "pthread_sigmask"),
    reason="native pending-signal guard lifecycle is POSIX-only",
)
@pytest.mark.parametrize("phase", ["mkdir", "copy"])
@pytest.mark.parametrize(
    ("sig", "expected_returncode"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130)],
)
def test_main_signal_during_guard_materialization(
    tmp_path: Path,
    phase: str,
    sig: signal.Signals,
    expected_returncode: int,
):
    """Actual main owns TERM/INT before creating any private guard path."""
    guard_source = tmp_path / "pytest_live_guard.py"
    guard_source.write_text("GUARD_MARKER = 'isolated-guard'\n", encoding="utf-8")
    temp_parent = tmp_path / "tmp"
    temp_parent.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "HERMES_PYTEST_LIVE_GUARD_SOURCE": str(guard_source),
            "TMPDIR": str(temp_parent),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", GUARD_RACE_HARNESS, phase, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )
    release = tmp_path / "release-guard"
    try:
        _wait_for_path(tmp_path / "guard-paused", proc)
        assert len(list(temp_parent.glob("hermes-pytest-live-guard.*"))) == 1
        os.kill(proc.pid, sig)
        release.write_text("release", encoding="utf-8")
        assert proc.wait(timeout=8) == expected_returncode, proc.stderr.read()
    finally:
        release.touch(exist_ok=True)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert not (tmp_path / "main-entered").exists()
    assert not list(temp_parent.glob("hermes-pytest-live-guard.*"))


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(signal, "pthread_sigmask"),
    reason="native pending-signal installation semantics are POSIX-only",
)
@pytest.mark.parametrize("phase", ["before-first", "between", "after-second"])
@pytest.mark.parametrize(
    ("sig", "expected_returncode"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130)],
)
def test_main_signal_during_handler_installation(
    tmp_path: Path,
    phase: str,
    sig: signal.Signals,
    expected_returncode: int,
):
    """Actual main owns pending TERM/INT across every handler-install phase."""
    guard_source = tmp_path / "pytest_live_guard.py"
    guard_source.write_text("GUARD_MARKER = 'isolated-guard'\n", encoding="utf-8")
    temp_parent = tmp_path / "tmp"
    temp_parent.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "HERMES_PYTEST_LIVE_GUARD_SOURCE": str(guard_source),
            "TMPDIR": str(temp_parent),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", INSTALL_RACE_HARNESS, phase, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )
    release = tmp_path / "release-install"
    try:
        _wait_for_path(tmp_path / "install-paused", proc)
        guard_dirs = list(temp_parent.glob("hermes-pytest-live-guard.*"))
        assert len(guard_dirs) == 1
        assert {path.name for path in guard_dirs[0].iterdir()} == {
            "pytest_live_guard.py"
        }
        os.kill(proc.pid, sig)
        release.write_text("release", encoding="utf-8")
        assert proc.wait(timeout=8) == expected_returncode, proc.stderr.read()
    finally:
        release.touch(exist_ok=True)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert not (tmp_path / "main-entered").exists()
    assert not list(temp_parent.glob("hermes-pytest-live-guard.*"))


def test_main_restores_handlers_after_partial_install(monkeypatch: pytest.MonkeyPatch):
    """A failed second installation cannot strand the first custom handler."""
    supports_masking = hasattr(parallel_runner.signal, "pthread_sigmask")
    original_handlers = {
        signal.SIGTERM: "original-term",
        signal.SIGINT: "original-int",
    }
    installed = dict(original_handlers)
    mask_calls: list[tuple[object, object]] = []

    def fake_getsignal(signum):
        return installed[signum]

    def fake_signal(signum, handler):
        if signum == signal.SIGINT and getattr(handler, "__name__", "") == "handle_signal":
            raise RuntimeError("synthetic second-handler failure")
        previous = installed[signum]
        installed[signum] = handler
        return previous

    def fake_pthread_sigmask(how, signals):
        mask_calls.append((how, signals))
        return frozenset()

    monkeypatch.delenv("HERMES_PYTEST_LIVE_GUARD_SOURCE", raising=False)
    monkeypatch.setattr(parallel_runner.signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(parallel_runner.signal, "signal", fake_signal)
    if supports_masking:
        monkeypatch.setattr(
            parallel_runner.signal, "pthread_sigmask", fake_pthread_sigmask
        )

    with pytest.raises(RuntimeError, match="synthetic second-handler failure"):
        parallel_runner.main()

    assert installed == original_handlers
    if supports_masking:
        assert [call[0] for call in mask_calls] == [signal.SIG_BLOCK, signal.SIG_SETMASK]


@pytest.mark.parametrize("phase", ["shutdown", "handler-restoration"])
@pytest.mark.parametrize(
    ("sig", "expected_returncode"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130)],
)
def test_main_normalizes_signal_during_teardown(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    sig: signal.Signals,
    expected_returncode: int,
):
    """A teardown interrupt returns conventionally instead of escaping main."""
    original_handlers = {
        signal.SIGTERM: "original-term",
        signal.SIGINT: "original-int",
    }
    installed = dict(original_handlers)
    injected = False
    real_request_shutdown = parallel_runner._ProcessRegistry.request_shutdown

    def fake_getsignal(signum):
        return installed[signum]

    def fake_signal(signum, handler):
        nonlocal injected
        previous = installed[signum]
        is_restoration = handler == original_handlers[signum]
        if phase == "handler-restoration" and is_restoration and not injected:
            injected = True
            installed[sig](sig, None)
        installed[signum] = handler
        return previous

    def controlled_shutdown(registry):
        nonlocal injected
        if phase == "shutdown" and not injected:
            injected = True
            installed[sig](sig, None)
        return real_request_shutdown(registry)

    monkeypatch.delenv("HERMES_PYTEST_LIVE_GUARD_SOURCE", raising=False)
    monkeypatch.setattr(parallel_runner.signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(parallel_runner.signal, "signal", fake_signal)
    monkeypatch.setattr(
        parallel_runner.signal,
        "pthread_sigmask",
        lambda _how, _signals: frozenset(),
        raising=False,
    )
    monkeypatch.setattr(
        parallel_runner._ProcessRegistry, "request_shutdown", controlled_shutdown
    )
    monkeypatch.setattr(parallel_runner, "_main", lambda _registry: 0)

    assert parallel_runner.main() == expected_returncode
    assert injected
    assert installed == original_handlers


def test_windows_registry_shutdown_uses_taskkill(monkeypatch: pytest.MonkeyPatch):
    """Mock the Windows tree lifecycle without claiming POSIX signal behavior."""

    class FakeProcess:
        pid = 4242

        def __init__(self):
            self.killed = 0

        def kill(self):
            self.killed += 1

    proc = FakeProcess()
    taskkill_calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        taskkill_calls.append(command)

    monkeypatch.setattr(parallel_runner.sys, "platform", "win32")
    monkeypatch.setattr(parallel_runner.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(parallel_runner.subprocess, "run", fake_run)
    registry = parallel_runner._ProcessRegistry()

    launched, pgid = registry.launch(["pytest"], start_new_session=True)
    assert launched is proc
    assert pgid is None
    registry.request_shutdown()

    assert taskkill_calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
    assert proc.killed == 1
    with pytest.raises(parallel_runner._ShutdownRequested):
        registry.launch(["late-child"])
    registry.finish(proc)
    assert len(taskkill_calls) == 2


@pytest.mark.skipif(os.name != "posix", reason="process-group signals are POSIX-only")
@pytest.mark.parametrize("phase", ["before", "during", "after"])
@pytest.mark.parametrize(
    ("sig", "expected_returncode"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130)],
)
def test_launch_gate_signal_cleanup(
    tmp_path: Path,
    phase: str,
    sig: signal.Signals,
    expected_returncode: int,
):
    """TERM/INT close the launch gate and clean every published group."""
    guard_source = tmp_path / "pytest_live_guard.py"
    guard_source.write_text("GUARD_MARKER = 'isolated-guard'\n", encoding="utf-8")
    temp_parent = tmp_path / "tmp"
    temp_parent.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "HERMES_PYTEST_LIVE_GUARD_SOURCE": str(guard_source),
            "TMPDIR": str(temp_parent),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", LAUNCH_GATE_HARNESS, phase, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )
    pgid = 0
    try:
        gate = {
            "before": tmp_path / "ready",
            "during": tmp_path / "publication-open",
            "after": tmp_path / "published",
        }[phase]
        _wait_for_path(gate, proc)
        os.kill(proc.pid, sig)
        _wait_for_path(tmp_path / "shutdown-observed", proc)
        if phase == "during":
            (tmp_path / "release-publication").write_text(
                "release", encoding="utf-8"
            )
        assert proc.wait(timeout=8) == expected_returncode, proc.stderr.read()

        if phase != "before":
            pgid_path = tmp_path / ("pgid" if phase == "during" else "published")
            _wait_for_path(pgid_path)
            pgid = int(pgid_path.read_text(encoding="utf-8").splitlines()[-1])
            deadline = time.monotonic() + 2
            while _pgid_exists(pgid) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not _pgid_exists(pgid), f"registered PGID survived: {pgid}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if pgid and _pgid_exists(pgid):
            _force_kill_group(pgid)

    assert (tmp_path / "post-rejected").exists()
    assert not (tmp_path / "post-started").exists()
    if phase == "before":
        assert not (tmp_path / "child-started").exists()
    guard_dir = Path((tmp_path / "guard-dir").read_text(encoding="utf-8"))
    assert guard_dir.parent == temp_parent
    assert not guard_dir.exists()
    assert not list(temp_parent.glob("hermes-pytest-live-guard.*"))


@pytest.mark.parametrize("name", [".venv", "venv"])
def test_selects_each_repo_local_candidate(staged: Path, name: str):
    """Both repo-local candidates are still honoured (this is not a lockout)."""
    _fake_venv(staged / name, f"SELECTED-{name}")

    proc = _run(staged)

    assert f"SELECTED-{name}" in proc.stdout, proc.stderr
    assert "DECOY-RUNTIME-VENV" not in proc.stdout


def test_dot_venv_wins_over_venv(staged: Path):
    """Probe order is load-bearing: `.venv` before `venv`."""
    _fake_venv(staged / ".venv", "SELECTED-dotvenv")
    _fake_venv(staged / "venv", "SELECTED-venv")

    proc = _run(staged)

    assert "SELECTED-dotvenv" in proc.stdout, proc.stderr
    assert "SELECTED-venv" not in proc.stdout


def test_reports_which_venv_it_selected(staged: Path):
    """The chosen interpreter is echoed — silence is what hid CLAWD-3136."""
    _fake_venv(staged / ".venv", "SELECTED-dotvenv")

    proc = _run(staged)

    assert f"venv: {staged / '.venv'}" in proc.stdout
