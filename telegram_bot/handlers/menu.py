import random
import string
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime
from contextlib import suppress
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from config import settings
import database.repository as repo
from database.connection import DatabaseManager
from ichancy_api.client import ichancy_api_client
from integrations.syriatel_cash import verify_incoming_deposit
from telegram_bot.keyboards.inline import (
    get_main_menu_keyboard,
    get_ichancy_submenu,
    get_contests_list_keyboard,
    get_contest_submit_keyboard,
    get_prediction_card_options_keyboard,
    get_prediction_cards_list_keyboard,
)

router = Router()
logger = logging.getLogger(__name__)

# 🔒 قفل حماية ضد النقر المزدوج السريع على أزرار التأكيد (Double-Click Guard)
_user_action_locks = set()

def _try_acquire_lock(user_id) -> bool:
    uid = str(user_id)
    if uid in _user_action_locks:
        return False
    _user_action_locks.add(uid)
    return True

def _release_lock(user_id):
    _user_action_locks.discard(str(user_id))

MAX_WITHDRAW_SYP = 5_000_000

def _get_min_deposit_syp():
    """أدنى مبلغ إيداع بالليرة — من قاعدة البيانات، مع fallback."""
    try:
        s = repo.get_bot_settings()
        return int(s.get('min_deposit_syp', 20000) or 20000)
    except:
        return 20000

def _get_min_deposit_usd():
    """أدنى مبلغ إيداع بالدولار — من قاعدة البيانات، مع fallback."""
    try:
        s = repo.get_bot_settings()
        return int(s.get('min_deposit_usd', 5) or 5)
    except:
        return 5

def _get_min_withdraw_syp():
    """أدنى مبلغ سحب بالليرة — من قاعدة البيانات، مع fallback."""
    try:
        s = repo.get_bot_settings()
        return int(s.get('min_withdraw_syp', 25000) or 25000)
    except:
        return 25000

def _get_min_withdraw_usd():
    """أدنى مبلغ سحب بالدولار — من قاعدة البيانات، مع fallback (احتياطي فقط)."""
    try:
        s = repo.get_bot_settings()
        return int(s.get('min_withdraw_usd', 10) or 10)
    except:
        return 10

# 🆕 دوال مساعدة لتحويل الليرة القديمة ↔ الجديدة
def _syp_old_to_new(amount_old):
    return amount_old / 100

def _syp_new_to_old(amount_new):
    return amount_new * 100

def _get_syp_version():
    try:
        s = repo.get_bot_settings()
        return str(s.get('syp_version', 'old') or 'old')
    except:
        return 'old'

def _fmt_syp_dual(amount_old):
    old = f"{int(amount_old):,}"
    nv = _syp_old_to_new(amount_old)
    new_str = f"{int(nv):,}" if nv == int(nv) else f"{nv:,.2f}"
    return f"{old} ل.س  <code>({new_str} ل.س جديدة)</code>"

def _fmt_limits_dual(amount_old):
    old = f"{int(amount_old):,}"
    nv = _syp_old_to_new(amount_old)
    new_str = f"{int(nv):,}" if nv == int(nv) else f"{nv:,.2f}"
    return f"{old} ل.س  <code>({new_str} ل.س جديدة)</code>"

def _fmt_syp_copy_label(amount_old):
    ver = _get_syp_version()
    if ver == 'new':
        nv = _syp_old_to_new(amount_old)
        new_str = f"{int(nv):,}" if nv == int(nv) else f"{nv:,.2f}"
        return f"📋 نسخ {new_str} ل.س جديدة"
    return f"📋 نسخ {int(amount_old):,} ل.س"

def _get_syp_copy_amount(amount_old):
    ver = _get_syp_version()
    if ver == 'new':
        nv = _syp_old_to_new(amount_old)
        return str(int(nv)) if nv == int(nv) else f"{nv:.2f}"
    return str(int(amount_old))

ADMIN_IDS = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]


def is_admin_user(user_id) -> bool:
    return str(user_id) in ADMIN_IDS


def get_admin_target_chat_ids():
    return ADMIN_IDS or [str(settings.ADMIN_ID)]


async def notify_admins(bot, text, reply_markup=None, parse_mode="HTML"):
    for admin_id in get_admin_target_chat_ids():
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logger.warning(f"notify_admins failed for {admin_id}: {e}")


def get_support_chat_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 إنهاء المحادثة", callback_data="end_support_chat")]
    ])


def get_admin_reply_keyboard(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ رد على المستخدم", callback_data=f"reply_user_{user_id}")],
        [InlineKeyboardButton(text="👤 تفاصيل المستخدم", callback_data=f"user_details_{user_id}")]
    ])


async def forward_support_message_to_admins(message: Message):
    """Forward/copy any support-chat message to admins with a safe reply button."""
    sender = message.from_user
    user = repo.get_user(str(sender.id))

    display_name = sender.full_name or sender.first_name or "مستخدم"
    username = f"@{sender.username}" if sender.username else "بدون معرف"
    bot_balance = int((user or {}).get('bot_balance') or 0)
    ichancy_username = (user or {}).get('ichancy_username') or 'غير مرتبط'
    player_id = (user or {}).get('player_id') or 'غير متوفر'

    caption = (
        "📨 <b>رسالة دعم مباشر جديدة</b>\n\n"
        f"👤 <b>المرسل:</b> {display_name}\n"
        f"📛 <b>المعرف:</b> {username}\n"
        f"🆔 <b>Telegram ID:</b> <code>{sender.id}</code>\n"
        f"💎 <b>رصيد البوت:</b> <code>{bot_balance:,} SYP</code>\n"
        f"🎮 <b>iChancy:</b> <code>{ichancy_username}</code>\n"
        f"🔑 <b>Player ID:</b> <code>{player_id}</code>"
    )
    reply_markup = get_admin_reply_keyboard(sender.id)

    sent_any = False
    for admin_id in get_admin_target_chat_ids():
        try:
            await message.bot.send_message(chat_id=admin_id, text=caption, reply_markup=reply_markup, parse_mode="HTML")
            await message.bot.copy_message(chat_id=admin_id, from_chat_id=message.chat.id, message_id=message.message_id)
            sent_any = True
        except Exception as e:
            logger.warning(f"forward_support_message_to_admins failed for {admin_id}: {e}")
    return sent_any


def get_log_channel_id():
    return getattr(settings, "LOG_CHANNEL_ID", None)


# 🆕 تحسين دالة إرسال السجلات لتعيد bool وتدعم التتبع
async def send_log_message(bot, text, parse_mode="HTML"):
    log_channel_id = get_log_channel_id()
    if not log_channel_id:
        logger.debug("LOG_CHANNEL_ID not configured")
        return False
    try:
        await bot.send_message(chat_id=log_channel_id, text=text, parse_mode=parse_mode)
        logger.debug(f"✅ Log sent to {log_channel_id}")
        return True
    except Exception as e:
        logger.error(f"❌ send_log_message failed: {e}")
        return False


async def send_coming_soon(callback: CallbackQuery, title: str):
    await safe_edit_text(
        callback.message,
        f"✨ <b>{title}</b>\n\nهذه الميزة سيتم تجهيزها بشكل أجمل في التحديث القادم.\nيمكنك الآن العودة للقائمة الرئيسية أو متابعة الخدمات المتاحة.",
        reply_markup=get_user_menu_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


class BotStates(StatesGroup):
    selecting_deposit_currency = State()
    selecting_deposit_gateway = State()
    entering_deposit_amount = State()
    entering_deposit_proof = State()

    selecting_withdraw_currency = State()
    selecting_withdraw_gateway = State()
    entering_withdraw_recipient = State()
    entering_withdraw_amount = State()
    confirming_withdraw = State()

    entering_gift_amount = State()
    entering_gift_code_to_redeem = State()

    entering_admin_message = State()
    support_chat_active = State()
    replying_to_user = State()

    entering_ichancy_username = State()
    entering_ichancy_password = State()

    entering_game_deposit_amount = State()
    entering_game_withdraw_amount = State()
    confirming_game_deposit = State()
    confirming_game_withdraw = State()

    entering_admin_search = State()
    entering_rejection_reason = State()
    entering_contest_proof = State()


def get_user_menu_keyboard(user_id):
    is_admin = is_admin_user(user_id)
    return get_main_menu_keyboard(is_admin=is_admin)


def safe_balance(user):
    """🛡️ إرجاع رصيد البوت بأمان (يتعامل مع None/مستخدم جديد)."""
    return int(user.get('bot_balance') or 0) if user else 0


def has_usd_deposits_approved(telegram_id):
    history = repo.get_user_transactions_history(telegram_id, limit=100)
    usd_gateways = ['sham_usd', 'usdt_trc', 'usdt_bep']
    for tx in history:
        if tx['type'] == 'deposit_bot' and tx['payment_method'] in usd_gateways and tx['status'] == 'approved':
            return True
    return False


def has_pending_transaction(telegram_id, tx_type=None):
    """فحص وجود أي طلب معلّق للمستخدم مباشرة من قاعدة البيانات.

    كان الفحص سابقاً يعتمد على آخر 20 عملية فقط، وهذا قد يسمح بإنشاء طلب جديد
    إذا كان هناك طلب معلّق قديم خارج آخر 20 سجل. هذه الدالة الآن تستخدم
    repository لضمان فحص كل الطلبات المعلّقة فعلياً.
    """
    return repo.has_pending_transaction(telegram_id, tx_type)


def is_valid_phone_number(value: str) -> bool:
    cleaned = value.replace(' ', '').replace('-', '')
    return cleaned.isdigit() and 8 <= len(cleaned) <= 15


def is_valid_sham_account(value: str) -> bool:
    cleaned = value.strip().replace(' ', '')
    return len(cleaned) >= 6


def is_valid_usdt_address(value: str) -> bool:
    cleaned = value.strip()
    if cleaned.startswith('T') and 20 <= len(cleaned) <= 50:
        return True
    if cleaned.startswith('0x') and len(cleaned) == 42:
        return True
    return False


def validate_recipient_by_gateway(gateway: str, recipient: str):
    if gateway in ['syriatel', 'mtn'] and not is_valid_phone_number(recipient):
        return False, "❌ رقم الهاتف غير صالح. يرجى إدخال رقم صحيح."
    if gateway in ['sham_syp', 'sham_usd'] and not is_valid_sham_account(recipient):
        return False, "❌ معرف أو رقم حساب شام كاش غير صالح."
    if gateway in ['usdt_trc', 'usdt_bep'] and not is_valid_usdt_address(recipient):
        return False, "❌ عنوان المحفظة غير صالح مبدئياً."
    return True, None


async def safe_edit_text(target_message, text, reply_markup=None, parse_mode="HTML"):
    try:
        await target_message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except Exception as e:
        logger.warning(f"safe_edit_text fallback triggered: {e}")
        try:
            await target_message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except Exception as inner_e:
            logger.error(f"safe_edit_text final fallback failed: {inner_e}")
            return False


async def safe_delete_message(target_message):
    try:
        await target_message.delete()
        return True
    except Exception as e:
        logger.warning(f"safe_delete_message ignored: {e}")
        return False


async def safe_answer_callback(callback: CallbackQuery, text=None, show_alert=False):
    try:
        await callback.answer(text or "✅", show_alert=show_alert)
        return True
    except Exception as e:
        logger.warning(f"safe_answer_callback ignored: {e}")
        return False


async def send_expired_flow_message(target_message, user_id):
    try:
        await target_message.answer(
            "⚠️ <b>انتهت صلاحية هذه العملية أو تم استخدام هذا الزر سابقاً.</b>\n\n"
            "يرجى العودة إلى القائمة الرئيسية وبدء العملية من جديد.",
            reply_markup=get_user_menu_keyboard(user_id),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"send_expired_flow_message failed: {e}")


async def ensure_admin_callback(callback: CallbackQuery) -> bool:
    if not is_admin_user(callback.from_user.id):
        await safe_answer_callback(callback, "❌ غير مسموح لك باستخدام أدوات الإدارة.", show_alert=True)
        return False
    return True


async def ensure_admin_message(message: Message, state: FSMContext | None = None) -> bool:
    if not is_admin_user(message.from_user.id):
        if state:
            await state.clear()
        await message.answer("❌ غير مسموح لك باستخدام أدوات الإدارة.")
        return False
    return True


async def require_ichancy_registered(callback: CallbackQuery) -> bool:
    user = repo.get_user(str(callback.from_user.id))
    if not user or not user.get('player_id'):
        await safe_edit_text(
            callback.message,
            "👑 <b>قيصر جديد في اللعبة!</b>\n\n"
            "يجب عليك تسجيل حساب iChancy أولاً لتحصل على Player ID الخاص بك.\n"
            "توجه إلى قسم ⚡️ <b>حساب iChancy</b> وأنشئ حسابك الآن.",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback)
        return False
    return True



async def _deliver_flow_message(target_message, text, reply_markup=None, parse_mode="HTML", edit=False):
    """Send the same entry screen from either a callback or a /start deep link."""
    if edit:
        return await safe_edit_text(
            target_message,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    await target_message.answer(
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    return True


async def _ensure_service_gate(target_message, user_id, service, edit=False):
    allowed, reason = repo.service_gate_status(service)
    if allowed:
        return True
    await _deliver_flow_message(
        target_message,
        f"🛡️ <b>الخدمة غير متاحة مؤقتاً</b>\n\n{reason}",
        reply_markup=get_user_menu_keyboard(user_id),
        edit=edit,
    )
    return False


async def start_deposit_flow(target_message, user_id, state: FSMContext, edit=False):
    """Open the existing deposit flow without creating a financial transaction."""
    if not await _ensure_service_gate(target_message, user_id, 'deposit', edit=edit):
        return False
    telegram_id = str(user_id)
    user = repo.get_user(telegram_id)
    if not user or not user.get('player_id'):
        await _deliver_flow_message(
            target_message,
            "👑 <b>قيصر جديد في اللعبة!</b>\n\n"
            "يجب عليك تسجيل حساب iChancy أولاً لتحصل على Player ID الخاص بك.\n"
            "توجه إلى قسم ⚡️ <b>حساب iChancy</b> وأنشئ حسابك الآن.",
            reply_markup=get_user_menu_keyboard(user_id),
            edit=edit,
        )
        return False

    if has_pending_transaction(telegram_id, 'deposit_bot'):
        await _deliver_flow_message(
            target_message,
            "⏳ لديك بالفعل طلب إيداع معلق. يرجى انتظار مراجعته قبل إنشاء طلب جديد.",
            reply_markup=get_user_menu_keyboard(user_id),
            edit=edit,
        )
        return False

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇸🇾 ليرة سورية", callback_data="dep_curr_syp"),
            InlineKeyboardButton(text="🇺🇸 دولار أمريكي (USD)", callback_data="dep_curr_usd")
        ],
        [InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="back_to_main_menu")]
    ])
    await _deliver_flow_message(
        target_message,
        "📥 <b>إيداع في البوت:</b>\n\nالرجاء تحديد العملة التي تود الإيداع بها 👇:",
        reply_markup=keyboard,
        edit=edit,
    )
    await state.set_state(BotStates.selecting_deposit_currency)
    return True


async def start_withdraw_flow(target_message, user_id, state: FSMContext, edit=False):
    """Open the existing withdraw flow without deducting any balance."""
    if not await _ensure_service_gate(target_message, user_id, 'withdraw', edit=edit):
        return False
    telegram_id = str(user_id)
    user = repo.get_user(telegram_id)
    if not user or not user.get('player_id'):
        await _deliver_flow_message(
            target_message,
            "👑 <b>قيصر جديد في اللعبة!</b>\n\n"
            "يجب عليك تسجيل حساب iChancy أولاً لتحصل على Player ID الخاص بك.\n"
            "توجه إلى قسم ⚡️ <b>حساب iChancy</b> وأنشئ حسابك الآن.",
            reply_markup=get_user_menu_keyboard(user_id),
            edit=edit,
        )
        return False

    if safe_balance(user) <= 0:
        await _deliver_flow_message(
            target_message,
            "❌ ليس لديك أي رصيد قابل للسحب في البوت حالياً!",
            reply_markup=get_user_menu_keyboard(user_id),
            edit=edit,
        )
        return False

    if has_pending_transaction(telegram_id, 'withdraw_bot'):
        await _deliver_flow_message(
            target_message,
            "⏳ لديك بالفعل طلب سحب معلق. يرجى انتظار مراجعته قبل إنشاء طلب جديد.",
            reply_markup=get_user_menu_keyboard(user_id),
            edit=edit,
        )
        return False

    if not has_usd_deposits_approved(telegram_id):
        await state.update_data(withdraw_currency='syp')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Syriatel Cash", callback_data="wit_gate_syriatel")],
            [InlineKeyboardButton(text="🟡 MTN Cash", callback_data="wit_gate_mtn")],
            [InlineKeyboardButton(text="📱 Sham Cash (SYP)", callback_data="wit_gate_sham_syp")],
            [InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="back_to_main_menu")]
        ])
        await _deliver_flow_message(
            target_message,
            f"📤 <b>سحب من البوت (ليرة سورية):</b>\n\n"
            f"💰 رصيدك: <code>{safe_balance(user):,} SYP</code>\n"
            "⚠️ السحب بالدولار غير متاح لأنك لم تقم بإيداع دولار سابقاً.\n\n"
            "يرجى اختيار طريقة السحب 👇:",
            reply_markup=keyboard,
            edit=edit,
        )
        await state.set_state(BotStates.selecting_withdraw_gateway)
        return True

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇸🇾 ليرة سورية", callback_data="wit_curr_syp"),
            InlineKeyboardButton(text="🇺🇸 دولار أمريكي (USD)", callback_data="wit_curr_usd")
        ],
        [InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="back_to_main_menu")]
    ])
    bot_settings = repo.get_bot_settings()
    usd_sell_rate = float(bot_settings['usd_sell_rate'])
    approx_usd = safe_balance(user) / usd_sell_rate if usd_sell_rate > 0 else 0
    await _deliver_flow_message(
        target_message,
        f"📤 <b>سحب من البوت:</b>\n\n"
        f"💰 رصيدك: <code>{safe_balance(user):,} SYP</code>\n"
        f"💱 ما يعادل تقريباً: <code>{approx_usd:,.2f} USD</code> (حسب سعر الصرف الحالي)\n\n"
        "اختر عملة السحب 👇:",
        reply_markup=keyboard,
        edit=edit,
    )
    await state.set_state(BotStates.selecting_withdraw_currency)
    return True


async def start_gift_flow(target_message, user_id, state: FSMContext, edit=False):
    """Open the existing gift-code flow without deducting any balance yet."""
    if not await _ensure_service_gate(target_message, user_id, None, edit=edit):
        return False
    telegram_id = str(user_id)
    user = repo.get_user(telegram_id)
    if not user or safe_balance(user) <= 0:
        await _deliver_flow_message(
            target_message,
            "❌ لا يوجد لديك رصيد كافٍ لإنشاء كود هدية.",
            reply_markup=get_user_menu_keyboard(user_id),
            edit=edit,
        )
        return False

    await _deliver_flow_message(
        target_message,
        f"🎁 <b>إهداء رصيد</b>\n\nرصيدك الحالي: <code>{safe_balance(user):,} SYP</code>\n\n"
        "أرسل الآن المبلغ الذي تريد تحويله إلى كود هدية (بالليرة السورية):",
        edit=edit,
    )
    await state.set_state(BotStates.entering_gift_amount)
    return True

def format_deposit_admin_message(tx_id, telegram_id, username, amount, currency, gateway, transfer_number, amount_syp, user_balance, player_id=None, ichancy_username=None):
    username_text = f"@{username}" if username else "بدون معرف"
    msg = (
        "📥 <b>طلب إيداع جديد</b>\n\n"
        f"🆔 <b>رقم الطلب:</b> <code>#{tx_id}</code>\n"
        f"👤 <b>المستخدم:</b> {username_text}\n"
        f"💬 <b>Telegram ID:</b> <code>{telegram_id}</code>\n"
    )
    if ichancy_username:
        msg += f"🎮 <b>اسم اللاعب (iChancy):</b> <code>{ichancy_username}</code>\n"
    if player_id:
        msg += f"🔑 <b>Player ID:</b> <code>{player_id}</code>\n"
    msg += (
        f"💰 <b>المبلغ الأصلي:</b> <code>{amount:,} {currency}</code>\n"
        f"💱 <b>المكافئ بالليرة:</b> {_fmt_syp_dual(amount_syp)}\n"
        f"💳 <b>الوسيلة:</b> <code>{gateway.upper()}</code>\n"
        f"🔢 <b>الإثبات:</b> <code>{transfer_number}</code>\n"
        f"💎 <b>رصيد المستخدم الحالي:</b> {_fmt_syp_dual(user_balance)}"
    )
    return msg


def format_withdraw_admin_message(tx_id, telegram_id, username, entered_syp, gateway, recipient, gross_label, commission_label, net_label, user_balance_before, player_id=None, ichancy_username=None):
    username_text = f"@{username}" if username else "بدون معرف"
    gateway_labels = {
        'syriatel': 'سيريتل كاش', 'mtn': 'MTN كاش',
        'sham_syp': 'شام كاش (ليرة)', 'sham_usd': 'شام كاش (دولار)',
        'usdt_trc': 'USDT TRC20', 'usdt_bep': 'USDT BEP20',
    }
    gateway_name = gateway_labels.get(gateway, gateway.upper())
    msg = (
        "🚨 <b>طلب سحب جديد</b>\n\n"
        f"🆔 <b>رقم الطلب:</b> <code>#{tx_id}</code>\n"
        f"👤 <b>المستخدم:</b> {username_text}\n"
        f"💬 <b>Telegram ID:</b> <code>{telegram_id}</code>\n"
    )
    if ichancy_username:
        msg += f"🎮 <b>اسم اللاعب (iChancy):</b> <code>{ichancy_username}</code>\n"
    if player_id:
        msg += f"🔑 <b>Player ID:</b> <code>{player_id}</code>\n"
    msg += (
        f"💳 <b>الوسيلة:</b> <code>{gateway_name}</code>\n"
        f"📱 <b>المستلم:</b> <code>{recipient}</code>\n"
        f"💰 <b>المبلغ المخصوم:</b> {_fmt_syp_dual(entered_syp)}\n"
        f"📈 <b>القيمة قبل العمولة:</b> <code>{gross_label}</code>\n"
        f"🏷️ <b>العمولة:</b> <code>{commission_label}</code>\n"
        f"🎁 <b>الصافي:</b> <code>{net_label}</code>\n"
        f"💎 <b>رصيد المستخدم قبل الطلب:</b> {_fmt_syp_dual(user_balance_before)}\n\n"
        f"📤 <b>أرسل للمستخدم:</b> <code>{net_label}</code> عبر {gateway_name}"
    )
    return msg


# ================================================================
# 🆕 دوال الإيداع والسحب من / إلى اللعبة (مضافة للاستخدام المستقبلي)
# ================================================================

async def deposit_to_player_game(
    user_id: int,
    amount_syp: Decimal,
    bot
) -> bool:
    """
    تحويل مبلغ من رصيد البوت (SYP) إلى حساب اللاعب في اللعبة (NSP).
    النسبة الثابتة: 1 SYP = 1 NSP

    🔒 آمن ذرياً (Update 3): يخصم المبلغ مسبقاً قبل استدعاء الـ API،
    فلو فشل الـ API يُعاد الرصيد فوراً. مستحيل ضياع/تضاعف المبلغ.
    """
    try:
        logger.info(f"🔄 Starting deposit_to_player for user {user_id}, amount_syp: {amount_syp}")
        user = repo.get_user(user_id)
        if not user:
            logger.error(f"❌ User {user_id} not found")
            return False

        player_id = user.get('player_id')
        if not player_id:
            logger.error(f"❌ User {user_id} has no player_id")
            return False

        username = user.get('telegram_username') or 'Unknown'

        # 🆕 النسبة الثابتة 1 SYP = 1 NSP للمبلغ النقدي، ويُضاف بونص اللعب تلقائياً حسب إعدادات الأدمن
        cash_amount = int(amount_syp)
        if cash_amount < 1:
            return {'success': False, 'reason': 'invalid_amount'}

        # 🔒 الخطوة 1: خصم ذري مسبق (قفل الصف + خصم + سجل pending)
        # هذا يحمي من: النقر المزدوج + انقطاع الاتصال
        reserve = repo.reserve_game_deposit_atomic(user_id, amount_syp, player_id)
        if not reserve.get('success'):
            reason = reserve.get('reason')
            if reason == 'insufficient':
                old_balance = int(reserve.get('old_balance', 0))
                logger.error(f"❌ Insufficient bot balance: {old_balance} < {amount_syp}")
                log_text = (
                    f"❌ <b>فشل شحن اللعبة - رصيد غير كافٍ</b>\n\n"
                    f"👤 المستخدم: {username} ({user_id})\n"
                    f"💰 المبلغ المطلوب: {amount_syp:,} SYP\n"
                    f"⚠️ رصيد البوت لديك: {old_balance:,} SYP\n"
                    f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                await send_log_message(bot, log_text)
            else:
                logger.error(f"❌ reserve_game_deposit_atomic failed: {reason}")
            return False

        tx_id = reserve['tx_id']
        bonus_amount = int(reserve.get('bonus_amount') or 0)
        cashback_amount = int(reserve.get('cashback_amount') or 0)
        checkin_amount = int(reserve.get('checkin_amount') or 0)
        total_to_game = int(reserve.get('total_to_game') or cash_amount)

        # 🔒 الخطوة 2: استدعاء iChancy API بالمبلغ الإجمالي (شحن نقدي + بونص مرفق)
        logger.info(f"📤 Calling API depositToPlayer: player_id={player_id}, cash={cash_amount}, bonus={bonus_amount}, cashback={cashback_amount}, checkin={checkin_amount}, total={total_to_game}")
        deposit_result = await ichancy_api_client.deposit_to_player(
            player_id=player_id,
            amount=total_to_game
        )

        if not deposit_result or not deposit_result.get('success'):
            error_msg = deposit_result.get('message', 'Unknown error') if deposit_result else 'No response from API'
            logger.error(f"❌ depositToPlayer API failed: {error_msg}")
            if deposit_result and deposit_result.get('uncertain'):
                # لا نعيد الرصيد تلقائياً لأن API قد يكون نفذ الشحن لكن التحقق من الرصيد فشل/تأخر.
                # نترك المعاملة pending للمراجعة اليدوية حتى لا يحدث شحن فعلي + رد رصيد للمستخدم.
                log_text = (
                    f"⚠️ <b>شحن لعبة غير مؤكد ويحتاج مراجعة</b>\n\n"
                    f"👤 المستخدم: {username} ({user_id})\n"
                    f"🎮 حساب اللعبة: {player_id}\n"
                    f"💰 المبلغ النقدي: {cash_amount:,} SYP\n"
                    f"🎁 البونص المرفق: {bonus_amount:,} SYP\n"
                    f"🎮 الإجمالي المطلوب: {total_to_game:,} NSP\n"
                    f"⚠️ السبب: {error_msg}\n"
                    f"📌 المعاملة بقيت pending: <code>#{tx_id}</code>"
                )
                await send_log_message(bot, log_text)
                return {'success': False, 'uncertain': True, 'tx_id': tx_id, 'message': error_msg}
            # 🔒 إعادة الرصيد فوراً بعد فشل مؤكد للـ API
            repo.revert_game_transaction(tx_id)
            log_text = (
                f"❌ <b>فشل شحن اللعبة - خطأ API (تم إعادة الرصيد)</b>\n\n"
                f"👤 المستخدم: {username} ({user_id})\n"
                f"🎮 حساب اللعبة: {player_id}\n"
                f"💰 المبلغ النقدي: {cash_amount:,} SYP\n"
                f"🎁 البونص المرفق: {bonus_amount:,} SYP\n"
                f"🎮 الإجمالي للعبة: {total_to_game:,} NSP\n"
                f"🔴 الخطأ: {error_msg}\n"
                f"🔁 تم إعادة الرصيد النقدي والبونص للمستخدم\n"
                f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await send_log_message(bot, log_text)
            return False

        # 🔒 الخطوة 3: نجح الـ API → تأكيد العملية + تفعيل البونص النشط + تحديث رصيد اللعبة محلياً
        repo.confirm_reserved_game_deposit(tx_id)
        cached_game_balance = repo.get_user_game_balance(user_id)
        repo.update_user_game_balance(user_id, cached_game_balance + total_to_game)

        logger.info(f"✅ depositToPlayer successful for user {user_id} (tx #{tx_id})")
        log_text = (
            f"✅ <b>شحن ناجح إلى حساب اللعبة</b>\n\n"
            f"👤 المستخدم: {username} ({user_id})\n"
            f"🎮 حساب اللعبة: {player_id}\n"
            f"💰 الشحن النقدي: {cash_amount:,} SYP\n"
            f"🎁 بونص اللعب المرفق: {bonus_amount:,} SYP\n"
            f"💱 الإجمالي: {total_to_game:,} NSP\n"
            f"🟢 الحالة: مكتمل\n"
            f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await send_log_message(bot, log_text)
        return {'success': True, 'cash_amount': cash_amount, 'bonus_amount': bonus_amount, 'cashback_amount': cashback_amount, 'checkin_amount': checkin_amount, 'total_to_game': total_to_game, 'tx_id': tx_id}

    except Exception as e:
        logger.error(f"❌ Exception in deposit_to_player: {e}", exc_info=True)
        log_text = (
            f"🚨 <b>خطأ استثناء في شحن اللعبة</b>\n\n"
            f"👤 المستخدم ID: {user_id}\n"
            f"💰 المبلغ: {amount_syp:,} SYP\n"
            f"❌ الخطأ: {str(e)}\n"
            f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await send_log_message(bot, log_text)
        return False



async def withdraw_from_player_game(
    user_id: int,
    amount_nsp: Decimal,
    bot
) -> bool:
    """سحب مبلغ من حساب اللاعب في اللعبة (NSP) إلى رصيد البوت (SYP).
    النسبة الثابتة: 1 NSP = 1 SYP

    🔒 آمن ذرياً (Update 3): نسجل المعاملة pending أولاً، ثم نستدعي الـ API،
    ثم نضيف الرصيد فقط عند النجاح. مستحيل ضياع/تضاعف المبلغ.
    🆕 (Rollover Guard): يمنع السحب إذا كان هناك بونص محجوز لم يتم تدويره.
    """
    try:
        logger.info(f"🔄 Starting withdraw_from_player for user {user_id}, amount_nsp: {amount_nsp}")
        user = repo.get_user(user_id)
        if not user:
            logger.error(f"❌ User {user_id} not found")
            return False

        player_id = user.get('player_id')
        if not player_id:
            logger.error(f"❌ User {user_id} has no player_id")
            return False

        username = user.get('telegram_username') or 'Unknown'

        # 🎁 لا يوجد تدوير الآن: بونص اللعبة النشط يُخصم من مبلغ السحب عند التسوية.

        # 🆕 النسبة الثابتة 1 NSP = 1 SYP
        amount_syp = int(amount_nsp)

        # 🔒 الخطوة 1: تسجيل المعاملة pending كدليل على العملية الجارية
        # (الرصيد لا يُضاف هنا بعد؛ يُضاف فقط عند نجاح الـ API)
        tx_id = repo.create_transaction(
            telegram_id=str(user_id),
            tx_type='withdraw_from_game',
            amount=amount_syp,
            payment_method='game',
            transfer_number=f'Withdrawing {int(amount_nsp)} NSP from player {player_id}',
            status='pending'
        )

        # 🔒 الخطوة 2: استدعاء iChancy API
        logger.info(f"📥 Calling API withdrawFromPlayer: player_id={player_id}, amount_nsp={int(amount_nsp)}")
        withdraw_result = await ichancy_api_client.withdraw_from_player(
            player_id=player_id,
            amount=int(amount_nsp)
        )

        if not withdraw_result or not withdraw_result.get('success'):
            error_msg = withdraw_result.get('message', 'Unknown error') if withdraw_result else 'No response'
            logger.error(f"❌ withdrawFromPlayer API failed: {error_msg}")
            # 🔒 تعليم المعاملة كفاشلة دون إضافة رصيد (المبلغ لم يُخصم من اللعبة)
            if tx_id:
                repo.update_transaction_status(tx_id, 'failed')
                repo.update_transaction_rejection_reason(tx_id, f'iChancy API failed: {error_msg}')
            log_text = (
                f"❌ <b>فشل السحب من اللعبة - خطأ API</b>\n\n"
                f"👤 المستخدم: {username} ({user_id})\n"
                f"🎮 حساب اللعبة: {player_id}\n"
                f"💰 المبلغ: {int(amount_nsp):,} NSP\n"
                f"🔴 الخطأ: {error_msg}\n"
                f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await send_log_message(bot, log_text)
            return False

        # 🔒 الخطوة 3: نجح الـ API → خصم بونص اللعبة النشط أولاً ثم إضافة الصافي لرصيد البوت
        settlement = repo.settle_game_withdraw_with_active_bonus(user_id, amount_syp, tx_id=tx_id)
        if not settlement.get('ok'):
            logger.error(f"❌ settlement failed after iChancy withdraw: {settlement}")
            return {'success': False, 'reason': 'settlement_failed'}
        # تحديث رصيد اللعبة محلياً بالمبلغ الكامل المسحوب من iChancy
        cached_game_balance = repo.get_user_game_balance(user_id)
        repo.update_user_game_balance(user_id, max(cached_game_balance - int(amount_nsp), 0))

        cash_credited = int(settlement.get('cash_credited') or 0)
        bonus_deducted = int(settlement.get('bonus_deducted') or 0)
        logger.info(f"✅ withdrawFromPlayer successful for user {user_id}")
        log_text = (
            f"✅ <b>سحب ناجح من حساب اللعبة</b>\n\n"
            f"👤 المستخدم: {username} ({user_id})\n"
            f"🎮 حساب اللعبة: {player_id}\n"
            f"💱 المسحوب من اللعبة: {int(amount_nsp):,} NSP\n"
            f"🎁 خصم بونص لعب نشط: {bonus_deducted:,} SYP\n"
            f"💎 الصافي المضاف لرصيد البوت: {cash_credited:,} SYP\n"
            f"🟢 الحالة: مكتمل\n"
            f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await send_log_message(bot, log_text)
        return {'success': True, **settlement}

    except Exception as e:
        logger.error(f"❌ Exception in withdraw_from_player: {e}", exc_info=True)
        log_text = (
            f"🚨 <b>خطأ استثناء في سحب اللعبة</b>\n\n"
            f"👤 المستخدم ID: {user_id}\n"
            f"💰 المبلغ: {amount_nsp:,} NSP\n"
            f"❌ الخطأ: {str(e)}\n"
            f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        await send_log_message(bot, log_text)
        return False



def get_syriatel_auto_config():
    """إعدادات أتمتة Syriatel من قاعدة البيانات مع fallback لمتغيرات البيئة."""
    try:
        bs = repo.get_bot_settings() or {}
    except Exception:
        bs = {}
    mode = str(bs.get('syriatel_auto_mode') or getattr(settings, 'SYRIATEL_AUTO_MODE', 'off') or 'off').strip()
    if mode not in ('off', 'verify_only', 'auto_approve'):
        mode = 'off'
    channel_id = str(bs.get('syriatel_auto_channel_id') or getattr(settings, 'SYRIATEL_AUTO_CHANNEL_ID', '') or '').strip()
    return mode, channel_id


async def send_syriatel_auto_message(bot, text):
    """إرسال إشعار لقناة Syriatel Auto الخاصة إن وُجدت."""
    _, channel_id = get_syriatel_auto_config()
    if not channel_id:
        return False
    try:
        await bot.send_message(chat_id=channel_id, text=text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning(f"Syriatel auto channel send failed: {e}")
        return False


async def auto_approve_syriatel_deposit(bot, tx_id, external_ref=None):
    """قبول إيداع Syriatel تلقائياً بعد التحقق من API، مع نفس منطق البونصات الرئيسي."""
    tx = repo.get_transaction_by_id(tx_id)
    if not tx or tx.get('status') != 'pending':
        return {'ok': False, 'reason': 'not_pending'}
    deposit_amount = int(float(tx.get('amount') or 0))

    # منع إعادة استخدام نفس رقم العملية
    if external_ref and repo.is_external_ref_used(external_ref, exclude_tx_id=tx_id):
        return {'ok': False, 'reason': 'external_ref_used'}

    # أفضل بونص عام/فلاش
    bonus_info = repo.calculate_best_deposit_bonus(deposit_amount, tx.get('payment_method'))
    public_bonus_amount = int(bonus_info.get('bonus_amount') or 0)
    bonus_rule = bonus_info.get('rule')

    # بونص VIP + مكافأة الترقية كما في قبول الأدمن
    vip_deposit_bonus = 0
    vip_deposit_pct = 0
    vip_upgrade = {'upgraded': False}
    try:
        vip_settings = repo.get_vip_settings()
        tiers = vip_settings.get('tiers', [])
        if vip_settings.get('vip_enabled', True) and tiers:
            total_before = repo.get_user_total_deposits(tx['user_telegram_id'])
            old_vip_info = repo.get_vip_tier_info(total_before, tiers)
            new_vip_info = repo.get_vip_tier_info(total_before + deposit_amount, tiers)
            vip_deposit_pct = float(old_vip_info.get('current_bonus_pct') or 0)
            if vip_deposit_pct > 0:
                vip_deposit_bonus = int(deposit_amount * (vip_deposit_pct / 100.0))
            if new_vip_info.get('current_index', 0) > old_vip_info.get('current_index', 0):
                vip_upgrade = {
                    'upgraded': True,
                    'new_tier': new_vip_info['tier_names'][new_vip_info['current_index']],
                    'new_tier_index': new_vip_info['current_index'],
                    'reward': int(new_vip_info.get('current_reward') or 0),
                }
    except Exception as e:
        logger.error(f"Auto Syriatel VIP calc error: {e}")

    vip_upgrade_reward = int(vip_upgrade.get('reward') or 0) if vip_upgrade.get('upgraded') else 0
    bonus_amount = public_bonus_amount + vip_deposit_bonus + vip_upgrade_reward

    result = repo.approve_deposit_atomic(
        telegram_id=tx['user_telegram_id'],
        deposit_amount=deposit_amount,
        bonus_amount=bonus_amount,
        tx_id=tx_id,
        reviewed_by='auto_syriatel',
        new_vip_tier=int(vip_upgrade.get('new_tier_index')) if vip_upgrade.get('upgraded') else None
    )
    if not result.get('ok') or result.get('already_approved'):
        return {'ok': False, 'reason': result.get('reason') or 'approve_failed'}
    if external_ref:
        repo.set_transaction_external_ref(tx_id, external_ref)

    if vip_upgrade.get('upgraded'):
        try:
            DatabaseManager.execute_query(
                "UPDATE users SET vip_tier = %s WHERE telegram_id = %s",
                (int(vip_upgrade.get('new_tier_index') or 0), str(tx['user_telegram_id']))
            )
            await bot.send_message(
                chat_id=tx['user_telegram_id'],
                text=(
                    f"🎉 <b>مبروك! تمت ترقيتك إلى {vip_upgrade.get('new_tier')}!</b>\n\n"
                    f"💎 مكافأة الترقية: <code>{vip_upgrade_reward:,} ل.س</code>\n"
                    "(أُضيفت لرصيد المكافآت 🎁 للاستخدام في اللعبة)"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Auto Syriatel VIP notify/update failed: {e}")

    # الإحالات: تفعيل فقط، أرباحها أسبوعية من خسائر اللعبة
    if repo.are_referrals_enabled():
        try:
            newly_activated_referrer = repo.activate_referral_if_needed(tx['user_telegram_id'])
            if newly_activated_referrer:
                active_count = repo.get_active_referrals_count(newly_activated_referrer)
                with suppress(Exception):
                    await bot.send_message(
                        chat_id=newly_activated_referrer,
                        text=(
                            "🎉 <b>إحالة نشطة جديدة!</b>\n\n"
                            "قام أحد أصدقائك بإكمال أول إيداع مقبول.\n"
                            f"✅ إحالاتك النشطة الآن: <code>{active_count}</code>\n"
                            "💡 أرباح الإحالات تُحسب أسبوعياً من خسارة المحالين في اللعبة."
                        ),
                        parse_mode="HTML"
                    )
        except Exception as e:
            logger.warning(f"Auto Syriatel referral activation failed: {e}")

    bonus_lines = []
    if public_bonus_amount > 0 and bonus_rule:
        bonus_lines.append(f"🎁 بونص العرض: <code>{public_bonus_amount:,} SYP</code> — <code>{bonus_rule.get('title')}</code>")
    if vip_deposit_bonus > 0:
        bonus_lines.append(f"🏆 بونص VIP: <code>{vip_deposit_bonus:,} SYP</code> (<code>{vip_deposit_pct:g}%</code>)")
    if vip_upgrade_reward > 0:
        bonus_lines.append(f"🎉 مكافأة ترقية VIP: <code>{vip_upgrade_reward:,} SYP</code>")
    bonus_text = ("\n" + "\n".join(bonus_lines) + f"\n🎁 إجمالي البونص: <code>{bonus_amount:,} SYP</code>") if bonus_amount > 0 else ""

    user = repo.get_user(tx['user_telegram_id'])
    await bot.send_message(
        chat_id=tx['user_telegram_id'],
        text=(
            "✅ <b>تم التحقق من حوالة Syriatel Cash تلقائياً وقبول الإيداع!</b>\n\n"
            f"📌 رقم الطلب: <code>#{tx_id}</code>\n"
            f"🔢 رقم العملية: <code>{external_ref or '—'}</code>\n"
            f"💰 مبلغ الإيداع: {_fmt_syp_dual(deposit_amount)}"
            f"{bonus_text}\n"
            f"💎 رصيدك النقدي الآن: {_fmt_syp_dual(safe_balance(user))}"
        ),
        parse_mode="HTML"
    )
    auto_log_text = (
        "✅ <b>قبول إيداع Syriatel تلقائي</b>\n\n"
        f"📌 الطلب: <code>#{tx_id}</code>\n"
        f"👤 المستخدم: <code>{tx['user_telegram_id']}</code>\n"
        f"🔢 العملية: <code>{external_ref or '—'}</code>\n"
        f"💰 المبلغ: {_fmt_syp_dual(deposit_amount)}\n"
        f"🎁 البونص: <code>{bonus_amount:,} SYP</code>"
    )
    await send_log_message(bot, auto_log_text)
    await send_syriatel_auto_message(bot, auto_log_text)
    return {'ok': True, 'bonus_amount': bonus_amount, 'new_balance': safe_balance(user)}

# معالجات القوائم الرئيسية
# ================================================================

@router.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu_callback(callback: CallbackQuery, state: FSMContext):
    # ✅ مسح أي حالة جارية (إيداع/سحب/هدية) لمنع بقاء المستخدم عالقاً
    await state.clear()

    telegram_id = str(callback.from_user.id)
    user = repo.get_user(telegram_id)

    if not user:
        await safe_edit_text(
            callback.message,
            "👋 <b>مرحباً بك!</b>\n\n"
            "يبدو أنك لم تبدأ استخدام البوت بعد.\n"
            "الرجاء الضغط على /start للمتابعة.",
            parse_mode="HTML"
        )
        await safe_answer_callback(callback)
        return

    bot_balance = safe_balance(user)
    game_balance = repo.get_user_game_balance(telegram_id)
    affiliate_balance = int(user.get('affiliate_balance') or 0)
    affiliate_line = f"🤝 <b>أرباح الإحالات:</b><code>{affiliate_balance:,} ل.س</code>\n" if affiliate_balance > 0 else ""
    bot_balance_new = bot_balance / 100
    bot_balance_new_str = f"{int(bot_balance_new):,}" if bot_balance_new == int(bot_balance_new) else f"{bot_balance_new:,.2f}"
    balance_text = (
        f"💎 <b>رصيد البوت:</b><code>{bot_balance:,} ل.س</code> <i>({bot_balance_new_str} ل.س جديدة)</i>\n"
        f"{affiliate_line}"
        f"🎮 <b>رصيد اللعبة (iChancy):</b><code>{game_balance:,} NSP</code>\n\n"
        " <i>اختر الخدمة المطلوبة:</i>"
    )

    await safe_edit_text(
        callback.message,
        balance_text,
        reply_markup=get_user_menu_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data == "ichancy_menu")
async def ichancy_menu_callback(callback: CallbackQuery):
    telegram_id = str(callback.from_user.id)
    user = repo.get_user(telegram_id)

    if not user:
        await callback.message.edit_text("الرجاء استخدام /start أولاً.")
        await callback.answer()
        return

    if user.get('ichancy_username'):
        player_id = user.get('player_id')
        api_balance = await ichancy_api_client.get_player_balance(player_id)
        if api_balance is not None:
            display_balance = int(api_balance)
            repo.update_user_game_balance(telegram_id, display_balance)
        else:
            display_balance = repo.get_user_game_balance(telegram_id)

        status_text = (
            "⚡️ <b>حساب iChancy الخاص بك:</b>\n\n"
            f"👤 <b>اسم المستخدم:</b><code>{user['ichancy_username']}</code>\n"
            f"📧 <b>الإيميل:</b><code>{user['ichancy_email']}</code>\n"
            f"🔒 <b>كلمة المرور:</b><code>{user['ichancy_password']}</code>\n"
            f"🆔 <b>معرف اللاعب (Player ID):</b><code>{player_id}</code>\n\n"
            f"💰 <b>رصيد اللعبة الفعلي:</b><code>{display_balance:,} NSP</code>"
        )
        await callback.message.edit_text(status_text, reply_markup=get_ichancy_submenu(has_account=True), parse_mode="HTML")
    else:
        text = "⚠️ <b>ليس لديك حساب iChancy مرتبط بعد!</b>\n\nيمكنك إنشاء حساب جديد تلقائياً وربطه بالبوت بضغطة زر:"
        await callback.message.edit_text(text, reply_markup=get_ichancy_submenu(has_account=False), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "create_ichancy_account")
async def create_ichancy_account_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("👤 يرجى إدخال اسم المستخدم المطلق للحساب الجديد (بالأحرف والأرقام الإنجليزية فقط):")
    await state.set_state(BotStates.entering_ichancy_username)
    await callback.answer()


@router.message(BotStates.entering_ichancy_username)
async def process_ichancy_username(message: Message, state: FSMContext):
    username = message.text.strip().lower()
    if not username.isalnum():
        await message.answer("❌ يجب أن يكون اسم المستخدم حروفاً إنجليزية وأرقاماً فقط دون مسافات! يرجى إدخاله مجدداً:")
        return
    await state.update_data(ichancy_username=username)
    await message.answer("🔒 يرجى إدخال كلمة المرور المطلوبة لحسابك:")
    await state.set_state(BotStates.entering_ichancy_password)


@router.message(BotStates.entering_ichancy_password)
async def process_ichancy_password(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    username = data.get('ichancy_username')
    telegram_id = str(message.from_user.id)

    email = f"{username}@gmail.com"
    await message.answer("⏳ جاري تسجيل حسابك الفوري عبر iChancy API...")

    result = await ichancy_api_client.register_account(username, password, email)

    retry_count = 0
    while not result['success'] and retry_count < 5:
        retry_count += 1
        random_suffix = ''.join(random.choices(string.digits, k=5))
        username = f"{data.get('ichancy_username')}{random_suffix}"
        email = f"{username}@gmail.com"
        result = await ichancy_api_client.register_account(username, password, email)

    if result['success']:
        player_id = result.get('player_id')
        if not player_id:
            player_id = await ichancy_api_client.get_player_id(username)

        if player_id:
            repo.update_user_ichancy_details(telegram_id, username, password, email, player_id)
            success_text = (
                "✅ <b>تم إنشاء وربط الحساب بنجاح!</b>\n\n"
                f"👤 <b>اسم الدخول:</b><code>{username}</code>\n"
                f"🔒 <b>كلمة المرور:</b><code>{password}</code>\n"
                f"📧 <b>الإيميل المسجل:</b><code>{email}</code>\n"
                f"🆔 <b>معرف اللاعب:</b><code>{player_id}</code>"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡️ خيارات الحساب", callback_data="ichancy_menu")],
                [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="back_to_main_menu")]
            ])
            await message.answer(success_text, reply_markup=keyboard, parse_mode="HTML")
            await send_log_message(
                message.bot,
                "🆕 <b>تسجيل لاعب جديد</b>\n\n"
                f"👤 تيليغرام: @{message.from_user.username if message.from_user.username else message.from_user.first_name}\n"
                f"🆔 Telegram ID: <code>{telegram_id}</code>\n"
                f"⚡ iChancy Username: <code>{username}</code>\n"
                f"📧 Email: <code>{email}</code>\n"
                f"🎮 Player ID: <code>{player_id}</code>\n"
                f"💎 رصيد البوت: <code>{repo.get_user(telegram_id)['bot_balance']:,} SYP</code>\n"
                f"🎯 رصيد اللعبة: <code>{repo.get_user_game_balance(telegram_id):,} NSP</code>"
            )
        else:
            await message.answer(
                "⚠️ <b>تم إنشاء الحساب بنجاح لكن ربط معرف اللاعب تأخر قليلاً.</b>\n\n"
                f"👤 <b>اسم الدخول:</b><code>{username}</code>\n"
                f"🔒 <b>كلمة المرور:</b><code>{password}</code>\n"
                f"📧 <b>الإيميل:</b><code>{email}</code>\n\n"
                "يرجى المحاولة بعد لحظات من خلال قسم ⚡️ حساب iChancy أو التواصل مع الإدارة لإعادة مزامنة المعرف.",
                reply_markup=get_user_menu_keyboard(message.from_user.id),
                parse_mode="HTML"
            )
            await send_log_message(
                message.bot,
                "⚠️ <b>تم إنشاء حساب جديد لكن Player ID لم يُجلب بعد</b>\n\n"
                f"👤 تيليغرام: @{message.from_user.username if message.from_user.username else message.from_user.first_name}\n"
                f"🆔 Telegram ID: <code>{telegram_id}</code>\n"
                f"⚡ iChancy Username: <code>{username}</code>\n"
                f"📧 Email: <code>{email}</code>"
            )
    else:
        error_msg = result.get('error', 'خطأ غير معروف')
        await message.answer(
            f"❌ فشل إنشاء الحساب:\n <b>{error_msg}</b>\n\nيرجى المحاولة مجدداً عبر القائمة الرئيسية.",
            reply_markup=get_user_menu_keyboard(message.from_user.id),
            parse_mode="HTML"
        )

    await state.clear()


# ================================================================
# الإيداع في البوت (مع التحسينات الجديدة)
# ================================================================

@router.callback_query(F.data == "deposit_bot")
async def deposit_bot_callback(callback: CallbackQuery, state: FSMContext):
    await start_deposit_flow(callback.message, callback.from_user.id, state, edit=True)
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("dep_curr_"), BotStates.selecting_deposit_currency)
async def process_deposit_currency(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[2]
    await state.update_data(deposit_currency=currency)

    if currency == 'syp':
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Syriatel Cash", callback_data="dep_gate_syriatel")],
            [InlineKeyboardButton(text="🟡 MTN Cash", callback_data="dep_gate_mtn")],
            [InlineKeyboardButton(text="📱 Sham Cash (SYP)", callback_data="dep_gate_sham_syp")],
            [InlineKeyboardButton(text="🔙 عودة", callback_data="deposit_bot")]
        ])
        await callback.message.edit_text("🇸🇾 <b>يرجى اختيار وسيلة الإيداع بالليرة السورية:</b>", reply_markup=keyboard, parse_mode="HTML")
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Sham Cash (USD)", callback_data="dep_gate_sham_usd")],
            [InlineKeyboardButton(text="🪙 USDT - TRC20", callback_data="dep_gate_usdt_trc")],
            [InlineKeyboardButton(text="🪙 USDT - BEP20", callback_data="dep_gate_usdt_bep")],
            [InlineKeyboardButton(text="🔙 عودة", callback_data="deposit_bot")]
        ])
        await callback.message.edit_text("🇺🇸 <b>يرجى اختيار وسيلة الإيداع بالدولار الأمريكي:</b>", reply_markup=keyboard, parse_mode="HTML")

    await state.set_state(BotStates.selecting_deposit_gateway)
    await callback.answer()


@router.callback_query(F.data.startswith("dep_gate_"), BotStates.selecting_deposit_gateway)
async def process_deposit_gateway(callback: CallbackQuery, state: FSMContext):
    gateway = callback.data.replace("dep_gate_", "")
    payment_route = repo.get_payment_routing_context(gateway)
    payment_address = payment_route.get('address') or ''
    await state.update_data(
        deposit_gateway=gateway,
        deposit_payment_destination=payment_address,
        deposit_cashier_profile_id=payment_route.get('cashier_profile_id'),
        deposit_cashier_profile_name=payment_route.get('cashier_profile_name'),
        deposit_payment_source=payment_route.get('source'),
    )

    min_dep_syp = _get_min_deposit_syp()
    min_dep_syp_new = min_dep_syp / 100
    min_dep_syp_new_str = f"{int(min_dep_syp_new):,}" if min_dep_syp_new == int(min_dep_syp_new) else f"{min_dep_syp_new:,.2f}"
    instructions = {
        'syriatel': f"🟢 <b>إيداع سيريتل كاش:</b>\n\nيرجى تحويل الرصيد إلى أحد الأرقام التالية:\n📱 <code>{payment_address}</code>\n\n⚠️ الحد الأدنى: {min_dep_syp:,} ل.س <i>({min_dep_syp_new_str} جديدة)</i>",
        'mtn': f"🟡 <b>إيداع MTN كاش:</b>\n\nيرجى تحويل الرصيد إلى الرقم التالي:\n📱 <code>{payment_address}</code>\n\n⚠️ الحد الأدنى: {min_dep_syp:,} ل.س <i>({min_dep_syp_new_str} جديدة)</i>",
        'sham_syp': f"📱 <b>إيداع شام كاش (ليرة):</b>\n\nيرجى تحويل الرصيد إلى:\n🆔 <code>{payment_address}</code>\n\n⚠️ الحد الأدنى: {min_dep_syp:,} ل.س <i>({min_dep_syp_new_str} جديدة)</i>",
        'sham_usd': f"📱 <b>إيداع شام كاش (دولار):</b>\n\nيرجى تحويل الرصيد إلى:\n🆔 <code>{payment_address}</code>\n\n⚠️ الحد الأدنى: {_get_min_deposit_usd()} دولار",
        'usdt_trc': f"🪙 <b>إيداع USDT (TRC-20):</b>\n\nيرجى إرسال الـ USDT إلى:\n🔑 <code>{payment_address}</code>\n\n⚠️ الحد الأدنى: {_get_min_deposit_usd()} USDT",
        'usdt_bep': f"🪙 <b>إيداع USDT (BEP-20):</b>\n\nيرجى إرسال الـ USDT إلى:\n🔑 <code>{payment_address}</code>\n\n⚠️ الحد الأدنى: {_get_min_deposit_usd()} USDT"
    }.get(gateway, "الرجاء التحويل إلى حسابات الإدارة.")

    await callback.message.edit_text(f"{instructions}\n\n<b>الآن يرجى إدخال قيمة المبلغ الذي قمت بتحويله 👇:</b>", parse_mode="HTML")
    await state.set_state(BotStates.entering_deposit_amount)
    await callback.answer()


# 🆕 استبدال معالج المبلغ القديم بالنسخة المُحسَّنة
@router.message(BotStates.entering_deposit_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    """معالج مبلغ الإيداع مع الحسابات الصحيحة وعرض بيانات المستخدم الكاملة"""
    user_id = message.from_user.id
    user_data = await state.get_data()

    try:
        amount_text = message.text.strip().replace(',', '')
        try:
            amount = Decimal(amount_text)
        except (ValueError, InvalidOperation):
            await message.reply("❌ يجب إدخال رقم صحيح\n\nمثال: 100 أو 100.50")
            return

        currency = user_data.get('deposit_currency')  # 'syp' أو 'usd'
        gateway = user_data.get('deposit_gateway')

        # التحقق من الحدود حسب العملة
        if currency == 'usd':
            if amount < _get_min_deposit_usd():
                await message.reply(f"❌ الحد الأدنى للإيداع بالدولار: ${_get_min_deposit_usd()}")
                return
            if amount > 5000:
                await message.reply("❌ الحد الأقصى للإيداع بالدولار: $5000")
                return

            # حساب الصرف: ضرب × سعر الشراء المخزّن في قاعدة البيانات
            # مهم: هذا السعر قابل للتعديل من لوحة الأدمن، لذلك لا نعتمد على .env هنا.
            bot_settings = repo.get_bot_settings()
            exchange_rate_buy = Decimal(str(bot_settings['usd_buy_rate']))
            if exchange_rate_buy <= 0:
                await message.reply("❌ خطأ في إعدادات سعر شراء الدولار. يرجى التواصل مع الدعم.")
                await state.clear()
                return

            amount_in_syp = amount * exchange_rate_buy

            logger.info(f"💵 USD Deposit: ${amount} × DB usd_buy_rate={exchange_rate_buy} = {amount_in_syp} SYP")
        else:  # syp
            if amount < _get_min_deposit_syp():
                await message.reply(f"❌ الحد الأدنى للإيداع بالليرة: {_get_min_deposit_syp():,} ل.س")
                return
            if amount > MAX_WITHDRAW_SYP:  # استخدمنا نفس حد السحب كحد أقصى
                await message.reply(f"❌ الحد الأقصى للإيداع: {MAX_WITHDRAW_SYP:,} ل.س")
                return

            amount_in_syp = amount
            logger.info(f"💷 SYP Deposit: {amount} SYP")

        # جلب بيانات المستخدم الكاملة
        user = repo.get_user(user_id)
        if not user:
            await message.reply("❌ خطأ: لم يتم العثور على بيانات المستخدم")
            return

        # حفظ البيانات في الـ state
        await state.update_data({
            'deposit_amount': float(amount),
            'deposit_amount_in_syp': float(amount_in_syp),
            'deposit_currency': currency,
            'deposit_gateway': gateway,
        })

        # بناء رسالة التأكيد مع البيانات الكاملة
        username = user.get('telegram_username') or 'بدون اسم'
        player_id = user.get('player_id') or 'لم يتم الربط'
        current_balance = Decimal(str(user.get('bot_balance', 0)))

        confirmation_text = (
            f"<b>📋 تفاصيل الإيداع النهائية</b>\n\n"
            f"<b>📊 بيانات حسابك:</b>\n"
            f"👤 اسم المستخدم: <code>{username}</code>\n"
            f"🆔 معرف Telegram: <code>{user_id}</code>\n"
            f"🎮 معرف اللاعب (Player ID): <code>{player_id}</code>\n"
            f"💰 الرصيد الحالي: {current_balance:,} SYP\n\n"
            f"<b>💸 تفاصيل العملية:</b>\n"
            f"💱 العملة: {currency.upper()}\n"
            f"🏦 طريقة الدفع: {gateway}\n"
            f"📌 المبلغ: {amount} {currency.upper()}\n"
        )
        if currency == 'usd':
            confirmation_text += (
                f"📊 سعر الصرف: 1 = {exchange_rate_buy} SYP\n"
                f"✅ المبلغ بـ SYP: {amount_in_syp:,} SYP\n\n"
            )
        else:
            confirmation_text += "\n"

        confirmation_text += (
            f"<b>ملاحظات:</b>\n"
            f"• سيتم إضافة {amount_in_syp:,} SYP لرصيدك\n"
            f"• الرصيد الجديد: {current_balance + amount_in_syp:,} SYP\n"
            f"• يرجى الحفظ الدقيق لبيانات التحويل\n\n"
            f"📤 <b>الآن أرسل إثبات التحويل:</b> صورة الإيصال أو رقم العملية 👇"
        )

        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء العملية والعودة للقائمة", callback_data="back_to_main_menu")]
        ])
        await message.reply(confirmation_text, reply_markup=confirm_keyboard, parse_mode="HTML")

        # إرسال السجل
        log_text = (
            f"<b>📥 محاولة إيداع جديدة</b>\n\n"
            f"<b>👤 بيانات المستخدم:</b>\n"
            f"🆔 Telegram ID: <code>{user_id}</code>\n"
            f"📝 اسم المستخدم: {username}\n"
            f"🎮 حساب اللعبة (Player ID): {player_id}\n\n"
            f"<b>💰 تفاصيل الإيداع:</b>\n"
            f"💱 العملة: {currency.upper()}\n"
            f"🏦 البوابة: {gateway}\n"
            f"📊 المبلغ: {amount} {currency.upper()}\n"
            f"✅ المعادل بـ SYP: {amount_in_syp:,} SYP\n"
            f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🟡 الحالة: في انتظار التأكيد"
        )
        await send_log_message(message.bot, log_text)

        await state.set_state(BotStates.entering_deposit_proof)

    except Exception as e:
        logger.error(f"❌ Error in process_deposit_amount: {e}")
        await message.reply("❌ حدث خطأ في معالجة الطلب")


# معالج الإثبات (معدل لاستخدام المبلغ المحسوب مسبقاً)
@router.message(BotStates.entering_deposit_proof)
async def process_deposit_proof(message: Message, state: FSMContext):
    username = message.from_user.username or message.from_user.first_name

    data = await state.get_data()
    currency = data.get('deposit_currency')
    gateway = data.get('deposit_gateway')
    amount = data.get('deposit_amount')
    amount_to_save_syp = data.get('deposit_amount_in_syp')  # 🆕 استخدام القيمة المحسوبة مسبقاً

    # إذا لم تكن موجودة (للتوافق) نعيد حسابها بسعر شراء الدولار من قاعدة البيانات
    if amount_to_save_syp is None:
        if currency == 'usd':
            bot_settings = repo.get_bot_settings()
            usd_buy_rate = Decimal(str(bot_settings['usd_buy_rate']))
            amount_to_save_syp = Decimal(str(amount)) * usd_buy_rate
        else:
            amount_to_save_syp = amount

    photo_id = None
    transfer_number = "مكتوب داخل الإيصال"

    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.text:
        transfer_number = message.text.strip()

    short_tx_code = "CAESAR-D-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    await state.update_data(
        photo_id=photo_id,
        transfer_number=transfer_number,
        amount_to_save_syp=amount_to_save_syp,
        short_tx_code=short_tx_code,
        username=username
    )

    confirm_text = (
        "📝 <b>يرجى تأكيد تفاصيل طلب الإيداع الخاص بك:</b>\n\n"
        f"🔑 <b>رمز المعاملة:</b><code>{short_tx_code}</code>\n"
        f"💳 <b>بوابة الدفع:</b><code>{gateway.upper()}</code>\n"
        f"💰 <b>المبلغ المدخل:</b><code>{amount:,} {'ل.س' if currency == 'syp' else 'USD'}</code>\n"
        f"💱 <b>الرصيد المكافئ للتعبئة:</b>{_fmt_syp_dual(amount_to_save_syp)}\n"
        f"🔢 <b>الإثبات:</b> {transfer_number}\n\n"
        "💡 اضغط على زر التأكيد لإرسال طلبك للمراجعة 👇:"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 تأكيد وإرسال الطلب", callback_data="confirm_my_deposit")],
        [InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data="back_to_main_menu")]
    ])

    if photo_id:
        await message.answer_photo(photo=photo_id, caption=confirm_text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message.answer(confirm_text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "confirm_my_deposit")
async def confirm_my_deposit_callback(callback: CallbackQuery, state: FSMContext):
    if not _try_acquire_lock(callback.from_user.id):
        await safe_answer_callback(callback, "⏳ جاري إرسال طلبك، يرجى الانتظار...", show_alert=True)
        return
    try:
        allowed, reason = repo.service_gate_status('deposit')
        if not allowed:
            await callback.message.answer(f"🛡️ {reason}", reply_markup=get_user_menu_keyboard(callback.from_user.id))
            await state.clear()
            await safe_answer_callback(callback, reason, show_alert=True)
            return
        data = await state.get_data()
        if not data:
            await send_expired_flow_message(callback.message, callback.from_user.id)
            await safe_answer_callback(callback, "⚠️ انتهت صلاحية الطلب!", show_alert=True)
            return

        telegram_id = str(callback.from_user.id)
        if has_pending_transaction(telegram_id, 'deposit_bot'):
            await callback.message.answer(
                "⏳ لديك بالفعل طلب إيداع معلق. يرجى انتظار مراجعته قبل إرسال طلب جديد.",
                reply_markup=get_user_menu_keyboard(callback.from_user.id)
            )
            await state.clear()
            await safe_answer_callback(callback)
            return

        username = data.get('username')
        currency = data.get('deposit_currency')
        gateway = data.get('deposit_gateway')
        amount = data.get('deposit_amount')
        amount_to_save_syp = data.get('amount_to_save_syp')
        short_tx_code = data.get('short_tx_code')
        transfer_number = data.get('transfer_number')
        photo_id = data.get('photo_id')
        cashier_profile_id = data.get('deposit_cashier_profile_id')
        cashier_profile_name = data.get('deposit_cashier_profile_name')
        payment_destination = data.get('deposit_payment_destination')
        if not payment_destination:
            fallback_route = repo.get_payment_routing_context(gateway)
            payment_destination = fallback_route.get('address')
            cashier_profile_id = fallback_route.get('cashier_profile_id')
            cashier_profile_name = fallback_route.get('cashier_profile_name')

        if not all([currency, gateway, amount, amount_to_save_syp, short_tx_code]):
            await send_expired_flow_message(callback.message, callback.from_user.id)
            await state.clear()
            await safe_answer_callback(callback, "⚠️ بيانات الطلب غير مكتملة.", show_alert=True)
            return

        tx_id = repo.create_transaction(
            telegram_id=telegram_id,
            tx_type='deposit_bot',
            amount=amount_to_save_syp,
            payment_method=gateway,
            transfer_number=f"Code: {short_tx_code} | Info: {transfer_number}",
            status='pending',
            cashier_profile_id=cashier_profile_id,
            cashier_profile_name=cashier_profile_name,
            payment_destination=payment_destination,
        )

        # ✅ تحقق تلقائي لإيداعات Syriatel Cash عبر API عند توفر رقم العملية/رقم المرسل
        auto_verified = False
        auto_verify_note = ""
        syriatel_auto_mode, syriatel_auto_channel_id = get_syriatel_auto_config()
        if (
            gateway == 'syriatel'
            and syriatel_auto_mode != 'off'
            and getattr(settings, 'SYRIATEL_API_TOKEN', None)
            and getattr(settings, 'SYRIATEL_API_QUERY', None)
            and transfer_number
            and transfer_number != "مكتوب داخل الإيصال"
        ):
            try:
                tx_row = repo.get_transaction_by_id(tx_id)
                verify = await verify_incoming_deposit(
                    expected_amount=amount_to_save_syp,
                    user_reference=transfer_number,
                    created_at=tx_row.get('created_at') if tx_row else None,
                )
                if verify.get('ok'):
                    external_ref = verify.get('external_ref')
                    if repo.is_external_ref_used(external_ref, exclude_tx_id=tx_id):
                        auto_verify_note = "⚠️ رقم العملية موجود سابقاً، تم تحويل الطلب للمراجعة اليدوية."
                    elif syriatel_auto_mode == 'auto_approve':
                        approved = await auto_approve_syriatel_deposit(callback.bot, tx_id, external_ref=external_ref)
                        if approved.get('ok'):
                            auto_verified = True
                        else:
                            auto_verify_note = f"⚠️ تحقق API نجح لكن تعذر القبول التلقائي ({approved.get('reason')}). تم تحويل الطلب للمراجعة اليدوية."
                    else:
                        repo.set_transaction_external_ref(tx_id, external_ref)
                        auto_verify_note = f"✅ تم التحقق من الحوالة عبر API. رقم العملية: {external_ref}. بانتظار قبول المشرف."
                else:
                    auto_verify_note = f"ℹ️ لم يتم التحقق تلقائياً ({verify.get('reason')}). تم إرسال الطلب للمراجعة اليدوية."
            except Exception as e:
                logger.error(f"Syriatel auto verify flow failed: {e}", exc_info=True)
                auto_verify_note = "ℹ️ تعذر التحقق التلقائي مؤقتاً، تم إرسال الطلب للمراجعة اليدوية."

        if auto_verify_note:
            await send_syriatel_auto_message(
                callback.bot,
                f"🟡 <b>Syriatel Auto</b>\n\n📌 الطلب: <code>#{tx_id}</code>\n👤 المستخدم: <code>{telegram_id}</code>\n💰 المبلغ: {_fmt_syp_dual(amount_to_save_syp)}\n🔎 النتيجة: {auto_verify_note}"
            )

        if auto_verified:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.message.answer(
                "✅ <b>تم إرسال طلب الإيداع والتحقق منه تلقائياً عبر Syriatel Cash.</b>\n\n"
                f"📌 رقم الطلب: <code>#{tx_id}</code>\n"
                "تم قبول الإيداع وإضافة الرصيد إلى حسابك.",
                reply_markup=get_user_menu_keyboard(callback.from_user.id),
                parse_mode="HTML"
            )
            await state.clear()
            await safe_answer_callback(callback, "تم التحقق والقبول تلقائياً ✅")
            return

        success_text = (
            "✅ <b>تم إرسال طلب الشحن بنجاح للمراجعة!</b>\n\n"
            f"🔑 <b>رمز التحقق:</b><code>{short_tx_code}</code>\n"
            f"📌 <b>رقم الطلب:</b><code>#{tx_id}</code>\n"
            f"💰 <b>المبلغ المطلوب تعبئته:</b><code>{amount:,} {'ل.س' if currency == 'syp' else 'USD'}</code>\n"
            f"💱 <b>الرصيد المكافئ:</b>{_fmt_syp_dual(amount_to_save_syp)}\n\n"
            f"{('\n' + auto_verify_note + '\n') if auto_verify_note else ''}"
            "سيتم تدقيق الطلب والموافقة عليه خلال دقائق وإشعارك تلقائياً!"
        )

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.warning(f"edit_reply_markup ignored: {e}")

        await callback.message.answer(success_text, reply_markup=get_user_menu_keyboard(callback.from_user.id), parse_mode="HTML")

        admin_text = format_deposit_admin_message(
            tx_id=tx_id,
            telegram_id=telegram_id,
            username=username,
            amount=amount,
            currency='SYP' if currency == 'syp' else 'USD',
            gateway=gateway,
            transfer_number=transfer_number,
            amount_syp=amount_to_save_syp,
            user_balance=repo.get_user(telegram_id)['bot_balance'],
            player_id=repo.get_user(telegram_id).get('player_id'),
            ichancy_username=repo.get_user(telegram_id).get('ichancy_username')
        )
        if cashier_profile_name:
            admin_text += f"\n👤 <b>مشرف الاستلام:</b> <code>{cashier_profile_name}</code>"
        if payment_destination:
            admin_text += f"\n🏦 <b>عنوان الاستلام المثبت:</b> <code>{payment_destination}</code>"
        if auto_verify_note:
            admin_text += f"\n\n{auto_verify_note}"

        # 🆕 (Update 5) زر نسخ ذكي للإيداع — أرقام فقط، حسب نوع العملة
        if currency == 'usd':
            # إيداع بالدولار → نسخ المبلغ بالدولار (ما حوّله المستخدم فعلياً)
            dep_usd = float(amount)
            copy_button_label = f"📋 نسخ {dep_usd:,.2f} USD"
            copy_dep_data = f"copy_usd_{dep_usd:.2f}"
        else:
            # إيداع بالليرة → نسخ المبلغ بالليرة الجديدة
            dep_old = int(float(amount_to_save_syp))
            dep_new = dep_old / 100
            dep_new_str = f"{int(dep_new):,}" if dep_new == int(dep_new) else f"{dep_new:,.2f}"
            copy_button_label = f"📋 نسخ {dep_new_str} ل.س جديدة"
            copy_dep_data = f"copy_amt_{dep_old}"
        admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ قبول", callback_data=f"approve_dep_{tx_id}"),
                InlineKeyboardButton(text="❌ رفض", callback_data=f"reject_dep_{tx_id}")
            ],
            [InlineKeyboardButton(text="👤 تفاصيل المستخدم", callback_data=f"user_details_{telegram_id}")],
            [InlineKeyboardButton(text=copy_button_label, callback_data=copy_dep_data)]
        ])

        # طلبات سيرياتيل يمكن إرسالها لقناة خاصة إن تم ضبطها من لوحة المشرف/البيئة
        _, _syriatel_channel = get_syriatel_auto_config()
        target_chat = _syriatel_channel if (gateway == 'syriatel' and _syriatel_channel) else (settings.DEPOSIT_CHANNEL_ID if settings.DEPOSIT_CHANNEL_ID else None)
        try:
            if target_chat:
                if photo_id:
                    await callback.bot.send_photo(chat_id=target_chat, photo=photo_id, caption=admin_text, reply_markup=admin_keyboard, parse_mode="HTML")
                else:
                    await callback.bot.send_message(chat_id=target_chat, text=admin_text, reply_markup=admin_keyboard, parse_mode="HTML")
            else:
                for admin_id in get_admin_target_chat_ids():
                    try:
                        if photo_id:
                            await callback.bot.send_photo(chat_id=admin_id, photo=photo_id, caption=admin_text, reply_markup=admin_keyboard, parse_mode="HTML")
                        else:
                            await callback.bot.send_message(chat_id=admin_id, text=admin_text, reply_markup=admin_keyboard, parse_mode="HTML")
                    except Exception as inner_e:
                        logger.warning(f"Failed to notify admin {admin_id}: {inner_e}")
        except Exception as e:
            logger.error(f"Failed to post to deposit channel/admin: {e}")

        await send_log_message(
        callback.bot,
        "📥 <b>طلب إيداع جديد</b>\n\n"
        f"👤 المستخدم: @{callback.from_user.username if callback.from_user.username else callback.from_user.first_name}\n"
        f"🆔 الآيدي: <code>{telegram_id}</code>\n"
        f"📌 رقم الطلب: <code>#{tx_id}</code>\n"
        f"💰 المبلغ الأصلي: <code>{amount}</code> <b>{'SYP' if currency == 'syp' else 'USD'}</b>\n"
        f"💱 المكافئ: <code>{amount_to_save_syp:,} SYP</code>\n"
        f"💳 الوسيلة: <code>{gateway}</code>"
        )
        await state.clear()
        await safe_answer_callback(callback, "تم إرسال الطلب للمراجعة!")


    finally:
        _release_lock(callback.from_user.id)

# ================================================================
# السحب من البوت (بدون تغيير)
# ================================================================

@router.callback_query(F.data == "withdraw_bot")
async def withdraw_bot_callback(callback: CallbackQuery, state: FSMContext):
    await start_withdraw_flow(callback.message, callback.from_user.id, state, edit=True)
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("wit_curr_"), BotStates.selecting_withdraw_currency)
async def process_withdraw_currency(callback: CallbackQuery, state: FSMContext):
    currency = callback.data.split("_")[2]
    await state.update_data(withdraw_currency=currency)

    if currency == 'syp':
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Syriatel Cash", callback_data="wit_gate_syriatel")],
            [InlineKeyboardButton(text="🟡 MTN Cash", callback_data="wit_gate_mtn")],
            [InlineKeyboardButton(text="📱 Sham Cash (SYP)", callback_data="wit_gate_sham_syp")],
            [InlineKeyboardButton(text="🔙 القائمة", callback_data="withdraw_bot")]
        ])
        await callback.message.edit_text("🇸🇾 <b>اختر وسيلة سحب الليرة السورية:</b>", reply_markup=keyboard, parse_mode="HTML")
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Sham Cash (USD)", callback_data="wit_gate_sham_usd")],
            [InlineKeyboardButton(text="🪙 USDT - TRC20", callback_data="wit_gate_usdt_trc")],
            [InlineKeyboardButton(text="🪙 USDT - BEP20", callback_data="wit_gate_usdt_bep")],
            [InlineKeyboardButton(text="🔙 القائمة", callback_data="withdraw_bot")]
        ])
        await callback.message.edit_text("🇺🇸 <b>اختر وسيلة سحب الدولار الأمريكي:</b>", reply_markup=keyboard, parse_mode="HTML")

    await state.set_state(BotStates.selecting_withdraw_gateway)
    await callback.answer()


@router.callback_query(F.data.startswith("wit_gate_"), BotStates.selecting_withdraw_gateway)
async def process_withdraw_gateway(callback: CallbackQuery, state: FSMContext):
    gateway = callback.data.replace("wit_gate_", "")
    await state.update_data(withdraw_gateway=gateway)

    prompt = {
        'syriatel': "🟢 <b>سحب سيريتل كاش:</b>\n\nيرجى إدخال رقم الهاتف المستلم:",
        'mtn': "🟡 <b>سحب MTN كاش:</b>\n\nيرجى إدخال رقم الهاتف المستلم:",
        'sham_syp': "📱 <b>سحب شام كاش (ليرة):</b>\n\nيرجى إدخال رقم حساب شام كاش المستلم:",
        'sham_usd': "📱 <b>سحب شام كاش (دولار):</b>\n\nيرجى إدخال رقم حساب شام كاش بالدولار المستلم:",
        'usdt_trc': "🪙 <b>سحب USDT (TRC-20):</b>\n\nيرجى إدخال عنوان المحفظة بدقة:",
        'usdt_bep': "🪙 <b>سحب USDT (BEP-20):</b>\n\nيرجى إدخال عنوان المحفظة بدقة:"
    }.get(gateway, "الرجاء إدخال تفاصيل المستلم:")

    await callback.message.edit_text(prompt, parse_mode="HTML")
    await state.set_state(BotStates.entering_withdraw_recipient)
    await callback.answer()


@router.message(BotStates.entering_withdraw_recipient)
async def process_withdraw_recipient(message: Message, state: FSMContext):
    recipient = message.text.strip()
    data = await state.get_data()
    gateway = data.get('withdraw_gateway')

    is_valid, error_message = validate_recipient_by_gateway(gateway, recipient)
    if not is_valid:
        await message.answer(error_message)
        return

    await state.update_data(withdraw_recipient=recipient)

    currency = data.get('withdraw_currency')
    if currency == 'usd':
        prompt = "💵 <b>أدخل المبلغ الذي تريد سحبه بالليرة السورية (SYP) من رصيدك:</b>"
    else:
        prompt = "💵 <b>أدخل المبلغ الذي تريد سحبه بالليرة السورية:</b>"

    await message.answer(prompt, parse_mode="HTML")
    await state.set_state(BotStates.entering_withdraw_amount)


@router.message(BotStates.entering_withdraw_amount)
async def process_withdraw_amount(message: Message, state: FSMContext):
    amount_text = message.text.strip().replace(',', '')
    try:
        entered_syp = float(amount_text)
        if entered_syp <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ الرجاء إدخال مبلغ رقمي صحيح أكبر من 0:")
        return

    if entered_syp < _get_min_withdraw_syp():
        await message.answer(f"❌ الحد الأدنى للسحب هو {_get_min_withdraw_syp():,} SYP.")
        return
    if entered_syp > MAX_WITHDRAW_SYP:
        await message.answer(f"❌ الحد الأعلى للسحب هو {MAX_WITHDRAW_SYP:,} SYP.")
        return

    telegram_id = str(message.from_user.id)
    user = repo.get_user(telegram_id)
    data = await state.get_data()
    currency = data.get('withdraw_currency')
    gateway = data.get('withdraw_gateway')
    recipient = data.get('withdraw_recipient')

    bot_settings = repo.get_bot_settings()
    usd_sell_rate = float(bot_settings['usd_sell_rate'])
    withdraw_commission = float(bot_settings['withdraw_commission'])

    if entered_syp > safe_balance(user):
        await message.answer(f"❌ رصيدك غير كافٍ! رصيدك الحالي: <code>{safe_balance(user):,} SYP</code>. أعد إدخال المبلغ:", parse_mode="HTML")
        return

    if currency == 'usd':
        if usd_sell_rate == 0:
            await message.answer("❌ خطأ في إعدادات سعر بيع الدولار. يرجى التواصل مع الدعم.")
            await state.clear()
            return
        usd_value = entered_syp / usd_sell_rate
        commission_value = usd_value * (withdraw_commission / 100.0)
        net_value = usd_value - commission_value
        rate_display = f"{usd_sell_rate:,} SYP"
        currency_label = "USD"
        commission_label = f"{commission_value:,.2f} USD"
        net_label = f"{net_value:,.2f} USD"
        gross_label = f"{usd_value:,.2f} USD"

        # 🆕 تحقق من أدنى سحب بالدولار
        min_usd = _get_min_withdraw_usd()
        if net_value < min_usd:
            await message.answer(
                f"❌ الحد الأدنى للسحب بالدولار هو ${min_usd}.\n\n"
                f"صافي المبلغ بعد العمولة: ${net_value:,.2f}\n"
                f"يرجى زيادة مبلغ السحب."
            )
            return
    else:
        commission_value = entered_syp * (withdraw_commission / 100.0)
        net_value = entered_syp - commission_value
        rate_display = "—"
        currency_label = "SYP"
        commission_label = f"{commission_value:,.0f} SYP"
        net_label = f"{net_value:,.0f} SYP"
        gross_label = f"{entered_syp:,.0f} SYP"

    await state.update_data(
        entered_syp=entered_syp,
        commission_value=commission_value,
        net_value=net_value,
        rate_display=rate_display,
        currency_label=currency_label,
        commission_label=commission_label,
        net_label=net_label,
        gross_label=gross_label
    )

    confirm_msg = (
        "📋 <b>تفاصيل طلب السحب:</b>\n\n"
        f"💱 <b>العملة:</b> {currency_label}\n"
        f"💳 <b>طريقة السحب:</b><code>{gateway.upper()}</code>\n"
        f"👤 <b>معلومات الاستلام:</b><code>{recipient}</code>\n"
        f"💰 <b>المبلغ المخصوم من رصيدك:</b>{_fmt_syp_dual(entered_syp)}\n"
        f"📈 <b>سعر الصرف المعتمد:</b><code>{rate_display}</code>\n"
        f"🧾 <b>القيمة قبل العمولة:</b><code>{gross_label}</code>\n"
        f"🏷️ <b>العمولة ({withdraw_commission}%):</b><code>{commission_label}</code>\n"
        f"🎁 <b>الصافي الذي سيصلك:</b><code>{net_label}</code>\n\n"
        "<b>هل تريد تأكيد طلب السحب؟</b>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد السحب", callback_data="confirm_my_withdraw")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_my_withdraw")]
    ])

    await message.answer(confirm_msg, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(BotStates.confirming_withdraw)


@router.callback_query(F.data == "confirm_my_withdraw", BotStates.confirming_withdraw)
async def confirm_withdraw_callback(callback: CallbackQuery, state: FSMContext):
    allowed, reason = repo.service_gate_status('withdraw')
    if not allowed:
        await callback.message.answer(f"🛡️ {reason}", reply_markup=get_user_menu_keyboard(callback.from_user.id))
        await state.clear()
        await safe_answer_callback(callback, reason, show_alert=True)
        return
    data = await state.get_data()
    if not data:
        await send_expired_flow_message(callback.message, callback.from_user.id)
        await safe_answer_callback(callback, "⚠️ انتهت صلاحية الطلب.", show_alert=True)
        return

    telegram_id = str(callback.from_user.id)
    user = repo.get_user(telegram_id)
    if not user:
        await send_expired_flow_message(callback.message, callback.from_user.id)
        await state.clear()
        await safe_answer_callback(callback)
        return

    entered_syp = data.get('entered_syp')
    gateway = data.get('withdraw_gateway')
    recipient = data.get('withdraw_recipient')
    gross_label = data.get('gross_label')
    commission_label = data.get('commission_label')
    net_label = data.get('net_label')

    if entered_syp is None or not gateway or not recipient:
        await send_expired_flow_message(callback.message, callback.from_user.id)
        await state.clear()
        await safe_answer_callback(callback, "⚠️ بيانات الطلب غير مكتملة.", show_alert=True)
        return

    short_tx_code = "CAESAR-W-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    # خصم الرصيد وإنشاء طلب السحب داخل عملية ذرية واحدة في قاعدة البيانات.
    # هذا يمنع الضغط المزدوج ويضمن عدم خصم الرصيد بدون إنشاء طلب.
    atomic_result = repo.create_withdraw_transaction_atomic(
        telegram_id=telegram_id,
        amount=entered_syp,
        payment_method=gateway,
        transfer_number=f"Code: {short_tx_code} | Recipient: {recipient}"
    )

    if not atomic_result.get('success'):
        reason = atomic_result.get('reason')
        if reason == 'pending':
            await callback.message.answer(
                "⏳ لديك بالفعل طلب سحب قيد المراجعة. لا يمكن إرسال طلب جديد الآن.",
                reply_markup=get_user_menu_keyboard(callback.from_user.id)
            )
        elif reason == 'insufficient':
            current_balance = int(atomic_result.get('old_balance', 0))
            await callback.message.answer(
                f"❌ لا يمكن تنفيذ الطلب لأن الرصيد أصبح غير كافٍ.\n"
                f"💎 رصيدك الحالي: <code>{current_balance:,} SYP</code>\n"
                "يرجى المحاولة مجدداً.",
                reply_markup=get_user_menu_keyboard(callback.from_user.id),
                parse_mode="HTML"
            )
        else:
            await callback.message.answer(
                "❌ حدث خطأ أثناء إنشاء طلب السحب. لم يتم خصم أي رصيد. يرجى المحاولة لاحقاً.",
                reply_markup=get_user_menu_keyboard(callback.from_user.id)
            )
        await state.clear()
        await safe_answer_callback(callback)
        return

    tx_id = atomic_result['tx_id']
    user_balance_before = atomic_result['old_balance']

    await safe_delete_message(callback.message)

    success_text = (
        "✅ <b>تم تقديم طلب السحب بنجاح!</b>\n\n"
        f"🔑 <b>رمز المعاملة:</b><code>{short_tx_code}</code>\n"
        f"📌 <b>رقم الطلب:</b><code>#{tx_id}</code>\n"
        f"💰 <b>المبلغ المخصوم:</b>{_fmt_syp_dual(entered_syp)}\n"
        f"🏷️ <b>العمولة:</b><code>{commission_label}</code>\n"
        f"🎁 <b>الصافي:</b><code>{net_label}</code>\n"
        f"📱 <b>المستلم:</b><code>{recipient}</code>\n\n"
        "⏳ سيتم مراجعة الطلب من قبل المشرفين."
    )
    await callback.message.answer(success_text, reply_markup=get_user_menu_keyboard(callback.from_user.id), parse_mode="HTML")

    admin_text = format_withdraw_admin_message(
        tx_id=tx_id,
        telegram_id=telegram_id,
        username=callback.from_user.username,
        entered_syp=entered_syp,
        gateway=gateway,
        recipient=recipient,
        gross_label=gross_label,
        commission_label=commission_label,
        net_label=net_label,
        user_balance_before=user_balance_before,
        player_id=user.get('player_id'),
        ichancy_username=user.get('ichancy_username')
    )
    copy_wd_label = _fmt_syp_copy_label(entered_syp)
    # 🆕 (Update 5) زر نسخ ذكي ينسخ الصافي بعد العمولة، حسب نوع الوسيلة
    net_value_num = data.get('net_value') or 0
    currency_label = data.get('currency_label') or 'SYP'
    if currency_label == 'USD':
        # سحب بالدولار → نسخ الصافي بالدولار
        net_usd = float(net_value_num)
        copy_wd_label = f"📋 نسخ {net_usd:,.2f} USD"
        copy_wd_data = f"copy_usd_{net_usd:.2f}"
    else:
        # سحب بالليرة → نسخ الصافي بالليرة الجديدة
        net_old = int(net_value_num)
        net_new = net_old / 100
        net_new_str = f"{int(net_new):,}" if net_new == int(net_new) else f"{net_new:,.2f}"
        copy_wd_label = f"📋 نسخ {net_new_str} ل.س جديدة"
        copy_wd_data = f"copy_amt_{net_old}"
    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ تم التحويل", callback_data=f"approve_withdraw_{tx_id}"),
            InlineKeyboardButton(text="❌ رفض", callback_data=f"reject_withdraw_{tx_id}")
        ],
        [InlineKeyboardButton(text="👤 تفاصيل المستخدم", callback_data=f"user_details_{telegram_id}")],
        [InlineKeyboardButton(text=copy_wd_label, callback_data=copy_wd_data)]
    ])

    target_chat = settings.WITHDRAWAL_CHANNEL_ID if settings.WITHDRAWAL_CHANNEL_ID else None
    try:
        if target_chat:
            await callback.bot.send_message(chat_id=target_chat, text=admin_text, reply_markup=admin_keyboard, parse_mode="HTML")
        else:
            await notify_admins(callback.bot, admin_text, reply_markup=admin_keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to post to withdrawal channel/admin: {e}")

    await send_log_message(
        callback.bot,
        "📤 <b>طلب سحب جديد</b>\n\n"
        f"👤 المستخدم: @{callback.from_user.username if callback.from_user.username else callback.from_user.first_name}\n"
        f"🆔 الآيدي: <code>{telegram_id}</code>\n"
        f"📌 رقم الطلب: <code>#{tx_id}</code>\n"
        f"💰 المبلغ المخصوم: {_fmt_syp_dual(entered_syp)}\n"
        f"🎁 الصافي: <code>{net_label}</code>\n"
        f"💳 الوسيلة: <code>{gateway}</code>"
    )
    await state.clear()
    await safe_answer_callback(callback, "تم إرسال الطلب.")

# 🆕 معالج زر نسخ المبلغ (للمشرف)
@router.callback_query(F.data.startswith("copy_amt_"))
async def copy_amount_callback(callback: CallbackQuery):
    """زر نسخ الصافي بالليرة الجديدة — أرقام فقط للّصق السهل في المحفظة."""
    try:
        amount_old = int(float(callback.data.split("copy_amt_")[1]))
        # تحويل الليرة القديمة إلى جديدة (100:1)
        new_val = amount_old / 100
        copy_val = str(int(new_val)) if new_val == int(new_val) else f"{new_val:.2f}"
        await callback.answer(copy_val, show_alert=True)
    except Exception as e:
        logger.warning(f"copy_amount_callback failed: {e}")
        await callback.answer("⚠️ تعذر النسخ", show_alert=True)


@router.callback_query(F.data.startswith("copy_usd_"))
async def copy_usd_callback(callback: CallbackQuery):
    """زر نسخ الصافي بالدولار — أرقام فقط للّصق السهل."""
    try:
        copy_val = callback.data.split("copy_usd_")[1]
        await callback.answer(copy_val, show_alert=True)
    except Exception as e:
        logger.warning(f"copy_usd_callback failed: {e}")
        await callback.answer("⚠️ تعذر النسخ", show_alert=True)

@router.callback_query(F.data == "cancel_my_withdraw", BotStates.confirming_withdraw)
async def cancel_withdraw_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🚫 <b>تم إلغاء طلب السحب.</b>", reply_markup=get_user_menu_keyboard(callback.from_user.id), parse_mode="HTML")
    await callback.answer("تم الإلغاء.")
    await state.clear()


# ================================================================
# باقي المعالجات (الإدارة، الرسائل، الأقسام القادمة)
# ================================================================

@router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await safe_answer_callback(callback, "❌ هذه اللوحة للمشرفين فقط.", show_alert=True)
        return
    try:
        from telegram_bot.handlers.admin import caesar_control_panel
        await caesar_control_panel(callback)
    except Exception as e:
        logger.error(f"admin_panel_callback failed: {e}")
        await safe_edit_text(
            callback.message,
            "⚠️ تعذر فتح لوحة الأدمن الآن. تأكد من إعدادات المشرفين ثم حاول مجدداً.",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback)


@router.callback_query(F.data == "message_admin")
async def message_admin_callback(callback: CallbackQuery, state: FSMContext):
    await safe_edit_text(
        callback.message,
        "✅ <b>تم فتح محادثة مع الإدارة.</b>\n\n"
        "📝 يمكنك الآن إرسال رسائلك، صورك، أو ملفاتك بحرية.\n"
        "سيقوم فريق الدعم بالرد عليك في أقرب وقت.\n\n"
        "🔴 لإنهاء المحادثة اضغط الزر بالأسفل.",
        reply_markup=get_support_chat_keyboard(),
        parse_mode="HTML"
    )
    try:
        repo.get_or_create_support_ticket(callback.from_user.id)
    except Exception as e:
        logger.warning(f"Could not create support ticket on open: {e}")
    await state.set_state(BotStates.support_chat_active)
    await safe_answer_callback(callback, "تم فتح محادثة الدعم.")


@router.callback_query(F.data == "end_support_chat")
async def end_support_chat_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        repo.close_open_support_ticket_for_user(callback.from_user.id)
    except Exception as e:
        logger.warning(f"Could not close support ticket: {e}")
    await safe_edit_text(
        callback.message,
        "✅ <b>تم إنهاء محادثة الدعم.</b>\n\nيمكنك العودة للقائمة الرئيسية أو فتح محادثة جديدة في أي وقت.",
        reply_markup=get_user_menu_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback, "تم إنهاء المحادثة.")


@router.message(BotStates.support_chat_active)
async def process_support_chat_message(message: Message, state: FSMContext):
    try:
        ticket = repo.get_or_create_support_ticket(message.from_user.id)
        msg_text = message.text or message.caption or f"[{getattr(message, 'content_type', 'message')}]"
        if ticket:
            repo.add_support_message(
                ticket_id=ticket['id'],
                sender_type='user',
                sender_id=message.from_user.id,
                message_text=msg_text,
                content_type=str(getattr(message, 'content_type', 'message')),
                telegram_message_id=message.message_id
            )
    except Exception as e:
        logger.warning(f"Could not save support message: {e}")
    sent = await forward_support_message_to_admins(message)
    if sent:
        await message.answer(
            "✅ تم إرسال رسالتك إلى الإدارة.\n\nيمكنك إرسال رسالة أخرى أو إنهاء المحادثة من الزر بالأسفل.",
            reply_markup=get_support_chat_keyboard()
        )
    else:
        await message.answer(
            "⚠️ تعذر إرسال رسالتك الآن. يرجى المحاولة لاحقاً أو فتح الدعم الرسمي.",
            reply_markup=get_support_chat_keyboard()
        )


@router.message(BotStates.entering_admin_message)
async def process_admin_message(message: Message, state: FSMContext):
    # توافق خلفي مع الحالة القديمة: أي رسالة هنا تُعامل كرسالة دعم مباشر.
    await state.set_state(BotStates.support_chat_active)
    await process_support_chat_message(message, state)


@router.callback_query(F.data.startswith("reply_user_"))
async def reply_user_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user.id):
        await safe_answer_callback(callback, "❌ هذا الزر للمشرفين فقط.", show_alert=True)
        return

    target_user_id = callback.data.replace("reply_user_", "", 1).strip()
    if not target_user_id.isdigit():
        await safe_answer_callback(callback, "⚠️ معرف المستخدم غير صالح.", show_alert=True)
        return

    await state.update_data(reply_target_user_id=target_user_id)
    await state.set_state(BotStates.replying_to_user)
    await callback.message.answer(
        f"↩️ <b>رد على المستخدم</b>\n\n"
        f"🆔 Telegram ID: <code>{target_user_id}</code>\n\n"
        "اكتب الرد الآن، ويمكنك إرسال نص، صورة، أو ملف.",
        parse_mode="HTML"
    )
    await safe_answer_callback(callback, "اكتب ردك الآن.")


@router.message(BotStates.replying_to_user)
async def process_admin_reply_to_user(message: Message, state: FSMContext):
    if not is_admin_user(message.from_user.id):
        await state.clear()
        await message.answer("❌ غير مسموح لك باستخدام الرد الإداري.")
        return

    data = await state.get_data()
    target_user_id = data.get('reply_target_user_id')
    if not target_user_id:
        await state.clear()
        await message.answer("⚠️ انتهت صلاحية الرد. يرجى الضغط على زر الرد من جديد.")
        return

    try:
        await message.bot.send_message(
            chat_id=target_user_id,
            text="📩 <b>رد من الإدارة</b>",
            parse_mode="HTML"
        )
        await message.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        try:
            ticket = repo.get_or_create_support_ticket(target_user_id)
            msg_text = message.text or message.caption or f"[{getattr(message, 'content_type', 'message')}]"
            if ticket:
                repo.add_support_message(
                    ticket_id=ticket['id'],
                    sender_type='admin',
                    sender_id=message.from_user.id,
                    message_text=msg_text,
                    content_type=str(getattr(message, 'content_type', 'message')),
                    telegram_message_id=message.message_id
                )
        except Exception as e:
            logger.warning(f"Could not save admin support reply: {e}")
        await message.answer("✅ تم إرسال الرد للمستخدم بنجاح.")
    except Exception as e:
        logger.warning(f"process_admin_reply_to_user failed: {e}")
        await message.answer("⚠️ تعذر إرسال الرد للمستخدم. قد يكون المستخدم حظر البوت أو لم يبدأ المحادثة.")
    finally:
        await state.clear()


@router.callback_query(F.data == "contact_us")
async def contact_us_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗳️ رسالة للإدارة", callback_data="message_admin")],
        [InlineKeyboardButton(text="📞 فتح الدعم", url=settings.SUPPORT_LINK)],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_main_menu")]
    ])
    await safe_edit_text(
        callback.message,
        "✉️ <b>تواصل معنا</b>\n\n"
        "نحن هنا لمساعدتك 👑\n"
        "اختر الطريقة المناسبة:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data == "guides_menu")
async def guides_menu_callback(callback: CallbackQuery):
    base = getattr(settings, 'RENDER_EXTERNAL_URL', 'https://ichancy100.onrender.com')
    url = f"{base}/guides.html"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💭 فتح الشروحات", web_app=WebAppInfo(url=url))],
        [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="back_to_main_menu")]
    ])
    await safe_edit_text(
        callback.message,
        "💭 <b>الشروحات — Caesar 👑</b>\n\nاختر ما تريد الاطلاع عليه من خلال فتح دليل الشروحات أدناه 👇",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data == "contests_menu")
async def contests_menu_callback(callback: CallbackQuery):
    contests = repo.get_open_contests(limit=10)
    if not contests:
        await safe_edit_text(
            callback.message,
            "👑 <b>مسابقات القيصر</b>\n\nلا توجد مسابقات مفتوحة حالياً. تابعنا قريباً ✨",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback)
        return
    await safe_edit_text(
        callback.message,
        "👑 <b>مسابقات القيصر</b>\n\nاختر المسابقة التي تريد المشاركة فيها:",
        reply_markup=get_contests_list_keyboard(contests),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("contest_detail:"))
async def contest_detail_callback(callback: CallbackQuery):
    contest_id = int(callback.data.split(':', 1)[1])
    contest = repo.get_contest(contest_id)
    if not contest:
        await safe_answer_callback(callback, "المسابقة غير موجودة", show_alert=True)
        return
    my_entry = repo.get_contest_entry(contest_id, str(callback.from_user.id))
    joined_text = f"\n✅ لقد أرسلت مشاركتك بالفعل وحالتها: <code>{my_entry.get('status')}</code>\n" if my_entry else ""
    text = (
        f"👑 <b>{contest.get('title')}</b>\n\n"
        f"📝 <b>الوصف:</b>\n{contest.get('description') or '—'}\n\n"
        f"🏆 <b>نوع الجائزة:</b> <code>{contest.get('reward_type')}</code>\n"
        f"💰 <b>قيمة الجائزة:</b> <code>{int(contest.get('reward_amount') or 0):,} SYP</code>\n"
        f"👥 <b>عدد الفائزين:</b> <code>{int(contest.get('winners_limit') or 1)}</code>\n"
        f"📎 <b>إثبات مطلوب:</b> <code>{'نعم' if contest.get('requires_proof') else 'لا'}</code>\n"
        f"{joined_text}"
    )
    keyboard = get_user_menu_keyboard(callback.from_user.id) if my_entry else get_contest_submit_keyboard(contest_id)
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("contest_submit:"))
async def contest_submit_callback(callback: CallbackQuery, state: FSMContext):
    contest_id = int(callback.data.split(':', 1)[1])
    contest = repo.get_contest(contest_id)
    if not contest:
        await safe_answer_callback(callback, "المسابقة غير موجودة", show_alert=True)
        return
    await state.update_data(contest_id=contest_id)
    await safe_edit_text(
        callback.message,
        "📩 أرسل الآن مشاركتك أو إثباتك للمسابقة.\nيمكنك إرسال:\n- نص\n- رابط\n- صورة\n- ملف\n\nوسيتم حفظ المشاركة ومراجعتها من الإدارة.",
        reply_markup=get_user_menu_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await state.set_state(BotStates.entering_contest_proof)
    await safe_answer_callback(callback)


@router.message(BotStates.entering_contest_proof)
async def process_contest_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    contest_id = int(data.get('contest_id') or 0)
    if not contest_id:
        await message.answer("❌ تعذر تحديد المسابقة الحالية.")
        await state.clear()
        return

    proof_text = (message.text or message.caption or '').strip()
    proof_type = 'text'
    proof_file_id = None

    if message.photo:
        proof_type = 'photo'
        proof_file_id = message.photo[-1].file_id
        if not proof_text:
            proof_text = 'صورة مرفقة كإثبات مشاركة'
    elif message.document:
        proof_type = 'document'
        proof_file_id = message.document.file_id
        if not proof_text:
            proof_text = f"ملف مرفق: {message.document.file_name or 'document'}"
    elif message.text:
        proof_type = 'text'
    else:
        await message.answer("❌ أرسل نصاً أو صورة أو ملفاً كمشاركة للمسابقة.")
        return

    result = repo.add_contest_entry(
        contest_id,
        str(message.from_user.id),
        proof_text=proof_text,
        proof_type=proof_type,
        proof_file_id=proof_file_id
    )
    reasons = {
        'contest_not_found': 'المسابقة غير موجودة.',
        'contest_closed': 'تم إغلاق المسابقة.',
        'already_joined': 'لقد شاركت مسبقاً في هذه المسابقة.',
        'not_saved': 'تعذر حفظ المشاركة.'
    }
    if not result.get('ok'):
        await message.answer(f"❌ {reasons.get(result.get('reason'), 'تعذر تسجيل المشاركة.')}")
        await state.clear()
        return

    # ✅ إرسال إشعار إلى قناة المسابقات
    entry_id = result.get('entry_id')
    contest = repo.get_contest(contest_id)
    user = repo.get_user(str(message.from_user.id))

    if contest and entry_id:
        channel_id = getattr(settings, 'CONTEST_CHANNEL_ID', None)
        if channel_id:
            # بناء رسالة الإشعار
            username = user.get('telegram_username') or message.from_user.first_name or 'مستخدم'
            username_text = f"@{username}" if user.get('telegram_username') else username

            admin_text = (
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "👑 <b>مشاركة جديدة في المسابقة</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🏆 <b>المسابقة:</b> {contest.get('title')}\n"
                f"🆔 <b>رقم المسابقة:</b> <code>#{contest_id}</code>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "👤 <b>بيانات المشارك:</b>\n"
                f"├─ 📛 الاسم: {username_text}\n"
                f"├─ 🆔 Telegram ID: <code>{message.from_user.id}</code>\n"
                f"├─ 🎮 Player ID: <code>{user.get('player_id') or 'غير مرتبط'}</code>\n"
                f"└─ 💎 رصيد البوت: <code>{int(user.get('bot_balance') or 0):,} SYP</code>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📎 <b>نوع المشاركة:</b> <code>{proof_type}</code>\n"
                f"📝 <b>نص المشاركة:</b> {proof_text}\n\n"
                f"⏰ <b>الوقت:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"📌 <b>رقم المشاركة:</b> <code>#{entry_id}</code>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━"
            )

            # أزرار القبول/الرفض
            admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ قبول المشاركة", callback_data=f"approve_contest_{entry_id}"),
                    InlineKeyboardButton(text="❌ رفض المشاركة", callback_data=f"reject_contest_{entry_id}")
                ],
                [InlineKeyboardButton(text="👤 تفاصيل المستخدم", callback_data=f"user_details_{message.from_user.id}")]
            ])

            try:
                # إرسال الصورة إذا كانت موجودة
                if proof_type == 'photo' and proof_file_id:
                    await message.bot.send_photo(
                        chat_id=channel_id,
                        photo=proof_file_id,
                        caption=admin_text,
                        reply_markup=admin_keyboard,
                        parse_mode="HTML"
                    )
                elif proof_type == 'document' and proof_file_id:
                    await message.bot.send_document(
                        chat_id=channel_id,
                        document=proof_file_id,
                        caption=admin_text,
                        reply_markup=admin_keyboard,
                        parse_mode="HTML"
                    )
                else:
                    await message.bot.send_message(
                        chat_id=channel_id,
                        text=admin_text,
                        reply_markup=admin_keyboard,
                        parse_mode="HTML"
                    )
            except Exception as e:
                logger.warning(f"Failed to send contest notification to channel: {e}")

    await message.answer(
        "✅ تم استلام مشاركتك بنجاح.\nسيتم مراجعتها من الإدارة، وعند الفوز ستصلك الجائزة أو كود الهدية مباشرة.",
        reply_markup=get_user_menu_keyboard(message.from_user.id)
    )
    await state.clear()


@router.callback_query(F.data == "jackpot_menu")
async def jackpot_menu_callback(callback: CallbackQuery):
    await send_coming_soon(callback, "👑 مسابقات القيصر")


@router.callback_query(F.data == "offers_menu")
async def offers_menu_callback(callback: CallbackQuery):
    """عرض البونصات والعروض الفعالة للمستخدم."""
    try:
        rules = repo.get_active_bonus_rules()
    except Exception as e:
        logger.error(f"offers_menu failed to load bonus rules: {e}")
        rules = []

    if not rules:
        text = (
            "🎁 <b>العروض والبونصات</b>\n\n"
            "لا توجد عروض فعّالة حالياً.\n"
            "تابعنا باستمرار، سيتم إضافة عروض جديدة قريباً ✨"
        )
    else:
        method_labels = {
            'all': 'كل طرق الإيداع',
            'syriatel': 'Syriatel Cash',
            'mtn': 'MTN Cash',
            'sham_syp': 'Sham Cash SYP',
            'sham_usd': 'Sham Cash USD',
            'usdt_trc': 'USDT TRC20',
            'usdt_bep': 'USDT BEP20',
        }
        text = (
            "🎁 <b>العروض والبونصات الفعّالة</b>\n\n"
            "💡 يتم تطبيق البونص تلقائياً عند قبول طلب الإيداع إذا كانت شروط العرض مطابقة.\n"
            "إذا انطبق أكثر من عرض، تحصل على أعلى بونص فقط.\n\n"
        )
        for rule in rules[:10]:
            method = method_labels.get(rule.get('payment_method'), rule.get('payment_method') or 'كل الطرق')
            min_amount = float(rule.get('min_amount_syp') or 0)
            max_bonus = float(rule.get('max_bonus_syp') or 0)
            text += (
                f"🏷️ <b>{rule.get('title')}</b>\n"
                f"📈 البونص: <code>{float(rule.get('percent') or 0):g}%</code>\n"
                f"💳 طريقة الإيداع: <code>{method}</code>\n"
            )
            if min_amount > 0:
                text += f"💰 الحد الأدنى: <code>{min_amount:,.0f} SYP</code>\n"
            else:
                text += "💰 الحد الأدنى: <code>بدون حد أدنى</code>\n"
            if max_bonus > 0:
                text += f"🛡️ الحد الأعلى للبونص: <code>{max_bonus:,.0f} SYP</code>\n"
            text += "\n"

        text += "📥 للاستفادة من العرض اختر <b>شحن رصيد</b> من القائمة الرئيسية."

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=get_user_menu_keyboard(callback.from_user.id),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)



@router.callback_query(F.data == "prediction_cards_menu")
async def prediction_cards_menu_callback(callback: CallbackQuery):
    """عرض بطاقات التوقع المفتوحة للمستخدم."""
    cards = repo.get_open_prediction_cards(limit=10)
    if not cards:
        text = (
            "🎫 <b>بطاقات التوقع</b>\n\n"
            "لا توجد بطاقات توقع مفتوحة حالياً.\n"
            "تابعنا لاحقاً عند فتح مباريات جديدة ✨"
        )
        await safe_edit_text(callback.message, text, reply_markup=get_user_menu_keyboard(callback.from_user.id), parse_mode="HTML")
        await safe_answer_callback(callback)
        return

    text = (
        "🎫 <b>بطاقات التوقع المفتوحة</b>\n\n"
        "اختر البطاقة التي تريد المشاركة فيها.\n"
        "⚠️ يمكنك تثبيت توقع واحد فقط لكل بطاقة."
    )
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=get_prediction_cards_list_keyboard(cards),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("prediction_card_detail:"))
async def prediction_card_detail_callback(callback: CallbackQuery):
    card_id = int(callback.data.split(':', 1)[1])
    card = repo.get_prediction_card(card_id)
    if not card:
        await safe_answer_callback(callback, "البطاقة غير موجودة", show_alert=True)
        return
    import json
    try:
        options = json.loads(card.get('options_json') or '[]')
    except Exception:
        options = []
    summary = repo.get_prediction_card_summary(card_id)
    total_entries = sum(int(x.get('count') or 0) for x in summary)
    my_entry = repo.get_user_prediction_entry(card_id, str(callback.from_user.id))
    my_text = f"\n✅ توقعك الحالي: <code>{my_entry.get('selected_option')}</code>\n" if my_entry else "\n"
    limit_text = f"{int(card.get('max_predictions') or 0):,}" if int(card.get('max_predictions') or 0) > 0 else "بدون حد"
    closes_at = card.get('closes_at')
    closes_label = closes_at.strftime('%Y-%m-%d %H:%M') if closes_at else 'غير محدد'
    options_text = "\n".join([f"• <code>{o}</code>" for o in options]) if options else "—"
    text = (
        f"🎫 <b>{card.get('title')}</b>\n\n"
        f"⚽ <b>المباراة:</b> <code>{card.get('team_a')}</code> × <code>{card.get('team_b')}</code>\n"
        f"📌 <b>الحالة:</b> <code>{card.get('status')}</code>\n"
        f"🎟️ <b>عدد البطاقات:</b> <code>{limit_text}</code>\n"
        f"👥 <b>المشاركات الحالية:</b> <code>{total_entries}</code>\n"
        f"💰 <b>الجائزة لكل فائز:</b> <code>{int(card.get('reward_syp') or 0):,} SYP</code>\n"
        f"🕒 <b>الإغلاق:</b> <code>{closes_label}</code>\n"
        f"{my_text}"
        f"\n🎯 <b>خيارات التوقع:</b>\n{options_text}"
    )
    keyboard = get_user_menu_keyboard(callback.from_user.id) if my_entry or str(card.get('status')) != 'open' else get_prediction_card_options_keyboard(card_id, options, callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("predict_select:"))
async def prediction_select_callback(callback: CallbackQuery):
    _, card_id, selected_option = callback.data.split(':', 2)
    result = repo.add_prediction_entry_safe(int(card_id), str(callback.from_user.id), selected_option)
    reasons = {
        'card_not_found': 'البطاقة غير موجودة.',
        'card_closed': 'تم إغلاق هذه البطاقة أو انتهى وقتها.',
        'already_predicted': 'لقد ثبّت توقعك مسبقاً لهذه البطاقة.',
        'limit_reached': 'اكتمل عدد البطاقات المتاحة.',
        'invalid_option': 'الخيار المحدد غير صالح.',
        'not_saved': 'تعذر حفظ التوقع، حاول لاحقاً.',
    }
    if not result.get('ok'):
        await safe_answer_callback(callback, reasons.get(result.get('reason'), 'تعذر تسجيل التوقع.'), show_alert=True)
        return

    # ✅ إرسال إشعار إلى قناة المسابقات عند التوقع
    channel_id = getattr(settings, 'CONTEST_CHANNEL_ID', None)
    if channel_id:
        try:
            card = repo.get_prediction_card(int(card_id))
            user = repo.get_user(str(callback.from_user.id))
            if card and user:
                username = user.get('telegram_username') or callback.from_user.first_name or 'مستخدم'
                username_text = f"@{username}" if user.get('telegram_username') else username

                summary = repo.get_prediction_card_summary(int(card_id))
                total_entries = sum(int(x.get('count') or 0) for x in summary)

                notification_text = (
                    "━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "🎫 <b>توقع جديد</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"⚽ <b>المباراة:</b> <code>{card.get('team_a')}</code> × <code>{card.get('team_b')}</code>\n"
                    f"🎫 <b>البطاقة:</b> {card.get('title')}\n"
                    f"🎯 <b>التوقع:</b> <code>{selected_option}</code>\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"👤 <b>المشارك:</b> {username_text}\n"
                    f"🆔 <b>Telegram ID:</b> <code>{callback.from_user.id}</code>\n"
                    f"🎮 <b>Player ID:</b> <code>{user.get('player_id') or 'غير مرتبط'}</code>\n\n"
                    f"⏰ <b>الوقت:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"👥 <b>إجمالي التوقعات:</b> <code>{total_entries}</code>\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━"
                )

                await callback.bot.send_message(
                    chat_id=channel_id,
                    text=notification_text,
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.warning(f"Failed to send prediction notification to channel: {e}")

    await safe_answer_callback(callback, '✅ تم تثبيت توقعك بنجاح', show_alert=True)
    callback.data = f'prediction_card_detail:{card_id}'
    await prediction_card_detail_callback(callback)


@router.callback_query(F.data == "deposit_game_acc")
async def deposit_game_acc_callback(callback: CallbackQuery, state: FSMContext):
    """شحن حساب اللعبة: تحويل رصيد البوت (SYP) إلى رصيد اللعبة (NSP) — فوري."""
    if not await _ensure_service_gate(callback.message, callback.from_user.id, 'game', edit=True):
        await safe_answer_callback(callback)
        return
    if not await require_ichancy_registered(callback):
        return

    telegram_id = str(callback.from_user.id)
    user = repo.get_user(telegram_id)
    if not user or int(user.get('bot_balance', 0)) <= 0:
        await safe_edit_text(
            callback.message,
            "❌ <b>لا يوجد رصيد في البوت حالياً.</b>\n\nيرجى تنفيذ عملية <b>شحن رصيد</b> داخل البوت أولاً، ثم العودة لشحن حساب اللعبة.",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback)
        return

    bot_settings = repo.get_bot_settings()
    game_min_deposit = int(bot_settings.get('game_min_deposit_syp') or 20000)
    bot_balance = int(safe_balance(user))
    cached_game_balance = repo.get_user_game_balance(telegram_id)

    if bot_balance < game_min_deposit:
        await safe_edit_text(
            callback.message,
            f"❌ <b>رصيدك الحالي لا يكفي لشحن حساب اللعبة.</b>\n\n"
            f"💎 رصيدك في البوت: <code>{bot_balance:,} SYP</code>\n"
            f"⚠️ الحد الأدنى لشحن اللعبة: <code>{game_min_deposit:,} SYP</code>\n\n"
            "يرجى شحن رصيد البوت أولاً ثم العودة لشحن حساب اللعبة.",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback)
        return

    await state.update_data(game_deposit_cached_balance=cached_game_balance, game_min_deposit_syp=game_min_deposit)

    text = (
        f"📥 <b>شحن حساب اللعبة (iChancy)</b>\n\n"
        f"💎 <b>رصيدك في البوت:</b> <code>{bot_balance:,} SYP</code>\n"
        f"🎮 <b>رصيد اللعبة المسجل:</b> <code>{cached_game_balance:,} NSP</code>\n"
        f"💱 <b>نسبة التحويل:</b> <code>1 SYP = 1 NSP</code>\n"
        f"⚠️ <b>الحد الأدنى للشحن:</b> <code>{game_min_deposit:,} SYP</code>\n\n"
        f"📊 <b>أقصى NSP يمكنك شحنها الآن:</b> <code>{bot_balance:,} NSP</code>\n\n"
        f"اكتب المبلغ الذي تريد تحويله <b>بالليرة السورية (SYP)</b> 👇\n"
        f"<i>ملاحظة: لا ننتظر تحديث رصيد اللعبة من الموقع هنا لتبقى العملية أسرع.</i>"
    )
    await safe_edit_text(callback.message, text, parse_mode="HTML")
    await state.set_state(BotStates.entering_game_deposit_amount)
    await safe_answer_callback(callback)


@router.message(BotStates.entering_game_deposit_amount)
async def process_game_deposit_amount(message: Message, state: FSMContext):
    try:
        amount_syp = int(message.text.strip().replace(',', ''))
        if amount_syp <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ الرجاء إدخال مبلغ رقمي صحيح:")
        return

    telegram_id = str(message.from_user.id)
    user = repo.get_user(telegram_id)

    data = await state.get_data()
    game_min_deposit = int(data.get('game_min_deposit_syp') or repo.get_bot_settings().get('game_min_deposit_syp') or 20000)
    if amount_syp < game_min_deposit:
        await message.answer(
            f"❌ الحد الأدنى لشحن حساب اللعبة هو <code>{game_min_deposit:,} SYP</code>.\n\n"
            "يرجى إدخال مبلغ يساوي أو أكبر من الحد الأدنى:",
            parse_mode="HTML"
        )
        return
    if amount_syp > int(safe_balance(user)):
        await message.answer(f"❌ رصيدك لا يكفي. رصيدك الحالي: {int(safe_balance(user)):,} SYP. أعد الإدخال:")
        return

    # 🆕 نسبة 1:1 — المبلغ بالـ SYP يساوي المبلغ بالـ NSP، مع بونص لعب مرفق حسب إعدادات الأدمن
    amount_nsp = amount_syp
    bonus_available = int(user.get('bonus_balance') or 0)
    bonus_base_balance = int(user.get('bonus_base_balance') or 0)
    bonus_to_apply = repo.calculate_game_bonus_for_deposit(amount_syp, bonus_available, bonus_base_balance)
    cashback_pending = int(user.get('cashback_pending_balance') or 0)
    checkin_pending = int(user.get('checkin_pending_balance') or 0)
    total_nsp = amount_nsp + bonus_to_apply + cashback_pending + checkin_pending
    data = await state.get_data()
    await state.update_data(
        game_deposit_syp=amount_syp,
        game_deposit_nsp=total_nsp,
        game_deposit_cash_nsp=amount_nsp,
        game_deposit_bonus=bonus_to_apply,
        game_deposit_cashback=cashback_pending,
        game_deposit_checkin=checkin_pending,
        game_deposit_cached_balance=int(data.get('game_deposit_cached_balance') or repo.get_user_game_balance(telegram_id)),
    )

    bonus_line = ""
    if bonus_to_apply > 0:
        bonus_line = f"🎁 بونص اللعب المرفق: <code>{bonus_to_apply:,} NSP</code>\n"
    elif bonus_available > 0:
        settings = repo.get_bot_settings()
        if not settings.get('game_bonus_enabled', True) or float(settings.get('game_bonus_apply_percent') or 0) <= 0:
            bonus_line = "🎁 لديك بونص متاح، لكن إرفاق بونص اللعب متوقف حالياً من الإدارة.\n"
        elif bonus_base_balance <= 0:
            bonus_line = "🎁 لديك بونص متاح، لكنه غير مرتبط بإيداع نقدي حالياً؛ سيتم استخدام نسبة الإرفاق الاحتياطية عند توفرها.\n"
        else:
            bonus_line = "🎁 لديك بونص متاح، لكن لم تنطبق شروط إرفاقه على هذا الشحن.\n"
    else:
        bonus_line = "🎁 لا يوجد بونص لعب متاح حالياً لهذا الشحن.\n"

    confirm_text = (
        f"📋 <b>تأكيد شحن حساب اللعبة</b>\n\n"
        f"💱 شحن من رصيدك: <code>{amount_syp:,} SYP</code> → <code>{amount_nsp:,} NSP</code>\n"
        f"{bonus_line}"
        f"🎮 الإجمالي الذي سيصل للعبة: <code>{total_nsp:,} NSP</code>\n"
        f"📊 النسبة: 1 SYP = 1 NSP\n"
        f"💎 رصيدك النقدي بعد العملية: <code>{int(safe_balance(user)) - amount_syp:,} SYP</code>\n"
        f"🎁 رصيد البونص بعد العملية: <code>{max(0, bonus_available - bonus_to_apply):,} SYP</code>\n\n"
        f"⚠️ <b>بونص اللعب يُستخدم داخل اللعبة، وعند السحب يُخصم البونص النشط أولاً.</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد الشحن", callback_data="confirm_game_deposit")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="back_to_main_menu")]
    ])
    await message.answer(confirm_text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(BotStates.confirming_game_deposit)


@router.callback_query(F.data == "confirm_game_deposit", BotStates.confirming_game_deposit)
async def confirm_game_deposit_callback(callback: CallbackQuery, state: FSMContext):
    allowed, reason = repo.service_gate_status('game')
    if not allowed:
        await callback.message.answer(f"🛡️ {reason}", reply_markup=get_user_menu_keyboard(callback.from_user.id))
        await state.clear()
        await safe_answer_callback(callback, reason, show_alert=True)
        return
    data = await state.get_data()
    if not data or 'game_deposit_syp' not in data:
        await send_expired_flow_message(callback.message, callback.from_user.id)
        await state.clear()
        await safe_answer_callback(callback, "⚠️ انتهت صلاحية الطلب.", show_alert=True)
        return

    amount_syp = Decimal(str(data['game_deposit_syp']))
    await state.clear()
    await safe_edit_text(
        callback.message,
        "⏳ <b>جاري تنفيذ عملية الشحن...</b>\n\nيرجى الانتظار، يتم الآن التواصل مع iChancy.",
        parse_mode="HTML"
    )

    result = await deposit_to_player_game(
        user_id=str(callback.from_user.id),
        amount_syp=amount_syp,
        bot=callback.bot
    )
    success = result.get('success') if isinstance(result, dict) else bool(result)

    if success:
        user = repo.get_user(str(callback.from_user.id))
        new_game = repo.get_user_game_balance(str(callback.from_user.id))
        cash_amount = int(result.get('cash_amount') or amount_syp) if isinstance(result, dict) else int(amount_syp)
        bonus_amount = int(result.get('bonus_amount') or 0) if isinstance(result, dict) else 0
        total_to_game = int(result.get('total_to_game') or data['game_deposit_nsp']) if isinstance(result, dict) else int(data['game_deposit_nsp'])
        bonus_success_line = f"🎁 بونص اللعب المرفق: <code>{bonus_amount:,} NSP</code>\n" if bonus_amount > 0 else ""
        await safe_edit_text(
            callback.message,
            f"✅ <b>تم شحن حساب اللعبة بنجاح!</b>\n\n"
            f"💱 من رصيدك: <code>{cash_amount:,} ل.س</code> → <code>{cash_amount:,} NSP</code>\n"
            f"{bonus_success_line}"
            f"🎮 الإجمالي الذي وصل للعبة: <code>{total_to_game:,} NSP</code>\n"
            f"💎 رصيد البوت الآن: {_fmt_syp_dual(safe_balance(user))}\n"
            f"🎁 بونص اللعب النشط داخل اللعبة: <code>{int(user.get('game_bonus_amount') or 0):,} SYP</code>\n"
            f"🎮 رصيد اللعبة المسجل: <code>{new_game:,} NSP</code>\n\n"
            f"🏠 يمكنك العودة للقائمة الرئيسية من الأسفل.",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback, "تم الشحن بنجاح!")
    else:
        if isinstance(result, dict) and result.get('uncertain'):
            await safe_edit_text(
                callback.message,
                "⚠️ <b>عملية الشحن قيد التحقق اليدوي</b>\n\n"
                "أرسل iChancy نتيجة غير مؤكدة: قد يكون الشحن تم فعلاً لكن لم نتمكن من تأكيد تغير الرصيد فوراً.\n"
                "تم إبقاء العملية معلّقة وسيقوم المشرف بمراجعتها. لم يتم رد الرصيد تلقائياً لحماية حسابك من أي تضارب.",
                reply_markup=get_user_menu_keyboard(callback.from_user.id),
                parse_mode="HTML"
            )
            await safe_answer_callback(callback, "قيد التحقق", show_alert=True)
        else:
            await safe_edit_text(
                callback.message,
                "❌ <b>فشلت عملية الشحن!</b>\n\nتمت إعادة الرصيد إذا لم يتم تنفيذ العملية. قد يكون السبب:\n"
                "• فشل الاتصال بخوادم iChancy\n• خطأ مؤقت في الجلسة أو الشبكة\n\nحاول مجدداً بعد قليل.",
                reply_markup=get_user_menu_keyboard(callback.from_user.id),
                parse_mode="HTML"
            )
            await safe_answer_callback(callback, "فشل الشحن.", show_alert=True)


@router.callback_query(F.data == "withdraw_game_acc")
async def withdraw_game_acc_callback(callback: CallbackQuery, state: FSMContext):
    """سحب من حساب اللعبة: تحويل رصيد اللعبة (NSP) إلى رصيد البوت (SYP) — فوري."""
    if not await _ensure_service_gate(callback.message, callback.from_user.id, 'game', edit=True):
        await safe_answer_callback(callback)
        return
    if not await require_ichancy_registered(callback):
        return

    telegram_id = str(callback.from_user.id)
    user = repo.get_user(telegram_id)
    if not user:
        await callback.message.edit_text("الرجاء استخدام /start أولاً.")
        await callback.answer()
        return

    # جلب رصيد اللعبة الفعلي مرة واحدة فقط، ثم استخدامه خلال نفس العملية لتقليل البطء.
    await safe_edit_text(callback.message, "⏳ <b>جاري جلب رصيدك الفعلي في اللعبة...</b>", parse_mode="HTML")
    api_balance = await ichancy_api_client.get_player_balance(user['player_id'])
    if api_balance is not None:
        game_balance = int(api_balance)
        repo.update_user_game_balance(telegram_id, game_balance)
    else:
        game_balance = repo.get_user_game_balance(telegram_id)
        logger.warning(f"Could not fetch live balance for user {telegram_id}, falling back to DB cached: {game_balance}")
    await state.update_data(game_withdraw_balance=game_balance)

    if game_balance <= 0:
        await safe_edit_text(
            callback.message,
            f"❌ <b>لا يوجد رصيد في حساب اللعبة حالياً.</b>\n\nرصيدك في اللعبة: <code>{game_balance:,} NSP</code>\n"
            "توجّه إلى اللعبة وحقق بعض النقاط، ثم عُد لسحبها إلى البوت.",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback)
        return

    active_bonus = int(user.get('game_bonus_amount') or 0)
    net_if_full = max(0, int(game_balance) - active_bonus)
    bonus_info_line = ""
    if active_bonus > 0:
        bonus_info_line = f"🎁 <b>بونص لعب نشط:</b> <code>{active_bonus:,} SYP</code> <i>(يُخصم أولاً عند السحب)</i>\n"

    text = (
        f"📤 <b>سحب من حساب اللعبة (iChancy)</b>\n\n"
        f"🎮 <b>رصيدك الفعلي في اللعبة:</b> <code>{game_balance:,} NSP</code>\n"
        f"{bonus_info_line}"
        f"💎 <b>رصيدك في البوت:</b> <code>{int(safe_balance(user)):,} SYP</code>\n"
        f"💱 <b>نسبة التحويل:</b> <code>1 NSP = 1 SYP</code>\n\n"
        f"📊 <b>الصافي إذا سحبت كامل الرصيد:</b> <code>{net_if_full:,} SYP</code>\n\n"
        f"اكتب المبلغ الذي تريد سحبه <b>بنقاط اللعبة (NSP)</b> 👇"
    )
    await safe_edit_text(callback.message, text, parse_mode="HTML")
    await state.set_state(BotStates.entering_game_withdraw_amount)
    await safe_answer_callback(callback)


@router.message(BotStates.entering_game_withdraw_amount)
async def process_game_withdraw_amount(message: Message, state: FSMContext):
    try:
        amount_nsp = int(message.text.strip().replace(',', ''))
        if amount_nsp <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ الرجاء إدخال مبلغ رقمي صحيح (بنقاط NSP):")
        return

    telegram_id = str(message.from_user.id)
    user = repo.get_user(telegram_id)

    data = await state.get_data()
    game_balance = int(data.get('game_withdraw_balance') or repo.get_user_game_balance(telegram_id))

    if amount_nsp > game_balance:
        await message.answer(f"❌ رصيدك في اللعبة لا يكفي. رصيدك الفعلي: <code>{game_balance:,} NSP</code>. أعد الإدخال:", parse_mode="HTML")
        return

    # 🆕 نسبة 1:1 — المبلغ بالـ NSP يساوي بالـ SYP، مع خصم بونص اللعب النشط أولاً
    amount_syp = amount_nsp
    active_bonus = int(user.get('game_bonus_amount') or 0)
    bonus_deducted = min(active_bonus, amount_syp)
    cash_to_credit = max(0, amount_syp - bonus_deducted)
    await state.update_data(
        game_withdraw_nsp=amount_nsp,
        game_withdraw_syp=cash_to_credit,
        game_withdraw_gross_syp=amount_syp,
        game_withdraw_bonus_deducted=bonus_deducted,
        game_withdraw_balance=game_balance,
    )

    bonus_line = ""
    if bonus_deducted > 0:
        bonus_line = f"🎁 خصم بونص لعب نشط: <code>{bonus_deducted:,} SYP</code>\n"

    confirm_text = (
        f"📋 <b>تأكيد السحب من حساب اللعبة</b>\n\n"
        f"💱 سيتم سحب: <code>{amount_nsp:,} NSP</code> من حساب اللعبة\n"
        f"{bonus_line}"
        f"💎 الصافي الذي سيضاف لرصيد البوت: <code>{cash_to_credit:,} SYP</code>\n"
        f"📊 النسبة: 1 NSP = 1 SYP\n"
        f"💎 رصيد البوت بعد العملية: <code>{int(safe_balance(user)) + cash_to_credit:,} SYP</code>\n\n"
        f"⚠️ <b>بونص اللعب النشط غير قابل للسحب نقداً ويُخصم أولاً.</b>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد السحب", callback_data="confirm_game_withdraw")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="back_to_main_menu")]
    ])
    await message.answer(confirm_text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(BotStates.confirming_game_withdraw)


@router.callback_query(F.data == "confirm_game_withdraw", BotStates.confirming_game_withdraw)
async def confirm_game_withdraw_callback(callback: CallbackQuery, state: FSMContext):
    allowed, reason = repo.service_gate_status('game')
    if not allowed:
        await callback.message.answer(f"🛡️ {reason}", reply_markup=get_user_menu_keyboard(callback.from_user.id))
        await state.clear()
        await safe_answer_callback(callback, reason, show_alert=True)
        return
    data = await state.get_data()
    if not data or 'game_withdraw_nsp' not in data:
        await send_expired_flow_message(callback.message, callback.from_user.id)
        await state.clear()
        await safe_answer_callback(callback, "⚠️ انتهت صلاحية الطلب.", show_alert=True)
        return

    amount_nsp = Decimal(str(data['game_withdraw_nsp']))
    await state.clear()
    await safe_edit_text(
        callback.message,
        "⏳ <b>جاري تنفيذ عملية السحب من اللعبة...</b>\n\nيرجى الانتظار، يتم الآن التواصل مع iChancy.",
        parse_mode="HTML"
    )

    result = await withdraw_from_player_game(
        user_id=str(callback.from_user.id),
        amount_nsp=amount_nsp,
        bot=callback.bot
    )
    success = result.get('success') if isinstance(result, dict) else bool(result)

    if success:
        user = repo.get_user(str(callback.from_user.id))
        new_game = repo.get_user_game_balance(str(callback.from_user.id))
        cash_credited = int(result.get('cash_credited') or 0) if isinstance(result, dict) else int(data.get('game_withdraw_syp') or 0)
        bonus_deducted = int(result.get('bonus_deducted') or 0) if isinstance(result, dict) else int(data.get('game_withdraw_bonus_deducted') or 0)
        bonus_line = f"🎁 خصم بونص لعب نشط: <code>{bonus_deducted:,} SYP</code>\n" if bonus_deducted > 0 else ""
        await safe_edit_text(
            callback.message,
            f"✅ <b>تم سحب رصيد اللعبة بنجاح!</b>\n\n"
            f"💱 تم سحب: <code>{int(amount_nsp):,} NSP</code> من اللعبة\n"
            f"{bonus_line}"
            f"💎 الصافي المضاف لرصيد البوت: <code>{cash_credited:,} ل.س</code>\n"
            f"💎 رصيد البوت الآن: {_fmt_syp_dual(safe_balance(user))}\n"
            f"🎮 رصيد اللعبة المسجل: <code>{new_game:,} NSP</code>\n\n"
            f"🏠 يمكنك العودة للقائمة الرئيسية من الأسفل.",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback, "تم السحب بنجاح!")
    else:
        await safe_edit_text(
            callback.message,
            "❌ <b>فشلت عملية السحب من اللعبة!</b>\n\nلم يتم إضافة أي رصيد للبوت. قد يكون السبب:\n"
            "• تغيّر رصيدك في اللعبة أثناء العملية\n• فشل الاتصال بخوادم iChancy\n\nحاول مجدداً بعد قليل.",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback, "فشل السحب.", show_alert=True)


# ================================================================
# ✅ معالجات الإحالات والسجل والهدايا (كانت مفقودة - أزرار ميتة)
# ================================================================

@router.callback_query(F.data == "referral_menu")
async def referral_menu_callback(callback: CallbackQuery):
    """عرض رابط الإحالة وعدد الإحالات النشطة."""
    telegram_id = str(callback.from_user.id)
    user = repo.get_user(telegram_id)
    if not user:
        await callback.message.edit_text("الرجاء استخدام /start أولاً.")
        await callback.answer()
        return

    registered = repo.get_referrals_count(telegram_id)
    active = repo.get_active_referrals_count(telegram_id)
    percent = repo.get_affiliate_percent_by_active_count(active)
    total_earnings = int(user.get('affiliate_balance') or 0)
    referrals_enabled = repo.are_referrals_enabled()
    me = await callback.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{telegram_id}"

    status_text = "🟢 مفعّل" if referrals_enabled else "🔴 متوقف حالياً"
    if active < 3:
        next_level = "تحتاج إلى 3 إحالات نشطة لبدء الربح"
    elif active < 5:
        next_level = "المستوى القادم: 5 إحالات نشطة = 1.5% من خسارة المحالين"
    elif active < 10:
        next_level = "المستوى القادم: 10 إحالات نشطة = 2% من خسارة المحالين"
    else:
        next_level = "أنت في أعلى مستوى إحالات حالياً 👑"

    text = (
        "🤝 <b>نظام الإحالات</b>\n\n"
        "شارك رابطك مع أصدقائك واربح من صافي خسارتهم الأسبوعية في اللعبة كأنك شريك في البوت.\n\n"
        "🎯 <b>شرائح أرباح الإحالات القابلة للسحب:</b>\n"
        "• 3 إحالات نشطة = 1% من خسارة المحالين\n"
        "• 5 إحالات نشطة = 1.5% من خسارة المحالين\n"
        "• 10 إحالات نشطة = 2% من خسارة المحالين\n\n"
        "✅ <b>الإحالة النشطة:</b> من سجّل عبر رابطك وأكمل أول إيداع مقبول.\n"
        "💡 الأرباح تُحسب أسبوعياً من خسارة المحالين في اللعبة، وتضاف إلى رصيد أرباح إحالات قابل للسحب.\n\n"
        f"⚙️ <b>حالة النظام:</b> {status_text}\n"
        f"👥 <b>الإحالات المسجلة:</b> <code>{registered}</code>\n"
        f"✅ <b>الإحالات النشطة:</b> <code>{active}</code>\n"
        f"📈 <b>نسبتك الحالية:</b> <code>{percent}%</code>\n"
        f"💵 <b>رصيد أرباح الإحالات القابل للسحب:</b> <code>{total_earnings:,} SYP</code>\n"
        f"🔜 <b>{next_level}</b>\n\n"
        f"🔗 <b>رابط الإحالة الخاص بك:</b>\n<code>{ref_link}</code>\n\n"
        "انسخ الرابط وشاركه مع أصدقائك 🚀"
    )
    rows = []
    if total_earnings > 0:
        rows.append([InlineKeyboardButton(text="💵 تحويل أرباح الإحالات إلى رصيد البوت للسحب", callback_data="transfer_affiliate_balance")])
    rows.append([InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="back_to_main_menu")])
    await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data == "transfer_affiliate_balance")
async def transfer_affiliate_balance_callback(callback: CallbackQuery):
    telegram_id = str(callback.from_user.id)
    result = repo.transfer_affiliate_balance_to_bot(telegram_id)
    if result.get('ok'):
        await safe_edit_text(
            callback.message,
            f"✅ <b>تم تحويل أرباح الإحالات إلى رصيد البوت.</b>\n\n💵 المبلغ: <code>{int(result.get('amount') or 0):,} SYP</code>\n💎 رصيد البوت الجديد: <code>{int(result.get('new_bot_balance') or 0):,} SYP</code>",
            reply_markup=get_user_menu_keyboard(callback.from_user.id),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback, "تم التحويل")
    else:
        await safe_answer_callback(callback, "لا يوجد رصيد إحالات قابل للتحويل", show_alert=True)


@router.callback_query(F.data == "history_menu")
async def history_menu_callback(callback: CallbackQuery):
    """عرض آخر معاملات المستخدم."""
    telegram_id = str(callback.from_user.id)
    history = repo.get_user_transactions_history(telegram_id, limit=10)

    if not history:
        text = "🔄 <b>سجل المعاملات</b>\n\nلا توجد معاملات بعد."
    else:
        status_emoji = {'approved': '🟢', 'pending': '🟡', 'rejected': '🔴'}
        lines = ["🔄 <b>آخر معاملاتك:</b>\n"]
        for tx in history:
            e = status_emoji.get(tx.get('status'), '⚪️')
            lines.append(
                f"{e} <code>#{tx['id']}</code> {tx.get('type', '')} — "
                f"<code>{float(tx.get('amount', 0)):,.0f}</code> "
                f"[{tx.get('status', '')}]"
            )
        text = "\n".join(lines)

    await safe_edit_text(callback.message, text, reply_markup=get_user_menu_keyboard(callback.from_user.id), parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data == "gift_send")
async def gift_send_callback(callback: CallbackQuery, state: FSMContext):
    await start_gift_flow(callback.message, callback.from_user.id, state, edit=True)
    await safe_answer_callback(callback)


@router.message(BotStates.entering_gift_amount)
async def process_gift_amount(message: Message, state: FSMContext):
    allowed, reason = repo.service_gate_status(None)
    if not allowed:
        await message.answer(f"🛡️ {reason}", reply_markup=get_user_menu_keyboard(message.from_user.id))
        await state.clear()
        return
    try:
        amount = int(message.text.strip().replace(',', ''))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ الرجاء إدخال مبلغ رقمي صحيح:")
        return
    if amount < 1000:
        await message.answer("❌ الحد الأدنى لكود الهدية هو 1,000 SYP:")
        return

    code = "CAESAR-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    success, msg = repo.create_gift(str(message.from_user.id), amount, code)
    if not success:
        await message.answer(f"❌ {msg}", reply_markup=get_user_menu_keyboard(message.from_user.id))
        await state.clear()
        return

    await message.answer(
        f"✅ <b>تم إنشاء كود الهدية بنجاح!</b>\n\n"
        f"🎫 <b>الكود:</b> <code>{code}</code>\n"
        f"💰 <b>القيمة:</b> <code>{amount:,} SYP</code>\n\n"
        "شارك هذا الكود مع من تريد. تم خصم المبلغ من رصيدك.",
        reply_markup=get_user_menu_keyboard(message.from_user.id),
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "gift_redeem")
async def gift_redeem_callback(callback: CallbackQuery, state: FSMContext):
    """بدء عملية استرداد كود هدية."""
    if not await _ensure_service_gate(callback.message, callback.from_user.id, None, edit=True):
        await safe_answer_callback(callback)
        return
    await safe_edit_text(
        callback.message,
        "🎫 <b>استرداد كود هدية</b>\n\nأرسل الآن كود الهدية الذي تريد استرداده:",
        parse_mode="HTML"
    )
    await state.set_state(BotStates.entering_gift_code_to_redeem)
    await safe_answer_callback(callback)


@router.message(BotStates.entering_gift_code_to_redeem)
async def process_gift_redeem(message: Message, state: FSMContext):
    allowed, reason = repo.service_gate_status(None)
    if not allowed:
        await message.answer(f"🛡️ {reason}", reply_markup=get_user_menu_keyboard(message.from_user.id))
        await state.clear()
        return
    code = message.text.strip()
    success, msg = repo.redeem_gift(code, str(message.from_user.id))
    if success:
        try:
            user = repo.get_user(str(message.from_user.id)) or {}
            username = user.get('telegram_username') or message.from_user.username or '—'
            gift_data = repo.get_gift_by_code(code) or repo.get_campaign_code_info(code) or {}
            sender_id = gift_data.get('sender_telegram_id', 'Unknown')
            amount = gift_data.get('amount', 0)
            is_bonus = str(code).upper().startswith('CAESAR-BONUS-')
            type_label = "🎁 بونص لعب" if is_bonus else "💵 كاش قابل للسحب"
            await send_log_message(
                message.bot,
                "🎫 <b>تم استرداد كود هدية جديد!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 <b>المسترد:</b> @{username} (<code>{message.from_user.id}</code>)\n"
                f"🏷️ <b>نوع الهدية:</b> <code>{type_label}</code>\n"
                f"🎫 <b>الكود:</b> <code>{code}</code>\n"
                f"💰 <b>القيمة:</b> <code>{amount:,} SYP</code>\n"
                f"👑 <b>المُصدر:</b> <code>{sender_id}</code>\n"
                f"⏰ <b>الوقت:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
        except Exception as log_err:
            logger.warning(f"Failed to log gift redemption: {log_err}")

    emoji = "✅" if success else "❌"
    await message.answer(
        f"{emoji} {msg}",
        reply_markup=get_user_menu_keyboard(message.from_user.id),
        parse_mode="HTML"
    )
    await state.clear()
