"""Collectors: read-only probes that turn a Target into a Snapshot."""

from ..models import Snapshot, Target
from . import envfile, http, kubernetes, postgres, redis
from ._util import warn

# Target field that enables a source -> module whose collect(target) returns Snapshot fields.
SOURCES = (
    ("url", http),
    ("env_file", envfile),
    ("postgres_dsn", postgres),
    ("redis_url", redis),
    ("kube_namespace", kubernetes),
)


def collect(target: Target) -> Snapshot:
    snapshot = Snapshot()
    for field, module in SOURCES:
        if getattr(target, field) is None:
            continue
        try:
            fields = module.collect(target)
        except Exception as error:  # a broken source must never abort the run
            warn(field, error)
            continue
        for name, value in fields.items():
            setattr(snapshot, name, value)
    return snapshot
