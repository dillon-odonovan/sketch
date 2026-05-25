"""Container entrypoint.

Resolves DISCORD_TOKEN and ANTHROPIC_API_KEY before importing the bot so
config.py's import-time required-var checks succeed.

Resolution order for each variable:
1. Variable already in environment (set by local dev, docker run -e, or
   systemd EnvironmentFile when the secret is fetched on the host).
2. Google Secret Manager: fetch the latest version of the secret named by
   the corresponding SKETCH_SECRET_<NAME> override (defaults below) under
   GCP_PROJECT, using Application Default Credentials. On GCE this resolves
   via the metadata server; locally it uses `gcloud auth application-default
   login` credentials.

Defaults (env var ← secret name; override env var in parens):
  DISCORD_TOKEN     ← sketch-discord-token      (SKETCH_SECRET_DISCORD_TOKEN)
  ANTHROPIC_API_KEY ← sketch-anthropic-api-key  (SKETCH_SECRET_ANTHROPIC_API_KEY)
"""

import os
import sys

_SECRETS: tuple[tuple[str, str, str], ...] = (
    ("DISCORD_TOKEN", "SKETCH_SECRET_DISCORD_TOKEN", "sketch-discord-token"),
    (
        "ANTHROPIC_API_KEY",
        "SKETCH_SECRET_ANTHROPIC_API_KEY",
        "sketch-anthropic-api-key",
    ),
)


def _fetch_from_secret_manager(secret_name: str) -> str:
    project = os.environ.get("GCP_PROJECT", "").strip()
    if not project:
        raise RuntimeError(
            f"GCP_PROJECT is not set; cannot resolve {secret_name!r} from "
            "Secret Manager."
        )

    # Imported lazily so local runs with the env vars preset don't pay the
    # cost or require the GCP client to be importable.
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("utf-8")


def main() -> None:
    for env_var, override_var, default_secret in _SECRETS:
        if os.environ.get(env_var, "").strip():
            continue
        secret_name = os.environ.get(override_var, "").strip() or default_secret
        os.environ[env_var] = _fetch_from_secret_manager(secret_name)

    # Import after the env vars are populated so config.py's required-var
    # checks pass.
    from sketch import bot

    bot.main()


if __name__ == "__main__":
    sys.exit(main())
