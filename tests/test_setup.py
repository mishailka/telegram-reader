import os
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient
from telethon import errors

from telegram_reader import storage
from telegram_reader.setup import Wizard, create_app


def test_local_credentials_encrypted_and_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_READER_DATA_DIR", str(tmp_path))
    account = {"api_id": 123, "api_hash": "PRIVATE_HASH", "session": "PRIVATE_SESSION", "allowed_chat_ids": ["1"]}
    storage.save_account(account)
    raw = (tmp_path / "account.dpapi").read_bytes()
    assert b"PRIVATE" not in raw
    assert storage.load_account() == account
    storage.remove_account()
    assert storage.load_account() is None


def test_setup_requires_host_origin_and_random_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_READER_DATA_DIR", str(tmp_path))
    origin = "http://127.0.0.1:9999"
    app = create_app("test-token", origin)
    with TestClient(app, base_url=origin) as client:
        assert client.post("/api/status", json={}).status_code == 403
        assert client.post("/api/status", json={}, headers={"Origin": "https://evil.invalid", "X-Setup-Token": "test-token"}).status_code == 403
        assert client.post("/api/status", json={}, headers={"Origin": origin, "X-Setup-Token": "wrong"}).status_code == 403
        result = client.post("/api/status", json={}, headers={"Origin": origin, "X-Setup-Token": "test-token"})
        assert result.status_code == 200
        assert result.json()["has_account"] is False
        assert client.get("/", headers={"Host": "evil.invalid"}).status_code == 403
        assert "no-store" in client.get("/").headers["cache-control"]


def test_setup_rejects_large_request(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_READER_DATA_DIR", str(tmp_path))
    origin = "http://127.0.0.1:9999"
    with TestClient(create_app("token", origin), base_url=origin) as client:
        result = client.post("/api/status", content=b"x" * 65537, headers={"Origin": origin, "X-Setup-Token": "token"})
        assert result.status_code == 413


async def test_login_two_factor_state_machine(monkeypatch):
    from types import SimpleNamespace as Obj
    client = Obj(connect=AsyncMock(), send_code_request=AsyncMock(return_value=Obj(phone_code_hash="hash")),
                 sign_in=AsyncMock(side_effect=[errors.SessionPasswordNeededError(None), None]), disconnect=AsyncMock())
    monkeypatch.setattr("telegram_reader.setup.load_account", lambda: None)
    monkeypatch.setattr("telegram_reader.setup.make_client", lambda a: client)
    wizard = Wizard()
    assert (await wizard.action("begin", {"api_id": 123, "api_hash": "a" * 32, "phone": "+79991234567"}))["state"] == "code"
    assert (await wizard.action("code", {"code": "12345"}))["state"] == "password"
    assert (await wizard.action("password", {"password": "test"}))["state"] == "scope"


async def test_setup_does_not_accept_save_before_login():
    with pytest.raises(ValueError):
        await Wizard().action("save", {"allowed_chat_ids": None})
