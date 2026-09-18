import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_server_has_only_expected_tools(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "telegram_reader.server"],
                                   env={"TELEGRAM_READER_DATA_DIR": str(tmp_path)})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            expected = {"connection_status", "list_chats", "list_folders", "inspect_folder", "analyze_chats_for_folders", "suggest_folder_plan", "preview_folder_changes", "create_folder", "update_folder", "set_folder_chats", "reorder_folders", "delete_folder", "list_folder_backups", "restore_folder_backup", "apply_folder_plan", "read_history", "search_messages", "message_context", "download_attachment", "prepare_message", "send_message", "message_delivery_status"}
            assert {t.name for t in listing.tools} == expected
            assert all(t.annotations.destructiveHint is False for t in listing.tools if t.name not in {"create_folder", "update_folder", "set_folder_chats", "reorder_folders", "delete_folder", "restore_folder_backup", "apply_folder_plan"})
            send = next(t for t in listing.tools if t.name == "send_message")
            assert send.annotations.readOnlyHint is False
            assert send.annotations.idempotentHint is True
            status = await session.call_tool("connection_status", {})
            assert status.structuredContent["connected"] is False
            result = await session.call_tool("read_history", {"chat_id": "1"})
            assert result.structuredContent["error"] == "request_rejected"
            assert "Connect.cmd" in result.structuredContent["message"]
            sending = await session.call_tool("send_message", {"draft_id": "nonexistent"})
            assert sending.structuredContent["error"] == "request_rejected"
