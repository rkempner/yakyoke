"""Tests for the daemon's bearer-token auth.

Uses FastAPI's TestClient so we don't need a running server. The lifespan
hook spawns a real worker thread that polls an empty queue; that's harmless
for these tests and shuts down cleanly when the TestClient context exits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yakyoke.config import Config
from yakyoke.daemon import create_app


def _config(tmp_path: Path, token: str = "") -> Config:
    return Config(
        data_dir=tmp_path,
        default_model="fake/test",
        host="127.0.0.1",
        port=8765,
        max_agent_steps=4,
        api_token=token,
    )


def _bootstrap(tmp_path: Path) -> None:
    """Make sure the data dir layout exists before create_app touches it."""
    (tmp_path / "tasks").mkdir(exist_ok=True)


# ---------- auth disabled (default) ----------


def test_no_token_means_open_api(tmp_path):
    _bootstrap(tmp_path)
    cfg = _config(tmp_path, token="")
    app = create_app(cfg)
    with TestClient(app) as client:
        # Health is always open.
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["auth_required"] is False

        # Listing works without an Authorization header.
        r = client.get("/tasks")
        assert r.status_code == 200


# ---------- auth enabled ----------


def test_health_open_when_auth_enabled(tmp_path):
    _bootstrap(tmp_path)
    cfg = _config(tmp_path, token="secret-abc-123")
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["auth_required"] is True


def test_missing_token_rejected(tmp_path):
    _bootstrap(tmp_path)
    cfg = _config(tmp_path, token="secret-abc-123")
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/tasks")
        assert r.status_code == 401
        assert "missing bearer token" in r.json()["detail"]
        assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_wrong_token_rejected(tmp_path):
    _bootstrap(tmp_path)
    cfg = _config(tmp_path, token="secret-abc-123")
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get(
            "/tasks", headers={"Authorization": "Bearer not-the-real-token"}
        )
        assert r.status_code == 401
        assert "invalid bearer token" in r.json()["detail"]


def test_correct_token_accepted(tmp_path):
    _bootstrap(tmp_path)
    cfg = _config(tmp_path, token="secret-abc-123")
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get(
            "/tasks", headers={"Authorization": "Bearer secret-abc-123"}
        )
        assert r.status_code == 200


def test_create_task_requires_auth(tmp_path):
    _bootstrap(tmp_path)
    cfg = _config(tmp_path, token="secret-abc-123")
    app = create_app(cfg)
    with TestClient(app) as client:
        # Without token: rejected.
        r = client.post("/tasks", json={"prompt": "do a thing"})
        assert r.status_code == 401

        # With token: created.
        r = client.post(
            "/tasks",
            json={"prompt": "do a thing"},
            headers={"Authorization": "Bearer secret-abc-123"},
        )
        assert r.status_code == 201
        assert r.json()["prompt"] == "do a thing"


def test_malformed_authorization_header_rejected(tmp_path):
    _bootstrap(tmp_path)
    cfg = _config(tmp_path, token="secret-abc-123")
    app = create_app(cfg)
    with TestClient(app) as client:
        # Missing the "Bearer " prefix.
        r = client.get(
            "/tasks", headers={"Authorization": "secret-abc-123"}
        )
        assert r.status_code == 401

        # Wrong scheme.
        r = client.get(
            "/tasks", headers={"Authorization": "Basic c2VjcmV0OmFiYw=="}
        )
        assert r.status_code == 401


def test_bearer_case_insensitive(tmp_path):
    """RFC 7235 says auth scheme is case-insensitive. Honor that."""
    _bootstrap(tmp_path)
    cfg = _config(tmp_path, token="secret-abc-123")
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get(
            "/tasks", headers={"Authorization": "bearer secret-abc-123"}
        )
        assert r.status_code == 200
