"""Telegram dialog-folder inspection, planning and mutation helpers."""
from __future__ import annotations

import re
import uuid
import json
from datetime import datetime, timezone

from telethon import types, utils
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    UpdateDialogFilterRequest,
    UpdateDialogFiltersOrderRequest,
)
from .storage import data_dir, protect, unprotect


FOLDER_FLAGS = (
    "contacts", "non_contacts", "groups", "broadcasts", "bots",
    "exclude_muted", "exclude_read", "exclude_archived",
)
DEFAULT_TITLES = ("Работа", "Личное", "Боты", "Каналы", "Проекты", "Не разобрано")
WORK_WORDS = re.compile(r"(?:работ|work|project|проект|клиент|client|заказ|team|команд|коллег|office|job|task|задач)", re.I)


def _title(filter_obj) -> str:
    title = getattr(filter_obj, "title", "")
    return getattr(title, "text", None) or str(title)


def _ids(peers) -> list[str]:
    result = []
    for peer in peers or []:
        try:
            result.append(str(utils.get_peer_id(peer)))
        except (TypeError, ValueError):
            continue
    return result


def _filter_dict(filter_obj) -> dict:
    return {
        "folder_id": int(filter_obj.id),
        "title": _title(filter_obj),
        "emoticon": getattr(filter_obj, "emoticon", None),
        "color": getattr(filter_obj, "color", None),
        "rules": {key: bool(getattr(filter_obj, key, False)) for key in FOLDER_FLAGS},
        "pinned_chat_ids": _ids(getattr(filter_obj, "pinned_peers", [])),
        "include_chat_ids": _ids(getattr(filter_obj, "include_peers", [])),
        "exclude_chat_ids": _ids(getattr(filter_obj, "exclude_peers", [])),
    }


def _visible(reader, data: dict) -> dict:
    """Do not reveal folder peers outside the locally selected Telegram scope."""
    result = dict(data)
    for key in ("pinned_chat_ids", "include_chat_ids", "exclude_chat_ids"):
        result[key] = [chat_id for chat_id in data[key] if reader.allowed(int(chat_id))]
    return result


def _validate_title(title: str) -> str:
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Folder title cannot be empty.")
    title = title.strip()
    if len(title.encode("utf-8")) > 12:
        raise ValueError("Telegram folder titles are limited to 12 UTF-8 bytes.")
    return title


def _kind(dialog) -> str:
    entity = getattr(dialog, "entity", None)
    if getattr(entity, "bot", False):
        return "bot"
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_channel", False):
        return "channel"
    return "user"


def _category(kind: str, title: str, sample: str = "") -> tuple[str, float, str]:
    signal = f"{title} {sample}".strip()
    if kind == "bot":
        return "Боты", 1.0, "Telegram пометил чат как бота"
    if kind == "channel":
        return "Каналы", 0.98, "это канал или рассылка"
    if kind == "user":
        return ("Работа", 0.82, "в названии или последних сообщениях есть рабочие слова") if WORK_WORDS.search(signal) else ("Личное", 0.78, "личный диалог без рабочего сигнала")
    if WORK_WORDS.search(signal):
        return "Проекты", 0.82, "групповой чат с рабочими словами"
    return "Не разобрано", 0.38, "тип чата понятен, но категорию нельзя надёжно вывести автоматически"


async def _dialogs(reader):
    client = await reader.connect()
    rows = []
    async for dialog in client.iter_dialogs(limit=2000):
        if reader.allowed(dialog.id):
            rows.append(dialog)
    return rows


async def list_folders(reader):
    client = await reader.connect()
    result = await client(GetDialogFiltersRequest())
    filters = list(getattr(result, "filters", []) or [])
    rows = [_visible(reader, _filter_dict(item)) for item in filters]
    return {"folders": rows, "count": len(rows), "editable_folder_ids": [r["folder_id"] for r in rows if r["folder_id"] > 1], "checked_at": datetime.now(timezone.utc).isoformat()}


async def _get_filter(reader, folder_id: int):
    if not 2 <= folder_id <= 255:
        raise ValueError("Only user-created folder IDs from 2 to 255 can be changed.")
    client = await reader.connect()
    result = await client(GetDialogFiltersRequest())
    for item in getattr(result, "filters", []) or []:
        if int(item.id) == folder_id:
            return item
    raise ValueError("Folder was not found.")


async def inspect_folder(reader, folder_id: int):
    item = await _get_filter(reader, folder_id)
    details = _visible(reader, _filter_dict(item))
    dialogs = await _dialogs(reader)
    by_id = {str(d.id): d for d in dialogs}
    members = []
    for chat_id in details["include_chat_ids"]:
        d = by_id.get(chat_id)
        members.append({"chat_id": chat_id, "title": getattr(d, "name", None), "source": "explicit_include", "accessible": d is not None})
    for chat_id in details["pinned_chat_ids"]:
        if chat_id not in details["include_chat_ids"]:
            d = by_id.get(chat_id)
            members.append({"chat_id": chat_id, "title": getattr(d, "name", None), "source": "pinned", "accessible": d is not None})
    return {**details, "members": members, "note": "Rule-based membership can also include chats not listed explicitly; use analyze_chats_for_folders for a complete candidate scan."}


async def analyze_chats(reader, chat_ids=None, include_recent_messages=False, recent_messages_per_chat=20):
    if not 0 <= recent_messages_per_chat <= 50:
        raise ValueError("recent_messages_per_chat must be between 0 and 50.")
    wanted = {str(x) for x in chat_ids} if chat_ids else None
    rows = []
    for dialog in await _dialogs(reader):
        if wanted is not None and str(dialog.id) not in wanted:
            continue
        sample = ""
        sample_count = 0
        if include_recent_messages and recent_messages_per_chat:
            async for message in reader.client.iter_messages(dialog.input_entity, limit=recent_messages_per_chat):
                text = getattr(message, "message", None) or ""
                if text:
                    sample += " " + text[:1000]
                sample_count += 1
        category, confidence, reason = _category(_kind(dialog), getattr(dialog, "name", ""), sample)
        rows.append({
            "chat_id": str(dialog.id), "title": dialog.name, "kind": _kind(dialog),
            "archived": bool(getattr(dialog, "archived", False)),
            "unread_count": getattr(dialog, "unread_count", 0),
            "suggested_category": category, "confidence": confidence, "reason": reason,
            "recent_messages_scanned": sample_count,
        })
    return {"chats": rows, "count": len(rows), "used_message_samples": include_recent_messages, "scan_limit": 2000, "checked_at": datetime.now(timezone.utc).isoformat()}


async def suggest_plan(reader, categories=None, include_recent_messages=False):
    categories = categories or list(DEFAULT_TITLES)
    allowed = set(DEFAULT_TITLES)
    unknown = [x for x in categories if x not in allowed]
    if unknown:
        raise ValueError(f"Unknown categories: {', '.join(unknown)}. Supported: {', '.join(DEFAULT_TITLES)}.")
    analysis = await analyze_chats(reader, include_recent_messages=include_recent_messages)
    plans = []
    for title in categories:
        chats = [r["chat_id"] for r in analysis["chats"] if r["suggested_category"] == title]
        rules = {}
        if title == "Боты": rules = {"bots": True}
        elif title == "Каналы": rules = {"broadcasts": True}
        elif title == "Личное": rules = {"contacts": True}
        plans.append({"title": title, "rules": rules, "include_chat_ids": chats, "exclude_chat_ids": [], "pinned_chat_ids": []})
    return {"folders": plans, "analysis": analysis, "overlap_policy": "Chats may appear in more than one folder; explicit includes are preserved.", "requires_preview": True}


async def _resolve(reader, values):
    return [await reader.entity(str(value)) for value in (values or [])]


async def _request_filter(reader, folder_id, current=None, title=None, rules=None, include=None, exclude=None, pinned=None,
                          include_peers_override=None, exclude_peers_override=None, pinned_peers_override=None):
    current_dict = _filter_dict(current) if current else {"rules": {}, "include_chat_ids": [], "exclude_chat_ids": [], "pinned_chat_ids": []}
    title = _validate_title(title if title is not None else current_dict.get("title", ""))
    merged_rules = {key: bool(current_dict.get("rules", {}).get(key, False)) for key in FOLDER_FLAGS}
    if rules:
        for key, value in rules.items():
            if key not in FOLDER_FLAGS:
                raise ValueError(f"Unknown folder rule: {key}.")
            merged_rules[key] = bool(value)
    include_peers = include_peers_override if include_peers_override is not None else (list(getattr(current, "include_peers", []) or []) if include is None else await _resolve(reader, include))
    exclude_peers = exclude_peers_override if exclude_peers_override is not None else (list(getattr(current, "exclude_peers", []) or []) if exclude is None else await _resolve(reader, exclude))
    pinned_peers = pinned_peers_override if pinned_peers_override is not None else (list(getattr(current, "pinned_peers", []) or []) if pinned is None else await _resolve(reader, pinned))
    kwargs = {key: value for key, value in merged_rules.items()}
    kwargs.update(id=folder_id, title=types.TextWithEntities(title, []),
                  include_peers=include_peers, exclude_peers=exclude_peers, pinned_peers=pinned_peers)
    return types.DialogFilter(**kwargs)


async def create_folder(reader, title, rules=None, include_chat_ids=None, exclude_chat_ids=None, pinned_chat_ids=None):
    title = _validate_title(title)
    if not any(bool(value) for value in (rules or {}).values()) and not (include_chat_ids or pinned_chat_ids):
        raise ValueError("A folder needs at least one automatic rule or an included/pinned chat.")
    client = await reader.connect()
    existing = await client(GetDialogFiltersRequest())
    ids = [int(x.id) for x in getattr(existing, "filters", []) or []]
    folder_id = max([1, *ids]) + 1
    if folder_id > 255:
        raise ValueError("Telegram has no free user folder ID.")
    obj = await _request_filter(reader, folder_id, title=title, rules=rules, include=include_chat_ids or [], exclude=exclude_chat_ids or [], pinned=pinned_chat_ids or [])
    await client(UpdateDialogFilterRequest(id=folder_id, filter=obj))
    return {"created": _visible(reader, _filter_dict(obj)), "changed": True}


async def update_folder(reader, folder_id, title=None, rules=None):
    current = await _get_filter(reader, int(folder_id))
    obj = await _request_filter(reader, int(folder_id), current=current, title=title, rules=rules)
    await reader.client(UpdateDialogFilterRequest(id=int(folder_id), filter=obj))
    return {"updated": _visible(reader, _filter_dict(obj)), "changed": True}


async def set_folder_chats(reader, folder_id, include_chat_ids=None, exclude_chat_ids=None, pinned_chat_ids=None, replace=False):
    current = await _get_filter(reader, int(folder_id))
    old = _filter_dict(current)
    def merge(key, values):
        values = [str(x) for x in (values or [])]
        return values if replace else list(dict.fromkeys(old[key] + values))
    if replace:
        async def replace_in_scope(old_peers, desired_ids):
            preserved = []
            for peer in old_peers or []:
                try:
                    if not reader.allowed(utils.get_peer_id(peer)):
                        preserved.append(peer)
                except (TypeError, ValueError):
                    preserved.append(peer)
            return preserved + await _resolve(reader, desired_ids or [])
        obj = await _request_filter(reader, int(folder_id), current=current,
                                    include_peers_override=await replace_in_scope(getattr(current, "include_peers", []), include_chat_ids),
                                    exclude_peers_override=await replace_in_scope(getattr(current, "exclude_peers", []), exclude_chat_ids),
                                    pinned_peers_override=await replace_in_scope(getattr(current, "pinned_peers", []), pinned_chat_ids))
    else:
        def peer_key(peer):
            try:
                return utils.get_peer_id(peer)
            except (TypeError, ValueError):
                return None
        def append_unique(old_peers, new_peers):
            existing = list(old_peers or [])
            seen = {peer_key(peer) for peer in existing}
            for peer in new_peers:
                if peer_key(peer) not in seen:
                    existing.append(peer)
                    seen.add(peer_key(peer))
            return existing
        obj = await _request_filter(
            reader, int(folder_id), current=current,
            include_peers_override=append_unique(getattr(current, "include_peers", []), await _resolve(reader, include_chat_ids or [])) if include_chat_ids else None,
            exclude_peers_override=append_unique(getattr(current, "exclude_peers", []), await _resolve(reader, exclude_chat_ids or [])) if exclude_chat_ids else None,
            pinned_peers_override=append_unique(getattr(current, "pinned_peers", []), await _resolve(reader, pinned_chat_ids or [])) if pinned_chat_ids else None,
        )
    await reader.client(UpdateDialogFilterRequest(id=int(folder_id), filter=obj))
    return {"updated": _filter_dict(obj), "changed": True, "replace": replace}


async def reorder_folders(reader, order):
    order = [int(x) for x in order]
    current = await list_folders(reader)
    custom = {x["folder_id"] for x in current["folders"] if x["folder_id"] > 1}
    if set(order) != custom:
        raise ValueError(f"order must contain exactly these editable folder IDs: {sorted(custom)}")
    await reader.client(UpdateDialogFiltersOrderRequest(order=order))
    return {"changed": True, "order": order}


async def delete_folder(reader, folder_id):
    await _get_filter(reader, int(folder_id))
    await reader.client(UpdateDialogFilterRequest(id=int(folder_id), filter=None))
    return {"deleted_folder_id": int(folder_id), "changed": True}


async def backup_folders(reader):
    """Save a DPAPI-protected folder snapshot outside the plugin directory."""
    client = await reader.connect()
    result = await client(GetDialogFiltersRequest())
    backup_id = uuid.uuid4().hex
    payload = {"backup_id": backup_id, "created_at": datetime.now(timezone.utc).isoformat(),
               "folders": [_filter_dict(item) for item in getattr(result, "filters", []) or []]}
    (data_dir() / f"folders-{backup_id}.dpapi").write_bytes(protect(json.dumps(payload).encode("utf-8")))
    return {"backup_id": backup_id, "created_at": payload["created_at"], "folder_count": len(payload["folders"])}


def _load_backup(backup_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(backup_id)):
        raise ValueError("Invalid folder backup ID.")
    path = data_dir() / f"folders-{backup_id}.dpapi"
    if not path.exists():
        raise ValueError("Folder backup was not found on this Windows account.")
    return json.loads(unprotect(path.read_bytes()))


def list_backups():
    rows = []
    for path in sorted(data_dir().glob("folders-*.dpapi"), key=lambda p: p.stat().st_mtime, reverse=True):
        backup_id = path.stem.removeprefix("folders-")
        try:
            payload = _load_backup(backup_id)
            rows.append({"backup_id": backup_id, "created_at": payload.get("created_at"), "folder_count": len(payload.get("folders", []))})
        except Exception:
            continue
    return {"backups": rows}


async def restore_backup(reader, backup_id):
    """Restore the selected-scope portion of a snapshot and preserve out-of-scope peers."""
    payload = _load_backup(backup_id)
    current = await list_folders(reader)
    current_by_id = {item["folder_id"]: item for item in current["folders"] if item["folder_id"] > 1}
    saved = {item["folder_id"]: item for item in payload.get("folders", []) if item["folder_id"] > 1}
    results = []
    for folder_id, item in saved.items():
        if folder_id in current_by_id:
            results.append(await update_folder(reader, folder_id, item["title"], item.get("rules")))
            visible = lambda values: [x for x in values if reader.allowed(int(x))]
            results.append(await set_folder_chats(reader, folder_id, visible(item.get("include_chat_ids", [])), visible(item.get("exclude_chat_ids", [])), visible(item.get("pinned_chat_ids", [])), replace=True))
        else:
            visible = lambda values: [x for x in values if reader.allowed(int(x))]
            results.append(await create_folder(reader, item["title"], item.get("rules"), visible(item.get("include_chat_ids", [])), visible(item.get("exclude_chat_ids", [])), visible(item.get("pinned_chat_ids", []))))
    for folder_id in set(current_by_id) - set(saved):
        results.append(await delete_folder(reader, folder_id))
    return {"restored": True, "backup_id": backup_id, "results": results, "folders": await list_folders(reader)}


def preview(current, plan):
    existing = {x["title"]: x for x in current["folders"]}
    changes = []
    for item in plan.get("folders", []):
        old = existing.get(item.get("title"))
        if old is None:
            changes.append({"action": "create", "title": item.get("title"), "include_chat_ids": item.get("include_chat_ids", [])})
        else:
            for key in ("rules", "include_chat_ids", "exclude_chat_ids", "pinned_chat_ids"):
                if key in item and item[key] != old.get(key):
                    changes.append({"action": "update", "folder_id": old["folder_id"], "title": old["title"], "field": key, "before": old.get(key), "after": item[key]})
    return {"changes": changes, "change_count": len(changes), "safe_to_apply": True, "requires_explicit_apply": True}
