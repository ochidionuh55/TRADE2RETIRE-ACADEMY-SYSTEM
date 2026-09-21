"""Roles and access — RBAC from day one.

A person's capabilities come from the roles assigned to them, never from
knowing a command. This mirrors the discipline proven elsewhere: the surface
does not decide access, the backend role does. RBAC is enforced server-side in
the handlers and services, not by hiding buttons.

Names are never hard-wired into business logic. A person's title in the org
(see ``app.org``) maps to one or more of these roles; the logic only ever asks
"does this person hold role X?", so re-org is a data change, not a code change.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """What a person may do. A person may hold more than one over time."""

    # Academy / student side
    STUDENT = "student"
    MENTOR = "mentor"
    INSTRUCTOR = "instructor"

    # Staff / company side
    SUPPORT = "support"          # customer support representative
    SALES = "sales"
    FINANCE = "finance"
    OPERATIONS = "operations"
    LEGAL = "legal"
    HEAD_OF_ACADEMY = "head_of_academy"
    HEAD_OF_SUPPORT = "head_of_support"

    # Ownership / management
    CO_OWNER = "co_owner"
    CEO = "ceo"
    ADMIN = "admin"

    SYSTEM = "system"  # the engine itself, as an actor on events


# Which roles are "staff" (have the staff operating loop). Additive: absence
# means no access.
STAFF_ROLES: frozenset[Role] = frozenset(
    {
        Role.MENTOR,
        Role.INSTRUCTOR,
        Role.SUPPORT,
        Role.SALES,
        Role.FINANCE,
        Role.OPERATIONS,
        Role.LEGAL,
        Role.HEAD_OF_ACADEMY,
        Role.HEAD_OF_SUPPORT,
        Role.CO_OWNER,
        Role.CEO,
        Role.ADMIN,
    }
)

# Roles that carry line-management duties (a team, a review queue).
# TODO(founder): confirm which heads manage which teams — reporting lines live
# in the ReportingLine table, unconfirmed until a founder approves them.
MANAGER_ROLES: frozenset[Role] = frozenset(
    {Role.MENTOR, Role.HEAD_OF_ACADEMY, Role.HEAD_OF_SUPPORT, Role.OPERATIONS}
)

# Roles that see company-wide command views (executive brief, exceptions).
MANAGEMENT_ROLES: frozenset[Role] = frozenset(
    {Role.ADMIN, Role.CEO, Role.CO_OWNER}
)


def has_role(roles: set[Role], *allowed: Role) -> bool:
    """Whether any of the person's roles is in ``allowed``."""
    return any(r in roles for r in allowed)


def is_staff(roles: set[Role]) -> bool:
    return any(r in STAFF_ROLES for r in roles)


def is_manager(roles: set[Role]) -> bool:
    return any(r in MANAGER_ROLES for r in roles)


def is_management(roles: set[Role]) -> bool:
    return any(r in MANAGEMENT_ROLES for r in roles)
