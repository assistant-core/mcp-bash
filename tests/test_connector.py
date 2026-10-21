def test_server_process_does_not_inherit_the_connector_settings(monkeypatch):
    from connector import server_env

    monkeypatch.setenv("LOCAL_CONNECTOR_URL", "wss://x/local-connector/ws")
    monkeypatch.setenv("LOCAL_CONNECTOR_TOKEN", "lc_secret")
    monkeypatch.setenv("BASH_WORKDIR", "/tmp")

    env = server_env()

    assert "LOCAL_CONNECTOR_URL" not in env
    assert "LOCAL_CONNECTOR_TOKEN" not in env
    assert env["BASH_WORKDIR"] == "/tmp"


def test_conflict_hint_repeats_the_reason_and_says_what_to_do():
    from types import SimpleNamespace

    from connector import close_hint

    hint = close_hint(
        SimpleNamespace(code=4409, reason="Connector web01 is already connected")
    )

    assert "Connector web01 is already connected" in hint
    assert "its own connector token" in hint
    assert "regenerated" in close_hint(SimpleNamespace(code=4401, reason=""))
    assert close_hint(None) == ""
