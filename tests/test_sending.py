import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon import errors, types

from telegram_reader import outbox
from telegram_reader.core import Reader, ReaderError
from test_reader import Client, msg


class SendingClient(Client):
    def __init__(self):
        super().__init__()
        self.get_entity = AsyncMock(return_value=types.User(id=1, first_name="Alice"))
        self.get_me = AsyncMock(return_value=types.User(id=42, first_name="Owner"))
        self.rpc = AsyncMock(return_value=types.UpdateShortSentMessage(id=100, pts=1, pts_count=1, date=msg(1).date, out=True))

    async def __call__(self, request):
        return await self.rpc(request)


@pytest.fixture
def sending(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_READER_DATA_DIR", str(tmp_path))
    client = SendingClient()
    account = {"session": "test", "allowed_chat_ids": ["1"]}
    return Reader(lambda: dict(account), lambda a: client), client, account


async def test_prepare_does_not_send_and_encrypts_content(sending, tmp_path):
    r, c, _ = sending
    draft = await r.prepare_message("1", "private outgoing text")
    assert draft["recipient"] == "Alice" and draft["sent"] is False
    c.rpc.assert_not_awaited()
    assert b"private outgoing text" not in (tmp_path / "outbox.sqlite3").read_bytes()


async def test_send_exact_text_and_single_use_across_restart(sending):
    r, c, account = sending
    draft = await r.prepare_message("1", "**Literal** <text>\nHello", silent=True)
    first = await r.send_message(draft["draft_id"])
    restarted = Reader(lambda: dict(account), lambda a: c)
    second = await restarted.send_message(draft["draft_id"])
    assert first == second and first["state"] == "sent"
    assert first["message_id"] == 100
    c.rpc.assert_awaited_once()
    request = c.rpc.call_args.args[0]
    assert request.message == "**Literal** <text>\nHello"
    assert request.silent is True and request.no_webpage is True
    assert request.allow_paid_stars == 0 and request.clear_draft is False
    assert request.entities is None


async def test_reply_and_preview_options(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Answer", reply_to_message_id=5, link_preview=True)
    await r.send_message(draft["draft_id"])
    request = c.rpc.call_args.args[0]
    assert request.reply_to.reply_to_msg_id == 5
    assert request.no_webpage is False


async def test_deleted_reply_target_never_sends(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Answer", reply_to_message_id=5)
    c.get_messages.return_value = None
    with pytest.raises(ReaderError, match="Reply target"):
        await r.send_message(draft["draft_id"])
    c.rpc.assert_not_awaited()


@pytest.mark.parametrize("text", ["", "  \n", "a" * 4097, "😀" * 2049])
async def test_invalid_text_never_sends(sending, text):
    r, c, _ = sending
    with pytest.raises(ReaderError):
        await r.prepare_message("1", text)
    c.rpc.assert_not_awaited()


async def test_revoked_scope_rechecked_at_send(sending):
    r, c, account = sending
    draft = await r.prepare_message("1", "Hello")
    account["allowed_chat_ids"] = []
    with pytest.raises(ReaderError, match="outside"):
        await r.send_message(draft["draft_id"])
    c.rpc.assert_not_awaited()


async def test_cannot_send_another_accounts_draft(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Hello")
    c.get_me.return_value = types.User(id=43, first_name="Other")
    with pytest.raises(ReaderError, match="this Telegram account"):
        await r.send_message(draft["draft_id"])
    c.rpc.assert_not_awaited()


async def test_timeout_is_unknown_and_retry_does_not_send(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Hello")
    c.rpc.side_effect = TimeoutError()
    result = await r.send_message(draft["draft_id"])
    assert result["state"] == "unknown"
    await r.send_message(draft["draft_id"])
    c.rpc.assert_awaited_once()


async def test_cancellation_is_persisted_as_uncertain(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Hello")
    c.rpc.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await r.send_message(draft["draft_id"])
    assert (await r.delivery_status(draft["draft_id"]))["state"] == "unknown"
    await r.send_message(draft["draft_id"])
    c.rpc.assert_awaited_once()


async def test_floodwait_allows_same_random_id_after_wait(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Hello")
    c.rpc.side_effect = [errors.FloodWaitError(None, capture=12), c.rpc.return_value]
    first = await r.send_message(draft["draft_id"])
    assert first["retry_after_seconds"] == 12
    second = await r.send_message(draft["draft_id"])
    assert second["state"] == "sent"
    assert c.rpc.call_args_list[0].args[0].random_id == c.rpc.call_args_list[1].args[0].random_id


async def test_explicit_denial_is_rejected(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Hello")
    c.rpc.side_effect = errors.ChatWriteForbiddenError(None)
    assert (await r.send_message(draft["draft_id"]))["state"] == "rejected"
    await r.send_message(draft["draft_id"])
    c.rpc.assert_awaited_once()


async def test_random_id_response_mapping(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Hello")
    async def response(request):
        return Obj(updates=[types.UpdateMessageID(id=222, random_id=request.random_id)])
    c.rpc.side_effect = response
    assert (await r.send_message(draft["draft_id"]))["message_id"] == 222


async def test_missing_mapping_never_claims_success(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Hello")
    c.rpc.return_value = Obj(updates=[])
    assert (await r.send_message(draft["draft_id"]))["state"] == "unknown"


async def test_expired_draft_never_sends(sending):
    r, c, _ = sending
    draft = await r.prepare_message("1", "Hello")
    with outbox.database() as conn:
        conn.execute("UPDATE drafts SET created=? WHERE id=?", (time.time() - 1801, draft["draft_id"]))
    with pytest.raises(ReaderError, match="expired"):
        await r.send_message(draft["draft_id"])
    c.rpc.assert_not_awaited()


async def test_claim_is_atomic_across_connections(sending):
    r, _, _ = sending
    draft = await r.prepare_message("1", "Hello")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: outbox.claim(draft["draft_id"], 42), range(4)))
    assert results.count(True) == 1
    assert (await r.send_message(draft["draft_id"]))["state"] == "sending"
