"""Collectors: read-only probes that turn a Target into a Snapshot."""

from ..models import Snapshot, Target
from . import envfile, http, kubernetes, postgres, redis
from ._util import url_secrets, warn

# Target field that enables a source -> module whose collect(target) returns Snapshot fields.
# The env file comes before Redis, which needs QUEUE_BULL_PREFIX from it.
SOURCES = (
    ("url", http),
    ("env_file", envfile),
    ("postgres_dsn", postgres),
    ("redis_url", redis),
    ("kube_namespace", kubernetes),
)


def collect(target: Target) -> Snapshot:
    snapshot = Snapshot()
    secrets = url_secrets(target.url, target.postgres_dsn, target.redis_url)
    for field, module in SOURCES:
        if getattr(target, field) is None:
            continue
        try:
            if module is redis:
                prefix = (snapshot.env or {}).get("QUEUE_BULL_PREFIX") or redis.DEFAULT_PREFIX
                fields = redis.collect(target, prefix)
            else:
                fields = module.collect(target)
        except Exception as error:  # a broken source must never abort the run
            warn(field, error, secrets)
            continue
        for name, value in fields.items():
            setattr(snapshot, name, value)
    return snapshot
