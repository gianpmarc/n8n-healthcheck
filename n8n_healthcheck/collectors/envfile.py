"""n8n environment variables from a KEY=VALUE file (docker --env-file / .env style)."""

from ..models import Target


def collect(target: Target) -> dict:
    with open(target.env_file, encoding="utf-8") as handle:
        return {"env": parse_env(handle.read())}


def parse_env(text: str) -> dict:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env
