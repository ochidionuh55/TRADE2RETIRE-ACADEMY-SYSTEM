"""Roles and access — RBAC from day one.

A person's capabilities come from the roles assigned to them, never from
knowing a command. This mirrors the discipline proven elsewhere: the surface
does not decide access, the backend role does.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """What a person may do. A person may hold more than one over time."""

    STUDENT = "student"
    MENTOR = "mentor"
    INSTRUCTOR = "instructor"
    SUPPORT = "support"
    SALES = "sales"
    FINANCE = "finance"
    ADMIN = "admin"
    CEO = "ceo"
    SYSTEM = "system"  # the engine itself, as an actor on events


# Which roles may reach which surfaces. Additive: absence means no access.
STAFF_ROLES: frozenset[Role] = frozenset(
    {Role.MENTOR, Role.INSTRUCTOR, Role.SUPPORT, Role.SALES, Role.FINANCE, Role.ADMIN, Role.CEO}
)
MANAGEMENT_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.CEO})


def has_role(roles: set[Role], *allowed: Role) -> bool:
    """Whether any of the person's roles is in ``allowed``."""
    return any(r in roles for r in allowed)
