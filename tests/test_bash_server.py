import asyncio
import importlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client

import bash_server


def run(command, **kwargs):
    return asyncio.run(bash_server.bash(command, **kwargs))


def test_returns_stdout_stderr_and_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)

    result = run("pwd; echo oops >&2; exit 3")

    assert result["stdout"].strip() == str(tmp_path.resolve())
    assert result["stderr"].strip() == "oops"
    assert result["exit_code"] == 3
    assert result["success"] is False


def test_timeout_kills_the_whole_process_group(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)
    marker = tmp_path / "child-finished"

    started = time.monotonic()
    result = run(f"(sleep 3; touch {marker}) & sleep 30", timeout_seconds=1)

    assert result["timed_out"] is True
    assert time.monotonic() - started < 5
    time.sleep(3.5)
    assert not marker.exists()


def test_long_output_is_truncated(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)

    limit = bash_server.MAX_OUTPUT_CHARS
    result = run(f"echo START; head -c {limit} /dev/zero | tr '\\0' a; echo END")

    assert result["truncated"] is True
    assert result["stdout"].startswith("START")
    assert "[output truncated]" in result["stdout"]
    assert result["stdout"].rstrip().endswith("END")


def test_huge_output_keeps_its_end(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)

    result = run("echo START; head -c 5000000 /dev/zero | tr '\\0' a; echo END")

    assert result["stdout"].startswith("START")
    assert result["stdout"].rstrip().endswith("END")
    assert len(result["stdout"]) < bash_server.MAX_OUTPUT_CHARS + 100


def test_short_output_is_kept_whole(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)

    # 18,000 characters but 50,000 bytes: spans the head and tail buffers.
    result = run("printf 'ữữữữữữữữ\\n%.0s' $(seq 2000)")

    assert result["truncated"] is False
    assert result["stdout"] == "ữữữữữữữữ\n" * 2000


def test_stdin_is_closed_so_prompts_do_not_hang(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)

    result = run("read -r answer; echo got:$answer", timeout_seconds=5)

    assert result["timed_out"] is False
    assert result["stdout"].strip() == "got:"


def test_tool_schema_describes_every_parameter():
    async def list_tools():
        async with Client(bash_server.mcp) as client:
            return await client.list_tools()

    (tool,) = asyncio.run(list_tools())
    assert tool.name == "bash"
    assert tool.annotations.destructive_hint is True
    for name, schema in tool.input_schema["properties"].items():
        assert schema.get("description"), name


def _listed_tool(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    # Reloading reads the env again; reload once more so other tests see defaults.
    mcp = importlib.reload(bash_server).mcp
    monkeypatch.undo()
    importlib.reload(bash_server)

    async def list_tools():
        async with Client(mcp) as client:
            return await client.list_tools()

    (tool,) = asyncio.run(list_tools())
    return tool


def test_tool_is_offered_only_in_the_apps_by_default(monkeypatch):
    monkeypatch.delenv("BASH_SURFACES", raising=False)

    tool = _listed_tool(monkeypatch)

    assert tool.meta["assistant_platform"] == {"surfaces": ["app"]}


def test_tool_name_and_surfaces_are_configurable(monkeypatch):
    tool = _listed_tool(
        monkeypatch, BASH_TOOL_NAME="web01_bash", BASH_SURFACES="app, api"
    )

    assert tool.name == "web01_bash"
    assert tool.meta["assistant_platform"] == {"surfaces": ["app", "api"]}


def test_invalid_tool_name_is_rejected(monkeypatch):
    monkeypatch.setenv("BASH_TOOL_NAME", "web 01.bash")
    try:
        with pytest.raises(SystemExit):
            importlib.reload(bash_server)
    finally:
        monkeypatch.undo()
        importlib.reload(bash_server)


def test_commands_do_not_see_server_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)
    monkeypatch.setenv("MCP_ENDPOINT", "wss://x/mcp/?token=mcpe_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")

    result = run("env")

    assert "mcpe_secret" not in result["stdout"]
    assert "aws-secret" not in result["stdout"]
    assert "PATH=" in result["stdout"]


def test_bash_pass_env_adds_named_variables(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("BASH_PASS_ENV", "GITHUB_TOKEN, MISSING_VAR")

    assert run("echo $GITHUB_TOKEN")["stdout"].strip() == "gh-token"


def test_huge_output_keeps_memory_bounded(tmp_path):
    # ru_maxrss is per process, so measure in a fresh interpreter.
    here = str(Path(bash_server.__file__).parent)
    script = (
        f"import asyncio, resource, sys; sys.path.insert(0, {here!r}); "
        "import bash_server as b; "
        "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss; "
        "r = asyncio.run(b.bash('head -c 300000000 /dev/zero', timeout_seconds=60)); "
        "grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before; "
        "print(r['truncated'], grown)"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout.split()
    truncated, grown = out[0] == "True", int(out[1])
    # ru_maxrss is bytes on macOS and KiB on Linux; 50 MB is generous for both.
    limit = 50 * 1024 * 1024 if sys.platform == "darwin" else 50 * 1024
    assert truncated
    assert grown < limit


def test_background_job_does_not_hold_the_result(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)

    started = time.monotonic()
    result = run("sleep 5 & echo started", timeout_seconds=30)

    assert result["stdout"].strip() == "started"
    assert time.monotonic() - started < 3


def test_escaped_child_cannot_hang_a_timed_out_command(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)
    escape = f'{sys.executable} -c "import os,time; os.setsid(); time.sleep(6)"'

    started = time.monotonic()
    result = run(f"{escape} & sleep 30", timeout_seconds=1)

    assert result["timed_out"] is True
    assert time.monotonic() - started < 4


def test_concurrent_commands_are_limited(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)

    async def three_at_once():
        return await asyncio.gather(*(bash_server.bash("sleep 1") for _ in range(3)))

    results = asyncio.run(three_at_once())

    assert sum(r.get("exit_code") == 0 for r in results) == 2
    assert sum("already running" in r.get("error", "") for r in results) == 1


def test_working_directory_carries_over_but_env_does_not(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)
    sub = (tmp_path / "sub").resolve()

    first = run("mkdir sub && cd sub && export FOO=1; exit 3")
    second = run("pwd; echo ${FOO:-unset}")

    assert first["cwd"] == str(sub)
    assert second["stdout"].split() == [str(sub), "unset"]


def test_restart_goes_back_to_the_initial_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)
    run("mkdir sub && cd sub")

    result = run("pwd", restart=True)

    assert result["stdout"].strip() == str(tmp_path.resolve())


def test_deleted_directory_falls_back_to_the_initial_one(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)
    run("mkdir sub && cd sub")
    (tmp_path / "sub").rmdir()

    assert run("pwd")["stdout"].strip() == str(tmp_path.resolve())


def test_timed_out_command_keeps_the_previous_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)
    run("mkdir sub && cd sub")

    timed_out = run("cd /; sleep 5", timeout_seconds=1)

    assert timed_out["cwd"] == str((tmp_path / "sub").resolve())
    assert run("pwd")["stdout"].strip() == timed_out["cwd"]


def test_every_command_is_audited(monkeypatch, tmp_path):
    monkeypatch.setattr(bash_server, "WORKDIR", tmp_path)

    run("echo hi; exit 2")

    (line,) = bash_server.AUDIT_LOG.read_text().splitlines()
    entry = json.loads(line)
    assert entry["command"] == "echo hi; exit 2"
    assert entry["exit_code"] == 2
    assert entry["cwd"] == str(tmp_path)
    assert bash_server.AUDIT_LOG.stat().st_mode & 0o777 == 0o600
