"""MCP server with one tool, `bash`, that runs commands on this machine.

Anyone who can chat with the connected assistant can run commands as the
user running this server. Use it only with assistants you trust, under a
low-privilege account. BASH_WORKDIR is the starting directory, not a
sandbox: commands can still `cd` anywhere the user can.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

logger = logging.getLogger("Bash")

# Fix UTF-8 encoding for Windows console
if sys.platform == "win32":
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

MAX_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 20_000
# Long output keeps its start and its end, where errors usually are. The
# middle is read and discarded so memory stays bounded however much a
# command prints.
HEAD_CHARS = 8_000
TAIL_CHARS = MAX_OUTPUT_CHARS - HEAD_CHARS
HEAD_BYTES = HEAD_CHARS * 4
TAIL_BYTES = TAIL_CHARS * 4
MAX_CONCURRENT_COMMANDS = 2
# Background jobs can hold the output pipes open after the command exits;
# stop reading this long after it does.
PIPE_GRACE_SECONDS = 1.0
# Commands see only these variables (plus BASH_PASS_ENV), so secrets in the
# server's environment, such as the Connector URL, never reach the assistant.
BASE_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
)
WORKDIR = Path(os.environ.get("BASH_WORKDIR") or Path.home()).expanduser()
# One JSON line per command, so the owner can review what the assistant ran.
AUDIT_LOG = Path(
    os.environ.get("BASH_AUDIT_LOG") or Path(__file__).with_name("audit.jsonl")
).expanduser()
# Machines connected to one assistant need different tool names (web01_bash,
# db01_bash): tools with the same name collide, and the model could not tell
# the machines apart.
TOOL_NAME = os.environ.get("BASH_TOOL_NAME") or "bash"
if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", TOOL_NAME):
    sys.exit("BASH_TOOL_NAME must be 1-64 letters, digits, '_' or '-'.")
# Assistant Core surfaces the tool is offered on. "app" (the web and mobile
# apps) keeps it away from website widget visitors, messaging apps, voice
# devices and the public API, where anyone the assistant talks to could run
# commands.
SURFACES = [
    s.strip()
    for s in (os.environ.get("BASH_SURFACES") or "app").split(",")
    if s.strip()
]

mcp = FastMCP("Bash")
_running = 0
# Where the last command ended; the next one starts there, like a terminal.
# None means WORKDIR.
_cwd: Path | None = None


def command_env() -> dict[str, str]:
    """Environment for commands: an allowlist, extended by BASH_PASS_ENV."""
    extra = os.environ.get("BASH_PASS_ENV", "").split(",")
    names = {*BASE_ENV, *(name.strip() for name in extra if name.strip())}
    return {name: os.environ[name] for name in names if name in os.environ}


class _Capture:
    """Keeps the start and the end of a stream and drains the middle."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = False
        self.truncated = False

    async def read(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            room = HEAD_BYTES - len(self.head)
            self.head.extend(chunk[:room])
            self.tail.extend(chunk[room:])
            if len(self.tail) > TAIL_BYTES:
                del self.tail[:-TAIL_BYTES]
                self.dropped = True

    def text(self) -> str:
        if self.dropped:
            head = self.head.decode("utf-8", errors="replace")[:HEAD_CHARS]
            tail = self.tail.decode("utf-8", errors="replace")[-TAIL_CHARS:]
        else:
            text = (self.head + self.tail).decode("utf-8", errors="replace")
            if len(text) <= MAX_OUTPUT_CHARS:
                return text
            head, tail = text[:HEAD_CHARS], text[-TAIL_CHARS:]
        self.truncated = True
        return f"{head}\n…[output truncated]…\n{tail}"


async def _wait_exit(process: asyncio.subprocess.Process, timeout: float) -> bool:
    """Wait for bash itself to exit; return False on timeout.

    Process.wait() also waits for every holder of the output pipes, so a
    background job would block it. The return code is set as soon as bash
    exits.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while process.returncode is None:
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _audit(command: str, cwd: Path, result: dict, seconds: float) -> None:
    entry = {
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": command,
        "cwd": str(cwd),
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "seconds": round(seconds, 2),
    }
    try:
        # Owner-only: commands can contain secrets.
        fd = os.open(AUDIT_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Cannot write audit log %s: %s", AUDIT_LOG, exc)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


@mcp.tool(
    name=TOOL_NAME,
    # shell_argument: Assistant Core matches approval rules ("always allow
    # git status") against each command of this argument's command line.
    meta={"assistant_platform": {"surfaces": SURFACES, "shell_argument": "command"}},
    annotations=ToolAnnotations(
        title="Run bash command",
        destructive_hint=True,
        open_world_hint=True,
    ),
)
async def bash(
    command: Annotated[
        str,
        "Bash command to run, for example `ls -la` or `git status`. The "
        "working directory carries over to the next call, like a terminal; "
        "environment variables, aliases and functions do not (`export` "
        "lasts one call). Runs non-interactively; commands that read input "
        "get end-of-file. Redirect the output of background jobs "
        "(`cmd > log 2>&1 &`).",
    ],
    timeout_seconds: Annotated[
        int,
        "Seconds to wait before stopping the command, from 1 to 120.",
    ] = 30,
    restart: Annotated[
        bool,
        "Run the command from the initial working directory instead of "
        "where the last command ended.",
    ] = False,
) -> dict:
    """Run a bash command on this machine and return its output.

    Each call is a new shell that starts where the last command ended; `cwd`
    in the result is where this one ended. stdout and stderr are returned
    separately; long output keeps its start and end. Background jobs keep
    running after the command returns.
    """
    global _running, _cwd
    bash_path = shutil.which("bash")
    if bash_path is None:
        return {"success": False, "error": "bash is not available on this machine"}

    if _running >= MAX_CONCURRENT_COMMANDS:
        return {
            "success": False,
            "error": f"{_running} commands are already running; try again later",
        }
    if restart or _cwd is None or not _cwd.is_dir():
        _cwd = WORKDIR
    _running += 1
    try:
        result = await _run(bash_path, command, timeout_seconds, _cwd)
    finally:
        _running -= 1
    _cwd = Path(result["cwd"])
    return result


async def _run(bash_path: str, command: str, timeout_seconds: int, cwd: Path) -> dict:
    timeout = max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
    logger.info("Running command (timeout %ss): %s", timeout, command)
    fd, cwd_file = tempfile.mkstemp(prefix="mcp-bash-cwd-")
    os.close(fd)
    # Record where the command ends, even when it calls `exit`. A killed
    # command records nothing and keeps the directory it started in.
    trap = f"trap {shlex.quote('pwd > ' + shlex.quote(cwd_file))} EXIT; "
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        bash_path,
        "-c",
        trap + command,
        cwd=cwd,
        env=command_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own process group so a timeout also stops the command's children.
        start_new_session=True,
    )
    stdout, stderr = _Capture(), _Capture()
    readers = [
        asyncio.create_task(stdout.read(process.stdout)),
        asyncio.create_task(stderr.read(process.stderr)),
    ]
    timed_out = not await _wait_exit(process, timeout)
    if timed_out:
        _kill_group(process)
        await _wait_exit(process, 5)
    _, pending = await asyncio.wait(readers, timeout=PIPE_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    # Release pipes still held by background jobs; asyncio has no public API.
    process._transport.close()
    ended_in = Path(cwd_file).read_text().strip()
    os.unlink(cwd_file)

    result = {
        "success": not timed_out and process.returncode == 0,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "truncated": stdout.truncated or stderr.truncated,
        "cwd": ended_in or str(cwd),
    }
    _audit(command, cwd, result, time.monotonic() - started)
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
