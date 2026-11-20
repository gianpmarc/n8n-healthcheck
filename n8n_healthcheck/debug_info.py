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
a code fence. Sections are recognised by their names. An instance or check line that a
mail client broke in two is joined again before it is read.

Whoever holds a section reads it through of(), which checks every value again: a
Snapshot can be built by hand or loaded from a file, and nothing but a known value, a
number that is not negative, `unlimited` or a version may come out of one.
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
SEVERAL = "the text holds more than one debug information: give a single one, the most recent"

# x.y.z, with the suffix the tool accepts elsewhere ("2.40.1-build7"). Only x.y.z is kept.
_VERSION = re.compile(r"v?([0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5})(?:[-+][0-9A-Za-z.+-]{0,40})?")
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ]|$)")
_FENCE = ("```", "~~~")
_PLAIN_VERSION = re.compile(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}")


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


# The words n8n writes -> the tool's own. n8n 1.x wrote `mysql` and a bare `scaling`.
_PLATFORM = {"docker (self-hosted)": "docker_self_hosted", "docker (cloud)": "docker_cloud", "npm": "npm"}
# n8n writes `postgres`; the tool's word is the value of DB_TYPE.
_DATABASE = {"sqlite": "sqlite", "postgres": "postgresdb", "mysql": "mysqldb", "mysqldb": "mysqldb", "mariadb": "mariadb"}
# value -> (execution_mode, multi_main). `scaling` alone does not say how many mains.
_MODE = {
    "regular": ("regular", False),
    "scaling": ("queue", None),
    "scaling (single-main)": ("queue", False),
    "scaling (multi-main)": ("queue", True),
}
_LICENSE = {
    "community": "community",
    "enterprise (production)": "enterprise_production",
    "enterprise (sandbox)": "enterprise_sandbox",
}
_SAVE = {"all": "all", "none": "none"}
# `memory` in the text is the tool's `default` (N8N_DEFAULT_BINARY_DATA_MODE).
_BINARY = {"memory": "default", "filesystem": "filesystem", "s3": "s3", "azure": "azure", "database": "database"}

# section -> key in the text -> reader. Everything else in the text is ignored.
READERS = {
    "core": {
        "n8nVersion": _version,
        "nodeJsVersion": _version,
        "platform": _one_of(_PLATFORM),
        "database": _one_of(_DATABASE),
        "executionMode": _one_of(_MODE),
        "concurrency": _concurrency,
        "license": _one_of(_LICENSE),
    },
    "storage": {
        "success": _one_of(_SAVE),
        "error": _one_of(_SAVE),
        "progress": _boolean,
        "manual": _boolean,
        "binaryMode": _one_of(_BINARY),
    },
    "pruning": {"enabled": _boolean, "maxAge": _amount("hour"), "maxCount": _amount("execution")},
    "security": {"secureCookie": _boolean, "blockFileAccessToN8nFiles": _boolean},
}
# The keys of the cluster section; an instance line starts with `instanceKey`, a check
# line with `check`.
_CLUSTER_KEYS = ("instanceCount", "versions", "instances", "checks", "instanceKey", "check")
TITLE = "debug info"


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


def _heading(body: str) -> str:
    """What a line would name as a heading: without its "#", bold marks or colon."""
    return body.lstrip("#").strip(" \t*_:").lower()


def _sort_versions(versions) -> list:
    return sorted(versions, key=lambda version: tuple(int(part) for part in version.split(".")))


def parse(text: str, today: Optional[date] = None) -> dict:
    """The Snapshot section for a debug information (see models.Snapshot.debug_info).

    Raises DebugInfoError for a text that is too large, is not a debug information (one
    with a `core` section holding a readable `n8nVersion`), or holds more than one. Two
    texts are never merged, since old values would then stand under the newer date.
    `today` is the day of the run, in UTC; a text dated after it has no date.

    A text holds more than one when a second `Generated at` is met, when a section that
    already held an item is opened again and holds one again, or when a key that is read
    comes twice in one section. A line that only names the title or a section proves
    nothing: a bug-report form, a ticket label or a signature puts one above or below a
    single text. Instance and check lines are list items, not keys: several are normal.
    """
    if not isinstance(text, str):
        raise DebugInfoError(NOT_DEBUG_INFO)
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        raise DebugInfoError(TOO_LARGE)
    today = today or datetime.now(timezone.utc).date()

    section = None
    holds = False  # an item was read under the section heading that is open now
    held: set = set()  # the sections an earlier heading of which held an item
    keys: dict = {name: set() for name in SECTIONS}  # the keys met in each section
    dated = False
    found: dict = {name: {} for name in READERS}
    generated_on = None
    instances = dict.fromkeys(INSTANCE_TYPES, 0)
    versions: set = set()
    checks_failed = None  # None until the list of checks, or one check, was seen
    item = None  # an instance or check line of the cluster section, until its last part

    def reading(key: Optional[str] = None) -> None:
        """An item of the open section is about to be read; `key` unless it is a list item."""
        nonlocal holds
        if section in held or key in keys[section]:
            raise DebugInfoError(SEVERAL)
        holds = True
        if key is not None:
            keys[section].add(key)

    def close_item() -> None:
        nonlocal item, checks_failed
        if item is None:
            return
        pairs, item = _pairs(item), None
        if "check" in pairs:  # the warnings text is dropped
            checks_failed = checks_failed or []
            if pairs.get("status") == "failed":
                checks_failed.append(pairs["check"] if pairs["check"] in KNOWN_CHECKS else OTHER_CHECK)
        else:  # one instance: counted by type, its ids never read
            if pairs.get("instanceType") in INSTANCE_TYPES:
                instances[pairs["instanceType"]] += 1
            versions.add(_version(pairs.get("version", "")))

    # A byte order mark and the non-breaking spaces a chat puts in are not part of a value.
    for line in text.replace("\ufeff", "").replace("\xa0", " ").splitlines():
        line = line.strip().lstrip("> \t")  # quote marks, on every level
        body = line.lstrip("-*•").strip()  # the bullet; a heading's "#" stays
        if not body or line.startswith(_FENCE):
            close_item()  # a blank line ends a list item
            continue
        key, colon, value = (part.strip() for part in body.partition(":"))
        if item is not None:
            # The rest of an instance or check line a mail client broke. Those are the
            # only long lines that are read, so only there can a line be a continuation:
            # it follows the item with no blank line between, has no bullet and no "#",
            # and starts nothing the text is known to hold. Anywhere else a line without
            # a colon is a heading (see below).
            starts = key in _CLUSTER_KEYS or key.lower() == "generated at" or _heading(body) in (*SECTIONS, TITLE)
            if line == body and not body.startswith("#") and not starts:
                item = f"{item} {body}"
                continue
            close_item()
        if not colon or not value and (body.startswith("#") or key.lower() in SECTIONS):
            # A heading, with or without its "#". One that is not known ends the section:
            # its items must not be read as those of the section before it.
            name = _heading(body)
            if name == TITLE:
                continue  # says nothing: forms and tickets repeat it above the text
            if holds:
                held.add(section)
            section, holds = name if name in SECTIONS else None, False
            continue
        if key.lower() == "generated at":
            if dated:
                raise DebugInfoError(SEVERAL)
            dated, generated_on = True, _generated_on(value, today)
        elif section == "cluster":
            if key in ("instanceKey", "check") or "instanceType" in _pairs(body):
                reading()
                item = body
            elif key in _CLUSTER_KEYS:
                reading(key)
                if key == "checks":
                    checks_failed = checks_failed or []
                elif key == "versions":
                    versions.update(_version(part.strip()) for part in value.split(","))
        elif key in READERS.get(section, {}):
            reading(key)
            read = READERS[section][key](value)
            if read is not None:
                found[section][key] = read
    close_item()

    core, storage, pruning, security = (found[name] for name in ("core", "storage", "pruning", "security"))
    if "n8nVersion" not in core:
        raise DebugInfoError(NOT_DEBUG_INFO)
    mode, multi_main = core.get("executionMode", (None, None))
    versions.discard(None)
    return {
        "generated_on": generated_on,
        "n8n_version": core["n8nVersion"],
        "node_version": core.get("nodeJsVersion"),
        "platform": core.get("platform"),
        "database": core.get("database"),
        "execution_mode": mode,
        "multi_main": multi_main,
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
        "cluster": {"instances": instances, "versions": _sort_versions(versions), "checks_failed": checks_failed}
        if "cluster" in held or section == "cluster" and holds  # a bare heading is no section
        else None,
    }


# --- reading a section back -----------------------------------------------------------


def _is_count(value) -> bool:
    return type(value) is int and 0 <= value < 10**9


def _checked_version(value) -> Optional[str]:
    return value if type(value) is str and _PLAIN_VERSION.fullmatch(value) else None


def _checked_day(value) -> Optional[str]:
    try:
        return value if type(value) is str and date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def checked(section) -> Optional[dict]:
    """`section` with every value that parse() could not have produced replaced by None.

    parse() only writes known values, so this changes nothing in what it returns. It is
    for a section that came from elsewhere: a snapshot file, or a Snapshot built in code.
    None when `section` is not a dictionary at all.
    """
    if not isinstance(section, dict):
        return None

    def one_of(key: str, allowed) -> Optional[str]:
        value = section.get(key)
        return value if type(value) is str and value in allowed else None

    def flag(value) -> Optional[bool]:
        return value if type(value) is bool else None

    limit, pruning, insecure, cluster = (
        section.get(key) for key in ("concurrency_limit", "pruning", "insecure", "cluster")
    )
    if isinstance(pruning, dict) and type(pruning.get("enabled")) is bool:
        pruning = {
            "enabled": pruning["enabled"],
            **{key: pruning.get(key) if _is_count(pruning.get(key)) else None for key in ("max_age_hours", "max_count")},
        }
    else:
        pruning = None
    if isinstance(cluster, dict):
        counted = cluster.get("instances") if isinstance(cluster.get("instances"), dict) else {}
        listed = cluster.get("versions") if isinstance(cluster.get("versions"), list) else []
        failed = cluster.get("checks_failed")
        cluster = {
            "instances": {name: counted.get(name) if _is_count(counted.get(name)) else 0 for name in INSTANCE_TYPES},
            "versions": _sort_versions({version for version in listed if _checked_version(version)}),
            "checks_failed": [name if type(name) is str and name in KNOWN_CHECKS else OTHER_CHECK for name in failed]
            if isinstance(failed, list)
            else None,
        }
    else:
        cluster = None
    insecure = insecure if isinstance(insecure, dict) else {}
    return {
        "generated_on": _checked_day(section.get("generated_on")),
        "n8n_version": _checked_version(section.get("n8n_version")),
        "node_version": _checked_version(section.get("node_version")),
        "platform": one_of("platform", _PLATFORM.values()),
        "database": one_of("database", _DATABASE.values()),
        "execution_mode": one_of("execution_mode", ("regular", "queue")),
        "multi_main": flag(section.get("multi_main")),
        "concurrency_limit": limit if limit == "unlimited" and type(limit) is str or _is_count(limit) and limit >= 1 else None,
        "license": one_of("license", _LICENSE.values()),
        "save_success": one_of("save_success", _SAVE.values()),
        "save_error": one_of("save_error", _SAVE.values()),
        "save_progress": flag(section.get("save_progress")),
        "save_manual": flag(section.get("save_manual")),
        "binary_mode": one_of("binary_mode", _BINARY.values()),
        "pruning": pruning,
        "insecure": {key: insecure.get(key) is True for key in ("secure_cookie", "file_access_blocked")},
        "cluster": cluster,
    }


def of(snapshot) -> Optional[dict]:
    """The debug information of a Snapshot, checked; None when the run has none.

    Rules, facts and the report read it only through here, never `snapshot.debug_info`
    itself.
    """
    return checked(snapshot.debug_info)


def label(section: dict) -> str:
    """How a fact's detail names a debug information: by its date."""
    day = section.get("generated_on")
    return f"debug information generated on {day}" if day else "debug information, date unknown"


def phrase(section: dict) -> str:
    """label() for the middle of a sentence of evidence: "The <phrase> lists ..."."""
    return label(section).replace(", date unknown", " of unknown date")
