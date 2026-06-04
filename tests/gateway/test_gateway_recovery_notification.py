"""Tests for the external-restart "gateway online" recovery marker (CLAWD-1019).

The in-band ``/restart`` command writes ``.restart_notify.json`` so the next boot
announces "gateway online". External restarts (systemd / SIGTERM /
``gateway run --replace``) leave no such marker, so fleet recoveries used to go
silent. The shutdown path now writes ``.gateway_recovery_notify.json`` when it
interrupts in-flight work; the next boot reads it to send the same home-channel
notification, then clears it so the message fires exactly once per restart.
"""

import json

import gateway.run as gateway_run


def test_recovery_marker_absent_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    assert gateway_run._recovery_notification_pending() is False


def test_write_recovery_marker_creates_pending_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    gateway_run._write_recovery_marker(3)

    marker = tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER
    assert marker.exists()
    assert json.loads(marker.read_text())["interrupted"] == 3
    assert gateway_run._recovery_notification_pending() is True


def test_clear_recovery_marker_fires_once(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    gateway_run._write_recovery_marker(1)
    assert gateway_run._recovery_notification_pending() is True

    gateway_run._clear_recovery_marker()
    assert gateway_run._recovery_notification_pending() is False

    # Idempotent: clearing an already-cleared marker must not raise.
    gateway_run._clear_recovery_marker()
    assert gateway_run._recovery_notification_pending() is False


def test_write_recovery_marker_never_raises(tmp_path, monkeypatch):
    # A write failure must be swallowed: shutdown must never be blocked by
    # notification I/O. (atomic_json_write itself creates parent dirs, so force
    # the failure by making the writer raise.)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(gateway_run, "atomic_json_write", _boom)
    gateway_run._write_recovery_marker(2)  # must not raise
    assert gateway_run._recovery_notification_pending() is False


# ── CLAWD-1144: extended marker (targets + ts) + _read_recovery_marker ──────


def test_read_recovery_marker_absent_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    assert gateway_run._read_recovery_marker() is None


def test_read_recovery_marker_round_trips_legacy_shape(tmp_path, monkeypatch):
    """A pre-CLAWD-1144 marker ({"interrupted": N}) reads back as a dict with no
    targets/ts so callers fall back to a fresh send."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    gateway_run._write_recovery_marker(4)

    marker = gateway_run._read_recovery_marker()
    assert marker == {"interrupted": 4}
    assert "targets" not in marker
    assert "ts" not in marker


def test_read_recovery_marker_round_trips_targets_and_ts(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    targets = [
        {"platform": "telegram", "chat_id": "home-1", "thread_id": None, "message_id": "m1"}
    ]
    gateway_run._write_recovery_marker(2, targets=targets, shutdown_ts=1717000000.0)

    marker = gateway_run._read_recovery_marker()
    assert marker["interrupted"] == 2
    assert marker["targets"] == targets
    assert marker["ts"] == 1717000000.0


def test_read_recovery_marker_corrupt_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).write_text("}{ not json")
    assert gateway_run._read_recovery_marker() is None


def test_read_recovery_marker_non_dict_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / gateway_run._GATEWAY_RECOVERY_MARKER).write_text('"just-a-string"')
    assert gateway_run._read_recovery_marker() is None
