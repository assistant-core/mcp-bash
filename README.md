# mcp-bash

A minimal MCP server that lets an AI assistant run bash commands on your machine, connected through an **MCP Endpoint** (Assistant Core, or any Xiaozhi-compatible endpoint). The connector dials out over WebSocket, so the machine needs no public URL or open port.

> ⚠️ **Anyone who can chat with the connected assistant can run commands as your user.** Use it only with assistants you trust, run it under a low-privilege account, and stop it when you are done.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Assistant Core admin → MCP → Local connector → Create Connector URL (shown once)
export MCP_ENDPOINT="wss://api.assistantcore.com/mcp_endpoint/mcp/?token=mcpe_..."
python connector.py
```

Then refresh tools in the admin; the assistant gets one tool, `bash`.

You can also put `MCP_ENDPOINT` in a `.env` file (see `.env.example`). To connect a different stdio MCP server, pass its script: `python connector.py my_server.py`.

`connector.py` is the equivalent of Xiaozhi's `mcp_pipe.py` and takes the same `MCP_ENDPOINT` URL.

## The `bash` tool

| Parameter | Description |
|---|---|
| `command` | Bash command to run |
| `timeout_seconds` | 1–120, default 30 |

Returns `stdout`, `stderr`, `exit_code`, `timed_out` and `cwd`.

- Commands start in `BASH_WORKDIR` (default: your home directory). This is a starting point, **not a sandbox**.
- Commands see only a minimal environment (`PATH`, `HOME`, `USER`, `SHELL`, locale, `TERM`, `TMPDIR`, `TZ`), so secrets in your shell, including the Connector URL, never reach the assistant. Pass extra variables explicitly with `BASH_PASS_ENV=GITHUB_TOKEN,AWS_PROFILE`; anything you pass, the assistant can read.
- stdin is closed, so commands that prompt for input fail instead of hanging.
- On timeout the whole process group is killed. A process that starts its own session (`setsid`) can outlive it; there is no sandbox to stop that.
- Background jobs keep running after the command returns; redirect their output (`cmd > log 2>&1 &`), since the result is returned as soon as bash exits.
- stdout and stderr are each truncated to 20,000 characters, and memory stays bounded however much a command prints.
- At most 2 commands run at once; extra calls return an error.
- Requires bash (macOS, Linux, or WSL on Windows).

## Reconnection

`connector.py` reconnects with exponential backoff (1 s up to 10 min), and starts over after a session that stayed up for a minute. If the Connector URL is regenerated or revoked in the admin, the log says so; restart with the new URL.

## Tests

```bash
pip install pytest && pytest -q
```
