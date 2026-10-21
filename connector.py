"""Connect a local stdio MCP server to an Assistant Core local connector.

The connector dials out over WebSocket, so the machine needs no public URL or
open port.

Usage:
    export LOCAL_CONNECTOR_URL="wss://api.assistantcore.com/local-connector/ws"
    export LOCAL_CONNECTOR_TOKEN="lc_..."
    python connector.py [server.py]   # defaults to bash_server.py
"""

import asyncio
import logging
import os
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
# Connector settings, which the MCP server never sees.
CONNECTOR_ENV = ("LOCAL_CONNECTOR_URL", "LOCAL_CONNECTOR_TOKEN")


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
    """The server's environment, without the connector settings."""
    return {k: v for k, v in os.environ.items() if k not in CONNECTOR_ENV}


async def run_session(uri: str, token: str, command: list[str]) -> None:
    """Pipe one WebSocket connection to a fresh MCP server process."""
    async with websockets.connect(
        uri, additional_headers={"Authorization": f"Bearer {token}"}
    ) as websocket:
        logger.info("Connected to %s", uri)
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


def close_hint(close) -> str:
    """What the owner should do about the relay closing the connection."""
    if close is None:
        return ""
    if close.code in (1008, 4401):
        return " (the connector token is invalid, regenerated or deleted)"
    if close.code == 4409:
        # Another machine uses this token, or offers a tool with the same name.
        return (
            f" ({close.reason}; give each machine its own connector token "
            "and BASH_TOOL_NAME)"
        )
    return ""


async def run_forever(uri: str, token: str, command: list[str]) -> None:
    backoff = INITIAL_BACKOFF
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await run_session(uri, token, command)
            logger.warning("Session ended")
        except websockets.ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            logger.warning("Connection closed: code=%s%s", code, close_hint(exc.rcvd))
        except (OSError, websockets.InvalidURI, websockets.InvalidHandshake) as exc:
            logger.warning("Connection failed: %s", exc)
        if asyncio.get_running_loop().time() - started >= STABLE_SESSION_SECONDS:
            backoff = INITIAL_BACKOFF
        logger.info("Reconnecting in %ss", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def main() -> None:
    load_dotenv()
    uri = os.environ.get("LOCAL_CONNECTOR_URL")
    token = os.environ.get("LOCAL_CONNECTOR_TOKEN")
    if not uri or not token:
        sys.exit(
            "Set LOCAL_CONNECTOR_URL and LOCAL_CONNECTOR_TOKEN to the connector "
            "settings from the assistant admin."
        )
    server = Path(
        sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("bash_server.py")
    )
    if not server.is_file():
        sys.exit(f"MCP server script not found: {server}")
    try:
        asyncio.run(run_forever(uri, token, [sys.executable, str(server)]))
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
