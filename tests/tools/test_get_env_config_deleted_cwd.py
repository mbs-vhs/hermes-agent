"""Independent verification for CLAWD-1702.

``os.getcwd()`` raises ``FileNotFoundError`` when the process's current working
directory has been deleted out from under it (issue #17558). Before the fix,
``terminal_tool._get_env_config`` called ``os.getcwd()`` directly for the
``local`` backend, so that exception cascaded and wedged every subsequent
terminal/file-tool call until the gateway restarted.

The fix adds ``_safe_getcwd()`` which falls back to ``tempfile.gettempdir()``.
These tests reproduce the wedge two ways:

  * a real repro — actually ``chdir`` into a directory that is then ``rmtree``'d,
    then call ``_get_env_config()`` and confirm it returns instead of raising;
  * a monkeypatched unit test on ``_safe_getcwd`` itself.
"""

import os
import shutil
import tempfile

import pytest

import tools.terminal_tool as terminal_tool
from tools.terminal_tool import _get_env_config

# NOTE: _safe_getcwd is imported lazily inside the tests that need it so that
# the behavioral _get_env_config tests below still collect (and demonstrate the
# raw FileNotFoundError) even when the fix is reverted and the helper is absent.


class TestSafeGetcwd:
    def test_returns_real_cwd_when_present(self, tmp_path, monkeypatch):
        from tools.terminal_tool import _safe_getcwd

        monkeypatch.chdir(tmp_path)
        # os.path.realpath because macOS /var -> /private/var etc.; on Linux
        # this is a no-op. tmp_path is a real dir, so getcwd must return it.
        assert os.path.realpath(_safe_getcwd()) == os.path.realpath(str(tmp_path))

    def test_falls_back_to_tempdir_on_filenotfound(self, monkeypatch, caplog):
        from tools.terminal_tool import _safe_getcwd

        def _boom():
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(os, "getcwd", _boom)
        result = _safe_getcwd()
        # v0.18 adopted upstream's terminal_tool: the deleted-cwd fallback is
        # TERMINAL_CWD or ~ (the fork used tempfile.gettempdir()). Either way the
        # invariant that matters is a usable, existing directory — no crash.
        assert result and os.path.isdir(result)


class TestGetEnvConfigDeletedCwd:
    def test_local_backend_does_not_raise_when_cwd_deleted(self, monkeypatch):
        """The real wedge: local backend + deleted cwd. _get_env_config must
        return a config falling back to the tempdir, not raise FileNotFoundError."""
        # Force the local backend (default) and clear TERMINAL_CWD so the
        # returned cwd flows straight from the _safe_getcwd() fallback.
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        # A directory we will chdir into and then delete out from under us.
        doomed = tempfile.mkdtemp(prefix="clawd1702-doomed-")
        safe_restore = tempfile.gettempdir()
        original_cwd = safe_restore
        try:
            original_cwd = os.getcwd()
        except FileNotFoundError:
            pass

        try:
            os.chdir(doomed)
            shutil.rmtree(doomed)
            # Sanity: os.getcwd() itself now raises — this is the exact wedge.
            with pytest.raises(FileNotFoundError):
                os.getcwd()

            # The fix must swallow that and return a usable config with a valid
            # fallback cwd (v0.18: upstream falls back to TERMINAL_CWD or ~).
            cfg = _get_env_config()
            assert cfg["env_type"] == "local"
            assert cfg["cwd"] and os.path.isdir(cfg["cwd"])
        finally:
            # Restore a valid cwd for the rest of this process's tests.
            os.chdir(original_cwd if os.path.isdir(original_cwd) else safe_restore)

    def test_docker_mount_cwd_does_not_raise_when_cwd_deleted(self, monkeypatch):
        """The second guarded call: docker backend with cwd passthrough also
        routes through _safe_getcwd when TERMINAL_CWD is unset."""
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        doomed = tempfile.mkdtemp(prefix="clawd1702-doomed-docker-")
        safe_restore = tempfile.gettempdir()
        original_cwd = safe_restore
        try:
            original_cwd = os.getcwd()
        except FileNotFoundError:
            pass

        try:
            os.chdir(doomed)
            shutil.rmtree(doomed)
            with pytest.raises(FileNotFoundError):
                os.getcwd()

            # Must not raise FileNotFoundError.
            cfg = _get_env_config()
            assert cfg["env_type"] == "docker"
        finally:
            os.chdir(original_cwd if os.path.isdir(original_cwd) else safe_restore)
