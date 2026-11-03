"""Physical reconnects obtain fresh credentials without mutating global clients."""

from psycopg import Connection

from agent_platform.azure.connections import token_connection


def test_every_physical_connection_requests_a_current_token(monkeypatch):
    supplied = []
    tokens = iter(["first-token", "refreshed-token"])
    sentinel = object()

    def connect(cls, conninfo="", **kwargs):
        supplied.append(kwargs["password"])
        return sentinel

    monkeypatch.setattr(Connection, "connect", classmethod(connect))
    renewable = token_connection(lambda: next(tokens))
    assert renewable.connect("host=synthetic") is sentinel
    assert renewable.connect("host=synthetic", password="obsolete") is sentinel
    assert supplied == ["first-token", "refreshed-token"]
