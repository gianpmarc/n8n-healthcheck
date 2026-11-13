"""Redis: server info and the depth of n8n's Bull queue, over a plain RESP socket."""

import socket
import ssl
from urllib.parse import unquote, urlsplit

from ..models import Target
from ._util import TIMEOUT, warn

# n8n queue mode uses Bull with prefix "bull" (QUEUE_BULL_PREFIX) and queue "jobs".
# Bull keeps waiting and active jobs in lists, failed jobs in a sorted set, and the
# job id counter in "<queue>:id", which exists as soon as one job has been enqueued.
DEFAULT_PREFIX = "bull"


def collect(target: Target, prefix: str = DEFAULT_PREFIX) -> dict:
    queue = f"{prefix}:jobs"
    url = urlsplit(target.redis_url)
    if url.scheme not in ("redis", "rediss"):
        raise ValueError("URL must start with redis:// or rediss://")
    sock = socket.create_connection((url.hostname, url.port or 6379), timeout=TIMEOUT)
    try:
        if url.scheme == "rediss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=url.hostname)
        stream = sock.makefile("rwb")
        if url.password:
            user = [unquote(url.username)] if url.username else []
            command(stream, "AUTH", *user, unquote(url.password))
        database = url.path.strip("/")
        if database:
            command(stream, "SELECT", database)
        info = parse_info(command(stream, "INFO"))
        # LLEN/ZCARD answer 0 for a missing key, so a wrong prefix would look like an idle queue.
        if command(stream, "EXISTS", queue + ":id"):
            waiting = command(stream, "LLEN", queue + ":wait")
            active = command(stream, "LLEN", queue + ":active")
            failed = command(stream, "ZCARD", queue + ":failed")
        else:
            waiting = active = failed = None
            warn("redis queue", f'"{queue}" not found: no job enqueued yet, or another QUEUE_BULL_PREFIX')
        return {
            "redis": {
                "version": info.get("redis_version", ""),
                "waiting_jobs": waiting,
                "active_jobs": active,
                "failed_jobs": failed,
                "used_memory_bytes": int(info.get("used_memory", 0)),
                "maxmemory_bytes": int(info.get("maxmemory", 0)),  # 0 = no limit
                "maxmemory_policy": info.get("maxmemory_policy", ""),
            }
        }
    finally:
        sock.close()


def command(stream, *args: str):
    """Send one command as a RESP array of bulk strings and return the decoded reply."""
    parts = [arg.encode() for arg in args]
    stream.write(b"*%d\r\n" % len(parts) + b"".join(b"$%d\r\n%s\r\n" % (len(p), p) for p in parts))
    stream.flush()
    return read_reply(stream)


def read_reply(stream):
    """Decode one RESP2 reply: simple string, error, integer, bulk string or array."""
    line = stream.readline()
    if not line.endswith(b"\r\n"):
        raise ConnectionError("connection closed by Redis")
    kind, payload = line[:1], line[1:-2].decode("utf-8", "replace")
    if kind == b"+":
        return payload
    if kind == b"-":
        raise RuntimeError(payload)
    if kind == b":":
        return int(payload)
    if kind == b"$":
        length = int(payload)
        return None if length < 0 else stream.read(length + 2)[:-2].decode("utf-8", "replace")
    if kind == b"*":
        return [read_reply(stream) for _ in range(int(payload))]
    raise ValueError(f"unexpected RESP reply type {kind!r}")


def parse_info(text: str) -> dict:
    """INFO output ('key:value' lines, '#' section headers) -> dict."""
    info = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith("#"):
            info[key] = value.strip()
    return info
