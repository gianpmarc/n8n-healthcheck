"""Collectors: read-only probes that turn a Target into a Snapshot."""

from ..models import REDACTED, Snapshot, Target
from . import envfile, http, kubernetes, postgres, redis
from ._util import url_secrets, warn

# Target field that enables a source -> module whose collect(target) returns Snapshot fields.
# The env file and Kubernetes come before Redis, which needs QUEUE_BULL_PREFIX from them.
SOURCES = (
    ("url", http),
    ("env_file", envfile),
    ("postgres_dsn", postgres),
    ("kube_namespace", kubernetes),
    ("redis_url", redis),
)


def collect(target: Target) -> Snapshot:
    snapshot = Snapshot()
    secrets = url_secrets(target.url, target.postgres_dsn, target.redis_url)
    for field, module in SOURCES:
        if getattr(target, field) is None:
            continue
        try:
            if module is redis:
                fields = redis.collect(target, bull_prefix(snapshot))
            else:
                fields = module.collect(target)
        except Exception as error:  # a broken source must never abort the run
            warn(field, error, secrets)
            continue
        for name, value in fields.items():
            setattr(snapshot, name, value)
    return snapshot


def bull_prefix(snapshot: Snapshot) -> str | None:
    """QUEUE_BULL_PREFIX from the env file, else from the n8n containers in Kubernetes.

    The env file wins, as it does for the rules. In Kubernetes every n8n container must
    agree (one that leaves the variable unset uses the default). None when the prefix
    cannot be told, so that no other instance's queue is probed: the containers
    disagree, the value is set but not readable, it may be set in a part of the
    environment that could not be read, or only a container that is not recognised as
    n8n sets it.
    """
    name = "QUEUE_BULL_PREFIX"
    if snapshot.env is not None:
        return snapshot.env.get(name) or redis.DEFAULT_PREFIX
    values, set_elsewhere = set(), False
    for deployment in (snapshot.kubernetes or {}).get("deployments", []):
        for container in deployment["containers"]:
            env = container["env"]
            # image name (last path segment, no tag or digest) contains "n8n": not the
            # n8nio/runners sidecar, Redis or a proxy
            image = container["image"].split("@")[0].rsplit("/", 1)[-1].split(":")[0]
            if "n8n" not in image.lower():
                set_elsewhere = set_elsewhere or name in env
            elif name not in env and container["env_complete"] is not True:
                return None
            else:
                values.add(env.get(name) or redis.DEFAULT_PREFIX)
    if not values:
        return None if set_elsewhere else redis.DEFAULT_PREFIX
    if len(values) > 1 or values & {kubernetes.UNKNOWN, REDACTED}:
        return None
    return values.pop()
