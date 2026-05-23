import os

# config.py reads DISCORD_TOKEN at import time. Set a stub value before any
# test imports a module that pulls in config. Guild config is no longer an
# env var — tests that need a store instantiate StaticGuildConfigStore
# directly with a hand-built mapping.
os.environ.setdefault("DISCORD_TOKEN", "test-token")
