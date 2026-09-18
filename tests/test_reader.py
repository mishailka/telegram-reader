from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon import types

from telegram_reader.core import Reader, ReaderError, message_dict, parse_date


def msg(identifier, text="hello", file=None):
    return Obj(id=identifier, date=datetime(2026, 9, 18, 12, identifier % 60, tzinfo=timezone.utc),
               message=text, sender=None, sender_id=1, out=False, file=file, reply_to=None,
               edit_date=None, peer_id=types.PeerChannel(5), media=Obj(ttl_seconds=None), noforwards=False)


class Client:
    def __init__(self):
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)
        self.get_input_entity = AsyncMock(side_effect=lambda x: x)
        self.get_entity = AsyncMock(return_value=Obj(noforwards=False))
        self.get_messages = AsyncMock(return_value=msg(5))
        self.get_me = AsyncMock(return_value=Obj(id=42, first_name="", last_name="", username="me"))
        self.rows = [msg(i) for i in range(10, 0, -1)]
        self.dialogs = []
        self.requests = []
        self.chunks = [b"abc", b"def"]

    def is_connected(self):
        return True

    async def iter_messages(self, entity, **kwargs):
        self.requests.append(kwargs)
        rows = self.rows
        if kwargs.get("offset_id"):
            rows = [m for m in rows if m.id < kwargs["offset_id"]]
        if kwargs.get("min_id"):
            rows = [m for m in rows if m.id > kwargs["min_id"]]
        if kwargs.get("offset_date"):
            rows = [m for m in rows if m.date < kwargs["offset_date"]]
        if kwargs.get("reverse"):
            rows = list(reversed(rows))
        for m in rows[:kwargs.get("limit", len(rows))]:
            yield m

    async def iter_dialogs(self, **kwargs):
        for d in self.dialogs[:kwargs.get("limit", 2000)]:
            yield d

    async def iter_download(self, media):
        for chunk in self.chunks:
            yield chunk


def reader(client=None, allowed=None):
    client = client or Client()
    account = {"session": "test", "allowed_chat_ids": allowed}
    return Reader(lambda: account, lambda a: client), client


async def test_unconnected_is_actionable():
    r = Reader(lambda: None)
    assert (await r.status())["connected"] is False
    with pytest.raises(ReaderError, match="Connect.cmd"):
        await r.messages("1")


async def test_scope_denies_read_before_entity_resolution():
    r, c = reader(allowed=["2"])
    with pytest.raises(ReaderError, match="outside"):
        await r.messages("1")
    c.get_input_entity.assert_not_called()


async def test_empty_scope_denies_everything():
    r, c = reader(allowed=[])
    with pytest.raises(ReaderError):
        await r.context("1", 5)


async def test_history_paginates_without_missing_or_duplicate_messages():
    r, c = reader()
    ids, before = [], 0
    while True:
        page = await r.messages("1", limit=3, before_id=before)
        ids.extend(m["message_id"] for m in page["messages"])
        assert page["marked_read"] is False
        if not page["has_more"]:
            break
        before = page["next_before_id"]
    assert ids == list(range(10, 0, -1))


async def test_date_window_and_search_filters():
    r, c = reader()
    page = await r.messages("1", since="2026-09-18T12:05:00Z", until="2026-09-18T12:09:00Z", query="contract", media_type="document")
    assert [m["message_id"] for m in page["messages"]] == [8, 7, 6, 5]
    assert c.requests[0]["search"] == "contract"
    assert isinstance(c.requests[0]["filter"], types.InputMessagesFilterDocument)
    assert page["has_more"] is False


async def test_context_correct_on_both_sides():
    r, _ = reader()
    result = await r.context("1", 5, before=2, after=3)
    assert [m["message_id"] for m in result["messages"]] == [3, 4, 5, 6, 7, 8]


async def test_deleted_target_is_explicit():
    r, c = reader()
    c.get_messages.return_value = None
    with pytest.raises(ReaderError, match="deleted"):
        await r.context("1", 5)


async def test_list_does_not_expose_denied_chat():
    r, c = reader(allowed=["2", "3"])
    c.dialogs = [Obj(id=i, name=f"Chat {i}", unread_count=1, unread_mentions_count=0,
                     is_user=True, is_group=False, archived=False, date=None, message=None) for i in (1, 2, 3)]
    first = await r.list_chats(limit=1)
    second = await r.list_chats(limit=1, offset=first["next_offset"])
    assert first["chats"][0]["chat_id"] == "2"
    assert second["chats"][0]["chat_id"] == "3"
    assert second["has_more"] is False


async def test_scope_change_takes_effect_without_restart():
    c = Client()
    account = {"session": "test", "allowed_chat_ids": None}
    r = Reader(lambda: dict(account), lambda a: c)
    await r.messages("1")
    account["allowed_chat_ids"] = ["2"]
    with pytest.raises(ReaderError):
        await r.messages("1")
    c.disconnect.assert_awaited_once()


async def test_download_safe_name_and_size(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_READER_DATA_DIR", str(tmp_path))
    r, c = reader()
    c.get_messages.return_value = msg(5, file=Obj(size=6, name="../../CON.exe", ext=".txt"))
    result = await r.download("1", 5, max_bytes=6)
    from pathlib import Path
    path = Path(result["path"])
    assert path.parent == tmp_path / "downloads"
    assert path.read_bytes() == b"abcdef"
    assert "CON" not in path.name


async def test_failed_download_cleans_partial_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_READER_DATA_DIR", str(tmp_path))
    r, c = reader()
    c.get_messages.return_value = msg(5, file=Obj(size=5, name="test", ext=".txt"))
    with pytest.raises(ReaderError, match="exceeded"):
        await r.download("1", 5, max_bytes=5)
    assert list((tmp_path / "downloads").iterdir()) == []


async def test_protected_download_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_READER_DATA_DIR", str(tmp_path))
    r, c = reader()
    c.get_messages.return_value = msg(5, file=Obj(size=6, name="test", ext=".txt"))
    c.get_entity.return_value = Obj(noforwards=True)
    with pytest.raises(ReaderError, match="protected"):
        await r.download("1", 5)
    assert not (tmp_path / "downloads").exists()


@pytest.mark.parametrize("value", ["@someone", "name", "0", "../../x"])
async def test_ids_cannot_be_ambiguous_names(value):
    r, _ = reader()
    with pytest.raises(ReaderError):
        await r.messages(value)


def test_links_are_only_for_channel_peers():
    m = msg(7)
    assert message_dict(m, -1000000000005)["link"] == "https://t.me/c/5/7"
    m.peer_id = types.PeerChat(100123)
    assert message_dict(m, -100123)["link"] is None


def test_timezone_normalization():
    assert parse_date("2026-09-18T12:00:00+04:00").hour == 8
    assert parse_date("2026-09-18").tzinfo == timezone.utc
