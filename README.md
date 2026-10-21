# mcp-bash

A minimal MCP server that lets an AI assistant run bash commands on your machine, connected as an Assistant Core **local connector**. The connector dials out over WebSocket, so the machine needs no public URL or open port.

> ⚠️ **Anyone who can chat with the connected assistant can run commands as your user.** Use it only with assistants you trust, run it under a low-privilege account, and stop it when you are done.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Assistant Core admin → MCP → Local connector → Add connector (token shown once)
export LOCAL_CONNECTOR_URL="wss://api.assistantcore.com/local-connector/ws"
export LOCAL_CONNECTOR_TOKEN="lc_..."
python connector.py
```

Then click **Refresh tools** in the admin: the assistant gets one tool, `bash`. Assistant Core offers a connector's tools only as the owner last refreshed them, so refresh again after changing `BASH_TOOL_NAME`, `BASH_SURFACES` or the server.

You can also put both settings in a `.env` file (see `.env.example`). To connect a different stdio MCP server, pass its script: `python connector.py my_server.py`.

## Where the tool is offered

On Assistant Core the tool is offered only in the web and mobile apps (`BASH_SURFACES=app`), never to website widget visitors, messaging apps, voice devices or the public API, where anyone the assistant talks to could run commands. To widen it, list the surfaces: `BASH_SURFACES=app,api`. The admin can narrow this further, not widen it.

## When it asks first

Assistant Core asks before every command unless a rule says otherwise, because the tool can change anything on the machine. The tool tells Assistant Core which argument holds the command line, so rules can name commands instead of the whole tool:

- On an approval card, tick **Don't ask again** (for commands starting with `git status`) to run `git status` without asking next time, in chat and in scheduled tasks. Manage these in Settings → Approvals.
- The assistant's owner sets rules for everyone in the admin (**Approvals**): **Allow** `git status`, **Ask** for `docker`, **Block** `rm`. Block wins over Ask, Ask over Allow.

A command line runs without asking only when every command in it is allowed: `git status && rm -rf x` still asks. Lines with `$(…)`, backticks, redirections or variables, and commands that run other commands (`sudo`, `bash -c`, `xargs`, `python`…), are never allowed by a command rule.

After updating this server, refresh tools in the admin once so Assistant Core sees the new definition.

## Several machines or several assistants

**One assistant, several machines.** Add a connector per machine in the admin, each with its own token, and give each machine its own tool name so the model can tell them apart:

```bash
# on web01, with web01's token
BASH_TOOL_NAME=web01_bash python connector.py
# on db01, with db01's token
BASH_TOOL_NAME=db01_bash python connector.py
```

Then refresh tools in the admin. A tool name belongs to the connector that offered it first: a second machine offering the same name is refused, and its log says so. Each token connects one machine at a time, and deleting or regenerating one disconnects only that machine.

**One machine, several assistants.** A connector token belongs to one assistant, so run one connector per assistant, each with its own token and audit log:

```bash
LOCAL_CONNECTOR_TOKEN=lc_AAA BASH_AUDIT_LOG=~/audit-a.jsonl python connector.py
LOCAL_CONNECTOR_TOKEN=lc_BBB BASH_AUDIT_LOG=~/audit-b.jsonl python connector.py
```

Everyone who can chat with any of those assistants can run commands on the machine.

## Limit what commands can reach

The server only starts commands; it does not confine them. For anything beyond a personal machine:

- Run the connector as a dedicated user without `sudo`, owning only the directories the assistant should touch.
- Better, run it in a rootless container with a read-only root filesystem and one writable volume, and restrict its outbound network if you can: commands can send anything they read to the internet.
- Pass secrets with `BASH_PASS_ENV` only when a task needs them, and review `audit.jsonl`.

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
- Commands see only a minimal environment (`PATH`, `HOME`, `USER`, `SHELL`, locale, `TERM`, `TMPDIR`, `TZ`), so secrets in your shell, including the connector token, never reach the assistant. Pass extra variables explicitly with `BASH_PASS_ENV=GITHUB_TOKEN,AWS_PROFILE`; anything you pass, the assistant can read.
- stdin is closed, so commands that prompt for input fail instead of hanging.
- On timeout the whole process group is killed. A process that starts its own session (`setsid`) can outlive it; there is no sandbox to stop that.
- Background jobs keep running after the command returns; redirect their output (`cmd > log 2>&1 &`), since the result is returned as soon as bash exits.
- stdout and stderr are each truncated to 20,000 characters, keeping the first 8,000 and the last 12,000, and memory stays bounded however much a command prints.
- At most 2 commands run at once; extra calls return an error.
- Every command is appended to `audit.jsonl` next to `bash_server.py` (or `BASH_AUDIT_LOG`): time, command, starting directory, exit code and duration, not the output. The file is readable only by you.
- Requires bash (macOS, Linux, or WSL on Windows).

## Reconnection

`connector.py` reconnects with exponential backoff (1 s up to 10 min), and starts over after a session that stayed up for a minute. If the connector token is regenerated or deleted in the admin, the log says so; restart with the new token. If the relay refuses the connection because the token is already in use or another machine offers the same tool name, the log names the conflict.

## Tests

```bash
pip install pytest && pytest -q
```
