"""Rules: each one inspects a Snapshot and returns a Finding or None."""

from ..models import Rule
from . import maintainability, performance, reliability, scalability, security

ALL_RULES: list[Rule] = [
    *scalability.RULES,
    *reliability.RULES,
    *performance.RULES,
    *maintainability.RULES,
    *security.RULES,
]
