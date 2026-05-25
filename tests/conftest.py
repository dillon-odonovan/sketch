import os

# config.py reads DISCORD_TOKEN and ANTHROPIC_API_KEY at import time. Set
# stubs before any test imports a module that pulls in config. Guild config
# is no longer an env var — tests that need a store instantiate
# StaticGuildConfigStore directly with a hand-built mapping.
#
# Plain assignment (not setdefault) so a present-but-empty inherited value
# — e.g. ANTHROPIC_API_KEY="" injected by a sandbox runner — gets replaced
# with our stub. setdefault would keep the empty value and config.py would
# reject it.
os.environ["DISCORD_TOKEN"] = "test-token"
os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"
