"""Rules: each one inspects a Snapshot and returns a Finding or None."""

from ..models import Rule

ALL_RULES: list[Rule] = []
