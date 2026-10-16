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

## Keep it running on a server

To keep the connector running after you close SSH, start it inside `tmux`:

```bash
tmux new -s connector
cd ~/mcp-bash && source .venv/bin/activate && python connector.py
# Ctrl+B then D to detach; the connector keeps running
```

```bash
tmux attach -t connector                  # back to the logs
tmux kill-session -t connector            # stop it
```

tmux survives SSH disconnects, but not a server reboot, and nothing restarts the connector if it exits.

## The `bash` tool

| Parameter | Description |
|---|---|
| `command` | Bash command to run |
| `timeout_seconds` | 1–120, default 30 |
| `restart` | Start from `BASH_WORKDIR` instead of where the last command ended |

Returns `stdout`, `stderr`, `exit_code`, `timed_out`, `truncated` and `cwd` (where the command ended).

- The first command starts in `BASH_WORKDIR` (default: your home directory); each later one starts where the previous one ended, like a terminal. Environment variables, aliases and functions do not carry over: every call is a new shell. A command that times out keeps the directory it started in. This is a starting point, **not a sandbox**.
- Commands see only a minimal environment (`PATH`, `HOME`, `USER`, `SHELL`, locale, `TERM`, `TMPDIR`, `TZ`), so secrets in your shell, including the Connector URL, never reach the assistant. Pass extra variables explicitly with `BASH_PASS_ENV=GITHUB_TOKEN,AWS_PROFILE`; anything you pass, the assistant can read.
- stdin is closed, so commands that prompt for input fail instead of hanging.
- On timeout the whole process group is killed. A process that starts its own session (`setsid`) can outlive it; there is no sandbox to stop that.
- Background jobs keep running after the command returns; redirect their output (`cmd > log 2>&1 &`), since the result is returned as soon as bash exits.
- stdout and stderr are each truncated to 20,000 characters, keeping the first 8,000 and the last 12,000, and memory stays bounded however much a command prints.
- At most 2 commands run at once; extra calls return an error.
- Every command is appended to `audit.jsonl` next to `bash_server.py` (or `BASH_AUDIT_LOG`): time, command, starting directory, exit code and duration, not the output. The file is readable only by you.
- Requires bash (macOS, Linux, or WSL on Windows).

## Reconnection

`connector.py` reconnects with exponential backoff (1 s up to 10 min), and starts over after a session that stayed up for a minute. If the Connector URL is regenerated or revoked in the admin, the log says so; restart with the new URL.

## Tests

```bash
pip install pytest && pytest -q
```
