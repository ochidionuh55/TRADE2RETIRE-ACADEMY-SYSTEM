"""Telegram bot — the T2R OS interface. No business logic lives here.

Every number and rule comes from the services layer over the canonical store.
The bot resolves who you are (by role, not by command knowledge) and renders.

Presentation: messages are HTML-formatted (bold titles, tap-to-copy codes) and
the home screen is a role-aware inline "command center", so staff tap rather
than memorise commands. Access is still enforced server-side per handler — the
buttons are only the visible affordance, never the gate.
"""

from __future__ import annotations

import asyncio
import html

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import select

from app.attendance import (
    FIELD,
    LEAVE,
    OFF_DUTY,
    OFFICE,
    REMOTE,
    SICK,
    TRAINING,
    attendance_today,
    check_in,
    current_office_code,
    is_valid_office_code,
    is_within_office,
    minutes_left_in_window,
    office_distance_m,
    office_enabled,
    office_geofence_enabled,
    work_date,
)
from app.config import PROCESS, get_settings
from app.daily import (
    MAX_PRIORITIES,
    add_priority,
    complete_priority,
    daily_rollup,
    has_closed,
    list_priorities,
    submit_daily_close,
)
from app.db import session_scope
from app.logging import configure_logging, get_logger
from app.models import OPEN_STATES, CheckIn, FridayReport, Intervention, Person
from app.onboarding import link_staff_by_code, roles_for, staff_roster
from app.people import resolve_person
from app.roles import Role, has_role, is_management, is_manager, is_staff
from app.services import (
    complete_intervention,
    format_brief,
    review_report,
    submit_friday_report,
    weekly_brief,
)

logger = get_logger(__name__)
router = Router()

# ── Presentation helpers ─────────────────────────────────────────────────────
RULE = "━━━━━━━━━━━━━━━━━━━━"


def esc(value: object) -> str:
    """HTML-escape any dynamic text before it goes into a formatted message."""
    return html.escape(str(value if value is not None else ""))


def _can_officecode(roles: set[Role]) -> bool:
    return has_role(
        roles,
        Role.OPERATIONS,
        Role.ADMIN,
        Role.CEO,
        Role.CO_OWNER,
        Role.HEAD_OF_ACADEMY,
        Role.HEAD_OF_SUPPORT,
    )


def _can_roster(roles: set[Role]) -> bool:
    return has_role(roles, Role.ADMIN, Role.CO_OWNER, Role.CEO)


def _menu_keyboard(roles: set[Role]) -> InlineKeyboardMarkup:
    """A tap-friendly home screen, showing only what this person may do."""
    rows: list[list[InlineKeyboardButton]] = []
    if is_staff(roles):
        rows.append(
            [
                InlineKeyboardButton(text="🏢 Check in", callback_data="cb:checkin"),
                InlineKeyboardButton(text="📅 My day", callback_data="cb:myday"),
            ]
        )
        rows.append([InlineKeyboardButton(text="👤 My record", callback_data="cb:me")])
    rows.append([InlineKeyboardButton(text="📝 Friday review", callback_data="cb:friday")])
    if has_role(roles, Role.MENTOR):
        rows.append([InlineKeyboardButton(text="📋 My queue", callback_data="cb:queue")])
    mgr: list[InlineKeyboardButton] = []
    if is_manager(roles) or is_management(roles):
        mgr.append(InlineKeyboardButton(text="👥 Today", callback_data="cb:today"))
        mgr.append(InlineKeyboardButton(text="📋 Standup", callback_data="cb:standup"))
    if has_role(roles, Role.CEO, Role.ADMIN):
        mgr.append(InlineKeyboardButton(text="📊 Brief", callback_data="cb:brief"))
    if mgr:
        rows.append(mgr)
    ops: list[InlineKeyboardButton] = []
    if _can_officecode(roles):
        ops.append(InlineKeyboardButton(text="🔑 Office code", callback_data="cb:officecode"))
    if _can_roster(roles):
        ops.append(InlineKeyboardButton(text="🧾 Roster", callback_data="cb:roster"))
    if ops:
        rows.append(ops)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _home_text(name: str) -> str:
    return (
        f"👋 <b>{esc(get_settings().academy_name)}</b>\n"
        f"Welcome back, <b>{esc(name)}</b>.\n\n"
        "<i>Your operating hub — tap below.</i>"
    )


# ── Core renderers (pure): both commands and buttons call these ──────────────


async def _me_text(uid: int, uname: str) -> str:
    async with session_scope() as s:
        person, roles = await resolve_person(s, uid, uname)
        role_str = ", ".join(sorted(r.value for r in roles)) or "student (unassigned)"
        today = work_date()
        ci = (
            await s.execute(
                select(CheckIn).where(
                    CheckIn.person_id == person.id, CheckIn.work_date == today
                )
            )
        ).scalar_one_or_none()
    lines = [
        f"👤 <b>{esc(person.full_name or 'You')}</b>",
        f"Roles: <b>{esc(role_str)}</b>",
    ]
    if ci is None:
        lines.append("Today: <i>not checked in yet</i> — /checkin")
    elif ci.presence_type == OFFICE and ci.verified:
        lines.append("Today: 🏢 Office — <b>verified</b> ✅")
    elif ci.presence_type == OFFICE:
        lines.append("Today: 🏢 Office — <i>unverified</i>")
    else:
        lines.append(f"Today: <b>{esc(ci.presence_type)}</b>")
    return "\n".join(lines)


async def _today_text(uid: int, uname: str) -> str:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
        if not (is_manager(roles) or is_management(roles)):
            return "🔒 Today's attendance is for managers."
        data = await attendance_today(s)
    return "\n".join(
        [
            f"👥 <b>ATTENDANCE</b> · {esc(data['work_date'])}",
            RULE,
            f"✅ Verified present   <b>{data['verified_present']}</b>",
            f"🟡 Office unverified  <b>{data['unverified_office']}</b>",
            f"🏠 Remote / field     <b>{data['remote_or_field']}</b>",
            f"🌴 Approved away      <b>{data['approved_away']}</b>",
            "",
            f"Checked in: <b>{data['checked_in']}</b>",
            f"Verification: {esc(data['office_verification'])} · {esc(data['office_method'])}",
            "<i>“Verified” means GPS or a code corroborated it — not self-report.</i>",
        ]
    )


async def _roster_text(uid: int, uname: str) -> str:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
        if not _can_roster(roles):
            return "🔒 The staff roster is for management."
        people = await staff_roster(s)
    lines = ["🧾 <b>STAFF ROSTER</b>", RULE]
    for p in people:
        if p["linked"]:
            lines.append(f"✅ <b>{esc(p['name'])}</b> — {esc(p['title'])}")
        else:
            lines.append(
                f"⬜ <b>{esc(p['name'])}</b> — {esc(p['title'])}  ·  "
                f"<code>{esc(p['join_code'])}</code>"
            )
    lines += ["", "<i>Send each person their code — they tap</i> /join CODE <i>to link.</i>"]
    return "\n".join(lines)


async def _officecode_text(uid: int, uname: str) -> str:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
    if not _can_officecode(roles):
        return "🔒 The office code is for operations/management."
    code = current_office_code()
    if code is None:
        return (
            "🔑 <b>Office code</b>\n"
            "Not configured yet. Set <code>T2R__OFFICE_SECRET</code> to enable the code "
            "(GPS check-in works independently)."
        )
    return (
        "🔑 <b>Office code</b>\n"
        f"<code>{esc(code)}</code>  <i>(tap to copy)</i>\n"
        f"Valid ~<b>{minutes_left_in_window()}</b> more min, then it rotates.\n"
        "<i>Show it in the office; staff enter it on</i> /checkin<i>.</i>"
    )


async def _brief_text(uid: int, uname: str) -> str:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
        if not has_role(roles, Role.CEO, Role.ADMIN):
            return "🔒 The executive brief is for management."
        data = await weekly_brief(s)
    return f"<pre>{esc(format_brief(data, get_settings().academy_name))}</pre>"


async def _queue_text(uid: int, uname: str) -> str:
    async with session_scope() as s:
        person, roles = await resolve_person(s, uid, uname)
        if not has_role(roles, Role.MENTOR, Role.ADMIN):
            return "🔒 That view is for mentors."
        stmt = select(Intervention).where(Intervention.status.in_(tuple(OPEN_STATES)))
        if not has_role(roles, Role.ADMIN):
            stmt = stmt.where(Intervention.mentor_id == person.id)
        rows = (await s.execute(stmt.order_by(Intervention.due_at))).scalars().all()
        lines = []
        for iv in rows:
            subject = await s.get(Person, iv.subject_person_id)
            who = subject.full_name if subject else f"person {iv.subject_person_id}"
            mark = "⚠️ OVERDUE" if iv.status == "overdue" else "open"
            lines.append(f"#{iv.id} [{mark}] {esc(who)} — {esc(iv.reason)}")
    if not lines:
        return "📋 <b>Your queue</b>\nNothing open. 👍"
    return (
        "📋 <b>Your interventions</b>\n"
        + RULE
        + "\n"
        + "\n".join(lines)
        + "\n\n<i>Close one with</i> /done &lt;id&gt; &lt;outcome&gt;"
    )


# ── Home / identity ──────────────────────────────────────────────────────────


@router.message(CommandStart())
async def start(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
    name = person.full_name or "there"
    await message.answer(_home_text(name), reply_markup=_menu_keyboard(roles))


@router.message(Command("menu"))
async def menu(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
    await message.answer(
        _home_text(person.full_name or "there"), reply_markup=_menu_keyboard(roles)
    )


@router.message(Command("me"))
async def me(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await _me_text(message.from_user.id, message.from_user.full_name))


# ── Friday report flow ───────────────────────────────────────────────────────


class Friday(StatesGroup):
    collecting = State()


def _q(i: int):
    qs = PROCESS.friday_questions
    return qs[i] if 0 <= i < len(qs) else None


async def _friday_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Friday.collecting)
    await state.update_data(idx=0, answers={})
    first = _q(0)
    await message.answer(
        "📝 <b>Weekly trading review</b>\n"
        "Answer each prompt; send “<b>-</b>” to skip an optional one.\n\n"
        + esc(first.prompt if first else "")
    )


@router.message(Command("friday"))
async def friday_start(message: Message, state: FSMContext) -> None:
    await _friday_start(message, state)


@router.message(Friday.collecting, F.text & ~F.text.startswith("/"))
async def friday_step(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(data.get("idx", 0))
    answers = dict(data.get("answers", {}))
    question = _q(idx)
    if question is not None:
        text = (message.text or "").strip()
        if text == "-" and question.required:
            await message.answer("That one's required. " + esc(question.prompt))
            return
        answers[question.key] = None if text == "-" else text

    nxt = _q(idx + 1)
    if nxt is not None:
        await state.update_data(idx=idx + 1, answers=answers)
        await message.answer(esc(nxt.prompt))
        return

    if message.from_user is None:
        await state.clear()
        return
    async with session_scope() as s:
        person, _ = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        _, created = await submit_friday_report(s, person, answers)
    await state.clear()
    await message.answer(
        "✅ <b>Report recorded</b> — thank you." if created
        else "You already submitted this week's report; the first one stands."
    )


# ── Mentor ───────────────────────────────────────────────────────────────────


@router.message(Command("queue"))
async def queue(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await _queue_text(message.from_user.id, message.from_user.full_name))


@router.message(Command("done"))
async def done(message: Message) -> None:
    if message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Usage: <code>/done &lt;id&gt; &lt;what you did&gt;</code>")
        return
    iv_id, outcome = int(parts[1]), parts[2]
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not has_role(roles, Role.MENTOR, Role.ADMIN):
            await message.answer("🔒 That action is for mentors.")
            return
        result = await complete_intervention(s, person, iv_id, outcome)
    await message.answer(
        f"✅ Intervention <b>#{iv_id}</b> closed." if result else "Couldn't close that one."
    )


@router.message(Command("review"))
async def review(message: Message) -> None:
    if message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Usage: <code>/review &lt;report_id&gt; &lt;your note&gt;</code>")
        return
    report_id, note = int(parts[1]), parts[2]
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not has_role(roles, Role.MENTOR, Role.ADMIN):
            await message.answer("🔒 Reviews are for mentors.")
            return
        report = await s.get(FridayReport, report_id)
        if report is None:
            await message.answer("No such report.")
            return
        await review_report(s, person, report_id, note)
    await message.answer("✅ Review recorded.")


# ── CEO / management ─────────────────────────────────────────────────────────


@router.message(Command("brief"))
async def brief(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await _brief_text(message.from_user.id, message.from_user.full_name))


@router.message(Command("today"))
async def today(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await _today_text(message.from_user.id, message.from_user.full_name))


# ── Staff check-in (Slice 3 · 3.6) ───────────────────────────────────────────


class Checkin(StatesGroup):
    presence = State()
    code = State()


_PRESENCE_BUTTONS: list[tuple[str, str]] = [
    ("🏢 Office", OFFICE),
    ("🏠 Remote", REMOTE),
    ("🚗 Field", FIELD),
    ("🎓 Training", TRAINING),
    ("🌴 Leave", LEAVE),
    ("🤒 Sick", SICK),
    ("⏸ Off duty", OFF_DUTY),
]
_LABEL_TO_PRESENCE: dict[str, str] = dict(_PRESENCE_BUTTONS)


def _presence_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton(text=_PRESENCE_BUTTONS[i][0]),
            KeyboardButton(text=_PRESENCE_BUTTONS[i + 1][0]),
        ]
        for i in range(0, len(_PRESENCE_BUTTONS) - 1, 2)
    ]
    rows.append([KeyboardButton(text=_PRESENCE_BUTTONS[-1][0])])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


async def _checkin_start(message: Message, state: FSMContext, uid: int, uname: str) -> None:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
    if not is_staff(roles):
        await message.answer("🔒 Check-in is for staff.")
        return
    await state.set_state(Checkin.presence)
    await message.answer(
        "🗓 <b>Check in for today</b>\nWhere are you working?",
        reply_markup=_presence_keyboard(),
    )


async def _finish_checkin(
    message: Message,
    state: FSMContext,
    presence: str,
    code: str | None,
    lat: float | None = None,
    lng: float | None = None,
) -> None:
    async with session_scope() as s:
        person, _ = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        check, created = await check_in(
            s, person=person, presence_type=presence, office_code=code, lat=lat, lng=lng
        )
    await state.clear()
    if not created:
        await message.answer(
            f"You already checked in today (<b>{esc(check.presence_type)}</b>).",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if presence == OFFICE and check.verified:
        text = "✅ <b>Checked in — Office, verified.</b>\nHave a great day."
    elif presence == OFFICE:
        text = (
            "🟡 <b>Checked in — Office, unverified.</b>\n"
            "<i>Couldn't confirm you're at the office.</i>"
        )
    else:
        text = f"✅ <b>Checked in — {esc(presence)}.</b> Recorded."
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(Command("checkin"))
async def checkin_start(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await _checkin_start(message, state, message.from_user.id, message.from_user.full_name)


def _office_verify_keyboard() -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if office_geofence_enabled():
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📍 Share my location", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
    return ReplyKeyboardRemove()


def _office_prompt() -> str:
    if office_geofence_enabled() and office_enabled():
        return (
            "Confirm you're at the office — tap “📍 Share my location”.\n"
            "<i>(No GPS? You can type today's office code instead.)</i>"
        )
    if office_geofence_enabled():
        return "Confirm you're at the office — tap “📍 Share my location”."
    return "Enter today's office code (shown in the office):"


@router.message(Checkin.presence, F.text & ~F.text.startswith("/"))
async def checkin_presence(message: Message, state: FSMContext) -> None:
    presence = _LABEL_TO_PRESENCE.get((message.text or "").strip())
    if presence is None:
        await message.answer("Please tap one of the buttons.")
        return
    if presence == OFFICE and (office_geofence_enabled() or office_enabled()):
        await state.set_state(Checkin.code)
        await message.answer(_office_prompt(), reply_markup=_office_verify_keyboard())
        return
    await _finish_checkin(message, state, presence, None)


@router.message(Checkin.code, F.location)
async def checkin_location(message: Message, state: FSMContext) -> None:
    loc = message.location
    if loc is None:
        return
    within = is_within_office(loc.latitude, loc.longitude)
    if within is None:
        await message.answer("Location check isn't set up. Type today's office code, or /cancel.")
        return
    if within:
        await _finish_checkin(message, state, OFFICE, None, loc.latitude, loc.longitude)
        return
    dist = office_distance_m(loc.latitude, loc.longitude) or 0.0
    radius = get_settings().office_radius_m
    await message.answer(
        f"📍 That's ~<b>{dist:.0f}m</b> from the office (the zone is {radius}m).\n"
        "If you're actually working remotely, send /cancel and pick 🏠 Remote. "
        "Otherwise move closer and share your location again."
    )


@router.message(Checkin.code, F.text & ~F.text.startswith("/"))
async def checkin_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if office_enabled() and is_valid_office_code(code):
        await _finish_checkin(message, state, OFFICE, code)
        return
    if office_enabled():
        await message.answer(
            "That code didn't match the current office code. Type it again, "
            "or /cancel to pick a different presence."
        )
    else:
        await message.answer(
            "Please tap “📍 Share my location” to confirm the office, "
            "or /cancel to pick a different presence."
        )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("officecode"))
async def officecode(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await _officecode_text(message.from_user.id, message.from_user.full_name))


# ── Staff onboarding (Slice 3.5) ─────────────────────────────────────────────


@router.message(Command("join"))
async def join(message: Message) -> None:
    if message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/join YOURCODE</code>  <i>(ask your admin for your code)</i>"
        )
        return
    role_str = "staff"
    async with session_scope() as s:
        status, person = await link_staff_by_code(
            s,
            join_code=parts[1],
            telegram_id=message.from_user.id,
            full_name=message.from_user.full_name,
        )
        if status in ("linked", "already_linked") and person is not None:
            roles = await roles_for(s, person)
            role_str = ", ".join(sorted(r.value for r in roles)) or "staff"
    if status == "invalid":
        await message.answer(
            "That code isn't valid or has already been used. Ask your admin for a fresh one."
        )
    elif status == "already_linked":
        await message.answer(
            f"You're already linked, <b>{esc(person.full_name)}</b>. Roles: <b>{esc(role_str)}</b>."
        )
    else:
        await message.answer(
            f"✅ <b>Welcome, {esc(person.full_name)}!</b> You're linked.\n"
            f"Roles: <b>{esc(role_str)}</b>.\n\nTap /menu to see what you can do, or /checkin now."
        )


@router.message(Command("roster"))
async def roster(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await _roster_text(message.from_user.id, message.from_user.full_name))


# ── Daily operating loop (Slice 4): priorities, done, Daily Close ────────────


class Plan(StatesGroup):
    collecting = State()


class Close(StatesGroup):
    summary = State()
    blockers = State()


def _myday_keyboard(prios: list, closed: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in prios:
        if p.status == "open":
            label = p.body if len(p.body) <= 24 else p.body[:23] + "…"
            rows.append(
                [InlineKeyboardButton(text=f"✔ {label}", callback_data=f"cb:done:{p.id}")]
            )
    actions = [InlineKeyboardButton(text="✍️ Plan", callback_data="cb:plan")]
    if not closed:
        actions.append(InlineKeyboardButton(text="🌙 Close day", callback_data="cb:close"))
    rows.append(actions)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _myday(uid: int, uname: str) -> tuple[str, InlineKeyboardMarkup | None]:
    async with session_scope() as s:
        person, roles = await resolve_person(s, uid, uname)
        if not is_staff(roles):
            return "🔒 Your day view is for staff.", None
        prios = await list_priorities(s, person=person)
        closed = await has_closed(s, person=person)
    lines = [f"📅 <b>YOUR DAY</b> · {esc(work_date().isoformat())}", RULE]
    if not prios:
        lines.append("<i>No priorities set yet.</i> Tap ✍️ Plan to set up to 3.")
    else:
        done = sum(1 for p in prios if p.status == "done")
        for p in prios:
            mark = "✅" if p.status == "done" else "⬜"
            lines.append(f"{mark} {esc(p.body)}")
        lines.append("")
        tail = "  ·  🌙 day closed" if closed else ""
        lines.append(f"<b>{done}/{len(prios)}</b> done{tail}")
    return "\n".join(lines), _myday_keyboard(prios, closed)


async def _standup_text(uid: int, uname: str) -> str:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
        if not (is_manager(roles) or is_management(roles)):
            return "🔒 The standup view is for managers."
        data = await daily_rollup(s)
    lines = [f"📋 <b>TEAM STANDUP</b> · {esc(data['work_date'])}", RULE]
    people = data["people"]
    if not people:
        lines.append("<i>No priorities or closes logged yet today.</i>")
    else:
        for r in people:
            close_mark = "🌙" if r["closed"] else "▫️"
            lines.append(f"{close_mark} <b>{esc(r['name'])}</b> — {r['done']}/{r['total']} done")
    lines += [
        "",
        f"Planned: <b>{data['planned']}</b> · Closed: <b>{data['closed']}</b>",
        "<i>🌙 = submitted a Daily Close. Counts are self-reported.</i>",
    ]
    return "\n".join(lines)


def _plan_ack(status: str) -> str:
    if status == "full":
        return (
            f"You already have {MAX_PRIORITIES} priorities today — that's the cap. "
            "/myday to see them."
        )
    if status == "empty":
        return "That was empty — send a few words describing the priority."
    return "✅ Added. /myday to see your day."


async def _plan_start(message: Message, state: FSMContext, uid: int, uname: str) -> None:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
    if not is_staff(roles):
        await message.answer("🔒 Planning is for staff.")
        return
    await state.set_state(Plan.collecting)
    await message.answer(
        f"✍️ <b>Set today's priorities</b> (up to {MAX_PRIORITIES}).\n"
        "Send them one message at a time. /cancel when you're done."
    )


async def _close_start(message: Message, state: FSMContext, uid: int, uname: str) -> None:
    async with session_scope() as s:
        person, roles = await resolve_person(s, uid, uname)
        if not is_staff(roles):
            await message.answer("🔒 The Daily Close is for staff.")
            return
        if await has_closed(s, person=person):
            await message.answer("You've already closed today. The first close stands.")
            return
    await state.set_state(Close.summary)
    await message.answer("🌙 <b>Daily Close</b>\nWhat did you get done today?")


@router.message(Command("myday"))
async def myday(message: Message) -> None:
    if message.from_user is None:
        return
    text, kb = await _myday(message.from_user.id, message.from_user.full_name)
    await message.answer(text, reply_markup=kb)


@router.message(Command("plan"))
async def plan_start(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:  # one-shot: "/plan finish the audit"
        async with session_scope() as s:
            person, roles = await resolve_person(
                s, message.from_user.id, message.from_user.full_name
            )
            if not is_staff(roles):
                await message.answer("🔒 Planning is for staff.")
                return
            status, _ = await add_priority(s, person=person, body=parts[1])
        await message.answer(_plan_ack(status))
        return
    await _plan_start(message, state, message.from_user.id, message.from_user.full_name)


@router.message(Plan.collecting, F.text & ~F.text.startswith("/"))
async def plan_step(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        person, _ = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        status, _ = await add_priority(s, person=person, body=message.text or "")
        count = len(await list_priorities(s, person=person))
    if status == "full":
        await state.clear()
        await message.answer(f"That's your {MAX_PRIORITIES} for today. Tap /myday to work them.")
        return
    if status == "empty":
        await message.answer("Send a few words describing the priority (or /cancel).")
        return
    if count >= MAX_PRIORITIES:
        await state.clear()
        await message.answer(
            f"✅ Got it — that's {MAX_PRIORITIES}/{MAX_PRIORITIES}. /myday to work them."
        )
        return
    await message.answer(
        f"✅ Saved ({count}/{MAX_PRIORITIES}). Send another, or /cancel to finish."
    )


@router.message(Command("close"))
async def close_start(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await _close_start(message, state, message.from_user.id, message.from_user.full_name)


@router.message(Close.summary, F.text & ~F.text.startswith("/"))
async def close_summary(message: Message, state: FSMContext) -> None:
    await state.update_data(summary=(message.text or "").strip())
    await state.set_state(Close.blockers)
    await message.answer("Anything blocked or rolling into tomorrow? (send “-” if nothing)")


@router.message(Close.blockers, F.text & ~F.text.startswith("/"))
async def close_blockers(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        await state.clear()
        return
    data = await state.get_data()
    summary = data.get("summary", "")
    raw = (message.text or "").strip()
    blockers = None if raw == "-" else raw
    async with session_scope() as s:
        person, _ = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        _, created = await submit_daily_close(
            s, person=person, summary=summary, blockers=blockers
        )
    await state.clear()
    await message.answer(
        "🌙 <b>Day closed</b> — thank you. Your manager can see it in the standup."
        if created
        else "You'd already closed today; the first one stands."
    )


@router.message(Command("standup"))
async def standup(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await _standup_text(message.from_user.id, message.from_user.full_name))


# ── Inline command-center buttons ────────────────────────────────────────────


@router.callback_query(F.data == "cb:me")
async def cb_me(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(await _me_text(cq.from_user.id, cq.from_user.full_name))


@router.callback_query(F.data == "cb:today")
async def cb_today(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(await _today_text(cq.from_user.id, cq.from_user.full_name))


@router.callback_query(F.data == "cb:roster")
async def cb_roster(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(await _roster_text(cq.from_user.id, cq.from_user.full_name))


@router.callback_query(F.data == "cb:officecode")
async def cb_officecode(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(await _officecode_text(cq.from_user.id, cq.from_user.full_name))


@router.callback_query(F.data == "cb:brief")
async def cb_brief(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(await _brief_text(cq.from_user.id, cq.from_user.full_name))


@router.callback_query(F.data == "cb:queue")
async def cb_queue(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(await _queue_text(cq.from_user.id, cq.from_user.full_name))


@router.callback_query(F.data == "cb:checkin")
async def cb_checkin(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await _checkin_start(cq.message, state, cq.from_user.id, cq.from_user.full_name)


@router.callback_query(F.data == "cb:friday")
async def cb_friday(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await _friday_start(cq.message, state)


@router.callback_query(F.data == "cb:myday")
async def cb_myday(cq: CallbackQuery) -> None:
    await cq.answer()
    text, kb = await _myday(cq.from_user.id, cq.from_user.full_name)
    await cq.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "cb:plan")
async def cb_plan(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await _plan_start(cq.message, state, cq.from_user.id, cq.from_user.full_name)


@router.callback_query(F.data == "cb:close")
async def cb_close(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await _close_start(cq.message, state, cq.from_user.id, cq.from_user.full_name)


@router.callback_query(F.data == "cb:standup")
async def cb_standup(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.answer(await _standup_text(cq.from_user.id, cq.from_user.full_name))


@router.callback_query(F.data.startswith("cb:done:"))
async def cb_done_priority(cq: CallbackQuery) -> None:
    await cq.answer("Marked done ✅")
    try:
        pid = int((cq.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return
    async with session_scope() as s:
        person, _ = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        await complete_priority(s, person=person, priority_id=pid)
    text, kb = await _myday(cq.from_user.id, cq.from_user.full_name)
    await cq.message.answer(text, reply_markup=kb)


# The command menu shown when a user types "/". Access is still enforced
# server-side per handler; this list is only the visible affordance.
_MENU: list[BotCommand] = [
    BotCommand(command="menu", description="🏠 Home — your command center"),
    BotCommand(command="checkin", description="🏢 Check in for today"),
    BotCommand(command="myday", description="📅 Your priorities & Daily Close"),
    BotCommand(command="plan", description="✍️ Set today's priorities"),
    BotCommand(command="close", description="🌙 Close your day"),
    BotCommand(command="join", description="🔗 Link your staff account"),
    BotCommand(command="me", description="👤 Your record and roles"),
    BotCommand(command="friday", description="📝 Submit this week's trading review"),
    BotCommand(command="today", description="👥 Managers: today's attendance"),
    BotCommand(command="standup", description="📋 Managers: team standup"),
    BotCommand(command="officecode", description="🔑 Ops: current office code"),
    BotCommand(command="roster", description="🧾 Admin: staff join codes"),
    BotCommand(command="brief", description="📊 Management: executive brief"),
]


async def _publish_menu(bot: Bot) -> None:
    try:
        await bot.set_my_commands(_MENU)
        logger.info("bot.menu_published", count=len(_MENU))
    except Exception as exc:  # noqa: BLE001 - a menu failure must not stop the bot
        logger.warning("bot.menu_failed", error=str(exc))


async def run_bot() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.telegram_token:
        raise SystemExit("T2R__TELEGRAM_TOKEN is not set.")
    bot = Bot(
        settings.telegram_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await _publish_menu(bot)
    logger.info("bot.starting")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_bot())
