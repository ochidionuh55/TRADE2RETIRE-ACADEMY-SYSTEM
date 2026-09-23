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
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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
from app.command import (
    acknowledge_correspondence as command_acknowledge_correspondence,
)
from app.command import (
    create_assignment as command_create_assignment,
)
from app.command import (
    create_correspondence as command_create_correspondence,
)
from app.command import (
    inbox as command_inbox,
)
from app.command import (
    list_assignments as command_list_assignments,
)
from app.command import (
    management_choices as command_management_choices,
)
from app.command import (
    resolve_correspondence as command_resolve_correspondence,
)
from app.command import (
    staff_choices as command_staff_choices,
)
from app.command import (
    transition_assignment as command_transition_assignment,
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


def _tools_keyboard(roles: set[Role]) -> InlineKeyboardMarkup:
    """The secondary tool rows — everything this person may reach, but never the
    stage-specific next action, which The Office decides."""
    rows: list[list[InlineKeyboardButton]] = []
    if is_staff(roles):
        rows.append(
            [
                InlineKeyboardButton(text="🗂 My Desk", callback_data="cb:desk"),
                InlineKeyboardButton(text="👤 My record", callback_data="cb:me"),
            ]
        )
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
    if is_management(roles):
        rows.append(
            [
                InlineKeyboardButton(text="Assign", callback_data="cmd:assign"),
                InlineKeyboardButton(text="📥 Inbox", callback_data="cmd:inbox"),
            ]
        )
    ops: list[InlineKeyboardButton] = []
    if _can_officecode(roles):
        ops.append(InlineKeyboardButton(text="🔑 Office code", callback_data="cb:officecode"))
    if _can_roster(roles):
        ops.append(InlineKeyboardButton(text="🧾 Roster", callback_data="cb:roster"))
    if ops:
        rows.append(ops)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _greeting() -> str:
    hour = datetime.now(UTC).astimezone(ZoneInfo(get_settings().timezone)).hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


async def _edit_or_send(cq: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Refresh the screen in place; fall back to a new message if it can't be
    edited (unchanged content, or too old)."""
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001 - an un-editable message must not break the tap
        await cq.message.answer(text, reply_markup=kb)


async def _office(uid: int, uname: str) -> tuple[str, InlineKeyboardMarkup]:
    """The one screen a person walks into. It always knows where they are in
    their day and offers the single next action — arrive, plan, work, leave."""
    async with session_scope() as s:
        person, roles = await resolve_person(s, uid, uname)
        name = person.full_name or "there"
        staff = is_staff(roles)
        ci = None
        prios: list = []
        closed = False
        if staff:
            today = work_date()
            ci = (
                await s.execute(
                    select(CheckIn).where(
                        CheckIn.person_id == person.id, CheckIn.work_date == today
                    )
                )
            ).scalar_one_or_none()
            prios = await list_priorities(s, person=person)
            closed = await has_closed(s, person=person)

    lines = [
        f"🏢 <b>{esc(get_settings().academy_name)}</b>",
        f"{_greeting()}, <b>{esc(name)}</b>.",
        RULE,
    ]
    primary: list[list[InlineKeyboardButton]] = []

    if not staff:
        lines.append("<i>Your weekly trading review is below when you're ready.</i>")
        return "\n".join(lines), InlineKeyboardMarkup(
            inline_keyboard=primary + _tools_keyboard(roles).inline_keyboard
        )

    if ci is None:
        # Stage 1 — they haven't walked in yet.
        lines.append("You haven't checked in yet today.")
        primary.append([InlineKeyboardButton(text="🏢 Check in", callback_data="cb:checkin")])
    else:
        if ci.presence_type == OFFICE and ci.verified:
            lines.append("✅ Checked in — <b>Office</b> (verified)")
        elif ci.presence_type == OFFICE:
            lines.append("🟡 Checked in — <b>Office</b> (unverified)")
        else:
            lines.append(f"✅ Checked in — <b>{esc(ci.presence_type)}</b>")

        done = sum(1 for p in prios if p.status == "done")
        if closed:
            # Stage 5 — they've left for the day.
            lines += ["", "🌙 <b>Day closed.</b> See you tomorrow."]
            if prios:
                lines.append(f"{done}/{len(prios)} priorities done.")
        elif not prios:
            # Stage 2 — in, but no plan yet.
            lines += ["", "No priorities set yet."]
            primary.append(
                [InlineKeyboardButton(text="✍️ Set today's plan", callback_data="cb:plan")]
            )
        else:
            # Stages 3 & 4 — working the plan.
            lines += ["", "<b>Today's priorities</b>"]
            for p in prios:
                mark = "✅" if p.status == "done" else "⬜"
                lines.append(f"{mark} {esc(p.body)}")
            lines += ["", f"<b>{done}/{len(prios)}</b> done"]
            if done == len(prios):
                lines.append("🎉 All done — nice work.")
            for p in prios:
                if p.status == "open":
                    label = p.body if len(p.body) <= 24 else p.body[:23] + "…"
                    primary.append(
                        [
                            InlineKeyboardButton(
                                text=f"✔ {label}", callback_data=f"cb:done:{p.id}"
                            )
                        ]
                    )
            actions: list[InlineKeyboardButton] = []
            if len(prios) < MAX_PRIORITIES:
                actions.append(InlineKeyboardButton(text="✍️ Add", callback_data="cb:plan"))
            actions.append(InlineKeyboardButton(text="🌙 Close day", callback_data="cb:close"))
            primary.append(actions)

    return "\n".join(lines), InlineKeyboardMarkup(
        inline_keyboard=primary + _tools_keyboard(roles).inline_keyboard
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
    text, kb = await _office(message.from_user.id, message.from_user.full_name)
    await message.answer(text, reply_markup=kb)


@router.message(Command("menu"))
async def menu(message: Message) -> None:
    if message.from_user is None:
        return
    text, kb = await _office(message.from_user.id, message.from_user.full_name)
    await message.answer(text, reply_markup=kb)


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
        text = f"You already checked in today (<b>{esc(check.presence_type)}</b>)."
    elif presence == OFFICE and check.verified:
        text = "✅ <b>Checked in — Office, verified.</b>\nHave a great day."
    elif presence == OFFICE:
        text = (
            "🟡 <b>Checked in — Office, unverified.</b>\n"
            "<i>Couldn't confirm you're at the office.</i>"
        )
    else:
        text = f"✅ <b>Checked in — {esc(presence)}.</b> Recorded."
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    office_text, kb = await _office(message.from_user.id, message.from_user.full_name)
    await message.answer(office_text, reply_markup=kb)


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
        person, roles = await resolve_person(s, uid, uname)
        if not is_staff(roles):
            await message.answer("🔒 Planning is for staff.")
            return
        today = work_date()
        checked_in = (
            await s.execute(
                select(CheckIn.id).where(
                    CheckIn.person_id == person.id, CheckIn.work_date == today
                )
            )
        ).scalar_one_or_none()
        if checked_in is None:
            await message.answer("🏢 Check in first, then set today's plan. /checkin")
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
        today = work_date()
        checked_in = (
            await s.execute(
                select(CheckIn.id).where(
                    CheckIn.person_id == person.id, CheckIn.work_date == today
                )
            )
        ).scalar_one_or_none()
        if checked_in is None:
            await message.answer("🏢 Check in first. A day that never started can't be closed.")
            return
        if not await list_priorities(s, person=person):
            await message.answer("✍️ Set at least one priority before closing the day. /plan")
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
    text, kb = await _office(message.from_user.id, message.from_user.full_name)
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
            today = work_date()
            checked_in = (
                await s.execute(
                    select(CheckIn.id).where(
                        CheckIn.person_id == person.id, CheckIn.work_date == today
                    )
                )
            ).scalar_one_or_none()
            if checked_in is None:
                await message.answer("🏢 Check in first, then set today's plan. /checkin")
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
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not is_staff(roles):
            await state.clear()
            await message.answer("🔒 Planning is for staff.")
            return
        status, _ = await add_priority(s, person=person, body=message.text or "")
        count = len(await list_priorities(s, person=person))
    if status == "empty":
        await message.answer("Send a few words describing the priority (or /cancel).")
        return
    if status == "full" or count >= MAX_PRIORITIES:
        await state.clear()
        await message.answer(f"✅ That's your {MAX_PRIORITIES} for today — here's your day:")
        office_text, kb = await _office(message.from_user.id, message.from_user.full_name)
        await message.answer(office_text, reply_markup=kb)
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
    if message.from_user is None:
        await state.clear()
        return
    async with session_scope() as s:
        _, roles = await resolve_person(s, message.from_user.id, message.from_user.full_name)
    if not is_staff(roles):
        await state.clear()
        await message.answer("🔒 The Daily Close is for staff.")
        return
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
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not is_staff(roles):
            await state.clear()
            await message.answer("🔒 The Daily Close is for staff.")
            return
        _, created = await submit_daily_close(
            s, person=person, summary=summary, blockers=blockers
        )
    await state.clear()
    await message.answer(
        "🌙 <b>Day closed</b> — thank you. Your manager can see it in the standup."
        if created
        else "You'd already closed today; the first one stands."
    )
    office_text, kb = await _office(message.from_user.id, message.from_user.full_name)
    await message.answer(office_text, reply_markup=kb)


@router.message(Command("standup"))
async def standup(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(await _standup_text(message.from_user.id, message.from_user.full_name))


# ── T2R Command: assignments + management correspondence ─────────────────────


class AssignWork(StatesGroup):
    body = State()
    due = State()


class SendManagement(StatesGroup):
    category = State()
    body = State()


class ReplyManagement(StatesGroup):
    body = State()


async def _safe_notify(bot: Bot, telegram_id: int | None, text: str) -> None:
    if telegram_id is None:
        return
    try:
        await bot.send_message(telegram_id, text)
    except Exception as exc:  # noqa: BLE001 - delivery failure must not erase canonical state
        logger.warning("command.notification_failed", telegram_id=telegram_id, error=str(exc))


async def _desk_text(uid: int, uname: str) -> tuple[str, InlineKeyboardMarkup]:
    async with session_scope() as s:
        person, roles = await resolve_person(s, uid, uname)
        if not is_staff(roles):
            return "🔒 The Office Desk is for staff.", InlineKeyboardMarkup(inline_keyboard=[])
        items = await command_list_assignments(s, person=person)
        mail = await command_inbox(s, person=person)
        lines = ["🗂 <b>MY DESK</b>", RULE]
        buttons: list[list[InlineKeyboardButton]] = []
        if items:
            lines.append("<b>Assignments</b>")
            for item in items[:8]:
                creator = await s.get(Person, item.created_by_person_id)
                who = creator.full_name if creator else "Management"
                due = item.due_at.strftime("%a %d %b, %H:%M") if item.due_at else "No deadline"
                lines.append(f"📌 <b>#{item.id}</b> {esc(item.body)}\n   From {esc(who)} · {esc(item.status)} · {esc(due)}")  # noqa: E501
                if item.status == "assigned":
                    buttons.append([InlineKeyboardButton(text=f"🤝 Accept #{item.id}", callback_data=f"cmd:accept:{item.id}")])  # noqa: E501
                if item.status in ("assigned", "accepted", "in_progress", "blocked"):
                    buttons.append([InlineKeyboardButton(text=f"✅ Submit #{item.id}", callback_data=f"cmd:done:{item.id}")])  # noqa: E501
        else:
            lines.append("<i>No open assignments.</i>")
        lines += ["", f"📥 Open messages for you: <b>{len(mail)}</b>"]
        buttons.append([InlineKeyboardButton(text="📤 Report to management", callback_data="cmd:report")])  # noqa: E501
        if is_management(roles):
            buttons.append([InlineKeyboardButton(text="+ Assign work", callback_data="cmd:assign")])
            buttons.append([InlineKeyboardButton(text="📥 Management inbox", callback_data="cmd:inbox")])  # noqa: E501
        buttons.append([InlineKeyboardButton(text="🏠 Back to Office", callback_data="cb:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _inbox_text(uid: int, uname: str) -> tuple[str, InlineKeyboardMarkup]:
    async with session_scope() as s:
        person, _ = await resolve_person(s, uid, uname)
        rows = await command_inbox(s, person=person)
        lines = ["📥 <b>OFFICE INBOX</b>", RULE]
        buttons: list[list[InlineKeyboardButton]] = []
        if not rows:
            lines.append("<i>Nothing waiting for you.</i>")
        for item in rows[:12]:
            sender = await s.get(Person, item.sender_person_id)
            who = sender.full_name if sender else f"Person {item.sender_person_id}"
            lines.append(f"\n<b>#{item.id}</b> · {esc(item.category)} · from <b>{esc(who)}</b>\n{esc(item.body)}\n<i>{esc(item.status)}</i>")  # noqa: E501
            buttons.append([
                InlineKeyboardButton(text=f"↩ Reply #{item.id}", callback_data=f"cmd:reply:{item.id}"),  # noqa: E501
                InlineKeyboardButton(text=f"✓ Resolve #{item.id}", callback_data=f"cmd:resolve:{item.id}"),  # noqa: E501
            ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def _assign_start(message: Message, state: FSMContext, uid: int, uname: str) -> None:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
        if not is_management(roles):
            await message.answer("🔒 Assigning company work is for authorized management.")
            return
        people = await command_staff_choices(s)
    buttons = [[InlineKeyboardButton(text=p.full_name, callback_data=f"cmd:assignee:{p.id}")] for p in people]  # noqa: E501
    await state.clear()
    await message.answer("+ <b>ASSIGN WORK</b>\nWho should receive this assignment?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))  # noqa: E501


@router.message(Command("desk"))
async def desk(message: Message) -> None:
    if message.from_user is None:
        return
    text, kb = await _desk_text(message.from_user.id, message.from_user.full_name)
    await message.answer(text, reply_markup=kb)


@router.message(Command("assign"))
async def assign_work(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await _assign_start(message, state, message.from_user.id, message.from_user.full_name)


@router.callback_query(F.data == "cmd:assign")
async def cb_assign_work(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await _assign_start(cq.message, state, cq.from_user.id, cq.from_user.full_name)


@router.callback_query(F.data.startswith("cmd:assignee:"))
async def cb_assignee(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    target_id = int(cq.data.rsplit(":", 1)[1])
    async with session_scope() as s:
        _, roles = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        valid_ids = {p.id for p in await command_staff_choices(s)}
        if not is_management(roles) or target_id not in valid_ids:
            await cq.message.answer("🔒 That assignment target isn't available to you.")
            return
    await state.set_state(AssignWork.body)
    await state.update_data(assignee_id=target_id)
    await cq.message.answer("Type the assignment exactly as the staff member should receive it.")


@router.message(AssignWork.body, F.text & ~F.text.startswith("/"))
async def assign_body(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        _, roles = await resolve_person(s, message.from_user.id, message.from_user.full_name)
        if not is_management(roles):
            await state.clear()
            await message.answer("🔒 Assignment cancelled: your authorization could not be confirmed.")  # noqa: E501
            return
    await state.update_data(body=(message.text or "").strip())
    await state.set_state(AssignWork.due)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Today · 5 PM", callback_data="cmd:due:today"), InlineKeyboardButton(text="This week", callback_data="cmd:due:week")],  # noqa: E501
        [InlineKeyboardButton(text="No deadline", callback_data="cmd:due:none")],
    ])
    await message.answer("When is it due?", reply_markup=kb)


@router.callback_query(AssignWork.due, F.data.startswith("cmd:due:"))
async def assign_due(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    data = await state.get_data()
    due_kind = cq.data.rsplit(":", 1)[1]
    target_telegram: int | None = None
    target_name = "staff member"
    async with session_scope() as s:
        creator, roles = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        if not is_management(roles):
            await state.clear()
            await cq.message.answer("🔒 Assignment cancelled: authorization changed.")
            return
        item = await command_create_assignment(s, creator=creator, assignee_id=int(data["assignee_id"]), body=str(data["body"]), due_kind=due_kind)  # noqa: E501
        target = await s.get(Person, item.assignee_person_id)
        if target is not None:
            target_telegram, target_name = target.telegram_id, target.full_name
        item_id = item.id
        body = item.body
    await state.clear()
    await cq.message.answer(f"✅ Assignment <b>#{item_id}</b> sent to <b>{esc(target_name)}</b>.")
    await _safe_notify(cq.bot, target_telegram, f"📌 <b>NEW ASSIGNMENT #{item_id}</b>\nFrom: <b>{esc(cq.from_user.full_name)}</b>\n\n{esc(body)}\n\nOpen /desk to accept and track it.")  # noqa: E501


@router.callback_query(F.data.startswith("cmd:accept:"))
async def assignment_accept(cq: CallbackQuery) -> None:
    await cq.answer()
    item_id = int(cq.data.rsplit(":", 1)[1])
    async with session_scope() as s:
        person, roles = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        if not is_staff(roles):
            result = None
        else:
            result = await command_transition_assignment(s, person=person, assignment_id=item_id, action="accept")  # noqa: E501
    await cq.message.answer("🤝 Assignment accepted." if result else "Couldn't accept that assignment.")  # noqa: E501


@router.callback_query(F.data.startswith("cmd:done:"))
async def assignment_done(cq: CallbackQuery) -> None:
    await cq.answer()
    item_id = int(cq.data.rsplit(":", 1)[1])
    manager_telegram: int | None = None
    async with session_scope() as s:
        person, roles = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        result = await command_transition_assignment(s, person=person, assignment_id=item_id, action="done") if is_staff(roles) else None  # noqa: E501
        if result is not None:
            creator = await s.get(Person, result.created_by_person_id)
            manager_telegram = creator.telegram_id if creator else None
    if result is None:
        await cq.message.answer("Couldn't submit that assignment.")
        return
    await cq.message.answer("✅ Submitted to management. This is recorded as a completion claim, not auto-verified fact.")  # noqa: E501
    await _safe_notify(cq.bot, manager_telegram, f"📥 <b>ASSIGNMENT SUBMITTED #{item_id}</b>\nBy: <b>{esc(cq.from_user.full_name)}</b>\nOpen /desk or /inbox to review company activity.")  # noqa: E501


async def _report_start(message: Message, state: FSMContext, uid: int, uname: str) -> None:
    async with session_scope() as s:
        _, roles = await resolve_person(s, uid, uname)
        if not is_staff(roles):
            await message.answer("🔒 Management correspondence is for staff.")
            return
        people = await command_management_choices(s)
    if not people:
        await message.answer("No management recipient is configured yet.")
        return
    buttons = [[InlineKeyboardButton(text=p.full_name, callback_data=f"cmd:recipient:{p.id}")] for p in people]  # noqa: E501
    await state.clear()
    await message.answer("📤 <b>SEND TO MANAGEMENT</b>\nWho should receive it?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))  # noqa: E501


@router.message(Command("report"))
async def report_management(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    await _report_start(message, state, message.from_user.id, message.from_user.full_name)


@router.callback_query(F.data == "cmd:report")
async def cb_report_management(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await _report_start(cq.message, state, cq.from_user.id, cq.from_user.full_name)


@router.callback_query(F.data.startswith("cmd:recipient:"))
async def report_recipient(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    recipient_id = int(cq.data.rsplit(":", 1)[1])
    async with session_scope() as s:
        _, roles = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        valid_ids = {p.id for p in await command_management_choices(s)}
        if not is_staff(roles) or recipient_id not in valid_ids:
            await cq.message.answer("🔒 That recipient isn't available.")
            return
    await state.set_state(SendManagement.category)
    await state.update_data(recipient_id=recipient_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Report", callback_data="cmd:cat:report"), InlineKeyboardButton(text="🚧 Blocker", callback_data="cmd:cat:blocker")],  # noqa: E501
        [InlineKeyboardButton(text="🙋 Request", callback_data="cmd:cat:request"), InlineKeyboardButton(text="💡 Suggestion", callback_data="cmd:cat:suggestion")],  # noqa: E501
        [InlineKeyboardButton(text="⚠️ Complaint / concern", callback_data="cmd:cat:concern")],
    ])
    await cq.message.answer("What kind of message is this?", reply_markup=kb)


@router.callback_query(SendManagement.category, F.data.startswith("cmd:cat:"))
async def report_category(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await state.update_data(category=cq.data.rsplit(":", 1)[1])
    await state.set_state(SendManagement.body)
    await cq.message.answer("Type your message. It will become an attributable company record.")


@router.message(SendManagement.body, F.text & ~F.text.startswith("/"))
async def report_body(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    recipient_telegram: int | None = None
    recipient_name = "management"
    async with session_scope() as s:
        sender, roles = await resolve_person(s, message.from_user.id, message.from_user.full_name)
        valid_ids = {p.id for p in await command_management_choices(s)}
        recipient_id = int(data["recipient_id"])
        if not is_staff(roles) or recipient_id not in valid_ids:
            await state.clear()
            await message.answer("🔒 Message cancelled: recipient/authorization changed.")
            return
        item = await command_create_correspondence(s, sender=sender, recipient_id=recipient_id, category=str(data["category"]), body=message.text or "")  # noqa: E501
        recipient = await s.get(Person, recipient_id)
        if recipient is not None:
            recipient_telegram, recipient_name = recipient.telegram_id, recipient.full_name
        item_id, body, category = item.id, item.body, item.category
    await state.clear()
    await message.answer(f"✅ Sent to <b>{esc(recipient_name)}</b> as office record <b>#{item_id}</b>.")  # noqa: E501
    await _safe_notify(message.bot, recipient_telegram, f"📥 <b>NEW OFFICE MESSAGE #{item_id}</b>\nFrom: <b>{esc(message.from_user.full_name)}</b> · {esc(category)}\n\n{esc(body)}\n\nOpen /inbox to respond.")  # noqa: E501


@router.message(Command("inbox"))
async def office_inbox(message: Message) -> None:
    if message.from_user is None:
        return
    text, kb = await _inbox_text(message.from_user.id, message.from_user.full_name)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "cmd:inbox")
async def cb_office_inbox(cq: CallbackQuery) -> None:
    await cq.answer()
    text, kb = await _inbox_text(cq.from_user.id, cq.from_user.full_name)
    await cq.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cmd:reply:"))
async def correspondence_reply_start(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    item_id = int(cq.data.rsplit(":", 1)[1])
    async with session_scope() as s:
        person, _ = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        item = await command_acknowledge_correspondence(s, person=person, item_id=item_id)
    if item is None:
        await cq.message.answer("That message isn't in your inbox.")
        return
    await state.set_state(ReplyManagement.body)
    await state.update_data(reply_to=item_id, recipient_id=item.sender_person_id)
    await cq.message.answer(f"Reply to office message #{item_id}:")


@router.message(ReplyManagement.body, F.text & ~F.text.startswith("/"))
async def correspondence_reply_body(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    recipient_telegram: int | None = None
    async with session_scope() as s:
        sender, _ = await resolve_person(s, message.from_user.id, message.from_user.full_name)
        item = await command_create_correspondence(s, sender=sender, recipient_id=int(data["recipient_id"]), category="reply", body=message.text or "", parent_id=int(data["reply_to"]))  # noqa: E501
        recipient = await s.get(Person, item.recipient_person_id)
        recipient_telegram = recipient.telegram_id if recipient else None
        item_id, body = item.id, item.body
    await state.clear()
    await message.answer(f"↩ Reply recorded as <b>#{item_id}</b>.")
    await _safe_notify(message.bot, recipient_telegram, f"💬 <b>MANAGEMENT RESPONSE #{item_id}</b>\nFrom: <b>{esc(message.from_user.full_name)}</b>\n\n{esc(body)}\n\nOpen /inbox to continue the thread.")  # noqa: E501


@router.callback_query(F.data.startswith("cmd:resolve:"))
async def correspondence_resolve(cq: CallbackQuery) -> None:
    await cq.answer()
    item_id = int(cq.data.rsplit(":", 1)[1])
    async with session_scope() as s:
        person, _ = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        item = await command_resolve_correspondence(s, person=person, item_id=item_id)
    await cq.message.answer("✓ Office message resolved." if item else "That message isn't in your inbox.")  # noqa: E501


@router.callback_query(F.data == "cb:desk")
async def cb_desk(cq: CallbackQuery) -> None:
    await cq.answer()
    text, kb = await _desk_text(cq.from_user.id, cq.from_user.full_name)
    await cq.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "cb:home")
async def cb_command_home(cq: CallbackQuery) -> None:
    await cq.answer()
    text, kb = await _office(cq.from_user.id, cq.from_user.full_name)
    await _edit_or_send(cq, text, kb)


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


@router.callback_query(F.data.in_({"cb:myday", "cb:home"}))
async def cb_home(cq: CallbackQuery) -> None:
    await cq.answer()
    text, kb = await _office(cq.from_user.id, cq.from_user.full_name)
    await _edit_or_send(cq, text, kb)


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
    try:
        pid = int((cq.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await cq.answer("Invalid priority.", show_alert=True)
        return
    async with session_scope() as s:
        person, roles = await resolve_person(s, cq.from_user.id, cq.from_user.full_name)
        if not is_staff(roles):
            await cq.answer("Staff access required.", show_alert=True)
            return
        completed = await complete_priority(s, person=person, priority_id=pid)
    if completed is None:
        await cq.answer("That priority isn't yours.", show_alert=True)
        return
    await cq.answer("Marked done ✅")
    text, kb = await _office(cq.from_user.id, cq.from_user.full_name)
    await _edit_or_send(cq, text, kb)


# The command menu shown when a user types "/". Access is still enforced
# server-side per handler; this list is only the visible affordance.
_MENU: list[BotCommand] = [
    BotCommand(command="menu", description="🏠 Home — your command center"),
    BotCommand(command="checkin", description="🏢 Check in for today"),
    BotCommand(command="myday", description="📅 Your priorities & Daily Close"),
    BotCommand(command="desk", description="🗂 Your assignments & office inbox"),
    BotCommand(command="assign", description="Management: assign work"),
    BotCommand(command="report", description="📤 Send report/concern to management"),
    BotCommand(command="inbox", description="📥 Your office correspondence"),
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
