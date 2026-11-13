"""Collectors: read-only probes that turn a Target into a Snapshot."""

from ..models import Snapshot, Target


def collect(target: Target) -> Snapshot:
    raise NotImplementedError
