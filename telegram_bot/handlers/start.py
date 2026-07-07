import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command, CommandObject
from config import settings
import database.repository as repo
from telegram_bot.keyboards.inline import (
    get_main_menu_keyboard,
    get_terms_keyboard,
)
from telegram_bot.middlewares.terms_check import get_terms_text

router = Router()
logger = logging.getLogger(__name__)

# 🔧 إصلاح: قائمة الأدمن مع التسامح مع عدم وجود ADMIN_IDS في الإعدادات
ADMIN_IDS = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]


def is_admin_user(user_id) -> bool:
    return str(user_id) in ADMIN_IDS


def get_user_menu_keyboard(user_id):
    return get_main_menu_keyboard(is_admin=is_admin_user(user_id))


async def send_log_message(bot, text, parse_mode="HTML"):
    """إرسال رسالة سجل إلى قناة Log"""
    log_channel_id = getattr(settings, "LOG_CHANNEL_ID", None)
    if not log_channel_id:
        return False
    try:
        await bot.send_message(chat_id=log_channel_id, text=text, parse_mode=parse_mode)
        return True
    except Exception as e:
        logger.error(f"❌ send_log_message failed: {e}")
        return False


# ================================================================
# 🟢 دالة موحّدة لعرض القائمة الرئيسية الكاملة (15 زراً)
# ================================================================
async def show_main_menu(message: Message, user_id, edit: bool = False):
    """تعرض القائمة الرئيسية الكاملة مع رصيد المستخدم."""
    user = repo.get_user(str(user_id))
    bot_balance = int(user['bot_balance']) if user and user.get('bot_balance') is not None else 0
    game_balance = repo.get_user_game_balance(str(user_id)) if user else 0
    bot_balance_new = bot_balance / 100
    bot_balance_new_str = f"{int(bot_balance_new):,}" if bot_balance_new == int(bot_balance_new) else f"{bot_balance_new:,.2f}"

    text = (
        f"👑 <b>أهلاً بك في Caesar_Bot</b>\n\n"
        f"💎 <b>رصيد البوت:</b> <code>{bot_balance:,} ل.س</code> <i>({bot_balance_new_str} ل.س جديدة)</i>\n"
        f"🎮 <b>رصيد اللعبة (iChancy):</b> <code>{game_balance:,} NSP</code>\n\n"
        f"اختر الخدمة المطلوبة من الأزرار بالأسفل 👇"
    )

    keyboard = get_user_menu_keyboard(user_id)

    if edit:
        try:
            await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


# ================================================================
# ✅ معالجات الموافقة على الشروط (كانت مفقودة بالكامل!)
# ================================================================

@router.callback_query(F.data == "accept_terms")
async def accept_terms_callback(callback: CallbackQuery):
    """➡️ الإصلاح الأهم: عند الضغط على 'موافق' يتم تسجيل القبول وعرض القائمة الرئيسية."""
    telegram_id = str(callback.from_user.id)
    user = repo.get_user(telegram_id)
    if not user:
        repo.create_user(telegram_id, callback.from_user.username)
    repo.update_user_terms(telegram_id, accepted=True)

    try:
        await callback.message.delete()
    except Exception:
        pass

    await show_main_menu(callback.message, callback.from_user.id)
    await callback.answer("✅ شكراً لموافقتك على الشروط!")


@router.callback_query(F.data == "reject_terms")
async def reject_terms_callback(callback: CallbackQuery):
    """عند رفض الشروط."""
    await callback.message.edit_text(
        "❌ <b>تم رفض الشروط.</b>\n\nلا يمكنك استخدام البوت دون الموافقة على الشروط.\n"
        "للمحاولة مجدداً اضغط على /start",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 المحاولة مجدداً", callback_data="show_terms_only")
        ]])
    )
    await callback.answer()


@router.callback_query(F.data == "show_terms_only")
async def show_terms_only_callback(callback: CallbackQuery):
    """عرض نص الشروط مع زر الموافقة."""
    terms_text = get_terms_text()
    await callback.message.edit_text(terms_text, reply_markup=get_terms_keyboard(), parse_mode="HTML")
    await callback.answer()


# ================================================================
# ✅ أمر /start — يعرض القائمة الرئيسية دائماً
# ================================================================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, command: CommandObject):
    """معالج أمر البدء — يعرض القائمة الرئيسية دائماً."""
    await state.clear()
    user_id = message.from_user.id
    telegram_id = str(user_id)

    # معالجة الإحالة: /start ref_123456
    args = command.args
    if args and args.startswith("ref_"):
        referrer_id = args[4:].strip()
        existing = repo.get_user(telegram_id)
        if not existing:
            repo.create_user(telegram_id, message.from_user.username)
        if referrer_id and referrer_id != telegram_id:
            repo.add_referral(referrer_id, telegram_id)

    # إنشاء المستخدم احتياطياً (الـ middleware يفعل ذلك لكن للتأكيد)
    user = repo.get_user(telegram_id)
    if not user:
        repo.create_user(telegram_id, message.from_user.username)
        user = repo.get_user(telegram_id)

    # المستخدم العادي لا يرى القائمة الرئيسية قبل قبول الشروط
    if not is_admin_user(user_id) and user and not user.get('terms_accepted'):
        await message.answer(get_terms_text(), reply_markup=get_terms_keyboard(), parse_mode="HTML")
        return

    await show_main_menu(message, user_id)


# ================================================================
# ✅ معالجات الأوامر المنشورة في قائمة الأوامر (كانت بلا معالجات)
# ================================================================

@router.message(Command("home"))
async def cmd_home(message: Message, state: FSMContext):
    """العودة إلى القائمة الرئيسية."""
    await state.clear()
    await show_main_menu(message, message.from_user.id)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """إلغاء أي عملية جارية والعودة للقائمة الرئيسية."""
    await state.clear()
    await show_main_menu(message, message.from_user.id)


@router.message(Command("delete"))
async def cmd_delete(message: Message):
    """حذف الحساب مع تأكيد."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑️ نعم، احذف حسابي نهائياً", callback_data="delete_confirm"),
        InlineKeyboardButton(text="❌ إلغاء", callback_data="delete_cancel"),
    ]])
    await message.answer(
        "⚠️ <b>هل أنت متأكد من حذف حسابك؟</b>\n\n"
        "سيتم حذف جميع بياناتك وأرصدتك ومعاملاتك نهائياً ولا يمكن التراجع.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@router.callback_query(F.data == "delete_confirm")
async def delete_confirm_callback(callback: CallbackQuery):
    telegram_id = str(callback.from_user.id)
    repo.delete_user_completely(telegram_id)
    await callback.message.edit_text(
        "🗑️ <b>تم حذف حسابك بنجاح.</b>\n\nللبدء من جديد اضغط على /start",
        parse_mode="HTML"
    )
    await callback.answer("تم حذف الحساب.")


@router.callback_query(F.data == "delete_cancel")
async def delete_cancel_callback(callback: CallbackQuery):
    await show_main_menu(callback.message, callback.from_user.id, edit=True)
    await callback.answer("تم الإلغاء.")
