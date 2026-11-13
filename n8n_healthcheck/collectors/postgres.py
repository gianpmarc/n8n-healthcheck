"""Postgres: size and age of n8n's execution tables, and connection headroom.

The standard library has no Postgres driver, so this shells out to `psql`.
"""

import os
from urllib.parse import unquote, urlsplit, urlunsplit

from ..models import Target
from ._util import TIMEOUT, run

# Table and column names are n8n's defaults (schema on the search_path, no DB_TABLE_PREFIX):
# - execution_entity holds one row per execution; execution_data holds the payloads,
#   which is where most of the bytes are, so both are summed.
# - "stoppedAt" is the indexed column n8n prunes on, so min() is cheap and meaningful.
# - count(*) is exact for small tables; for large ones the planner estimate is used
#   instead, because a full count is slow exactly when the table is the problem.
QUERY = f"""
SET statement_timeout = {TIMEOUT * 1000};
SET default_transaction_read_only = on;
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


def collect(target: Target) -> dict:
    uri, password = split_dsn(target.postgres_dsn)
    env = {**os.environ, "PGCONNECT_TIMEOUT": str(TIMEOUT)}
    if password:
        env["PGPASSWORD"] = password  # via environment, so it never shows in `ps`
    args = ["psql", "-X", "-q", "-A", "-t", "-F", "|", "-w", "-v", "ON_ERROR_STOP=1"]
    output = run(args + ["-c", QUERY, "-d", uri], env=env, timeout=2 * TIMEOUT)
    return {"postgres": parse_row(output)}


def split_dsn(dsn: str) -> tuple:
    """'postgresql://user:secret@host/db' -> ('postgresql://user@host/db', 'secret')."""
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError("DSN must be a postgresql:// URI")
    userinfo, at, host = parts.netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    return urlunsplit(parts._replace(netloc=user + at + host)), unquote(password)


def parse_row(output: str) -> dict:
    """One 'version|count|bytes|age_days|connections|max_connections' row from psql -A -t."""
    version, count, size, age, connections, max_connections = output.strip().split("|")
    return {
        "version": version,
        "execution_count": int(count),
        "execution_table_bytes": int(size),
        "oldest_execution_age_days": float(age) if age else None,  # empty table
        "connections": int(connections),
        "max_connections": int(max_connections),
    }
