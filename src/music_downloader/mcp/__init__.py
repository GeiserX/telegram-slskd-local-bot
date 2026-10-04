"""The MCP front end: the same Pipeline as the Telegram bot, driven by an MCP client.

tools.py holds the tool logic over a Pipeline and returns plain dicts;
server.py registers those tools on an MCP server and serves them over stdio
(`python -m music_downloader mcp`) or streamable HTTP inside the bot process
(MCP_PORT, MCP_TOKEN). Nothing here imports telegram: the MCP caller is the
owner, so no Telegram allow-list or chat-delivery lock applies.
"""
