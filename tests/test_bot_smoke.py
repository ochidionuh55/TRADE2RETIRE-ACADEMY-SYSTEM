"""Smoke test — the bot module imports cleanly and its role-aware tool rows
build for every role. Catches a wiring/format error before deploy.
No network or DB: these exercise pure builders only.
"""

from __future__ import annotations

from app import bot
from app.roles import Role


def test_menu_commands_present() -> None:
    cmds = {c.command for c in bot._MENU}
    assert {"menu", "checkin", "join", "me", "today", "roster", "brief"} <= cmds


def test_tools_keyboard_builds_for_each_role() -> None:
    for roles in (
        set(),
        {Role.STUDENT},
        {Role.SUPPORT},
        {Role.MENTOR},
        {Role.CO_OWNER, Role.ADMIN},
        {Role.CEO},
        {Role.OPERATIONS},
    ):
        kb = bot._tools_keyboard(roles)
        assert kb.inline_keyboard  # always at least the Friday-review row


def test_admin_sees_roster_and_officecode_tools() -> None:
    kb = bot._tools_keyboard({Role.CO_OWNER, Role.ADMIN})
    labels = {b.text for row in kb.inline_keyboard for b in row}
    assert any("Roster" in t for t in labels)
    assert any("Office code" in t for t in labels)


def test_student_does_not_see_management_tools() -> None:
    kb = bot._tools_keyboard({Role.STUDENT})
    labels = {b.text for row in kb.inline_keyboard for b in row}
    assert not any("Roster" in t for t in labels)
    assert not any("Brief" in t for t in labels)
    assert not any("My record" in t for t in labels)  # a student isn't staff


def test_office_never_puts_the_next_action_in_the_tool_rows() -> None:
    # The stage-specific action (Check in / Plan / Close) is decided by The
    # Office, never baked into the static tool rows.
    labels = {
        b.text
        for row in bot._tools_keyboard({Role.CO_OWNER, Role.ADMIN}).inline_keyboard
        for b in row
    }
    assert not any("Check in" in t for t in labels)
    assert not any("Close day" in t for t in labels)


def test_greeting_is_a_greeting() -> None:
    assert bot._greeting().startswith("Good ")


def test_esc_escapes_html() -> None:
    assert bot.esc("<b>&") == "&lt;b&gt;&amp;"
    assert bot.esc(None) == ""
