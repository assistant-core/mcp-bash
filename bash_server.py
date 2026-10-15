"""MCP server with one tool, `bash`, that runs commands on this machine.

Anyone who can chat with the connected assistant can run commands as the
user running this server. Use it only with assistants you trust, under a
low-privilege account. BASH_WORKDIR is the starting directory, not a
sandbox: commands can still `cd` anywhere the user can.
"""

import asyncio
import logging
import os
import shutil
import signal
import sys
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
# Bytes kept per stream; output past this is read and discarded so memory
# stays bounded however much a command prints.
MAX_OUTPUT_BYTES = MAX_OUTPUT_CHARS * 4
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

mcp = FastMCP("Bash")
_running = 0


def command_env() -> dict[str, str]:
    """Environment for commands: an allowlist, extended by BASH_PASS_ENV."""
    extra = os.environ.get("BASH_PASS_ENV", "").split(",")
    names = {*BASE_ENV, *(name.strip() for name in extra if name.strip())}
    return {name: os.environ[name] for name in names if name in os.environ}


class _Capture:
    """Keeps the first MAX_OUTPUT_BYTES of a stream and drains the rest."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.truncated = False

    async def read(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            room = MAX_OUTPUT_BYTES - len(self.data)
            if len(chunk) > room:
                self.truncated = True
            self.data.extend(chunk[: max(room, 0)])

    def text(self) -> str:
        text = self.data.decode("utf-8", errors="replace")
        if len(text) > MAX_OUTPUT_CHARS:
            text, self.truncated = text[:MAX_OUTPUT_CHARS], True
        return text + "\n…[output truncated]" if self.truncated else text


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


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


@mcp.tool(
    annotations=ToolAnnotations(
        title="Run bash command",
        destructive_hint=True,
        open_world_hint=True,
    )
)
async def bash(
    command: Annotated[
        str,
        "Bash command to run, for example `ls -la` or `git status`. Runs "
        "non-interactively; commands that read input get end-of-file. "
        "Redirect the output of background jobs (`cmd > log 2>&1 &`).",
    ],
    timeout_seconds: Annotated[
        int,
        "Seconds to wait before stopping the command, from 1 to 120.",
    ] = 30,
) -> dict:
    """Run a bash command on this machine and return its output.

    The command starts in the configured working directory. stdout and stderr
    are returned separately, each truncated to a fixed length. Background jobs
    keep running after the command returns.
    """
    global _running
    bash_path = shutil.which("bash")
    if bash_path is None:
        return {"success": False, "error": "bash is not available on this machine"}

    if _running >= MAX_CONCURRENT_COMMANDS:
        return {
            "success": False,
            "error": f"{_running} commands are already running; try again later",
        }
    _running += 1
    try:
        return await _run(bash_path, command, timeout_seconds)
    finally:
        _running -= 1


async def _run(bash_path: str, command: str, timeout_seconds: int) -> dict:
    timeout = max(1, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
    logger.info("Running command (timeout %ss): %s", timeout, command)
    process = await asyncio.create_subprocess_exec(
        bash_path,
        "-c",
        command,
        cwd=WORKDIR,
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

    return {
        "success": not timed_out and process.returncode == 0,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "truncated": stdout.truncated or stderr.truncated,
        "cwd": str(WORKDIR),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
