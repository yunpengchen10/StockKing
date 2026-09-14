from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middlewares.auth import add_auth_middleware


def test_sidecar_token_protects_v1_routes(monkeypatch):
    monkeypatch.setenv("STOCK_KING_SIDECAR_TOKEN", "one-process-secret")
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "false")
    app = FastAPI()
    add_auth_middleware(app)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    client = TestClient(app)
    assert client.get("/api/v1/health").status_code == 401
    assert client.get("/api/v1/health", headers={"X-Stock-King-Token": "wrong"}).status_code == 401
    response = client.get("/api/v1/health", headers={"X-Stock-King-Token": "one-process-secret"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sidecar_token_is_optional_for_standalone_daily(monkeypatch):
    monkeypatch.delenv("STOCK_KING_SIDECAR_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "false")
    app = FastAPI()
    add_auth_middleware(app)

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    assert TestClient(app).get("/api/v1/health").status_code == 200

