"""Debug information: the text n8n offers under Help > About n8n, read as a source.

parse() turns the text into a small dictionary and nothing else survives. It works from
an allowlist: only the keys below are read, each by a strict reader that returns a value
of a fixed list, a whole number, a boolean or a version. A key that is not known, or a
value that does not fit, is dropped. The identifiers in the text (consumerId,
instanceKey, hostId, the user agent) are never read, so they cannot reach a snapshot, a
report, a profile or an error message.

The shape of the text is that of n8n's `useDebugInfo.ts`: a title, sections as
`## name`, items as `- key: value`, and a last line `Generated at: <ISO time>`. It
usually arrives through a ticket or a chat, so the reader does not depend on line ends,
blank lines, the `#` of a heading, the bullet character, indentation, `>` quote marks or
a code fence. Sections are recognised by their names.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Callable, Optional

MAX_BYTES = 64 * 1024

SECTIONS = ("core", "storage", "pruning", "security", "client", "cluster")
INSTANCE_TYPES = ("main", "worker", "webhook", "engine")
# A failed check with any other name is counted as OTHER_CHECK: names are n8n's, and a
# later n8n may add some.
KNOWN_CHECKS = ("hostid-clash", "lifecycle", "split-brain", "version-mismatch")
OTHER_CHECK = "other"

NO_CLUSTER = "this debug information has no cluster section (older n8n, or the instance registry was not loaded)"

# The refusals. Fixed sentences: no part of the text is ever echoed.
TOO_LARGE = f"the debug information is larger than {MAX_BYTES // 1024} KB"
NOT_DEBUG_INFO = (
    "this is not a debug information: no core section with a readable n8nVersion was found "
    "(copy the whole text from Help > About n8n > Debug information)"
)

# x.y.z, with the suffix the tool accepts elsewhere ("2.40.1-build7"). Only x.y.z is kept.
_VERSION = re.compile(r"v?([0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5})(?:[-+][0-9A-Za-z.+-]{0,40})?")
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ]|$)")
_FENCE = ("```", "~~~")


class DebugInfoError(ValueError):
    """A text that cannot be used as a debug information. The message never quotes it."""


# --- readers: each returns a known value, or None for anything that does not fit ----


def _one_of(values: dict) -> Callable:
    """A value of a fixed list, stored as the tool's own word for it."""
    return lambda raw: values.get(raw.lower())


def _boolean(raw: str) -> Optional[bool]:
    return {"true": True, "false": False}.get(raw.lower())


def _version(raw: str) -> Optional[str]:
    match = _VERSION.fullmatch(raw)
    return match.group(1) if match else None


def _concurrency(raw: str):
    """The limit on concurrent production executions: -1 is no limit, 0 is not valid."""
    if raw == "-1":
        return "unlimited"
    return int(raw) if re.fullmatch(r"[1-9][0-9]{0,8}", raw) else None


def _amount(unit: str) -> Callable:
    """A whole number followed by its unit, as in `336 hours`."""
    pattern = re.compile(rf"([0-9]{{1,9}}) {unit}s?")

    def read(raw: str) -> Optional[int]:
        match = pattern.fullmatch(raw)
        return int(match.group(1)) if match else None

    return read


_SAVE = _one_of({"all": "all", "none": "none"})

# section -> key in the text -> reader. Everything else in the text is ignored.
READERS = {
    "core": {
        "n8nVersion": _version,
        "nodeJsVersion": _version,
        "platform": _one_of(
            {"docker (self-hosted)": "docker_self_hosted", "docker (cloud)": "docker_cloud", "npm": "npm"}
        ),
        # n8n writes `postgres`; the tool's word is the value of DB_TYPE.
        "database": _one_of(
            {"sqlite": "sqlite", "postgres": "postgresdb", "mysqldb": "mysqldb", "mariadb": "mariadb"}
        ),
        "executionMode": _one_of(
            {"regular": "regular", "scaling (single-main)": "single-main", "scaling (multi-main)": "multi-main"}
        ),
        "concurrency": _concurrency,
        "license": _one_of(
            {
                "community": "community",
                "enterprise (production)": "enterprise_production",
                "enterprise (sandbox)": "enterprise_sandbox",
            }
        ),
    },
    "storage": {
        "success": _SAVE,
        "error": _SAVE,
        "progress": _boolean,
        "manual": _boolean,
        # `memory` in the text is the tool's `default` (N8N_DEFAULT_BINARY_DATA_MODE).
        "binaryMode": _one_of(
            {"memory": "default", "filesystem": "filesystem", "s3": "s3", "azure": "azure", "database": "database"}
        ),
    },
    "pruning": {"enabled": _boolean, "maxAge": _amount("hour"), "maxCount": _amount("execution")},
    "security": {"secureCookie": _boolean, "blockFileAccessToN8nFiles": _boolean},
}


def _pairs(body: str) -> dict:
    """The `key: value` pairs of one list line (`a: 1, b: 2`); the first of each key wins."""
    pairs: dict = {}
    for part in body.split(","):
        key, colon, value = part.partition(":")
        if colon:
            pairs.setdefault(key.strip(), value.strip())
    return pairs


def _generated_on(raw: str, today: date) -> Optional[str]:
    """The day of an ISO time; None when it is unreadable or in the future."""
    match = _DATE.match(raw)
    try:
        day = date(*(int(part) for part in match.groups())) if match else None
    except ValueError:
        return None
    return day.isoformat() if day is not None and day <= today else None


def parse(text: str, today: Optional[date] = None) -> dict:
    """The Snapshot section for a debug information (see models.Snapshot.debug_info).

    Raises DebugInfoError for a text that is too large or is not a debug information: one
    with a `core` section holding a readable `n8nVersion`. `today` is the day of the run,
    in UTC; a text dated after it has no date.
    """
    if not isinstance(text, str):
        raise DebugInfoError(NOT_DEBUG_INFO)
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        raise DebugInfoError(TOO_LARGE)
    today = today or datetime.now(timezone.utc).date()

    section = None
    found: dict = {name: {} for name in READERS}
    generated_on = None
    has_cluster = False
    instances = dict.fromkeys(INSTANCE_TYPES, 0)
    versions: set = set()
    checks_failed = None  # None until the list of checks, or one check, was seen

    # A byte order mark and the non-breaking spaces a chat puts in are not part of a value.
    for line in text.replace("\ufeff", "").replace("\xa0", " ").splitlines():
        line = line.strip().lstrip("> \t")  # quote marks, on every level
        body = line.lstrip("-*•").strip()  # the bullet; a heading's "#" stays
        if not body or line.startswith(_FENCE):
            continue
        key, colon, value = (part.strip() for part in body.partition(":"))
        if not colon or not value and (body.startswith("#") or key.lower() in SECTIONS):
            # A heading, with or without its "#". One that is not known ends the section:
            # its items must not be read as those of the section before it.
            name = body.lstrip("#").strip(" \t*_:").lower()
            section = name if name in SECTIONS else None
            has_cluster = has_cluster or section == "cluster"
            continue
        if key.lower() == "generated at":
            generated_on = _generated_on(value, today)
        elif section == "cluster":
            pairs = _pairs(body)
            if "instanceType" in pairs:  # one instance: counted by type, its ids never read
                if pairs["instanceType"] in INSTANCE_TYPES:
                    instances[pairs["instanceType"]] += 1
                versions.add(_version(pairs.get("version", "")))
            elif "check" in pairs and "status" in pairs:  # the warnings text is dropped
                checks_failed = checks_failed or []
                if pairs["status"] == "failed":
                    name = pairs["check"]
                    checks_failed.append(name if name in KNOWN_CHECKS else OTHER_CHECK)
            elif key == "checks":
                checks_failed = checks_failed or []
            elif key == "versions":
                versions.update(_version(part.strip()) for part in value.split(","))
        elif key in READERS.get(section, {}) and key not in found[section]:
            read = READERS[section][key](value)
            if read is not None:
                found[section][key] = read

    core, storage, pruning, security = (found[name] for name in ("core", "storage", "pruning", "security"))
    if "n8nVersion" not in core:
        raise DebugInfoError(NOT_DEBUG_INFO)
    mode = core.get("executionMode")
    versions.discard(None)
    return {
        "generated_on": generated_on,
        "n8n_version": core["n8nVersion"],
        "node_version": core.get("nodeJsVersion"),
        "platform": core.get("platform"),
        "database": core.get("database"),
        "execution_mode": None if mode is None else "regular" if mode == "regular" else "queue",
        "multi_main": None if mode is None else mode == "multi-main",
        "concurrency_limit": core.get("concurrency"),
        "license": core.get("license"),
        "save_success": storage.get("success"),
        "save_error": storage.get("error"),
        "save_progress": storage.get("progress"),
        "save_manual": storage.get("manual"),
        "binary_mode": storage.get("binaryMode"),
        "pruning": None
        if "enabled" not in pruning
        else {
            "enabled": pruning["enabled"],
            "max_age_hours": pruning.get("maxAge"),
            "max_count": pruning.get("maxCount"),
        },
        # True only when the text flags the setting as insecure; n8n omits it otherwise.
        "insecure": {
            "secure_cookie": security.get("secureCookie") is False,
            "file_access_blocked": security.get("blockFileAccessToN8nFiles") is False,
        },
        "cluster": {
            "instances": instances,
            "versions": sorted(versions, key=lambda version: tuple(int(part) for part in version.split("."))),
            "checks_failed": checks_failed,
        }
        if has_cluster
        else None,
    }


def label(section: dict) -> str:
    """How a fact's detail names a debug information: by its date."""
    day = section.get("generated_on")
    return f"debug information generated on {day}" if day else "debug information, date unknown"


def phrase(section: dict) -> str:
    """label() for the middle of a sentence of evidence: "The <phrase> lists ..."."""
    return label(section).replace(", date unknown", " of unknown date")
