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
            assert {t.name for t in listing.tools} == {"connection_status", "list_chats", "read_history", "search_messages", "message_context", "download_attachment", "prepare_message", "send_message", "message_delivery_status"}
            assert all(t.annotations.destructiveHint is False for t in listing.tools)
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
