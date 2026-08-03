"""BEHAVIOURAL companion to ``tests/gateway/test_gateway_utf8_encoding.py``.

That file is a *lint*: it walks the AST and asserts every ``read_text`` /
``write_text`` under ``gateway/`` carries an ``encoding=`` kwarg.  It proves the
kwarg is present.  It cannot prove the kwarg *does anything* — a reader could
carry ``encoding="utf-8"`` on a file nothing ever writes non-ASCII into, and the
lint would look identical.

The two markers read at ``gateway/run.py:1691`` and ``:1723`` are written by
``utils.atomic_json_write``, which is ``ensure_ascii=False`` + ``encoding="utf-8"``
(``utils.py:215-222``).  So the *writer* emits raw UTF-8 multibyte bytes whenever
a Telegram/Discord chat id, thread key or target label contains a non-ASCII
character — which is the normal case for the fleet's non-English home channels.
A reader that fell back to the process locale therefore had a real,
silent failure mode on any host whose locale is not UTF-8:

  * ``_read_recovery_marker()``  -> ``None``  (the "gateway online" recovery
    message is never sent after a restart)
  * ``_read_pinned_status()``    -> ``{}``    (the pinned status message id is
    lost, so the gateway pins a NEW message instead of editing the old one)

Both are swallowed by the surrounding ``except Exception``, so nothing is logged.

This test drives the real writer and the real reader in a child interpreter
forced onto an ASCII locale, and asserts the round-trip survives.

CONTROL (load-bearing): the child reports the encoding it actually resolved.  If
the harness failed to establish a non-UTF-8 locale the assertion below would be
vacuous, so that case FAILS the test rather than passing it quietly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest


_CHILD = textwrap.dedent(
    """
    import json, locale, os, pathlib, sys

    home = pathlib.Path(sys.argv[1])
    os.environ["HERMES_HOME"] = str(home)

    import gateway.run as gr
    gr._hermes_home = home

    # Non-ASCII home-channel identifiers, written through the REAL producer.
    gr._write_recovery_marker(
        interrupted=1,
        targets=[{"platform": "telegram", "chat_id": "caf\\u00e9-\\u03a9"}],
    )
    gr._write_pinned_status({"telegram:caf\\u00e9:": 4242})

    marker_bytes = (home / gr._GATEWAY_RECOVERY_MARKER).read_bytes()

    result = {
        "preferred_encoding": locale.getpreferredencoding(False),
        "utf8_mode": sys.flags.utf8_mode,
        "marker_has_non_ascii_bytes": any(b > 127 for b in marker_bytes),
        "recovery": gr._read_recovery_marker(),
        "pinned": gr._read_pinned_status(),
    }
    # stdout was reconfigured to utf-8 during import; write the report on fd 3
    # equivalent (a file) so no encoding of ours is part of what is measured.
    pathlib.Path(sys.argv[2]).write_text(json.dumps(result), encoding="utf-8")
    """
)


def _repo_root():
    import pathlib

    return pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="forcing a non-UTF-8 locale portably is a Linux-only lever here",
)
def test_markers_round_trip_when_the_process_locale_is_not_utf8(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    report = tmp_path / "report.json"

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        # Measure THIS checkout's gateway/run.py, not whatever copy of
        # hermes-agent happens to be importable in the child.
        "PYTHONPATH": str(_repo_root()),
        # PEP 540 UTF-8 mode auto-enables under LC_ALL=C, and PEP 538 coerces
        # the C locale to C.UTF-8.  Both have to be switched off or the child
        # silently lands back on UTF-8 and the control below trips.
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    proc = subprocess.run(
        [sys.executable, str(script), str(home), str(report)],
        cwd=str(_repo_root()),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"child interpreter failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    data = json.loads(report.read_text(encoding="utf-8"))

    # ── CONTROL ─────────────────────────────────────────────────────────────
    # Without these two the assertions underneath prove nothing: on a UTF-8
    # locale a bare read_text() succeeds too, and on an ASCII-only marker there
    # is no byte for either codec to disagree about.
    assert data["utf8_mode"] == 0, (
        "harness control failed: the child still ran in PEP 540 UTF-8 mode, so "
        "this test could not have distinguished a bare read_text() from an "
        f"encoding-pinned one. Child report: {data!r}"
    )
    assert "utf" not in data["preferred_encoding"].lower(), (
        "harness control failed: the child resolved a UTF-8 preferred encoding "
        f"({data['preferred_encoding']!r}); the locale lever did not take."
    )
    assert data["marker_has_non_ascii_bytes"], (
        "harness control failed: the marker on disk is pure ASCII, so no "
        "encoding difference is observable. atomic_json_write is supposed to "
        "be ensure_ascii=False — check utils.atomic_json_write."
    )

    # ── The property ────────────────────────────────────────────────────────
    assert data["recovery"] == {
        "interrupted": 1,
        "targets": [{"platform": "telegram", "chat_id": "café-Ω"}],
    }, (
        "gateway/run.py:_read_recovery_marker lost a marker written by its own "
        "writer. A bare .read_text() returns None here (UnicodeDecodeError is "
        f"swallowed by the except Exception), suppressing the recovery "
        f"notification after every restart. Got: {data['recovery']!r}"
    )
    assert data["pinned"] == {"telegram:café:": 4242}, (
        "gateway/run.py:_read_pinned_status lost the pinned message id map. A "
        "bare .read_text() returns {} here, so the gateway re-pins a new status "
        f"message instead of editing the existing one. Got: {data['pinned']!r}"
    )
