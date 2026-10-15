"""Connect a local stdio MCP server to an MCP Endpoint over WebSocket.

Equivalent to Xiaozhi's mcp_pipe.py: it uses the same MCP_ENDPOINT URL.

The connector dials out, so the machine needs no public URL or open port.

Usage:
    export MCP_ENDPOINT="wss://.../mcp_endpoint/mcp/?token=..."
    python connector.py [server.py]   # defaults to bash_server.py
"""

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import websockets
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("connector")

INITIAL_BACKOFF = 1
MAX_BACKOFF = 600
# A session that stayed up this long was healthy; start backoff over.
STABLE_SESSION_SECONDS = 60
# MCP JSON-RPC lines can be large (tool output); asyncio's default is 64 KiB.
STREAM_LIMIT = 4 * 1024 * 1024


def redact(uri: str) -> str:
    """Hide the token so the endpoint URL can be logged."""
    return re.sub(r"([?&]token=)[^&]+", r"\1***", uri)


async def _websocket_to_process(websocket, process) -> None:
    async for message in websocket:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        process.stdin.write(message.encode("utf-8") + b"\n")
        await process.stdin.drain()


async def _process_to_websocket(process, websocket) -> None:
    while line := await process.stdout.readline():
        await websocket.send(line.decode("utf-8").rstrip("\n"))
    logger.warning("MCP server exited with code %s", await process.wait())


def server_env() -> dict[str, str]:
    """The server's environment, without the Connector URL."""
    return {k: v for k, v in os.environ.items() if k != "MCP_ENDPOINT"}


async def run_session(uri: str, command: list[str]) -> None:
    """Pipe one WebSocket connection to a fresh MCP server process."""
    async with websockets.connect(uri) as websocket:
        logger.info("Connected to %s", redact(uri))
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=server_env(),
            limit=STREAM_LIMIT,
        )
        tasks = [
            asyncio.create_task(_websocket_to_process(websocket, process)),
            asyncio.create_task(_process_to_websocket(process, websocket)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()


async def run_forever(uri: str, command: list[str]) -> None:
    backoff = INITIAL_BACKOFF
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await run_session(uri, command)
            logger.warning("Session ended")
        except websockets.ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            hint = (
                " (the Connector URL is invalid or was regenerated)"
                if code in (1008, 4401)
                else ""
            )
            logger.warning("Connection closed: code=%s%s", code, hint)
        except (OSError, websockets.InvalidURI, websockets.InvalidHandshake) as exc:
            logger.warning("Connection failed: %s", exc)
        if asyncio.get_running_loop().time() - started >= STABLE_SESSION_SECONDS:
            backoff = INITIAL_BACKOFF
        logger.info("Reconnecting in %ss", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def main() -> None:
    load_dotenv()
    uri = os.environ.get("MCP_ENDPOINT")
    if not uri:
        sys.exit("Set MCP_ENDPOINT to the Connector URL from the assistant admin.")
    server = Path(
        sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("bash_server.py")
    )
    if not server.is_file():
        sys.exit(f"MCP server script not found: {server}")
    try:
        asyncio.run(run_forever(uri, [sys.executable, str(server)]))
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
