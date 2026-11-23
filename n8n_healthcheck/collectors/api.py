"""n8n's public API, with an API key: four usage numbers, read with GET and nothing else.

Executions per day and the failure rate (from the Insights summary, else counted from
the executions list), the users and the active workflows. A fifth call, the detail of
one user, only tells whether the key sees the whole instance: its answer is not read.

The key goes in one header of requests to the origin of the given URL and nowhere else:
a redirect to another origin is refused. Of every answer only counts, known status
words and dates are read; nothing else is kept, and no error text is passed on.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from ..models import Target
from ..usage import COUNTED, FAILED, NUMBERS, PERIOD_DAYS
from ._util import TIMEOUT

API = "/api/v1"
HEADER = "X-N8N-API-KEY"
PAGE_SIZE = 250  # the most n8n allows
MAX_PAGES = 40  # of one list: 10 000 items
MAX_REQUESTS = 130  # 1 + 40 for the executions, 40 + 1 for the users, 40 for the workflows, and a margin
MAX_BYTES = 32 * 1024 * 1024  # of one answer
TIME_LIMIT = 60  # seconds for the whole collector, on the clock
MAX_CURSOR = 2048
# A user id as n8n writes it; anything else is not put in a request path.
_USER_ID = re.compile(r"[0-9A-Za-z-]{1,64}")


class _Refused(Exception):
    """A call that gave no usable answer. `why` is the HTTP status or one of usage.REFUSED_WORDS."""

    def __init__(self, why):
        super().__init__()
        self.why = why


class _OtherOrigin(urllib.error.URLError):
    pass


def _origin(url: str) -> tuple:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or {"http": 80, "https": 443}.get(parts.scheme)


class _Session:
    """GET requests that carry the key, within the bounds of the collector."""

    def __init__(self, base: str, key: str):
        self._base, self._key = base, key
        self._deadline = time.monotonic() + TIME_LIMIT
        self._requests = 0
        self._lock = threading.Lock()
        self._sockets: list = []
        self._expired = False
        session, origin = self, _origin(base)

        class SameOrigin(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if _origin(newurl) != origin:
                    fp.close()
                    raise _OtherOrigin("redirect to another origin")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        # The sockets are kept so that the time limit can end a read that is under way: a
        # server that sends one byte every few seconds never trips the socket's own timeout.
        class Plain(http.client.HTTPConnection):
            def connect(self):
                super().connect()
                session._opened(self.sock)

        class Secure(http.client.HTTPSConnection):
            def connect(self):
                super().connect()
                session._opened(self.sock)

        class Http(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(Plain, req)

        class Https(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(Secure, req)

        self._open = urllib.request.build_opener(SameOrigin, Http, Https).open
        self._timer = threading.Timer(TIME_LIMIT, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _opened(self, sock) -> None:
        with self._lock:
            if self._expired:
                sock.close()
                raise TimeoutError
            self._sockets.append(sock)

    def _expire(self) -> None:
        with self._lock:
            self._expired = True
            for sock in self._sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:  # already closed
                    pass

    def close(self) -> None:
        self._timer.cancel()

    def get(self, path: str, read: bool = True):
        """The JSON a GET answers; None when `read` is False. Raises _Refused."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0 or self._expired:
            raise _Refused("slow")
        if self._requests >= MAX_REQUESTS:
            raise _Refused("limit")
        self._requests += 1
        request = urllib.request.Request(
            self._base + API + path, headers={HEADER: self._key, "Accept": "application/json"}, method="GET"
        )
        chunks, size = [], 0
        try:
            with self._open(request, timeout=max(0.1, min(TIMEOUT, remaining))) as response:
                if not read:
                    return None
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > MAX_BYTES:
                    raise _Refused("large")
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise _Refused("large")
                    if time.monotonic() >= self._deadline:
                        raise _Refused("slow")
                    chunks.append(chunk)
        except urllib.error.HTTPError as error:
            error.close()
            raise _Refused(error.code) from None
        except _OtherOrigin:
            raise _Refused("redirect") from None
        except (OSError, http.client.HTTPException, ValueError) as error:  # URLError is an OSError
            late = self._expired or time.monotonic() >= self._deadline
            timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
            raise _Refused("slow" if late or timed_out else "unreachable") from None
        if self._expired or time.monotonic() >= self._deadline:
            raise _Refused("slow")  # the read was ended by the time limit, not by the server
        try:
            return json.loads(b"".join(chunks))
        except (ValueError, RecursionError):
            raise _Refused("invalid") from None


def _pages(session: _Session, path: str):
    """Yield (items, cut) for each page of a list, MAX_PAGES at most.

    `cut` is True on the last page read when the list goes on beyond the bound.
    """
    cursor = None
    for number in range(1, MAX_PAGES + 1):
        body = session.get(f"{path}limit={PAGE_SIZE}" + (f"&cursor={quote(cursor, safe='')}" if cursor else ""))
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise _Refused("invalid")
        cursor = body.get("nextCursor")
        if cursor is not None and not (isinstance(cursor, str) and 0 < len(cursor) <= MAX_CURSOR):
            raise _Refused("invalid")
        yield [item for item in items if isinstance(item, dict)], cursor is not None and number == MAX_PAGES
        if cursor is None:
            return


def _count(body, name: str):
    value = body.get(name) if isinstance(body, dict) else None
    value = value.get("value") if isinstance(value, dict) else None
    return value if type(value) is int and value >= 0 else None


def _time(raw):
    """A date n8n wrote, in UTC; None for anything else."""
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _executions(session: _Session, now: datetime) -> dict:
    try:
        body = session.get("/insights/summary")  # no dates: the last 7 days
    except _Refused as refused:
        # Older n8n, Insights not available, scope missing or range not licensed.
        if refused.why not in (403, 404):
            raise
        return _listed(session, now)
    total, failed = _count(body, "total"), _count(body, "failed")
    if total is None or failed is None or failed > total:
        raise _Refused("invalid")
    return {
        "method": "insights",
        "total": total,
        "failed": failed,
        "period_days": PERIOD_DAYS,
        "estimate": False,
        "estimate_reason": None,
    }


def _listed(session: _Session, now: datetime) -> dict:
    """Count the executions list, newest first, back to PERIOD_DAYS before the run.

    n8n 1.x does not write a status in the list. There the executions that have stopped
    and are not waiting are counted, and how they ended stays unknown (`failed` is None).
    """
    cutoff = now - timedelta(days=PERIOD_DAYS)
    by_status = failed = stopped = 0
    with_status, oldest, reached, cut = True, None, False, False
    for items, cut in _pages(session, "/executions?"):
        for item in items:
            started = _time(item.get("startedAt"))
            if started is None:
                continue
            if started < cutoff:
                reached = True
                break
            oldest = started if oldest is None else min(oldest, started)
            status = item.get("status")
            if status is None:
                with_status = False
            elif status in COUNTED:
                by_status += 1
                failed += status in FAILED
            if item.get("stoppedAt") is not None and item.get("waitTill") is None:
                stopped += 1
        if reached:
            break
    # The time actually covered: the list may end, or the bound be reached, before 7 days.
    period = PERIOD_DAYS if reached else (now - oldest).total_seconds() / 86400 if oldest else 0
    if period < 1:
        raise _Refused("short")
    return {
        "method": "listed",
        "total": by_status if with_status else stopped,
        "failed": failed if with_status else None,
        "period_days": min(PERIOD_DAYS, round(period, 2)),
        "estimate": True,
        "estimate_reason": "saved_only" if reached else "page_limit" if cut else "short_history",
    }


def _users(session: _Session) -> tuple:
    """(users that are not pending, whether the key may read the detail of one user)."""
    count, user_id = 0, None
    for items, cut in _pages(session, "/users?"):
        count += sum(item.get("isPending") is not True for item in items)
        if user_id is None and items and isinstance(items[0].get("id"), str) and _USER_ID.fullmatch(items[0]["id"]):
            user_id = items[0]["id"]
        if cut:
            raise _Refused("limit")
    # Only an owner or an admin, who see every workflow and execution, may read a user's
    # detail; a member's key lists the users all the same. Only the status is looked at.
    try:
        if user_id is None:
            raise _Refused("invalid")
        session.get(f"/users/{user_id}", read=False)
    except _Refused:
        return count, False
    return count, True


def _active_workflows(session: _Session) -> int:
    count = 0
    for items, cut in _pages(session, "/workflows?active=true&"):
        count += len(items)
        if cut:
            raise _Refused("limit")
    return count


def _unencrypted(url: str) -> bool:
    """True when the key travels over plain http to a host that is not this machine."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "http" or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def collect(target: Target) -> dict:
    if not target.url:
        raise ValueError("an API key needs the n8n URL")
    if "@" in urlsplit(target.url).netloc:
        raise ValueError("credentials in the URL are not supported; pass the URL without user:password@")
    usage = {
        "executions": None,
        "active_workflows": None,
        "users": None,
        "refused": dict.fromkeys(NUMBERS),
        "whole_instance": False,
        "key_unencrypted": _unencrypted(target.url),
    }
    session = _Session(target.url.rstrip("/"), target.api_key)
    now = datetime.now(timezone.utc)
    try:
        # The heaviest answers last.
        for name in NUMBERS:
            try:
                if name == "executions":
                    usage[name] = _executions(session, now)
                elif name == "users":
                    usage[name], usage["whole_instance"] = _users(session)
                else:
                    usage[name] = _active_workflows(session)
            except _Refused as refused:
                usage["refused"][name] = refused.why
                if refused.why == 401 and name == "executions":
                    # The key itself was refused: the other calls would say the same.
                    usage["refused"] = dict.fromkeys(NUMBERS, 401)
                    break
    finally:
        session.close()
    return {"usage": usage}
