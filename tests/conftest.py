import os

# config.py reads DISCORD_TOKEN and GUILD_CONFIG_JSON at import time. Set stub
# values before any test imports a module that pulls in config.
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault(
    "GUILD_CONFIG_JSON",
    '{"111111111111111111": {"spreadsheet_id": "test-spreadsheet"}}',
)
