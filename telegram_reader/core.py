from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, errors, types, utils
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendMessageRequest

from .storage import data_dir, load_account
from . import outbox


class ReaderError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        raise ReaderError("Use an ISO date, for example 2026-09-18 or 2026-09-18T12:00:00+04:00.") from None


def check_limit(limit: int, maximum: int = 100) -> None:
    if not 1 <= limit <= maximum:
        raise ReaderError(f"limit must be between 1 and {maximum}.")


def parse_chat_id(value: str) -> int:
    if not re.fullmatch(r"-?[1-9][0-9]{0,19}", str(value)):
        raise ReaderError("Use the numeric chat_id returned by list_chats, not a name or username.")
    return int(value)


def name(entity) -> str | None:
    return utils.get_display_name(entity) if entity else None


def message_dict(message, chat_id: int) -> dict:
    sender = getattr(message, "sender", None)
    file = getattr(message, "file", None)
    reply = getattr(message, "reply_to", None)
    text = getattr(message, "message", None) or ""
    forwarded = getattr(message, "fwd_from", None)
    return {
        "chat_id": str(chat_id), "message_id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "edited_at": message.edit_date.isoformat() if getattr(message, "edit_date", None) else None,
        "sender_id": str(message.sender_id) if getattr(message, "sender_id", None) else None,
        "sender_name": name(sender), "outgoing": bool(getattr(message, "out", False)),
        "text": text[:16000], "text_truncated": len(text) > 16000,
        "reply_to_message_id": getattr(reply, "reply_to_msg_id", None),
        "topic_id": getattr(reply, "reply_to_top_id", None),
        "forwarded": bool(forwarded),
        "forwarded_from_name": getattr(forwarded, "from_name", None),
        "grouped_id": str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
        "service_event": type(message.action).__name__ if getattr(message, "action", None) else None,
        "attachment": {"name": file.name, "size": file.size, "mime_type": file.mime_type} if file else None,
        "link": f"https://t.me/c/{message.peer_id.channel_id}/{message.id}" if isinstance(getattr(message, "peer_id", None), types.PeerChannel) else None,
    }


def make_client(account: dict, *, updates: bool = False):
    return TelegramClient(
        StringSession(account.get("session", "")), account["api_id"], account["api_hash"],
        device_model="Telegram Reader for Codex", app_version="0.2.0",
        flood_sleep_threshold=0, request_retries=1, connection_retries=2,
        receive_updates=updates,
    )


class Reader:
    def __init__(self, account_loader=load_account, client_factory=make_client):
        self.account_loader = account_loader
        self.client_factory = client_factory
        self.client = None
        self.account = None
        self.lock = asyncio.Lock()

    async def close(self):
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def connect(self):
        account = self.account_loader()
        if not account or not account.get("session"):
            raise ReaderError("Telegram is not connected. Run Connect.cmd and complete local sign-in.")
        if account != self.account:
            await self.close()
            self.account = account
        if self.client is None:
            self.client = self.client_factory(account)
        if not self.client.is_connected():
            await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ReaderError("Telegram session expired or was revoked. Run Connect.cmd again.")
        return self.client

    def allowed(self, chat_id: int) -> bool:
        ids = self.account.get("allowed_chat_ids", [])
        return ids is None or str(chat_id) in ids

    async def entity(self, chat_id: str):
        numeric = parse_chat_id(chat_id)
        client = await self.connect()
        if not self.allowed(numeric):
            raise ReaderError("This chat is outside the access scope selected in the local setup window.")
        try:
            return await client.get_input_entity(numeric)
        except ValueError:
            # StringSession intentionally does not persist contact/entity databases.
            async for dialog in client.iter_dialogs(limit=2000):
                if dialog.id == numeric:
                    return dialog.input_entity
        raise ReaderError("Chat was not found in the first 2000 dialogs accessible to this session.")

    async def status(self):
        account = self.account_loader()
        if not account:
            return {"connected": False, "setup": "Run Connect.cmd", "mode": "read_and_send"}
        client = await self.connect()
        me = await client.get_me()
        return {"connected": True, "mode": "read_and_send", "user_id": str(me.id),
                "name": name(me), "username": me.username,
                "scope": "all_chats" if account.get("allowed_chat_ids") is None else "selected_chats",
                "allowed_chat_ids": account.get("allowed_chat_ids"), "checked_at": utc_now()}

    async def prepare_message(self, chat_id, text, reply_to_message_id=None, silent=False, link_preview=False):
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
            raise ReaderError("Text must contain 1-4096 UTF-16 units. Split longer messages explicitly.")
        if reply_to_message_id is not None and reply_to_message_id <= 0:
            raise ReaderError("reply_to_message_id must be positive.")
        entity = await self.entity(chat_id)
        recipient = await self.client.get_entity(entity)
        if reply_to_message_id is not None:
            reply = await self.client.get_messages(entity, ids=reply_to_message_id)
            if reply is None or isinstance(reply, types.MessageEmpty):
                raise ReaderError("The message to reply to was deleted or is inaccessible.")
        me = await self.client.get_me()
        payload = {"chat_id": str(chat_id), "recipient": name(recipient), "text": text,
                   "reply_to_message_id": reply_to_message_id, "silent": silent, "link_preview": link_preview}
        draft_id = outbox.create(me.id, payload)
        return {"draft_id": draft_id, "state": "ready", "expires_in_seconds": 1800,
                **{key: value for key, value in payload.items() if key != "random_id"},
                "format": "plain_text", "sent": False}

    async def delivery_status(self, draft_id):
        client = await self.connect()
        me = await client.get_me()
        record = outbox.get(draft_id, me.id)
        if record is None:
            raise ReaderError("Draft does not exist for this Telegram account.")
        if not self.allowed(int(record["payload"]["chat_id"])):
            raise ReaderError("This chat is outside the current access scope.")
        if record["result"]:
            return {**record["result"], "draft_id": draft_id}
        state = record["state"]
        if state == "ready" and time.time() - record["created"] > 1800:
            state = "expired"
        return {"draft_id": draft_id, "state": state, "chat_id": record["payload"]["chat_id"],
                "message": "Sending or interrupted: inspect chat history; do not create a replacement draft automatically." if state == "sending" else None}

    async def send_message(self, draft_id):
        client = await self.connect()
        me = await client.get_me()
        record = outbox.get(draft_id, me.id)
        if record is None:
            raise ReaderError("Draft does not exist for this Telegram account.")
        payload = record["payload"]
        entity = await self.entity(payload["chat_id"])
        client = self.client
        if (await client.get_me()).id != me.id:
            raise ReaderError("Account changed during the request. Nothing was sent; check connection_status.")
        if record["state"] != "ready":
            return await self.delivery_status(draft_id)
        if time.time() - record["created"] > 1800:
            raise ReaderError("Draft expired after 30 minutes. Prepare a new draft if sending is still requested.")
        reply_id = payload["reply_to_message_id"]
        if reply_id is not None:
            reply = await client.get_messages(entity, ids=reply_id)
            if reply is None or isinstance(reply, types.MessageEmpty):
                raise ReaderError("Reply target is no longer accessible. Nothing was sent.")
        request = SendMessageRequest(
            peer=entity, message=payload["text"], random_id=payload["random_id"],
            reply_to=types.InputReplyToMessage(reply_id) if reply_id is not None else None,
            silent=payload["silent"], no_webpage=not payload["link_preview"],
            clear_draft=False, allow_paid_stars=0,
        )
        if not outbox.claim(draft_id, me.id):
            return await self.delivery_status(draft_id)
        try:
            response = await client(request)
            message_id = response.id if isinstance(response, types.UpdateShortSentMessage) else None
            for update in getattr(response, "updates", []):
                if isinstance(update, types.UpdateMessageID) and update.random_id == payload["random_id"]:
                    message_id = update.id
                    break
            state = "sent" if message_id is not None else "unknown"
            result = {"state": state, "chat_id": payload["chat_id"], "message_id": message_id,
                      "checked_at": utc_now(), "reply_to_message_id": reply_id}
            if state == "unknown":
                result["message"] = "Telegram returned without a message ID. Inspect history; do not resend automatically."
            outbox.finish(draft_id, me.id, state, result)
            return {**result, "draft_id": draft_id}
        except (errors.FloodWaitError, errors.SlowModeWaitError) as exc:
            result = {"state": "rate_limited", "retry_after_seconds": exc.seconds,
                      "message": "Telegram rate limit. Retry the SAME draft after waiting."}
            # A FloodWait response explicitly rejects this attempt; it can safely be retried.
            outbox.finish(draft_id, me.id, "ready", None)
            return {**result, "draft_id": draft_id}
        except BaseException as exc:
            # Timeout, cancellation and crash can occur AFTER Telegram accepted the message.
            rejected = type(exc).__name__ in {"ChatWriteForbiddenError", "ChatAdminRequiredError", "UserBannedInChannelError",
                "UserIsBlockedError", "YouBlockedUserError", "MessageTooLongError", "MessageEmptyError", "PeerIdInvalidError",
                "ReplyMessageIdInvalidError", "StarsPaymentRequiredError", "ChatSendPlainForbiddenError"}
            state = "rejected" if rejected else "unknown"
            result = {"state": state, "chat_id": payload["chat_id"], "type": type(exc).__name__,
                      "message": "Delivery is not confirmed. Inspect chat history. Do not create another draft or resend automatically."}
            if rejected:
                result["message"] = "Telegram rejected the message. It was not sent; check the recipient's permissions or restrictions."
            outbox.finish(draft_id, me.id, state, result)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {**result, "draft_id": draft_id}

    async def list_chats(self, limit=50, offset=0, query="", unread_only=False, archived=None):
        check_limit(limit)
        if not 0 <= offset <= 2000:
            raise ReaderError("offset must be between 0 and 2000.")
        client = await self.connect()
        matches, scanned, more = [], 0, False
        async for dialog in client.iter_dialogs(limit=2000, archived=archived):
            scanned += 1
            if not self.allowed(dialog.id) or (query and query.casefold() not in dialog.name.casefold()):
                continue
            if unread_only and not dialog.unread_count and not getattr(dialog.dialog, "unread_mark", False):
                continue
            matches.append(dialog)
            if len(matches) > offset + limit:
                more = True
                break
        selected = matches[offset:offset + limit]
        more = more or scanned == 2000
        return {"chats": [{"chat_id": str(d.id), "title": d.name,
                  "kind": "user" if d.is_user else "group" if d.is_group else "channel",
                  "unread_count": d.unread_count, "unread_mentions_count": d.unread_mentions_count,
                  "archived": bool(d.archived), "last_message_at": d.date.isoformat() if d.date else None,
                  "last_message_id": d.message.id if d.message else None} for d in selected],
                "has_more": more, "next_offset": offset + len(selected) if more and selected else None,
                "scan_limit_reached": scanned == 2000, "scanned_dialogs": scanned,
                "checked_at": utc_now(), "note": "Offsets are best-effort while chat order changes; no messages were marked read."}

    async def messages(self, chat_id, limit=50, before_id=0, since=None, until=None,
                       query=None, sender_id=None, media_type=None):
        check_limit(limit)
        if before_id < 0:
            raise ReaderError("before_id must be non-negative.")
        start, end = parse_date(since), parse_date(until)
        if start and end and start >= end:
            raise ReaderError("since must be earlier than until (exclusive).")
        entity = await self.entity(chat_id)
        filters = {"photo": types.InputMessagesFilterPhotos, "document": types.InputMessagesFilterDocument,
                   "voice": types.InputMessagesFilterVoice, "video": types.InputMessagesFilterVideo,
                   "url": types.InputMessagesFilterUrl}
        if media_type and media_type not in filters:
            raise ReaderError("media_type must be photo, document, voice, video, or url.")
        args = dict(limit=limit + 1, offset_id=before_id, offset_date=end)
        if query is not None:
            args["search"] = query
        if sender_id:
            try:
                args["from_user"] = await self.client.get_input_entity(parse_chat_id(sender_id))
            except ValueError:
                raise ReaderError("Sender ID is not known to this session; read the relevant chat first.") from None
        if media_type:
            args["filter"] = filters[media_type]()
        found = []
        async for message in self.client.iter_messages(entity, **args):
            if start and message.date < start:
                break
            found.append(message)
        more = len(found) > limit
        selected = found[:limit]
        return {"messages": [message_dict(m, int(chat_id)) for m in selected],
                "order": "newest_first", "has_more": more,
                "next_before_id": selected[-1].id if more else None,
                "requested_since": start.isoformat() if start else None,
                "requested_until_exclusive": end.isoformat() if end else None,
                "checked_at": utc_now(), "source": "Telegram API", "marked_read": False}

    async def context(self, chat_id, message_id, before=10, after=10):
        if message_id <= 0 or not 0 <= before <= 30 or not 0 <= after <= 30:
            raise ReaderError("Use a positive message_id; before and after must be between 0 and 30.")
        entity = await self.entity(chat_id)
        target = await self.client.get_messages(entity, ids=message_id)
        if target is None or isinstance(target, types.MessageEmpty):
            raise ReaderError("Message was deleted or is inaccessible.")
        older = [m async for m in self.client.iter_messages(entity, offset_id=message_id, limit=before)] if before else []
        newer = [m async for m in self.client.iter_messages(entity, min_id=message_id, reverse=True, limit=after)] if after else []
        return {"messages": [message_dict(m, int(chat_id)) for m in [*reversed(older), target, *newer]],
                "target_message_id": message_id, "order": "oldest_first", "checked_at": utc_now(), "marked_read": False}

    async def download(self, chat_id, message_id, max_bytes=20 * 1024 * 1024):
        if message_id <= 0 or not 1 <= max_bytes <= 50 * 1024 * 1024:
            raise ReaderError("Positive message_id required. max_bytes must be between 1 and 52428800.")
        entity = await self.entity(chat_id)
        message = await self.client.get_messages(entity, ids=message_id)
        if not message or not message.file:
            raise ReaderError("Message has no downloadable attachment.")
        chat = await self.client.get_entity(entity)
        if getattr(chat, "noforwards", False) or getattr(message, "noforwards", False) or getattr(message.media, "ttl_seconds", None):
            raise ReaderError("Downloading protected or disappearing media is not supported.")
        size = message.file.size
        if not size or size > max_bytes:
            raise ReaderError("Attachment size is unknown or exceeds max_bytes.")
        extension = message.file.ext or ".bin"
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", extension):
            extension = ".bin"
        root = data_dir() / "downloads"
        root.mkdir(exist_ok=True)
        target = root / f"{chat_id}_{message_id}_{uuid.uuid4().hex}{extension}"
        partial = target.with_suffix(target.suffix + ".part")
        count = 0
        try:
            with partial.open("xb") as handle:
                async for chunk in self.client.iter_download(message.media):
                    count += len(chunk)
                    if count > max_bytes:
                        raise ReaderError("Download exceeded max_bytes and was stopped.")
                    handle.write(chunk)
            if count != size:
                raise ReaderError("Incomplete download; retry the request.")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        return {"path": str(target.resolve()), "bytes": count, "original_name": message.file.name,
                "chat_id": str(chat_id), "message_id": message_id, "downloaded_at": utc_now()}
