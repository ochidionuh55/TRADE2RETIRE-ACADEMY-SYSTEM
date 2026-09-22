"""Telegram bot — the MVP interface. No business logic lives here.

Every number and rule comes from the services layer over the canonical store.
The bot resolves who you are (by role, not by command knowledge) and renders.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
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
    minutes_left_in_window,
    office_enabled,
)
from app.config import PROCESS, get_settings
from app.db import session_scope
from app.logging import configure_logging, get_logger
from app.models import OPEN_STATES, FridayReport, Intervention, Person
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


class Friday(StatesGroup):
    collecting = State()


def _q(i: int):
    qs = PROCESS.friday_questions
    return qs[i] if 0 <= i < len(qs) else None


@router.message(CommandStart())
async def start(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
    name = person.full_name or "there"
    menu = ["Commands you can use:"]
    menu.append("• /friday — submit this week's trading review")
    menu.append("• /me — see your record")
    if has_role(roles, Role.MENTOR):
        menu += ["• /queue — your open interventions", "• /done <id> <outcome> — close one"]
    if has_role(roles, Role.CEO, Role.ADMIN):
        menu.append("• /brief — this week's executive brief")
    await message.answer(
        f"Welcome to {get_settings().academy_name}, {name}.\n\n" + "\n".join(menu)
    )


@router.message(Command("me"))
async def me(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        role_str = ", ".join(sorted(r.value for r in roles)) or "student (unassigned)"
    await message.answer(f"{person.full_name or 'You'}\nRoles: {role_str}")


# ── Friday report flow ───────────────────────────────────────────────────────


@router.message(Command("friday"))
async def friday_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Friday.collecting)
    await state.update_data(idx=0, answers={})
    first = _q(0)
    await message.answer(
        "📝 Weekly trading review. Answer each prompt; send “-” to skip an "
        "optional one.\n\n" + (first.prompt if first else "")
    )


@router.message(Friday.collecting, F.text)
async def friday_step(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(data.get("idx", 0))
    answers = dict(data.get("answers", {}))
    question = _q(idx)
    if question is not None:
        text = (message.text or "").strip()
        if text == "-" and question.required:
            await message.answer("That one's required. " + question.prompt)
            return
        answers[question.key] = None if text == "-" else text

    nxt = _q(idx + 1)
    if nxt is not None:
        await state.update_data(idx=idx + 1, answers=answers)
        await message.answer(nxt.prompt)
        return

    # Done — persist.
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
        "✅ Report recorded — thank you." if created
        else "You already submitted this week's report; the first one stands."
    )


# ── Mentor ───────────────────────────────────────────────────────────────────


@router.message(Command("queue"))
async def queue(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not has_role(roles, Role.MENTOR, Role.ADMIN):
            await message.answer("That view is for mentors.")
            return
        stmt = select(Intervention).where(Intervention.status.in_(tuple(OPEN_STATES)))
        if not has_role(roles, Role.ADMIN):
            stmt = stmt.where(Intervention.mentor_id == person.id)
        rows = (await s.execute(stmt.order_by(Intervention.due_at))).scalars().all()
        lines = []
        for iv in rows:
            subject = await s.get(Person, iv.subject_person_id)
            who = subject.full_name if subject else f"person {iv.subject_person_id}"
            mark = "⚠️ OVERDUE" if iv.status == "overdue" else "open"
            lines.append(f"#{iv.id} [{mark}] {who} — {iv.reason}")
    await message.answer(
        "Your interventions:\n\n" + "\n".join(lines) + "\n\nClose with /done <id> <outcome>"
        if lines
        else "Nothing open. 👍"
    )


@router.message(Command("done"))
async def done(message: Message) -> None:
    if message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Usage: /done <id> <what you did>")
        return
    iv_id, outcome = int(parts[1]), parts[2]
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not has_role(roles, Role.MENTOR, Role.ADMIN):
            await message.answer("That action is for mentors.")
            return
        result = await complete_intervention(s, person, iv_id, outcome)
    await message.answer(
        f"✅ Intervention #{iv_id} closed." if result else "Couldn't close that one."
    )


# ── CEO ──────────────────────────────────────────────────────────────────────


@router.message(Command("brief"))
async def brief(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        _, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not has_role(roles, Role.CEO, Role.ADMIN):
            await message.answer("The executive brief is for management.")
            return
        data = await weekly_brief(s)
    await message.answer(format_brief(data, get_settings().academy_name))


# ── review a report (mentor): /review <report_id> <note> ─────────────────────


@router.message(Command("review"))
async def review(message: Message) -> None:
    if message.from_user is None:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Usage: /review <report_id> <your note>")
        return
    report_id, note = int(parts[1]), parts[2]
    async with session_scope() as s:
        person, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not has_role(roles, Role.MENTOR, Role.ADMIN):
            await message.answer("Reviews are for mentors.")
            return
        report = await s.get(FridayReport, report_id)
        if report is None:
            await message.answer("No such report.")
            return
        await review_report(s, person, report_id, note)
    await message.answer("✅ Review recorded.")


# ── Staff check-in (Slice 3) ─────────────────────────────────────────────────


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


async def _finish_checkin(
    message: Message, state: FSMContext, presence: str, code: str | None
) -> None:
    async with session_scope() as s:
        person, _ = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        check, created = await check_in(
            s, person=person, presence_type=presence, office_code=code
        )
    await state.clear()
    if not created:
        await message.answer(
            f"You already checked in today ({check.presence_type}).",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if presence == OFFICE and check.verified:
        text = "✅ Checked in — OFFICE, *verified*. Have a great day."
    elif presence == OFFICE:
        text = (
            "🟡 Checked in — OFFICE, recorded as *unverified* "
            "(office verification isn't set up yet)."
        )
    else:
        text = f"✅ Checked in — {presence}. Recorded."
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(Command("checkin"))
async def checkin_start(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        _, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
    if not is_staff(roles):
        await message.answer("Check-in is for staff.")
        return
    await state.set_state(Checkin.presence)
    await message.answer("Where are you working today?", reply_markup=_presence_keyboard())


@router.message(Checkin.presence, F.text)
async def checkin_presence(message: Message, state: FSMContext) -> None:
    presence = _LABEL_TO_PRESENCE.get((message.text or "").strip())
    if presence is None:
        await message.answer("Please tap one of the buttons.")
        return
    if presence == OFFICE and office_enabled():
        await state.set_state(Checkin.code)
        await message.answer(
            "Enter today's office code (shown in the office):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await _finish_checkin(message, state, presence, None)


@router.message(Checkin.code, F.text)
async def checkin_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not is_valid_office_code(code):
        await message.answer(
            "That code didn't match the current office code. Type it again, "
            "or /cancel to pick a different presence."
        )
        return
    await _finish_checkin(message, state, OFFICE, code)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("officecode"))
async def officecode(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        _, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
    if not has_role(
        roles, Role.OPERATIONS, Role.ADMIN, Role.CEO, Role.CO_OWNER,
        Role.HEAD_OF_ACADEMY, Role.HEAD_OF_SUPPORT,
    ):
        await message.answer("The office code is for operations/management.")
        return
    code = current_office_code()
    if code is None:
        await message.answer(
            "Office verification isn't configured yet. Set T2R__OFFICE_SECRET to enable it."
        )
        return
    await message.answer(
        f"🔑 Office code: *{code}*\n"
        f"Valid ~{minutes_left_in_window()} more min, then it rotates.\n"
        "Display it in the office; staff enter it when they /checkin."
    )


@router.message(Command("today"))
async def today(message: Message) -> None:
    if message.from_user is None:
        return
    async with session_scope() as s:
        _, roles = await resolve_person(
            s, message.from_user.id, message.from_user.full_name
        )
        if not (is_manager(roles) or is_management(roles)):
            await message.answer("Today's attendance is for managers.")
            return
        data = await attendance_today(s)
    lines = [
        f"👥 ATTENDANCE — {data['work_date']}",
        "",
        f"Checked in            {data['checked_in']}",
        f"Verified present      {data['verified_present']}",
        f"Office (unverified)   {data['unverified_office']}",
        f"Remote / field        {data['remote_or_field']}",
        f"Approved away         {data['approved_away']}",
        "",
        f"Office verification: {data['office_verification']}",
        "Every figure is a canonical check-in; 'verified' means an office code corroborated it.",
    ]
    await message.answer("\n".join(lines))


# The command menu shown when a user types "/". Access is still enforced
# server-side per handler; this list is only the visible affordance.
_MENU: list[BotCommand] = [
    BotCommand(command="start", description="🏠 Home — your menu"),
    BotCommand(command="checkin", description="🏢 Check in for today"),
    BotCommand(command="me", description="👤 Your record and roles"),
    BotCommand(command="friday", description="📝 Submit this week's trading review"),
    BotCommand(command="today", description="👥 Managers: today's attendance"),
    BotCommand(command="officecode", description="🔑 Ops: current office code"),
    BotCommand(command="queue", description="🚨 Mentor: your open interventions"),
    BotCommand(command="review", description="✅ Mentor: review a report"),
    BotCommand(command="done", description="✔️ Mentor: close an intervention"),
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
    bot = Bot(settings.telegram_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await _publish_menu(bot)
    logger.info("bot.starting")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_bot())
