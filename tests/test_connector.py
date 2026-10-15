from connector import redact


def test_redact_hides_only_the_token():
    uri = "wss://api.example.com/mcp_endpoint/mcp/?token=mcpe_secret&x=1"
    assert redact(uri) == "wss://api.example.com/mcp_endpoint/mcp/?token=***&x=1"


def test_server_process_does_not_inherit_the_connector_url(monkeypatch):
    from connector import server_env

    monkeypatch.setenv("MCP_ENDPOINT", "wss://x/mcp/?token=mcpe_secret")
    monkeypatch.setenv("BASH_WORKDIR", "/tmp")

    env = server_env()

    assert "MCP_ENDPOINT" not in env
    assert env["BASH_WORKDIR"] == "/tmp"
