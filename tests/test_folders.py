from types import SimpleNamespace as Obj

import pytest
from telethon import types, utils

from telegram_reader.core import Reader
from telegram_reader.folders import (
    analyze_chats, create_folder, list_folders, preview, suggest_plan,
)


class FolderClient:
    def __init__(self):
        self.filters = []
        self.calls = []
        self.dialogs = [
            Obj(id=10, name="Иван", entity=Obj(bot=False), is_user=True, is_group=False, is_channel=False,
                archived=False, unread_count=0, input_entity=types.InputPeerUser(10, 1)),
            Obj(id=-1000000000005, name="Работа · проект", entity=Obj(bot=False), is_user=False, is_group=True, is_channel=False,
                archived=False, unread_count=2, input_entity=types.InputPeerChannel(5, 6)),
            Obj(id=20, name="Weather Bot", entity=Obj(bot=True), is_user=True, is_group=False, is_channel=False,
                archived=False, unread_count=0, input_entity=types.InputPeerUser(20, 2)),
        ]

    def is_connected(self):
        return True

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return Obj(id=42, username="me")

    async def get_input_entity(self, value):
        for dialog in self.dialogs:
            if dialog.id == value:
                return dialog.input_entity
        return value

    def __call__(self, request):
        async def result():
            self.calls.append(request)
            if type(request).__name__ == "GetDialogFiltersRequest":
                return Obj(filters=self.filters)
            return True
        return result()

    async def iter_dialogs(self, **kwargs):
        for dialog in self.dialogs:
            yield dialog

    async def iter_messages(self, entity, **kwargs):
        if False:
            yield None


def make_reader():
    client = FolderClient()
    account = {"session": "test", "allowed_chat_ids": None}
    return Reader(lambda: account, lambda _: client), client


@pytest.mark.asyncio
async def test_analyze_classifies_bots_and_work_groups():
    reader, _ = make_reader()
    result = await analyze_chats(reader)
    by_title = {row["title"]: row for row in result["chats"]}
    assert by_title["Weather Bot"]["suggested_category"] == "Боты"
    assert by_title["Работа · проект"]["suggested_category"] == "Проекты"
    assert by_title["Иван"]["suggested_category"] == "Личное"


@pytest.mark.asyncio
async def test_list_folders_serializes_peer_ids():
    reader, client = make_reader()
    client.filters = [types.DialogFilter(id=2, title=types.TextWithEntities("Работа", []),
        pinned_peers=[], include_peers=[types.InputPeerChannel(5, 6)], exclude_peers=[], groups=True)]
    result = await list_folders(reader)
    assert result["folders"][0]["include_chat_ids"] == ["-1000000000005"]
    assert result["folders"][0]["rules"]["groups"] is True


@pytest.mark.asyncio
async def test_create_folder_builds_native_filter_and_enforces_title_limit():
    reader, client = make_reader()
    result = await create_folder(reader, "Боты", {"bots": True})
    assert result["created"]["folder_id"] == 2
    request = client.calls[-1]
    assert type(request).__name__ == "UpdateDialogFilterRequest"
    assert request.filter.bots is True
    with pytest.raises(ValueError, match="12 UTF-8"):
        await create_folder(reader, "Слишком длинное имя", {})


def test_preview_reports_create_and_update_without_mutation():
    current = {"folders": [{"folder_id": 2, "title": "Работа", "rules": {"groups": False},
                              "include_chat_ids": ["1"], "exclude_chat_ids": [], "pinned_chat_ids": []}]}
    result = preview(current, {"folders": [
        {"title": "Работа", "rules": {"groups": True}, "include_chat_ids": ["1", "2"], "exclude_chat_ids": [], "pinned_chat_ids": []},
        {"title": "Боты", "rules": {"bots": True}, "include_chat_ids": [], "exclude_chat_ids": [], "pinned_chat_ids": []},
    ]})
    assert result["change_count"] == 3
    assert {change["action"] for change in result["changes"]} == {"create", "update"}


@pytest.mark.asyncio
async def test_suggest_plan_is_read_only():
    reader, client = make_reader()
    result = await suggest_plan(reader, categories=["Работа", "Боты"])
    assert [folder["title"] for folder in result["folders"]] == ["Работа", "Боты"]
    assert client.calls == []
