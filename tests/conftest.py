import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bash_server  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_session(monkeypatch, tmp_path_factory):
    """Each test starts in WORKDIR and writes its own audit log."""
    audit_log = tmp_path_factory.mktemp("audit") / "audit.jsonl"
    monkeypatch.setattr(bash_server, "_cwd", None)
    monkeypatch.setattr(bash_server, "AUDIT_LOG", audit_log)
    # Also for servers started in a subprocess.
    monkeypatch.setenv("BASH_AUDIT_LOG", str(audit_log))
