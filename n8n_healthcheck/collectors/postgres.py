"""Postgres: size and age of n8n's execution tables, and connection headroom.

The standard library has no Postgres driver, so this shells out to `psql`.
"""

import os
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from ..models import Target
from ._util import TIMEOUT, run

# Table and column names are n8n's defaults (schema on the search_path, no DB_TABLE_PREFIX):
# - execution_entity holds one row per execution; execution_data holds the payloads,
#   which is where most of the bytes are, so both are summed.
# - "stoppedAt" is the indexed column n8n prunes on, so min() is cheap and meaningful.
# - count(*) is exact for small tables; for large ones the planner estimate is used
#   instead, because a full count is slow exactly when the table is the problem.
QUERY = """
SELECT current_setting('server_version'),
       CASE WHEN c.reltuples > 100000 THEN c.reltuples::bigint
            ELSE (SELECT count(*) FROM execution_entity) END,
       pg_total_relation_size(c.oid)
         + COALESCE(pg_total_relation_size(to_regclass('execution_data')), 0),
       (SELECT EXTRACT(EPOCH FROM now() - min("stoppedAt")) / 86400 FROM execution_entity),
       (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'),
       current_setting('max_connections')
FROM pg_class c WHERE c.oid = 'execution_entity'::regclass
"""

# Sent ahead of QUERY in the same `-c`, which the server runs as one transaction: the
# server itself refuses writes and long-running statements. SET LOCAL ends with that
# transaction, so behind a connection pooler nothing is left on the shared server
# connection. (PGOPTIONS is not used: poolers reject the `options` startup parameter,
# and `?options=` in the DSN overrides it.)
GUARD = f"SET LOCAL transaction_read_only = on; SET LOCAL statement_timeout = {TIMEOUT * 1000};"


def collect(target: Target) -> dict:
    uri, password = split_dsn(target.postgres_dsn)
    env = {**os.environ, "PGCONNECT_TIMEOUT": str(TIMEOUT)}
    if password:
        env["PGPASSWORD"] = password  # via environment, so it never shows in `ps`
    args = ["psql", "-X", "-q", "-A", "-t", "-F", "|", "-w", "-v", "ON_ERROR_STOP=1"]
    output = run(args + ["-c", GUARD + QUERY, "-d", uri], env=env, timeout=2 * TIMEOUT)
    return {"postgres": parse_row(output)}


def split_dsn(dsn: str) -> tuple:
    """'postgresql://user:secret@host/db' -> ('postgresql://user@host/db', 'secret').

    The password may also be given as '?password=secret'; that form wins, as in libpq.
    """
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError("DSN must be a postgresql:// URI")
    userinfo, at, host = parts.netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    password = unquote(password)
    params = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "password":
            password = value
        else:
            params.append((key, value))
    uri = f"{parts.scheme}://{user}{at}{host}{parts.path}"
    if params:
        uri += "?" + urlencode(params, quote_via=quote)  # libpq does not read "+" as a space
    return uri, password


def parse_row(output: str) -> dict:
    """One 'version|count|bytes|age_days|connections|max_connections' row from psql -A -t."""
    version, count, size, age, connections, max_connections = output.strip().split("|")
    return {
        "version": version,
        "execution_count": int(count),
        "execution_table_bytes": int(size),
        "oldest_execution_age_days": float(age) if age else 0.0,  # empty table
        "connections": int(connections),
        "max_connections": int(max_connections),
    }
