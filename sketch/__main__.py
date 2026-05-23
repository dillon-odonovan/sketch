"""Container entrypoint.

Resolves DISCORD_TOKEN before importing the bot so config.py's import-time
read of os.environ succeeds.

Token resolution order:
1. DISCORD_TOKEN already in environment (set by local dev, docker run -e, or
   systemd EnvironmentFile when the secret is fetched on the host).
2. Google Secret Manager: fetch the latest version of the secret named by
   SKETCH_SECRET_DISCORD_TOKEN (default: sketch-discord-token) under
   GCP_PROJECT, using Application Default Credentials. On GCE this resolves
   via the metadata server; locally it uses `gcloud auth application-default
   login` credentials.
"""

import os
import sys


def _fetch_token_from_secret_manager() -> str:
    project = os.environ.get("GCP_PROJECT", "").strip()
    if not project:
        raise RuntimeError(
            "DISCORD_TOKEN is unset and GCP_PROJECT is not set; cannot resolve "
            "the token from Secret Manager."
        )
    secret_name = os.environ.get(
        "SKETCH_SECRET_DISCORD_TOKEN", "sketch-discord-token"
    ).strip()

    # Imported lazily so local runs with DISCORD_TOKEN preset don't pay the
    # cost or require the GCP client to be importable.
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("utf-8")


def main() -> None:
    if not os.environ.get("DISCORD_TOKEN", "").strip():
        os.environ["DISCORD_TOKEN"] = _fetch_token_from_secret_manager()

    # Import after the token is in env so config.py's required-var check passes.
    from sketch import bot

    bot.main()


if __name__ == "__main__":
    sys.exit(main())
