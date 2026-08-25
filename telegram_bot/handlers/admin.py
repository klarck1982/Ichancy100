import logging
import asyncio
import time
import random
import string
from datetime import datetime
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from config import settings
import database.repository as repo
from ichancy_api.client import ichancy_api_client
from database.connection import DatabaseManager

router = Router()
logger = logging.getLogger(__name__)

ADMIN_IDS = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]


# 🆕 دوال مساعدة لتحويل الليرة القديمة ↔ الجديدة (منسوخة من menu.py للاستخدام المحلي)
def _syp_old_to_new(amount_old):
    return amount_old / 100

def _fmt_syp_dual(amount_old):
    old = f"{int(amount_old):,}"
    nv = _syp_old_to_new(amount_old)
    new_str = f"{int(nv):,}" if nv == int(nv) else f"{nv:,.2f}"
    return f"{old} ل.س  <code>({new_str} ل.س جديدة)</code>"


def is_admin_user(user_id) -> bool:
    return str(user_id) in ADMIN_IDS


async def _send_with_retry(send_coro, label="send", max_retries=2, delay=1.0):
    """🆕 إرسال آمن مع إعادة محاولة لمعالجة تقطّعات شبكة Render المؤقتة."""
    for attempt in range(1, max_retries + 2):
        try:
            return await send_coro
        except Exception as e:
            if attempt <= max_retries:
                logger.warning(f"{label} failed (attempt {attempt}/{max_retries + 1}): {e} - retrying in {delay}s")
                await asyncio.sleep(delay)
            else:
                logger.error(f"{label} failed after {max_retries + 1} attempts: {e}")
                return None


async def safe_edit_text(target_message, text, reply_markup=None, parse_mode="HTML"):
    try:
        await target_message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except Exception as e:
        logger.warning(f"admin safe_edit_text fallback triggered: {e}")
        try:
            await target_message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except Exception as inner_e:
            logger.error(f"admin safe_edit_text final fallback failed: {inner_e}")
            return False


async def safe_answer_callback(callback: CallbackQuery, text=None, show_alert=False):
    try:
        await callback.answer(text or "✅", show_alert=show_alert)
        return True
    except Exception as e:
        logger.warning(f"admin safe_answer_callback ignored: {e}")
        return False


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


# 🆕 فلتر يطابق رسائل المشرف فقط عند وجود طلب رفض مخصص معلّق (مع استثناء الأوامر)
class HasPendingRejection(Filter):
    async def __call__(self, message: Message) -> bool:
        if not is_admin_user(message.from_user.id):
            return False
        # إذا كانت الرسالة أمراً يبدأ بـ / فإننا لا نعتبرها سبب رفض أبداً
        if message.text and message.text.strip().startswith('/'):
            if message.text.strip().lower() in ('/cancel', '/cancel_rejection', '/home', '/admin'):
                repo.clear_pending_rejection(message.from_user.id)
            return False
        return repo.get_pending_rejection(message.from_user.id) is not None


@router.callback_query(F.data.startswith("user_details_"))
async def user_details_callback(callback: CallbackQuery):
    """✅ معالج زر 'تفاصيل المستخدم' - يرسل التفاصيل لمحادثة المشرف الخاصة."""
    if not await ensure_admin_callback(callback):
        return
    telegram_id = callback.data.replace("user_details_", "")
    details = repo.get_user_details(telegram_id)
    if not details:
        await safe_answer_callback(callback, "⚠️ المستخدم غير موجود.", show_alert=True)
        return

    terms = "نعم ✅" if details.get('terms_accepted') else "لا ❌"
    text = (
        f"👤 <b>تفاصيل المستخدم</b>\n\n"
        f"🆔 <b>Telegram ID:</b> <code>{details['telegram_id']}</code>\n"
        f"📛 <b>الاسم:</b> <code>{details.get('telegram_username') or 'بدون معرف'}</code>\n"
        f"🎮 <b>اسم iChancy:</b> <code>{details.get('ichancy_username') or 'غير مرتبط'}</code>\n"
        f"🔑 <b>Player ID:</b> <code>{details.get('player_id') or 'غير متوفر'}</code>\n"
        f"💎 <b>رصيد البوت:</b> <code>{int(details.get('bot_balance', 0)):,} SYP</code>\n"
        f"🎮 <b>رصيد اللعبة:</b> <code>{int(details.get('game_balance', 0)):,} NSP</code>\n"
        f"📜 <b>وافق على الشروط:</b> {terms}\n\n"
        f"📊 <b>إحصائيات المعاملات:</b>\n"
        f"• الإجمالي: <code>{details['tx_count']}</code>\n"
        f"• معلّقة: <code>{details['pending_count']}</code>\n"
        f"• موافَق عليها: <code>{details['approved_count']}</code>\n"
        f"• مرفوضة: <code>{details['rejected_count']}</code>\n"
        f"👥 <b>الإحالات النشطة:</b> <code>{details['ref_count']}</code>"
    )
    try:
        await callback.bot.send_message(callback.from_user.id, text, parse_mode="HTML")
        await safe_answer_callback(callback, "✅ تم إرسال التفاصيل لمحادثتك الخاصة")
    except Exception:
        await safe_answer_callback(callback, text[:190], show_alert=True)


def get_rejection_reason_keyboard(prefix: str, tx_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="إيصال غير واضح", callback_data=f"{prefix}|receipt_unclear|{tx_id}"),
            InlineKeyboardButton(text="المبلغ غير مطابق", callback_data=f"{prefix}|amount_mismatch|{tx_id}")
        ],
        [
            InlineKeyboardButton(text="التحويل غير موجود", callback_data=f"{prefix}|transfer_missing|{tx_id}"),
            InlineKeyboardButton(text="بيانات ناقصة", callback_data=f"{prefix}|missing_data|{tx_id}")
        ],
        [InlineKeyboardButton(text="✍️ سبب مخصص", callback_data=f"{prefix}|custom|{tx_id}")],
        [InlineKeyboardButton(text="🔙 إلغاء", callback_data="caesar_control_panel")]
    ])


def map_reason_code(reason_code: str) -> str:
    return {
        'receipt_unclear': 'الإيصال غير واضح.',
        'amount_mismatch': 'المبلغ غير مطابق للطلب.',
        'transfer_missing': 'لم يتم العثور على التحويل.',
        'missing_data': 'البيانات المرسلة ناقصة أو غير كافية.',
    }.get(reason_code, 'تم رفض الطلب.')

class AdminStates(StatesGroup):
    entering_exchange_rate = State()
    entering_buy_rate = State()
    entering_sell_rate = State()
    entering_commission = State()
    entering_cookies = State()
    entering_bot_gift_amount = State()
    entering_bonus_title = State()
    entering_bonus_percent = State()
    entering_bonus_min_amount = State()
    entering_bonus_max_amount = State()
    editing_payment_address = State()
    # 🆕 إدارة المستخدمين
    searching_user = State()
    setting_balance = State()
    # 🆕 (Update 18) لوحة المتصدرين الأسبوعية
    entering_lb_prize_1 = State()
    entering_lb_prize_2 = State()
    entering_lb_prize_3 = State()
    entering_lb_min_turnover = State()



def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="👑 لوحة التحكم الرئيسية", callback_data="caesar_control_panel")],
        [
            InlineKeyboardButton(text="🩺 فحص النبض والمحاكاة", callback_data="adm_system_probe"),
            InlineKeyboardButton(text="🔄 تصفير حسابي الاختباري", callback_data="adm_reset_my_test_balance")
        ],
        [InlineKeyboardButton(text="🗑️ تصفير شامل لكل الأرصدة والتاريخ (Beta Reset)", callback_data="adm_reset_all_db")],
        [InlineKeyboardButton(text="💱 تعديل أسعار الصرف", callback_data="adm_rates_menu")],
        [
            InlineKeyboardButton(text="🏷️ نسبة عمولة السحب", callback_data="adm_comm_menu"),
            InlineKeyboardButton(text="🔑 تحديث كوكيز الموقع", callback_data="adm_cookie_menu")
        ],
        [InlineKeyboardButton(text="🎮 رصيد محفظة الوكيل الفعلي", callback_data="adm_agent_bal")],
        [InlineKeyboardButton(text="💳 عناوين الإيداع", callback_data="adm_payment_addresses")],
        [
            InlineKeyboardButton(text="🎫 إنشاء كود هدية", callback_data="adm_create_bot_gift"),
            InlineKeyboardButton(text="🎁 البونصات والعروض", callback_data="adm_bonus_menu")
        ],
        [InlineKeyboardButton(text="🤝 تفعيل/إيقاف الإحالات", callback_data="adm_referrals_toggle")],
        [InlineKeyboardButton(text="❌ إغلاق لوحة التحكم", callback_data="adm_close_panel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_admin_dashboard_keyboard(refresh_callback="caesar_control_panel"):
    webapp_url = f"{getattr(settings, 'RENDER_EXTERNAL_URL', 'https://ichancy100.onrender.com')}/dashboard?v=admin-campaigns-v9-20260716"
    from aiogram.types import WebAppInfo
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 فتح لوحة التحكم المتقدمة", web_app=WebAppInfo(url=webapp_url))],
        [
            InlineKeyboardButton(text="🔎 إدارة المستخدمين", callback_data="adm_users_menu"),
            InlineKeyboardButton(text="💱 أسعار الصرف", callback_data="adm_rates_menu")
        ],
        [
            InlineKeyboardButton(text="🎮 رصيد الوكيل", callback_data="adm_agent_bal"),
            InlineKeyboardButton(text="🔑 الكوكيز", callback_data="adm_cookie_menu")
        ],
        [
            InlineKeyboardButton(text="💳 عناوين الإيداع", callback_data="adm_payment_addresses"),
            InlineKeyboardButton(text="📈 تحديث السيولة", callback_data="adm_sync_liquidity")
        ],
        [
            InlineKeyboardButton(text="🩺 فحص النبض والمحاكاة", callback_data="adm_system_probe"),
            InlineKeyboardButton(text="🔄 تصفير حسابي الاختباري", callback_data="adm_reset_my_test_balance")
        ],
        [InlineKeyboardButton(text="🗑️ تصفير شامل لكل الأرصدة والتاريخ (Beta Reset)", callback_data="adm_reset_all_db")],
        [
            InlineKeyboardButton(text="🎫 إنشاء كود هدية", callback_data="adm_create_bot_gift"),
            InlineKeyboardButton(text="🎁 البونصات", callback_data="adm_bonus_menu")
        ],
        [InlineKeyboardButton(text="🤝 الإحالات", callback_data="adm_referrals_toggle")],
        [InlineKeyboardButton(text="🏆 لوحة المتصدرين الأسبوعية", callback_data="adm_lb_menu")],
        [InlineKeyboardButton(text="❌ إغلاق", callback_data="back_to_main_menu")]
    ])


async def get_total_bot_balance() -> int:
    conn = None
    cur = None
    try:
        conn = DatabaseManager.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(bot_balance), 0) FROM users")
        result = cur.fetchone()
        return int(result[0]) if result else 0
    except Exception as e:
        logger.error(f"Admin panel stats error: {e}")
        return 0
    finally:
        if cur:
            cur.close()
        if conn:
            DatabaseManager.put_connection(conn)


def build_admin_dashboard_text(bot_settings, is_cookie_alive, total_bot_balance, pending, recent, total_users, new_users, today_tx, approved_volume):
    cookie_status = "🟢 نشطة" if is_cookie_alive else "🔴 منتهية"
    cookie_age = repo.get_cookie_age_minutes()
    if cookie_age is None:
        cookie_age_text = "—"
    elif cookie_age < 60:
        cookie_age_text = f"منذ {cookie_age} دقيقة"
    elif cookie_age < 1440:
        cookie_age_text = f"منذ {cookie_age // 60} ساعة"
    else:
        cookie_age_text = f"منذ {cookie_age // 1440} يوم ⚠️"

    cookie_warn = " 🔴 يلزم تحديث!" if (cookie_age and cookie_age >= 720) else ""

    agent_balance = bot_settings.get('agent_balance', 0)
    usd_buy_rate = float(bot_settings['usd_buy_rate'])
    usd_sell_rate = float(bot_settings['usd_sell_rate'])
    withdraw_commission = float(bot_settings['withdraw_commission'])
    exchange_rate = int(bot_settings['exchange_rate'])

    dep_pending = [tx for tx in pending if tx['type'] == 'deposit_bot']
    wit_pending = [tx for tx in pending if tx['type'] == 'withdraw_bot']

    text = "👑 <b>═══ لوحة تحكم القيصر ═══</b>\n\n"
    text += "📊 <b>══ الإحصائيات العامة ══</b>\n"
    text += f"👥 <b>إجمالي المستخدمين:</b> <code>{total_users:,}</code>\n"
    text += f"🆕 <b>جدد اليوم:</b> <code>{new_users}</code> | 🔄 <b>معاملات اليوم:</b> <code>{today_tx}</code>\n"
    text += f"✅ <b>إجمالي المعاملات الموافَق عليها:</b> <code>{approved_volume:,} SYP</code>\n\n"

    text += "💰 <b>══ الأرصدة المالية ══</b>\n"
    text += f"💎 <b>رصيد البوت (مستخدمين):</b> <code>{total_bot_balance:,} SYP</code>\n"
    text += f"🎮 <b>محفظة الوكيل (iChancy):</b> <code>{agent_balance:,} NSP</code>\n\n"

    text += "💱 <b>══ أسعار الصرف الحالية ══</b>\n"
    text += f"📈 <b>سعر الإيداع (شراء $):</b> <code>{usd_buy_rate:,.2f} ل.س</code>\n"
    text += f"📉 <b>سعر السحب (بيع $):</b> <code>{usd_sell_rate:,.2f} ل.س</code>\n"
    text += f"🎮 <b>سعر اللعبة (NSP):</b> <code>1 NSP = {exchange_rate:,} ل.س</code>\n"
    text += f"🏷️ <b>عمولة السحب:</b> <code>{withdraw_commission:,.2f}%</code>\n\n"

    text += "🔑 <b>══ حالة الجلسة ══</b>\n"
    text += f"🍪 <b>كوكيز iChancy:</b> {cookie_status}{cookie_warn}\n"
    text += f"🕐 <b>آخر تحديث:</b> {cookie_age_text}\n\n"

    text += "📋 <b>══ الطلبات المعلّقة ══</b>\n"
    text += f"📥 إيداع: <code>{len(dep_pending)}</code> | 📤 سحب: <code>{len(wit_pending)}</code>\n"
    if dep_pending:
        text += "<i>طلبات الإيداع:</i>\n"
        for tx in dep_pending[:5]:
            text += f"  • <code>#{tx['id']}</code> - {tx['amount']:,.0f} SYP\n"
    if wit_pending:
        text += "<i>طلبات السحب:</i>\n"
        for tx in wit_pending[:5]:
            text += f"  • <code>#{tx['id']}</code> - {tx['amount']:,.0f} SYP\n"

    text += "\n🕒 <b>══ آخر 5 عمليات ══</b>\n"
    for tx in recent[:5]:
        status_emoji = '🟢' if tx['status'] == 'approved' else '🟡' if tx['status'] == 'pending' else '🔴'
        text += f"{status_emoji} <code>#{tx['id']}</code> {tx['type']} - {tx['amount']:,.0f}\n"

    return text


async def send_log_message(bot, text, parse_mode="HTML"):
    log_channel_id = getattr(settings, "LOG_CHANNEL_ID", None)
    if not log_channel_id:
        return
    try:
        await bot.send_message(chat_id=log_channel_id, text=text, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"send_log_message failed: {e}")


async def notify_user_about_transaction(bot, tx, text):
    try:
        await bot.send_message(chat_id=tx['user_telegram_id'], text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"notify user failed: {e}")


async def safe_edit_status_message(message, text, parse_mode="HTML"):
    """تحديث رسالة القناة مع الحفاظ على التفاصيل، ويدعم رسائل الصور/الكابتشن."""
    try:
        if getattr(message, 'photo', None):
            await message.edit_caption(caption=text, parse_mode=parse_mode)
        else:
            await message.edit_text(text, parse_mode=parse_mode)
        return True
    except Exception as e:
        logger.warning(f"safe_edit_status_message fallback triggered: {e}")
        try:
            await message.answer(text, parse_mode=parse_mode)
            return True
        except Exception as inner_e:
            logger.error(f"safe_edit_status_message final fallback failed: {inner_e}")
            return False


def append_final_status(original_text, status_text):
    base = (original_text or '').strip()
    if not base:
        return status_text
    return f"{base}\n\n{status_text}"


def get_withdraw_transfer_code(tx):
    raw = tx.get('transfer_number') or ''
    if raw.startswith('Code:'):
        return raw.split('|', 1)[0].replace('Code:', '').strip()
    return raw or '—'


def get_withdraw_recipient(tx):
    raw = tx.get('transfer_number') or ''
    if 'Recipient:' in raw:
        return raw.split('Recipient:', 1)[1].strip()
    return raw or '—'


def build_withdraw_approved_details(tx):
    user = repo.get_user(tx['user_telegram_id']) or {}
    bot_settings = repo.get_bot_settings()
    amount_syp = int(float(tx.get('amount') or 0))
    gateway = (tx.get('payment_method') or '').upper()
    recipient = get_withdraw_recipient(tx)
    code = get_withdraw_transfer_code(tx)
    commission_percent = float(bot_settings.get('withdraw_commission') or 0)

    if tx.get('payment_method') in ['sham_usd', 'usdt_trc', 'usdt_bep']:
        usd_sell_rate = float(bot_settings.get('usd_sell_rate') or 0)
        gross = (amount_syp / usd_sell_rate) if usd_sell_rate > 0 else 0
        commission = gross * (commission_percent / 100.0)
        net = gross - commission
        gross_label = f"{gross:,.2f} USD"
        commission_label = f"{commission:,.2f} USD"
        net_label = f"{net:,.2f} USD"
    else:
        commission = amount_syp * (commission_percent / 100.0)
        net = amount_syp - commission
        gross_label = f"{amount_syp:,.0f} SYP"
        commission_label = f"{commission:,.0f} SYP"
        net_label = f"{net:,.0f} SYP"

    username = user.get('telegram_username')
    username_text = f"@{username}" if username else "@None"
    return (
        "📤 <b>طلب سحب جديد!</b>\n\n"
        f"🔑 <b>رمز المعاملة:</b> <code>{code}</code>\n"
        f"🆔 <b>رقم الطلب:</b> <code>#{tx['id']}</code>\n"
        f"👤 <b>العضو:</b> {username_text} (<code>{tx['user_telegram_id']}</code>)\n"
        f"💳 <b>بوابة السحب:</b> <code>{gateway}</code>\n"
        f"📱 <b>العنوان:</b> <code>{recipient}</code>\n"
        f"💰 <b>المبلغ المخصوم:</b> <code>{amount_syp:,.0f} SYP</code>\n"
        f"📈 <b>القيمة قبل العمولة:</b> <code>{gross_label}</code>\n"
        f"🏷️ <b>العمولة:</b> <code>{commission_label}</code>\n"
        f"🎁 <b>الصافي المطلوب تحويله:</b> <code>{net_label}</code>\n\n"
        "✅ <b>تم تأكيد التحويل.</b>"
    )


async def _gather_dashboard_stats():
    """جمع كل إحصائيات الداشبورد (مُختصرة لتفادي التكرار)."""
    bot_settings = repo.get_bot_settings()
    if int(bot_settings.get('agent_balance', 0) or 0) == 0:
        try:
            live_bal = await ichancy_api_client.get_admin_balance()
            if live_bal is not None:
                repo.update_bot_settings(agent_balance=int(live_bal))
                bot_settings = repo.get_bot_settings()
        except Exception:
            pass
    return {
        'bot_settings': bot_settings,
        'is_cookie_alive': await ichancy_api_client.check_session_validity(),
        'total_bot_balance': await get_total_bot_balance(),
        'pending': repo.get_pending_requests(),
        'recent': repo.get_all_transactions(10),
        'total_users': repo.get_total_users_count(),
        'new_users': repo.get_new_users_today(),
        'today_tx': repo.get_today_transactions_count(),
        'approved_volume': repo.get_transactions_volume('approved'),
    }


@router.callback_query(F.data == "caesar_control_panel")
async def caesar_control_panel(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    s = await _gather_dashboard_stats()
    text = build_admin_dashboard_text(
        s['bot_settings'], s['is_cookie_alive'], s['total_bot_balance'],
        s['pending'], s['recent'], s['total_users'], s['new_users'],
        s['today_tx'], s['approved_volume']
    )
    keyboard = get_admin_dashboard_keyboard(refresh_callback="caesar_control_panel")
    # 🆕 إضافة زر توزيع الكاش باك الأسبوعي
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="💸 توزيع الكاش باك الأسبوعي", callback_data="adm_trigger_cashback")])
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data == "adm_trigger_cashback")
async def adm_trigger_cashback_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_answer_callback(callback, "⏳ جاري معالجة الكاش باك لجميع المستخدمين...", show_alert=True)
    await callback.message.answer("💸 <b>بدأت عملية توزيع الكاش باك الأسبوعي...</b>\nسيتم إشعار المستخدمين المؤهلين تلقائياً.")
    
    result = await repo.process_all_weekly_cashbacks(bot=callback.bot)
    
    summary = (
        f"✅ <b>اكتمل توزيع الكاش باك!</b>\n\n"
        f"👥 مستخدمون مؤهلون: <code>{result['processed']}</code>\n"
        f"💰 إجمالي المبالغ الموزعة: <code>{result['total_paid']:,} SYP</code>"
    )
    await callback.message.answer(summary, parse_mode="HTML")


@router.callback_query(F.data == "adm_sync_liquidity")
async def adm_sync_liquidity_callback(callback: CallbackQuery):
    """تحديث جميع أرصدة اللاعبين من API iChancy وتوليد تقرير السيولة الشامل."""
    if not await ensure_admin_callback(callback):
        return
    
    await safe_answer_callback(callback, "⏳ جاري مزامنة السيولة وتوليد التقرير...", show_alert=True)
    
    status_msg = await callback.message.answer("🔄 <b>بدأت عملية مزامنة أرصدة اللاعبين...</b>\nيرجى الانتظار، يتم جلب البيانات على دفعات لتجنب الحظر.")
    
    try:
        players = repo.get_all_player_ids()
        total_players = len(players)
        if total_players == 0:
            await status_msg.edit_text("❌ لا يوجد لاعبون مرتبطون بالبوت حالياً.")
            return

        batch_size = 15
        for i in range(0, total_players, batch_size):
            batch = players[i : i + batch_size]
            for player in batch:
                tid, pid = player['telegram_id'], player['player_id']
                try:
                    # جلب الرصيد وتحديثه في قاعدة البيانات
                    balance = await ichancy_api_client.get_player_balance(pid)
                    if balance is not None:
                        repo.update_user_game_balance(tid, balance)
                except Exception as e:
                    logger.warning(f"Failed to sync balance for {tid}: {e}")
            
            # تحديث رسالة التقدم كل دفعة
            processed = min(i + batch_size, total_players)
            await status_msg.edit_text(f"🔄 جاري المزامنة: <code>{processed}/{total_players}</code> لاعب...")
            await asyncio.sleep(1) # فاصل زمني لتجنب Rate Limit

        # 📊 حساب الإجماليات للتقرير
        bot_liquidity = repo.get_total_bot_balances()
        game_liquidity = repo.get_total_game_balances()
        agent_wallet = await ichancy_api_client.get_admin_balance()
        
        # حساب المؤشرات المالية
        net_liabilities = bot_liquidity + game_liquidity
        coverage_ratio = (agent_wallet / game_liquidity * 100) if game_liquidity > 0 else 100.0
        
        # تحديد حالة الأمان
        security_status = "🟢 آمن جداً" if agent_wallet >= game_liquidity else "🔴 عجز في السيولة"
        risk_note = "" if agent_wallet >= game_liquidity else f"⚠️ تحتاج لشحن محفظة الوكيل بمبلغ <code>{game_liquidity - agent_wallet:,} NSP</code> لتغطية كافة الأرصدة."

        report_text = (
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <b>تقرير السيولة الشامل</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💎 <b>سيولة البوت (نقد):</b>\n<code>{bot_liquidity:,} SYP</code>\n"
            f"🎮 <b>سيولة المنصة (أرصدة لاعبين):</b>\n<code>{game_liquidity:,} NSP</code>\n"
            f"🏦 <b>رصيد محفظة الوكيل (احتياطي):</b>\n<code>{agent_wallet:,} NSP</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📉 <b>صافي الالتزامات الإجمالية:</b>\n<code>{net_liabilities:,} SYP/NSP</code>\n"
            f"🛡️ <b>نسبة التغطية:</b> <code>{coverage_ratio:.2f}%</code>\n"
            f"🏁 <b>حالة الأمان:</b> {security_status}\n"
            f"{risk_note}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏰ <i>تاريخ التقرير: {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>"
        )
        
        await status_msg.delete()
        await callback.message.answer(report_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Liquidity sync error: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ حدث خطأ أثناء توليد التقرير:\n<code>{str(e)}</code>", parse_mode="HTML")


@router.callback_query(F.data == "adm_system_probe")
async def adm_system_probe_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_answer_callback(callback, "⏳ جاري فحص النبض ومحاكاة عمليات القيصر...")
    t0 = time.perf_counter()
    db_ok = False
    db_latency = 0
    try:
        cur = DatabaseManager.execute_query_dict("SELECT 1 as ping", fetch='one')
        if cur and cur.get('ping') == 1:
            db_ok = True
            db_latency = round((time.perf_counter() - t0) * 1000, 1)
    except Exception as e:
        logger.warning(f"Probe DB error: {e}")

    t1 = time.perf_counter()
    session_ok = False
    api_latency = 0
    try:
        session_ok = await ichancy_api_client.check_session_validity()
        api_latency = round((time.perf_counter() - t1) * 1000, 1)
    except Exception as e:
        logger.warning(f"Probe API error: {e}")

    dry_run_ok = False
    try:
        conn = DatabaseManager.get_connection()
        c = conn.cursor()
        c.execute("SELECT bot_balance FROM users WHERE telegram_id = %s FOR UPDATE", (str(callback.from_user.id),))
        row = c.fetchone()
        if row is not None:
            c.execute("UPDATE users SET bot_balance = bot_balance + 10000 WHERE telegram_id = %s", (str(callback.from_user.id),))
            conn.rollback() # 🔒 تراجع فوري حتى لا يتغير الرقم الفعلي!
            dry_run_ok = True
        else:
            conn.rollback()
    except Exception as e:
        logger.warning(f"Probe Dry-Run error: {e}")

    report = (
        "🩺 <b>تقرير فحص النبض والمحاكاة الشاملة للقيصر</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💾 <b>قاعدة بيانات Neon (PostgreSQL):</b>\n"
        f"└ الحالة: {'🟢 متصل بكفاءة' if db_ok else '🔴 تعذر الاتصال'}\n"
        f"└ سرعة الاستجابة (Latency): <code>{db_latency} ms</code>\n\n"
        f"🍪 <b>جلسة وكيل iChancy (Agent Session):</b>\n"
        f"└ الحالة: {'🟢 نشطة وفعالة' if session_ok else '🔴 تحتاج تحديث'}\n"
        f"└ سرعة الاتصال (API): <code>{api_latency} ms</code>\n\n"
        f"🧪 <b>محاكاة المعاملات الذرية (Dry-Run Test):</b>\n"
        f"└ فحص القفل الذري والتراجع: {'🟢 ناجح (بدون زيادة أرصدة)' if dry_run_ok else '🟡 يحتاج تحقق'}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>تم فحص الاتصال والتزامن وحسابات القيصر بسلاسة وبون أي تضخم أو تغيير في الأرصدة الحقيقية!</i>"
    )
    await safe_edit_text(callback.message, report, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 إعادة الفحص الآن", callback_data="adm_system_probe")],
        [InlineKeyboardButton(text="🔙 عودة للوحة التحكم", callback_data="caesar_control_panel")]
    ]), parse_mode="HTML")


@router.callback_query(F.data == "adm_reset_my_test_balance")
async def adm_reset_my_test_balance_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    tid = str(callback.from_user.id)
    try:
        DatabaseManager.execute_query(
            """UPDATE users
               SET bot_balance = 0, bonus_balance = 0, game_bonus_amount = 0, bonus_base_balance = 0
               WHERE telegram_id = %s""",
            (tid,)
        )
        DatabaseManager.execute_query(
            "DELETE FROM transactions WHERE user_telegram_id = %s AND (payment_method IN ('game', 'test', 'sandbox') OR reviewed_by = %s OR reviewed_by = 'auto_syriatel')",
            (tid, str(callback.from_user.id))
        )
        if hasattr(DatabaseManager, 'invalidate_settings_cache'):
            DatabaseManager.invalidate_settings_cache()
        await safe_answer_callback(callback, "✅ تم تصفير حسابك الاختباري وتنظيف المعاملات التجريبية بنجاح!", show_alert=True)
        await caesar_control_panel(callback)
    except Exception as e:
        logger.error(f"Reset test balance error: {e}")
        await safe_answer_callback(callback, "❌ تعذر تصفير الرصيد الاختباري", show_alert=True)


@router.callback_query(F.data == "adm_reset_all_db")
async def adm_reset_all_db_confirm(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_edit_text(
        callback.message,
        "⚠️ <b>تنبيه حاسم: تصفير شامل لكل قاعدة البيانات (إنهاء المرحلة التجريبية)</b>\n\n"
        "هل أنت متأكد من رغبتك في تصفير مسح جميع الأرصدة، وسجلات الإيداع والسحب، وسجلات الهدايا والحضور والعجلة، وإجمالي الإيداعات ومستويات VIP لجميع المستخدمين في قاعدة البيانات؟\n\n"
        "🛡️ <i>سيتم الاحتفاظ بحسابات وأسماء المستخدمين وربطهم بـ iChancy كما هي بأمان تام، مع إعادة كل الأرصدة والبيانات إلى الصفر!</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ نعم، انتقل للتأكيد النهائي", callback_data="adm_reset_all_db_step2")],
            [InlineKeyboardButton(text="❌ إلغاء وعودة", callback_data="caesar_control_panel")]
        ]),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data == "adm_reset_all_db_step2")
async def adm_reset_all_db_confirm_step2(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_edit_text(
        callback.message,
        "🚨 <b>التأكيد النهائي والأخير (Double Confirmation)</b>\n\n"
        "أنت على وشك تنفيذ تصفير كامل ومسح لجميع الجداول المالية والإحصائية في قاعدة البيانات للبدء من الصفر.\n"
        "هل تنفذ أمر التصفير الشامل الآن؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💥 تنفيذ التصفير الشامل الآن (EXECUTE)", callback_data="adm_reset_all_db_confirmed")],
            [InlineKeyboardButton(text="❌ تراجع وإلغاء", callback_data="caesar_control_panel")]
        ]),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data == "adm_reset_all_db_confirmed")
async def adm_reset_all_db_execute(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_answer_callback(callback, "⏳ جاري تنفيذ التصفير الشامل لكافة الخزائن والجداول...", show_alert=True)
    try:
        # 1) تصفير كل أرصدة ومؤشرات المستخدمين (مع الحفاظ على معلومات الدخول والربط والآيدي)
        DatabaseManager.execute_query("""
            UPDATE users SET 
                bot_balance = 0,
                game_balance = 0,
                bonus_balance = 0,
                game_bonus_amount = 0,
                bonus_base_balance = 0,
                total_deposits = 0,
                vip_tier = 0,
                affiliate_balance = 0,
                cashback_pending_balance = 0,
                checkin_pending_balance = 0;
        """)
        # 2) تفريغ جميع جداول الحركات والمعاملات والتاريخ بالكامل باستخدام TRUNCATE أو DELETE
        try:
            DatabaseManager.execute_query("""
                TRUNCATE TABLE transactions, gifts, wheel_spins, daily_checkins, 
                               cashback_payouts, affiliate_weekly_commissions, 
                               referral_commissions, prediction_entries, contest_entries RESTART IDENTITY CASCADE;
            """)
        except Exception:
            # احتياط آمن في حال عدم وجود بعض الجداول أو صلاحية TRUNCATE
            for tbl in ["transactions", "gifts", "wheel_spins", "daily_checkins", "cashback_payouts", "affiliate_weekly_commissions", "referral_commissions", "prediction_entries", "contest_entries"]:
                try:
                    DatabaseManager.execute_query(f"DELETE FROM {tbl};")
                except Exception:
                    pass

        # 3) تصفير إحصائيات رصيد الوكيل في إعدادات البوت
        DatabaseManager.execute_query("UPDATE bot_settings SET agent_balance = 0 WHERE id = 1;")
        if hasattr(DatabaseManager, 'invalidate_settings_cache'):
            DatabaseManager.invalidate_settings_cache()
        
        await safe_edit_text(
            callback.message,
            "💥 <b>تم التصفير الشامل بنجاح 100% (Beta Reset Completed)!</b>\n\n"
            "✅ تم تصفير جميع الأرصدة النقديّة والمكافآت وإجمالي الإيداعات ومستويات VIP لجميع المستخدمين إلى <code>0 ل.س</code>.\n"
            "✅ تم مسح جميع سجلات الإيداع والسحب والهدايا والحضور والعجلة والكاش باك وتصفير الجداول بالكامل.\n"
            "🛡️ تم الاحتفاظ بحسابات وأسماء المستخدمين وربطهم بـ iChancy كما هي بأمان.\n\n"
            "<i>النظام الآن مصَفّر بالكامل وجاهز للانطلاق الفعلي بعد انتهاء المرحلة التجريبية!</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 عودة للوحة التحكم الرئيسية", callback_data="caesar_control_panel")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Reset all DB error: {e}", exc_info=True)
        await safe_edit_text(
            callback.message,
            f"❌ تعذر إتمام التصفير الشامل:\n<code>{str(e)}</code>",
            parse_mode="HTML"
        )


@router.message(Command("reset_db"))
async def reset_db_cmd(message: Message):
    if not is_admin_user(message.from_user.id):
        return
    await message.answer(
        "⚠️ <b>تصفير شامل لكل قاعدة البيانات (إنهاء المرحلة التجريبية)</b>\n\nاضغط للبدء بخطوات التأكيد المزدوج:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑️ نعم، صَفّر كل الأرصدة والتاريخ الآن", callback_data="adm_reset_all_db")],
            [InlineKeyboardButton(text="❌ إلغاء وعودة", callback_data="caesar_control_panel")]
        ]),
        parse_mode="HTML"
    )


@router.message(Command("admin"))
async def admin_panel_cmd(message: Message):
    if not is_admin_user(message.from_user.id):
        await message.answer("❌ عذراً، لا تمتلك صلاحيات الأدمن للدخول لهذه القائمة!")
        return
    s = await _gather_dashboard_stats()
    text = build_admin_dashboard_text(
        s['bot_settings'], s['is_cookie_alive'], s['total_bot_balance'],
        s['pending'], s['recent'], s['total_users'], s['new_users'],
        s['today_tx'], s['approved_volume']
    )
    await message.answer(text, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")

@router.callback_query(F.data.startswith("approve_dep_"))
async def approve_dep_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    tx_id = int(callback.data.split("_")[-1])
    tx = repo.get_transaction_by_id(tx_id)
    if not tx or tx.get('status') != 'pending':
        await safe_answer_callback(callback, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
        return

    # 🆕 الرد على الـ callback فوراً (قبل أي عملية شبكة بطيئة) لتفادي خطأ "query is too old"
    await safe_answer_callback(callback, "⏳ جاري قبول الطلب...")

    # حساب البونص أولاً (قراءة فقط، آمنة خارج المعاملة)
    deposit_amount = int(float(tx['amount']))

    # 1) أفضل بونص عام/فلاش فقط (لا يتراكمان)
    bonus_info = repo.calculate_best_deposit_bonus(deposit_amount, tx.get('payment_method'))
    public_bonus_amount = int(bonus_info.get('bonus_amount') or 0)
    bonus_rule = bonus_info.get('rule')

    # 2) بونص VIP مستمر حسب طبقة المستخدم الحالية قبل هذا الإيداع
    vip_deposit_bonus = 0
    vip_deposit_pct = 0
    vip_current_tier_name = None
    vip_upgrade = {'upgraded': False}
    try:
        vip_settings = repo.get_vip_settings()
        tiers = vip_settings.get('tiers', [])
        if vip_settings.get('vip_enabled', True) and tiers:
            total_before = repo.get_user_total_deposits(tx['user_telegram_id'])
            old_vip_info = repo.get_vip_tier_info(total_before, tiers)
            new_vip_info = repo.get_vip_tier_info(total_before + deposit_amount, tiers)
            vip_deposit_pct = float(old_vip_info.get('current_bonus_pct') or 0)
            vip_current_tier_name = old_vip_info.get('tier_names', [''])[old_vip_info.get('current_index', 0)]
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
        logger.error(f"VIP pre-calc error: {e}")
        vip_upgrade = {'upgraded': False}

    vip_upgrade_reward = int(vip_upgrade.get('reward') or 0) if vip_upgrade.get('upgraded') else 0
    bonus_amount = public_bonus_amount + vip_deposit_bonus + vip_upgrade_reward
    # ملاحظة: الإيداع والبونص لم يعودا يُجمعان — الإيداع → bot_balance ، كل البونصات → bonus_balance

    # 🔒 اعتماد ذري كامل (Update 4): قفل + إضافة على الرصيد الفعلي + تعليم المعاملة
    # في معاملة واحدة — يمنع 'الكتابة فوق' والقبول المزدوج.
    result = repo.approve_deposit_atomic(
        telegram_id=tx['user_telegram_id'],
        deposit_amount=deposit_amount,
        bonus_amount=bonus_amount,
        tx_id=tx_id,
        reviewed_by=callback.from_user.id,
        new_vip_tier=int(vip_upgrade.get('new_tier_index')) if vip_upgrade.get('upgraded') else None
    )
    if not result.get('ok'):
        logger.error(f"approve_deposit_atomic failed for tx #{tx_id}: {result.get('reason')}")
        return
    if result.get('already_approved'):
        await safe_answer_callback(callback, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
        return

    new_balance = int(result.get('new_balance') or 0)

    # 🏆 تثبيت طبقة VIP الجديدة بعد قبول الإيداع (المكافأة احتُسبت ضمن bonus_amount أعلاه)
    try:
        if vip_upgrade.get('upgraded'):
            DatabaseManager.execute_query(
                "UPDATE users SET vip_tier = %s WHERE telegram_id = %s",
                (int(vip_upgrade.get('new_tier_index') or 0), str(tx['user_telegram_id']))
            )
            logger.info(f"VIP Upgrade for user {tx['user_telegram_id']} to {vip_upgrade.get('new_tier')}")
            try:
                await callback.bot.send_message(
                    chat_id=tx['user_telegram_id'],
                    text=(
                        f"🎉 <b>مبروك! تمت ترقيتك إلى {vip_upgrade.get('new_tier')}!</b>\n\n"
                        f"💎 مكافأة الترقية: <code>{vip_upgrade_reward:,} ل.س</code>\n"
                        f"(أُضيفت لرصيد المكافآت 🎁 للاستخدام في اللعبة)"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Failed to notify VIP upgrade: {e}")
    except Exception as e:
        logger.error(f"VIP upgrade update error: {e}")

    # حساب صافي مخاطرة البونص للأدمن: البونص ناقص ما يُسترد من عمولته لو سُحب.
    # (عمولة السحب تُسترد نسبةً فقط من البونص، فلا تغطّيه — راجع المنطق المالي.)
    try:
        _wc = float(repo.get_bot_settings().get('withdraw_commission') or 0)
    except Exception:
        _wc = 0.0
    bonus_recovered_by_commission = int(bonus_amount * (_wc / 100.0)) if bonus_amount > 0 else 0
    bonus_net_risk = int(bonus_amount) - bonus_recovered_by_commission

    if bonus_amount > 0:
        user_bonus_parts = []
        log_bonus_parts = []
        if public_bonus_amount > 0 and bonus_rule:
            user_bonus_parts.append(f"🎁 <b>بونص العرض:</b> <code>{public_bonus_amount:,} SYP</code> — <code>{bonus_rule.get('title')}</code>")
            log_bonus_parts.append(f"🎁 بونص العرض: <code>{public_bonus_amount:,} SYP</code> — <code>{bonus_rule.get('title')}</code>")
        if vip_deposit_bonus > 0:
            user_bonus_parts.append(f"🏆 <b>بونص VIP {vip_current_tier_name or ''}:</b> <code>{vip_deposit_bonus:,} SYP</code> (<code>{vip_deposit_pct:g}%</code>)")
            log_bonus_parts.append(f"🏆 بونص VIP: <code>{vip_deposit_bonus:,} SYP</code> (<code>{vip_deposit_pct:g}%</code>)")
        if vip_upgrade_reward > 0:
            user_bonus_parts.append(f"🎉 <b>مكافأة ترقية VIP:</b> <code>{vip_upgrade_reward:,} SYP</code> — <code>{vip_upgrade.get('new_tier')}</code>")
            log_bonus_parts.append(f"🎉 مكافأة ترقية VIP: <code>{vip_upgrade_reward:,} SYP</code> — <code>{vip_upgrade.get('new_tier')}</code>")

        bonus_user_line = (
            "\n" + "\n".join(user_bonus_parts) +
            f"\n🎁 <b>إجمالي البونص المضاف:</b> <code>{bonus_amount:,} SYP</code>" +
            "\n🎁 <b>أُضيف إلى رصيد المكافآت</b> (للاستخدام في اللعبة)"
        )
        risk_flag = "🔴" if bonus_net_risk > bonus_recovered_by_commission else "🟡"
        bonus_log_line = (
            "\n" + "\n".join(log_bonus_parts) +
            f"\n🎁 إجمالي البونص: <code>{bonus_amount:,} SYP</code> → رصيد المكافآت (مقيّد)" +
            f"\n{risk_flag} صافي مخاطرة البونص: <code>{bonus_net_risk:,} SYP</code> "
            f"(يُسترد من العمولة: <code>{bonus_recovered_by_commission:,}</code>)"
        )
    else:
        bonus_user_line = ""
        bonus_log_line = ""

    # 🤝 الإحالات: تفعيل الإحالة عند أول إيداع مقبول، ثم إضافة العمولة فوراً إذا كان صاحب الإحالة مؤهلاً.
    referral_log_line = ""
    if repo.are_referrals_enabled():
        newly_activated_referrer = repo.activate_referral_if_needed(tx['user_telegram_id'])
        referrer_id = newly_activated_referrer or repo.get_referrer_for_referred(tx['user_telegram_id'])
        if referrer_id:
            active_count = repo.get_active_referrals_count(referrer_id)
            if newly_activated_referrer:
                try:
                    await callback.bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            "🎉 <b>إحالة نشطة جديدة!</b>\n\n"
                            "قام أحد أصدقائك بإكمال أول إيداع مقبول.\n"
                            f"✅ إحالاتك النشطة الآن: <code>{active_count}</code>\n"
                            f"📈 نسبتك الحالية: <code>{repo.get_referral_percent_by_active_count(active_count)}%</code>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.warning(f"Failed to notify referrer activation {referrer_id}: {e}")

            # النظام الجديد: لا تُدفع عمولة على الإيداع نفسه.
            # الإحالة تُفعّل هنا فقط، وتُصرف أرباح المحيل أسبوعياً من خسارة المحالين في اللعبة.
            referral_log_line = (
                f"\n🤝 الإحالة: <code>{referrer_id}</code> مرتبطة بهذا المستخدم. "
                "سيتم احتساب أرباح المحيل أسبوعياً بناءً على خسارة المحال في اللعبة."
            )


    # تحديث رسالة قناة الإيداع مع الحفاظ على تفاصيل الطلب الأصلية
    deposit_status = (
        f"✅ <b>تم قبول طلب الإيداع #{tx_id} بنجاح.</b>\n"
        f"💰 <b>مبلغ الإيداع:</b> <code>{deposit_amount:,} SYP</code>"
        f"{bonus_log_line}"
        f"{referral_log_line}\n"
        f"💵 <b>الرصيد النقدي الجديد:</b> <code>{new_balance:,} SYP</code>"
    )
    original_text = getattr(callback.message, 'html_text', None) or getattr(callback.message, 'caption', None) or ''
    await safe_edit_status_message(
        callback.message,
        append_final_status(original_text, deposit_status),
        parse_mode="HTML"
    )
    await _send_with_retry(
        notify_user_about_transaction(
            callback.bot,
            tx,
            f"✅ <b>تم قبول طلب الإيداع الخاص بك</b>\n\n"
            f"📌 رقم الطلب: <code>#{tx_id}</code>\n"
            f"💰 مبلغ الإيداع: {_fmt_syp_dual(deposit_amount)}"
            f"{bonus_user_line}\n"
            f"💵 رصيدك النقدي: {_fmt_syp_dual(new_balance)}"
        ),
        label="notify_user(deposit approve)"
    )
    await _send_with_retry(
        send_log_message(
            callback.bot,
            f"✅ <b>تم قبول إيداع</b>\n\n"
            f"📌 الطلب: <code>#{tx_id}</code>\n"
            f"👤 المستخدم: <code>{tx['user_telegram_id']}</code>\n"
            f"💰 مبلغ الإيداع: {_fmt_syp_dual(deposit_amount)}"
            f"{bonus_log_line}"
            f"{referral_log_line}\n"
            f"💵 الرصيد النقدي الجديد: {_fmt_syp_dual(new_balance)}"
        ),
        label="send_log(deposit approve)"
    )


@router.callback_query(F.data.startswith("reject_dep_"))
async def reject_dep_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    tx_id = int(callback.data.split("_")[-1])
    tx = repo.get_transaction_by_id(tx_id)
    if not tx or tx.get('status') != 'pending':
        await safe_answer_callback(callback, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
        return
    await safe_edit_text(
        callback.message,
        f"❌ اختر سبب رفض طلب الإيداع <code>#{tx_id}</code>",
        reply_markup=get_rejection_reason_keyboard("dep_reject_reason", tx_id),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("approve_withdraw_"))
async def approve_withdraw_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    tx_id = int(callback.data.split("_")[-1])
    tx = repo.get_transaction_by_id(tx_id)
    if not tx or tx.get('status') != 'pending':
        await safe_answer_callback(callback, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
        return

    # 🆕 الرد على الـ callback فوراً لتفادي خطأ "query is too old"
    await safe_answer_callback(callback, "⏳ جاري اعتماد السحب...")

    # العمليات السريعة (قاعدة البيانات)
    repo.update_transaction_status(tx_id, 'approved', reviewed_by=callback.from_user.id)

    # تحديث رسالة قناة السحب مع إبقاء كل التفاصيل المالية، بدل اختصارها بسطر واحد.
    original_text = getattr(callback.message, 'html_text', None) or getattr(callback.message, 'caption', None) or ''
    final_text = append_final_status(original_text, "✅ <b>تم تأكيد التحويل.</b>") if original_text else build_withdraw_approved_details(tx)
    await safe_edit_status_message(
        callback.message,
        final_text,
        parse_mode="HTML"
    )
    await _send_with_retry(
        notify_user_about_transaction(
            callback.bot,
            tx,
            f"✅ <b>تمت الموافقة على طلب السحب الخاص بك</b>\n\n📌 رقم الطلب: <code>#{tx_id}</code>\n💰 المبلغ: <code>{int(float(tx['amount'])):,} SYP</code>"
        ),
        label="notify_user(withdraw approve)"
    )
    await _send_with_retry(
        send_log_message(
            callback.bot,
            f"✅ <b>تم قبول سحب</b>\n\n📌 الطلب: <code>#{tx_id}</code>\n👤 المستخدم: <code>{tx['user_telegram_id']}</code>\n💰 المبلغ: <code>{int(float(tx['amount'])):,} SYP</code>"
        ),
        label="send_log(withdraw approve)"
    )


@router.callback_query(F.data.startswith("reject_withdraw_"))
async def reject_withdraw_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    tx_id = int(callback.data.split("_")[-1])
    tx = repo.get_transaction_by_id(tx_id)
    if not tx or tx.get('status') != 'pending':
        await safe_answer_callback(callback, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
        return
    await safe_edit_text(
        callback.message,
        f"❌ اختر سبب رفض طلب السحب <code>#{tx_id}</code>",
        reply_markup=get_rejection_reason_keyboard("withdraw_reject_reason", tx_id),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.message(HasPendingRejection())
async def process_custom_rejection(message: Message):
    """✅ معالج الرفض المخصص - يعمل من محادثة المشرف الخاصة (يعبر مشكلة FSM عبر القنوات)."""
    pending = repo.get_pending_rejection(message.from_user.id)
    if not pending:
        return

    reason = (message.text or '').strip()
    if reason.startswith('/'):
        if reason.lower() in ('/cancel', '/cancel_rejection', '/home', '/admin'):
            repo.clear_pending_rejection(message.from_user.id)
            await message.answer("❌ تم إلغاء عملية الرفض المخصص.")
        return
    if not reason:
        await message.answer("❌ الرجاء إرسال سبب رفض واضح (نص):")
        return

    tx_id = pending['tx_id']
    tx_type = pending['tx_type']
    tx = repo.get_transaction_by_id(tx_id)
    if not tx or tx.get('status') != 'pending':
        await message.answer("⚠️ هذا الطلب تمت معالجته مسبقاً أو لم يعد متاحاً.")
        repo.clear_pending_rejection(message.from_user.id)
        return

    # معالجة رفض مشاركة المسابقة
    if tx_type == 'contest_entry':
        repo.reject_contest_entry(tx_id, reviewed_by=message.from_user.id)

        # إشعار المستخدم
        try:
            entry_data = DatabaseManager.execute_query_dict(
                "SELECT * FROM contest_entries WHERE id = %s",
                (tx_id,),
                fetch='one'
            )
            if entry_data:
                user_telegram_id = entry_data.get('user_telegram_id')
                contest_id = entry_data.get('contest_id')
                contest = repo.get_contest(contest_id) if contest_id else None
                contest_title = contest.get('title') if contest else 'المسابقة'

                user_text = (
                    "━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "❌ <b>تم رفض مشاركتك</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"👑 <b>المسابقة:</b> {contest_title}\n"
                    f"📝 <b>السبب:</b> {reason}\n\n"
                    "💡 يمكنك المحاولة مجدداً بمشاركة صحيحة.\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━"
                )
                await notify_user_about_transaction(message.bot, {'user_telegram_id': user_telegram_id}, user_text)
        except Exception as e:
            logger.warning(f"Could not notify user about custom contest rejection: {e}")

        # تحديث رسالة القناة
        if pending.get('channel_chat_id') and pending.get('channel_message_id'):
            try:
                await message.bot.edit_message_text(
                    chat_id=pending['channel_chat_id'],
                    message_id=pending['channel_message_id'],
                    text=f"❌ <b>تم رفض المشاركة #{tx_id}</b>\n📝 السبب: {reason}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Could not update channel message after custom contest rejection: {e}")

        await message.answer(f"✅ تم رفض المشاركة <code>#{tx_id}</code> وحفظ السبب وإشعار المستخدم بنجاح.", parse_mode="HTML")
        repo.clear_pending_rejection(message.from_user.id)

        # إرسال سجل
        await send_log_message(
            message.bot,
            f"❌ <b>تم رفض مشاركة مسابقة (سبب مخصص)</b>\n\n"
            f"📌 المشاركة: <code>#{tx_id}</code>\n"
            f"📝 السبب: {reason}\n"
            f"👤 المشرف: <code>{message.from_user.id}</code>"
        )
        return

    repo.update_transaction_status(tx_id, 'rejected', reviewed_by=message.from_user.id)
    repo.update_transaction_rejection_reason(tx_id, reason)

    if tx_type == 'withdraw_bot':
        # 🔒 إعادة ذرّية للرصيد (Update 4) — تُضاف على الرصيد الفعلي مباشرة
        repo.credit_balance_atomic(tx['user_telegram_id'], int(float(tx['amount'])))
        user_text = (
            f"❌ <b>تم رفض طلب السحب الخاص بك</b>\n\n"
            f"📌 رقم الطلب: <code>#{tx_id}</code>\n"
            f"📝 السبب: {reason}\n"
            f"💎 تم إعادة الرصيد إلى حسابك: <code>{int(float(tx['amount'])):,} SYP</code>"
        )
        log_text = (
            f"❌ <b>تم رفض سحب (سبب مخصص)</b>\n\n"
            f"📌 الطلب: <code>#{tx_id}</code>\n"
            f"👤 المستخدم: <code>{tx['user_telegram_id']}</code>\n"
            f"💰 المبلغ المعاد: <code>{int(float(tx['amount'])):,} SYP</code>\n"
            f"📝 السبب: {reason}"
        )
    else:
        user_text = (
            f"❌ <b>تم رفض طلب الإيداع الخاص بك</b>\n\n"
            f"📌 رقم الطلب: <code>#{tx_id}</code>\n"
            f"📝 السبب: {reason}"
        )
        log_text = (
            f"❌ <b>تم رفض إيداع (سبب مخصص)</b>\n\n"
            f"📌 الطلب: <code>#{tx_id}</code>\n"
            f"👤 المستخدم: <code>{tx['user_telegram_id']}</code>\n"
            f"📝 السبب: {reason}"
        )

    # إشعار المستخدم + السجل
    await notify_user_about_transaction(message.bot, tx, user_text)
    await send_log_message(message.bot, log_text)

    # تحديث رسالة القناة الأصلية لعرض الحالة النهائية
    if pending.get('channel_chat_id') and pending.get('channel_message_id'):
        try:
            await message.bot.edit_message_text(
                chat_id=pending['channel_chat_id'],
                message_id=pending['channel_message_id'],
                text=f"❌ <b>تم رفض الطلب #{tx_id}</b>\n📝 السبب: {reason}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not update channel message after rejection: {e}")

    await message.answer(f"✅ تم رفض الطلب <code>#{tx_id}</code> وحفظ السبب وإشعار المستخدم بنجاح.", parse_mode="HTML")
    repo.clear_pending_rejection(message.from_user.id)


@router.callback_query(F.data == "adm_cancel_rejection")
async def adm_cancel_rejection_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        return
    repo.clear_pending_rejection(callback.from_user.id)
    await safe_edit_text(callback.message, "❌ تم إلغاء طلب الرفض المخصص.", parse_mode="HTML")
    await safe_answer_callback(callback, "تم الإلغاء")




@router.callback_query(F.data.startswith("dep_reject_reason|"))
async def dep_reject_reason_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    _, reason_code, tx_id_text = callback.data.split("|")
    tx_id = int(tx_id_text)
    tx = repo.get_transaction_by_id(tx_id)
    if not tx or tx.get('status') != 'pending':
        await safe_answer_callback(callback, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
        return
    if reason_code == 'custom':
        repo.set_pending_rejection(callback.from_user.id, tx_id, 'deposit_bot', callback.message.chat.id, callback.message.message_id)
        await safe_edit_text(callback.message, f"✍️ <b>طلب رفض مخصص للإيداع #{tx_id}</b>\n\nأرسل سبب الرفض في <b>محادثتك الخاصة</b> مع البوت الآن 👇", parse_mode="HTML")
        try:
            await callback.bot.send_message(
                callback.from_user.id,
                f"✍️ <b>سبب رفض مخصص لطلب الإيداع #{tx_id}</b>\n\nأرسل الآن نص السبب (سيُرسل للمستخدم فوراً):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء الرفض المخصص", callback_data="adm_cancel_rejection")]]),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not DM admin for custom rejection: {e}")
        await safe_answer_callback(callback, "أرسل السبب في المحادثة الخاصة ✉️")
        return
    reason = map_reason_code(reason_code)
    # 🆕 الرد على الـ callback فوراً
    await safe_answer_callback(callback, "⏳ جاري رفض الإيداع...")
    repo.update_transaction_status(tx_id, 'rejected', reviewed_by=callback.from_user.id)
    repo.update_transaction_rejection_reason(tx_id, reason)
    await safe_edit_text(callback.message, f"❌ تم رفض طلب الإيداع <code>#{tx_id}</code>\n📝 السبب: {reason}", parse_mode="HTML")
    await _send_with_retry(
        notify_user_about_transaction(
            callback.bot,
            tx,
            f"❌ <b>تم رفض طلب الإيداع الخاص بك</b>\n\n📌 رقم الطلب: <code>#{tx_id}</code>\n📝 السبب: {reason}"
        ),
        label="notify_user(dep reject)"
    )
    await _send_with_retry(
        send_log_message(
            callback.bot,
            f"❌ <b>تم رفض إيداع</b>\n\n📌 الطلب: <code>#{tx_id}</code>\n👤 المستخدم: <code>{tx['user_telegram_id']}</code>\n📝 السبب: {reason}"
        ),
        label="send_log(dep reject)"
    )


@router.callback_query(F.data.startswith("withdraw_reject_reason|"))
async def withdraw_reject_reason_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    _, reason_code, tx_id_text = callback.data.split("|")
    tx_id = int(tx_id_text)
    tx = repo.get_transaction_by_id(tx_id)
    if not tx or tx.get('status') != 'pending':
        await safe_answer_callback(callback, "⚠️ هذا الطلب تمت معالجته مسبقاً.", show_alert=True)
        return
    if reason_code == 'custom':
        repo.set_pending_rejection(callback.from_user.id, tx_id, 'withdraw_bot', callback.message.chat.id, callback.message.message_id)
        await safe_edit_text(callback.message, f"✍️ <b>طلب رفض مخصص للسحب #{tx_id}</b>\n\nأرسل سبب الرفض في <b>محادثتك الخاصة</b> مع البوت الآن 👇", parse_mode="HTML")
        try:
            await callback.bot.send_message(
                callback.from_user.id,
                f"✍️ <b>سبب رفض مخصص لطلب السحب #{tx_id}</b>\n\nأرسل الآن نص السبب (سيُرسل للمستخدم فوراً):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء الرفض المخصص", callback_data="adm_cancel_rejection")]]),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not DM admin for custom rejection: {e}")
        await safe_answer_callback(callback, "أرسل السبب في المحادثة الخاصة ✉️")
        return
    reason = map_reason_code(reason_code)
    # 🆕 الرد على الـ callback فوراً
    await safe_answer_callback(callback, "⏳ جاري رفض السحب...")
    repo.update_transaction_status(tx_id, 'rejected', reviewed_by=callback.from_user.id)
    repo.update_transaction_rejection_reason(tx_id, reason)
    # 🔒 إعادة ذرّية للرصيد (Update 4) — تُضاف على الرصيد الفعلي مباشرة
    repo.credit_balance_atomic(tx['user_telegram_id'], int(float(tx['amount'])))
    await safe_edit_text(callback.message, f"❌ تم رفض طلب السحب <code>#{tx_id}</code>\n📝 السبب: {reason}", parse_mode="HTML")
    await _send_with_retry(
        notify_user_about_transaction(
            callback.bot,
            tx,
            f"❌ <b>تم رفض طلب السحب الخاص بك</b>\n\n📌 رقم الطلب: <code>#{tx_id}</code>\n📝 السبب: {reason}\n💎 تم إعادة الرصيد إلى حسابك: <code>{int(float(tx['amount'])):,} SYP</code>"
        ),
        label="notify_user(withdraw reject)"
    )
    await _send_with_retry(
        send_log_message(
            callback.bot,
            f"❌ <b>تم رفض سحب</b>\n\n📌 الطلب: <code>#{tx_id}</code>\n👤 المستخدم: <code>{tx['user_telegram_id']}</code>\n💰 المبلغ المعاد: <code>{int(float(tx['amount'])):,} SYP</code>\n📝 السبب: {reason}"
        ),
        label="send_log(withdraw reject)"
    )


@router.callback_query(F.data == "adm_payment_addresses")
async def adm_payment_addresses_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await state.clear()

    addresses = repo.get_all_payment_addresses()
    text = "💳 <b>إدارة عناوين الإيداع</b>\n\n"
    rows = []
    for item in addresses:
        source = "لوحة الأدمن" if item['source'] == 'database' else "Render"
        text += (
            f"{item['label']}\n"
            f"<code>{item['address'] or 'غير محدد'}</code>\n"
            f"📌 المصدر: <b>{source}</b>\n\n"
        )
        rows.append([InlineKeyboardButton(text=f"✏️ تعديل {item['label']}", callback_data=f"adm_pay_edit_{item['method']}")])

    rows.append([InlineKeyboardButton(text="♻️ إعادة عنوان لقيمة Render", callback_data="adm_pay_reset_menu")])
    rows.append([InlineKeyboardButton(text="🔙 عودة للوحة الأدمن", callback_data="back_to_admin_main")])
    await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("adm_pay_edit_"))
async def adm_pay_edit_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    method = callback.data.replace("adm_pay_edit_", "", 1)
    if method not in repo.PAYMENT_METHOD_LABELS:
        await safe_answer_callback(callback, "⚠️ طريقة الدفع غير معروفة.", show_alert=True)
        return

    label = repo.PAYMENT_METHOD_LABELS[method]
    current = repo.get_payment_address(method)
    fallback = repo.get_payment_address_fallback(method)
    source = "لوحة الأدمن" if repo.get_payment_address_source(method) == 'database' else "Render"
    await state.update_data(payment_method=method)
    await safe_edit_text(
        callback.message,
        f"✏️ <b>تعديل عنوان الإيداع</b>\n\n"
        f"💳 الطريقة: <b>{label}</b>\n"
        f"📌 المصدر الحالي: <b>{source}</b>\n"
        f"📍 العنوان الحالي:\n<code>{current or 'غير محدد'}</code>\n\n"
        f"🔁 قيمة Render الاحتياطية:\n<code>{fallback or 'غير محددة'}</code>\n\n"
        "أرسل العنوان/الأرقام الجديدة الآن كما تريد أن تظهر للمستخدمين 👇",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.editing_payment_address)
    await safe_answer_callback(callback)


@router.message(AdminStates.editing_payment_address)
async def process_payment_address_update(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    data = await state.get_data()
    method = data.get('payment_method')
    if method not in repo.PAYMENT_METHOD_LABELS:
        await state.clear()
        await message.answer("⚠️ انتهت صلاحية العملية. افتح لوحة عناوين الإيداع من جديد.")
        return

    address = (message.text or '').strip()
    if not address:
        await message.answer("❌ الرجاء إرسال عنوان/رقم صحيح غير فارغ:")
        return
    if len(address) > 900:
        await message.answer("❌ العنوان طويل جداً. الرجاء اختصاره وإعادة الإرسال:")
        return

    repo.set_payment_address(method, address, updated_by=message.from_user.id)
    label = repo.PAYMENT_METHOD_LABELS[method]
    await message.answer(
        f"✅ تم تحديث عنوان <b>{label}</b> بنجاح.\n\n"
        f"العنوان الجديد الذي سيظهر للمستخدمين فوراً:\n<code>{address}</code>",
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "adm_pay_reset_menu")
async def adm_pay_reset_menu_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    rows = []
    text = "♻️ <b>إعادة عنوان إيداع لقيمة Render</b>\n\nاختر الطريقة التي تريد حذف تعديل لوحة الأدمن عنها:\n\n"
    for method, label in repo.PAYMENT_METHOD_LABELS.items():
        source = repo.get_payment_address_source(method)
        source_text = "لوحة الأدمن" if source == 'database' else "Render أصلاً"
        text += f"{label}: <b>{source_text}</b>\n"
        rows.append([InlineKeyboardButton(text=f"♻️ {label}", callback_data=f"adm_pay_reset_{method}")])
    rows.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="adm_payment_addresses")])
    await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("adm_pay_reset_"))
async def adm_pay_reset_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    method = callback.data.replace("adm_pay_reset_", "", 1)
    if method not in repo.PAYMENT_METHOD_LABELS:
        await safe_answer_callback(callback, "⚠️ طريقة الدفع غير معروفة.", show_alert=True)
        return
    repo.reset_payment_address(method)
    label = repo.PAYMENT_METHOD_LABELS[method]
    fallback = repo.get_payment_address_fallback(method)
    await safe_edit_text(
        callback.message,
        f"✅ تم إعادة <b>{label}</b> إلى قيمة Render الاحتياطية.\n\n"
        f"القيمة الحالية الآن:\n<code>{fallback or 'غير محددة'}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 عناوين الإيداع", callback_data="adm_payment_addresses")],
            [InlineKeyboardButton(text="👑 لوحة الأدمن", callback_data="back_to_admin_main")]
        ]),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback, "تمت الإعادة لقيمة Render.")

@router.callback_query(F.data == "adm_rates_menu")
async def adm_rates_menu_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    bot_settings = repo.get_bot_settings()
    text = (
        "💱 <b>تعديل أسعار الصرف الحالية:</b>\n\n"
        f"1️⃣ <b>سعر صرف نقاط iChancy (NSP):</b><code>1 NSP = {int(bot_settings['exchange_rate']):,} ل.س</code>\n"
        f"2️⃣ <b>سعر شراء الدولار (عند الإيداع):</b><code>{float(bot_settings['usd_buy_rate']):,.2f} ل.س</code>\n"
        f"3️⃣ <b>سعر بيع الدولار (عند السحب):</b><code>{float(bot_settings['usd_sell_rate']):,.2f} ل.س</code>"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تعديل سعر صرف اللعبة (NSP)", callback_data="edit_rate_game")],
        [
            InlineKeyboardButton(text="✏️ تعديل سعر الإيداع (شراء)", callback_data="edit_rate_buy"),
            InlineKeyboardButton(text="✏️ تعديل سعر السحب (بيع)", callback_data="edit_rate_sell")
        ],
        [InlineKeyboardButton(text="🔙 عودة للوحة الأدمن", callback_data="back_to_admin_main")]
    ])
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data == "back_to_admin_main")
async def back_to_admin_main_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await state.clear()
    await caesar_control_panel(callback)


@router.callback_query(F.data == "edit_rate_game")
async def edit_rate_game_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await safe_edit_text(callback.message, "✏️ الرجاء إدخال <b>سعر صرف نقاط iChancy (NSP) الجديد بالليرة السورية</b> (أرقام فقط):")
    await state.set_state(AdminStates.entering_exchange_rate)
    await safe_answer_callback(callback)


@router.message(AdminStates.entering_exchange_rate)
async def process_new_exchange_rate(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    text = message.text.strip().replace(',', '')
    if not text.isdigit():
        await message.answer("❌ الرجاء إدخال أرقام صحيحة فقط:")
        return
    rate = int(text)
    repo.update_bot_settings(exchange_rate=rate)
    await message.answer(f"✅ تم تحديث سعر صرف اللعبة بنجاح إلى: <code>{rate:,} ل.س</code> لـ 1 NSP", parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "edit_rate_buy")
async def edit_rate_buy_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await safe_edit_text(callback.message, "✏️ الرجاء إدخال <b>سعر شراء الدولار الجديد عند الإيداع بالليرة السورية</b> (أرقام فقط):")
    await state.set_state(AdminStates.entering_buy_rate)
    await safe_answer_callback(callback)


@router.message(AdminStates.entering_buy_rate)
async def process_new_buy_rate(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    text = message.text.strip().replace(',', '')
    try:
        rate = float(text)
    except ValueError:
        await message.answer("❌ الرجاء إدخال قيمة عددية صحيحة:")
        return
    repo.update_bot_settings(usd_buy_rate=rate)
    await message.answer(f"✅ تم تحديث سعر الشراء عند الإيداع بنجاح إلى: <code>{rate:,} ل.س</code> للدولار", parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "edit_rate_sell")
async def edit_rate_sell_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await safe_edit_text(callback.message, "✏️ الرجاء إدخال <b>سعر بيع الدولار الجديد عند السحب بالليرة السورية</b> (أرقام فقط):")
    await state.set_state(AdminStates.entering_sell_rate)
    await safe_answer_callback(callback)


@router.message(AdminStates.entering_sell_rate)
async def process_new_sell_rate(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    text = message.text.strip().replace(',', '')
    try:
        rate = float(text)
    except ValueError:
        await message.answer("❌ الرجاء إدخال قيمة عددية صحيحة:")
        return
    repo.update_bot_settings(usd_sell_rate=rate)
    await message.answer(f"✅ تم تحديث سعر البيع عند السحب بنجاح إلى: <code>{rate:,} ل.س</code> للدولار", parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "adm_comm_menu")
async def adm_comm_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    bot_settings = repo.get_bot_settings()
    await safe_edit_text(
        callback.message,
        f"🏷️ <b>عمولة السحب الحالية:</b><code>{float(bot_settings['withdraw_commission']):,.2f}%</code>\n\n"
        "الرجاء إدخال نسبة العمولة الجديدة المئوية (أرقام فقط، مثال 10 لتعبر عن 10%):"
    )
    await state.set_state(AdminStates.entering_commission)
    await safe_answer_callback(callback)


@router.message(AdminStates.entering_commission)
async def process_new_commission(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    text = message.text.strip().replace('%', '')
    try:
        comm = float(text)
    except ValueError:
        await message.answer("❌ الرجاء إدخال نسبة مئوية عددية صحيحة:")
        return
    repo.update_bot_settings(withdraw_commission=comm)
    await message.answer(f"✅ تم تحديث نسبة عمولة السحب بنجاح إلى: <code>{comm}%</code>", parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "adm_cookie_menu")
async def adm_cookie_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await safe_edit_text(
        callback.message,
        "🔑 <b>تحديث كوكيز iChancy يدوياً:</b>\n\n"
        "لضمان استقرار العمليات، يرجى تسجيل الدخول الفعلي من متصفحك ونسخ كوكيز الجلسة ولصقها بالكامل هنا 👇:"
    )
    await state.set_state(AdminStates.entering_cookies)
    await safe_answer_callback(callback)


@router.message(AdminStates.entering_cookies)
async def process_new_cookies(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    cookie_str = message.text.strip()
    repo.update_bot_settings(ichancy_cookie=cookie_str)
    repo.update_cookie_timestamp()
    ichancy_api_client.update_headers_and_cookies(cookie_str)
    await message.answer("⏳ جاري فحص ومطابقة الكوكيز الجديدة مع المنصة...")
    is_alive = await ichancy_api_client.check_session_validity()
    if is_alive:
        await message.answer("✅ <b>رائع جداً! الكوكيز حية ونشطة 🟢</b>\nتم حفظ الجلسة الجديدة وتحديث النظام بالكامل بنجاح!\n🕐 سيتم تذكيرك تلقائياً بتحديثها كل 12 ساعة.")
    else:
        await message.answer(
            "⚠️ <b>تنبيه:</b> تم حفظ الكوكيز ولكن <b>فحص الاتصال مع المنصة فشل (EXPIRED 🔴)</b>!\n"
            "يرجى التحقق من أنك قمت بنسخ الكوكيز بعد تسجيل الدخول الفعلي والكامل لشبكة الداشبورد."
        )
    await state.clear()


@router.callback_query(F.data == "adm_agent_bal")
async def adm_agent_bal_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_edit_text(callback.message, "⏳ جاري جلب رصيد محفظة الوكيل من الداشبورد...")
    balance = await ichancy_api_client.get_admin_balance()
    if balance is not None:
        repo.update_bot_settings(agent_balance=int(balance))
        await safe_edit_text(
            callback.message,
            f"🎮 <b>رصيد محفظة الوكيل الحالي على iChancy:</b>\n\n"
            f"💰 الرصيد الفعلي: <code>{balance:,} NSP</code>\n\n"
            "تأكد دائماً من وجود رصيد كافٍ لتلبية طلبات شحن حسابات اللاعبين الفورية.",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
    else:
        await safe_edit_text(
            callback.message,
            "❌ <b>فشل الاتصال بالداشبورد لجلب الرصيد!</b>\n"
            "يرجى التحقق من كوكيز الجلسة عبر خيار تحديث الكوكيز يدوياً.",
            reply_markup=get_admin_keyboard(),
            parse_mode="HTML"
        )
    await safe_answer_callback(callback)


# ================================================================
# 🤝 التحكم بنظام الإحالات
# ================================================================

@router.callback_query(F.data == "adm_referrals_toggle")
async def adm_referrals_toggle_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    enabled = repo.are_referrals_enabled()
    status = "🟢 مفعّلة" if enabled else "🔴 متوقفة"
    text = (
        "🤝 <b>نظام الإحالات</b>\n\n"
        f"الحالة الحالية: <b>{status}</b>\n\n"
        "الشرائح المعتمدة:\n"
        "• 3 إحالات نشطة = 3%\n"
        "• 5 إحالات نشطة = 5%\n"
        "• 10 إحالات نشطة = 10%\n\n"
        "العمولة تُضاف فوراً إلى رصيد صاحب الإحالة عند قبول إيداع تابع مؤهل، وتُحسب على مبلغ الإيداع الأساسي فقط."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔴 إيقاف الإحالات" if enabled else "🟢 تفعيل الإحالات",
            callback_data="adm_referrals_disable" if enabled else "adm_referrals_enable"
        )],
        [InlineKeyboardButton(text="🔙 لوحة التحكم", callback_data="caesar_control_panel")]
    ])
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data == "adm_referrals_enable")
async def adm_referrals_enable_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    repo.set_referrals_enabled(True)
    await safe_answer_callback(callback, "تم تفعيل الإحالات ✅")
    await adm_referrals_toggle_callback(callback)


@router.callback_query(F.data == "adm_referrals_disable")
async def adm_referrals_disable_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    repo.set_referrals_enabled(False)
    await safe_answer_callback(callback, "تم إيقاف الإحالات مؤقتاً 🔴")
    await adm_referrals_toggle_callback(callback)



# ================================================================
# 🎁 البونصات والعروض على طرق الإيداع
# ================================================================

BONUS_PAYMENT_METHOD_LABELS = {
    'all': 'كل طرق الإيداع',
    'syriatel': 'Syriatel Cash',
    'mtn': 'MTN Cash',
    'sham_syp': 'Sham Cash SYP',
    'sham_usd': 'Sham Cash USD',
    'usdt_trc': 'USDT TRC20',
    'usdt_bep': 'USDT BEP20',
}


def get_bonus_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ إنشاء عرض بونص", callback_data="bonus_create")],
        [InlineKeyboardButton(text="📋 العروض الحالية", callback_data="bonus_list")],
        [InlineKeyboardButton(text="🔙 لوحة التحكم", callback_data="caesar_control_panel")]
    ])


@router.callback_query(F.data == "adm_bonus_menu")
async def adm_bonus_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await state.clear()
    active_count = len(repo.get_active_bonus_rules())
    await safe_edit_text(
        callback.message,
        "🎁 <b>البونصات والعروض</b>\n\n"
        "تُطبّق البونصات فقط على <b>طرق الإيداع</b> عند قبول طلب الإيداع من المشرف.\n"
        "إذا انطبق أكثر من عرض، يحصل المستخدم على <b>أعلى بونص فقط</b>.\n\n"
        f"📌 العروض الفعالة حالياً: <code>{active_count}</code>",
        reply_markup=get_bonus_menu_keyboard(),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data == "bonus_create")
async def bonus_create_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await state.clear()
    await safe_edit_text(
        callback.message,
        "➕ <b>إنشاء عرض بونص جديد</b>\n\n"
        "أرسل اسم العرض.\n"
        "مثال: <code>عرض شام كاش 10%</code>",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.entering_bonus_title)
    await safe_answer_callback(callback)


@router.message(AdminStates.entering_bonus_title)
async def process_bonus_title(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    title = (message.text or '').strip()
    if len(title) < 3:
        await message.answer("❌ اسم العرض قصير جداً. أرسل اسماً أوضح:")
        return
    await state.update_data(bonus_title=title[:150])
    await message.answer(
        "📈 أرسل نسبة البونص المئوية.\n"
        "مثال: <code>10</code> تعني 10%",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.entering_bonus_percent)


@router.message(AdminStates.entering_bonus_percent)
async def process_bonus_percent(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    raw = (message.text or '').strip().replace('%', '').replace(',', '.')
    try:
        percent = float(raw)
    except ValueError:
        await message.answer("❌ الرجاء إدخال نسبة رقمية صحيحة. مثال: <code>10</code>", parse_mode="HTML")
        return
    if percent <= 0 or percent > 100:
        await message.answer("❌ النسبة يجب أن تكون أكبر من 0 وأقل أو تساوي 100.")
        return
    await state.update_data(bonus_percent=percent)

    rows = [[InlineKeyboardButton(text=label, callback_data=f"bonus_method_{key}")]
            for key, label in BONUS_PAYMENT_METHOD_LABELS.items()]
    rows.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="adm_bonus_menu")])
    await message.answer(
        "💳 اختر طريقة الإيداع التي يطبق عليها البونص:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("bonus_method_"))
async def bonus_method_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    method = callback.data.replace("bonus_method_", "")
    if method not in BONUS_PAYMENT_METHOD_LABELS:
        await safe_answer_callback(callback, "طريقة غير معروفة", show_alert=True)
        return
    await state.update_data(bonus_payment_method=method)
    await safe_edit_text(
        callback.message,
        "💰 أرسل الحد الأدنى للإيداع بالليرة السورية.\n"
        "مثال: <code>100000</code>\n"
        "أرسل <code>0</code> إذا بدون حد أدنى.",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.entering_bonus_min_amount)
    await safe_answer_callback(callback)


@router.message(AdminStates.entering_bonus_min_amount)
async def process_bonus_min_amount(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    raw = (message.text or '').strip().replace(',', '')
    try:
        min_amount = int(raw)
    except ValueError:
        await message.answer("❌ الرجاء إدخال رقم صحيح. مثال: <code>100000</code>", parse_mode="HTML")
        return
    if min_amount < 0:
        await message.answer("❌ الحد الأدنى لا يمكن أن يكون سالباً.")
        return
    await state.update_data(bonus_min_amount=min_amount)
    await message.answer(
        "🛡️ أرسل الحد الأعلى لقيمة البونص بالليرة السورية.\n"
        "مثال: <code>50000</code>\n"
        "أرسل <code>0</code> إذا بدون حد أعلى.",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.entering_bonus_max_amount)


@router.message(AdminStates.entering_bonus_max_amount)
async def process_bonus_max_amount(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    raw = (message.text or '').strip().replace(',', '')
    try:
        max_amount = int(raw)
    except ValueError:
        await message.answer("❌ الرجاء إدخال رقم صحيح. مثال: <code>50000</code>", parse_mode="HTML")
        return
    if max_amount < 0:
        await message.answer("❌ الحد الأعلى لا يمكن أن يكون سالباً.")
        return

    data = await state.get_data()
    title = data['bonus_title']
    percent = data['bonus_percent']
    method = data['bonus_payment_method']
    min_amount = data['bonus_min_amount']

    rule_id = repo.create_bonus_rule(
        title=title,
        percent=percent,
        payment_method=method,
        min_amount_syp=min_amount,
        max_bonus_syp=max_amount,
        created_by=message.from_user.id
    )

    if not rule_id:
        await message.answer("❌ تعذر إنشاء العرض. يرجى المحاولة لاحقاً.")
        await state.clear()
        return

    await message.answer(
        "✅ <b>تم إنشاء عرض البونص وتفعيله!</b>\n\n"
        f"🆔 رقم العرض: <code>#{rule_id}</code>\n"
        f"🏷️ الاسم: <b>{title}</b>\n"
        f"📈 النسبة: <code>{percent:g}%</code>\n"
        f"💳 الطريقة: <code>{BONUS_PAYMENT_METHOD_LABELS.get(method, method)}</code>\n"
        f"💰 الحد الأدنى: <code>{min_amount:,} SYP</code>\n"
        f"🛡️ الحد الأعلى للبونص: <code>{max_amount:,} SYP</code>\n\n"
        "سيتم تطبيق العرض تلقائياً عند قبول الإيداعات المطابقة.",
        reply_markup=get_bonus_menu_keyboard(),
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "bonus_list")
async def bonus_list_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    rules = repo.get_all_bonus_rules(limit=20)
    if not rules:
        await safe_edit_text(
            callback.message,
            "📋 <b>العروض الحالية</b>\n\nلا توجد عروض بونص بعد.",
            reply_markup=get_bonus_menu_keyboard(),
            parse_mode="HTML"
        )
        await safe_answer_callback(callback)
        return

    text = "📋 <b>آخر عروض البونص:</b>\n\n"
    rows = []
    for r in rules:
        status = "🟢 فعال" if r.get('is_active') else "🔴 متوقف"
        method_label = BONUS_PAYMENT_METHOD_LABELS.get(r.get('payment_method'), r.get('payment_method'))
        text += (
            f"<b>#{r['id']} - {r['title']}</b>\n"
            f"{status} | {float(r['percent']):g}% | {method_label}\n"
            f"حد أدنى: {float(r.get('min_amount_syp') or 0):,.0f} SYP | "
            f"حد أعلى: {float(r.get('max_bonus_syp') or 0):,.0f} SYP\n\n"
        )
        if r.get('is_active'):
            rows.append([InlineKeyboardButton(text=f"🛑 إيقاف #{r['id']}", callback_data=f"bonus_disable_{r['id']}")])
    rows.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="adm_bonus_menu")])
    await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("bonus_disable_"))
async def bonus_disable_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    rule_id = int(callback.data.replace("bonus_disable_", ""))
    repo.disable_bonus_rule(rule_id)
    await safe_answer_callback(callback, "تم إيقاف العرض.")
    await bonus_list_callback(callback)



# ================================================================
# 🎫 إنشاء كود هدية من البوت للمستخدمين
# ================================================================

@router.callback_query(F.data == "adm_create_bot_gift")
async def adm_create_bot_gift_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await state.clear()
    await safe_edit_text(
        callback.message,
        "🎫 <b>إنشاء كود هدية من المشرف</b>\n\n"
        "اختر نوع الكود الذي تريد إنشاءه:\n\n"
        "🎁 <b>كود بونص:</b> يبدأ بـ <code>CAESAR-BONUS-</code> ويضاف إلى رصيد مكافآت اللعب.\n"
        "💵 <b>كود كاش:</b> يبدأ بـ <code>CAESAR-CASH-</code> ويضاف إلى رصيد البوت القابل للسحب.\n\n"
        "ملاحظة: الكود يستخدم مرة واحدة فقط، ولا يتم خصم قيمته من رصيد الأدمن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 كود بونص للعب", callback_data="adm_bot_gift_type_bonus")],
            [InlineKeyboardButton(text="💵 كود كاش قابل للسحب", callback_data="adm_bot_gift_type_cash")],
            [InlineKeyboardButton(text="🔙 عودة للوحة الأدمن", callback_data="caesar_control_panel")]
        ]),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("adm_bot_gift_type_"))
async def adm_bot_gift_type_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    gift_type = callback.data.replace("adm_bot_gift_type_", "")
    if gift_type not in {"bonus", "cash"}:
        await safe_answer_callback(callback, "نوع غير معروف", show_alert=True)
        return
    await state.update_data(bot_gift_type=gift_type)
    prefix = "CAESAR-BONUS-" if gift_type == "bonus" else "CAESAR-CASH-"
    label = "بونص للعب" if gift_type == "bonus" else "كاش قابل للسحب"
    await safe_edit_text(
        callback.message,
        f"🎫 <b>إنشاء كود {label}</b>\n\n"
        f"أرسل قيمة الكود بالليرة السورية. مثال: <code>50000</code>\n\n"
        f"سيتم إنشاء كود يبدأ بـ: <code>{prefix}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 اختيار نوع آخر", callback_data="adm_create_bot_gift")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.entering_bot_gift_amount)
    await safe_answer_callback(callback)


@router.message(AdminStates.entering_bot_gift_amount)
async def process_bot_gift_amount(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return

    text = (message.text or '').strip().replace(',', '')
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ الرجاء إدخال مبلغ صحيح بالأرقام فقط. مثال: <code>50000</code>", parse_mode="HTML")
        return

    if amount < 1000:
        await message.answer("❌ الحد الأدنى لكود الهدية هو <code>1,000 SYP</code>.", parse_mode="HTML")
        return

    data = await state.get_data()
    gift_type = data.get('bot_gift_type') or 'bonus'
    prefix = "CAESAR-BONUS-" if gift_type == "bonus" else "CAESAR-CASH-"
    type_label = "بونص للعب" if gift_type == "bonus" else "كاش قابل للسحب"
    code = prefix + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

    success, msg = repo.create_bot_gift(message.from_user.id, amount, code)
    if not success:
        await message.answer(f"❌ {msg}")
        await state.clear()
        return

    result_text = (
        "✅ <b>تم إنشاء كود هدية من المشرف بنجاح!</b>\n\n"
        f"🏷️ <b>النوع:</b> <code>{type_label}</code>\n"
        f"🎫 <b>الكود:</b> <code>{code}</code>\n"
        f"💰 <b>القيمة:</b> <code>{amount:,} SYP</code>\n"
        "🔁 <b>عدد الاستخدامات:</b> مرة واحدة فقط\n\n"
        "يمكنك الآن إرسال هذا الكود للمستخدم المطلوب أو نشره في القناة."
    )
    await message.answer(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎫 إنشاء كود آخر", callback_data="adm_create_bot_gift")],
            [InlineKeyboardButton(text="🏠 لوحة التحكم", callback_data="caesar_control_panel")]
        ]),
        parse_mode="HTML"
    )

    await send_log_message(
        message.bot,
        "🎫 <b>إنشاء كود هدية من البوت</b>\n\n"
        f"👑 الأدمن: <code>{message.from_user.id}</code>\n"
        f"🎫 الكود: <code>{code}</code>\n"
        f"💰 القيمة: <code>{amount:,} SYP</code>"
    )
    await state.clear()


# ================================================================
# 🆕 إدارة المستخدمين (بحث + تعديل رصيد)
# ================================================================

@router.callback_query(F.data == "adm_users_menu")
async def adm_users_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await state.clear()
    total = repo.get_total_users_count()
    today = repo.get_new_users_today()
    await safe_edit_text(
        callback.message,
        f"🔎 <b>إدارة المستخدمين</b>\n\n"
        f"👥 <b>إجمالي المسجّلين:</b> <code>{total:,}</code>\n"
        f"🆕 <b>جدد اليوم:</b> <code>{today}</code>\n\n"
        f"أرسل الآن <b>Telegram ID</b> أو <b>اسم المستخدم</b> للبحث عنه 👇",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.searching_user)
    await safe_answer_callback(callback)


@router.message(AdminStates.searching_user)
async def process_user_search(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    query = (message.text or '').strip()
    if not query:
        await message.answer("❌ الرجاء إدخال قيمة للبحث:")
        return

    users = repo.search_user(query)
    if not users:
        await message.answer(
            f"❌ لم يتم العثور على مستخدم بـ: <code>{query}</code>\n\nحاول مرة أخرى أو اضغط /admin للعودة.",
            parse_mode="HTML"
        )
        return

    text = f"🔎 <b>نتائج البحث ({len(users)}):</b>\n\n"
    keyboard_rows = []
    for u in users:
        tid = u['telegram_id']
        uname = u.get('telegram_username') or 'بدون معرف'
        bal = int(u.get('bot_balance', 0))
        text += (
            f"👤 <code>{tid}</code> | {uname}\n"
            f"   💎 <code>{bal:,} SYP</code>"
            f" | 🎮 <code>{u.get('ichancy_username') or '—'}</code>\n\n"
        )
        keyboard_rows.append([InlineKeyboardButton(
            text=f"✏️ تعديل {uname[:15]} ({bal:,})",
            callback_data=f"setbal_{tid}"
        )])

    keyboard_rows.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="adm_users_menu")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows), parse_mode="HTML")


@router.callback_query(F.data.startswith("setbal_"))
async def set_balance_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    telegram_id = callback.data.replace("setbal_", "")
    user = repo.get_user(telegram_id)
    if not user:
        await safe_answer_callback(callback, "⚠️ المستخدم غير موجود.", show_alert=True)
        return
    await state.update_data(target_user_id=telegram_id)
    await safe_edit_text(
        callback.message,
        f"✏️ <b>تعديل رصيد المستخدم</b>\n\n"
        f"👤 <code>{telegram_id}</code> ({user.get('telegram_username') or 'بدون اسم'})\n"
        f"💎 <b>الرصيد الحالي:</b> <code>{int(user.get('bot_balance', 0)):,} SYP</code>\n\n"
        f"أرسل الرصيد الجديد بالـ SYP (مثال: <code>50000</code>):\n"
        f"<i>يمكن استخدام +5000 أو -3000 لإضافة/خصم نسبي</i>",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.setting_balance)
    await safe_answer_callback(callback)


@router.message(AdminStates.setting_balance)
async def process_set_balance(message: Message, state: FSMContext):
    if not await ensure_admin_message(message, state):
        return
    data = await state.get_data()
    telegram_id = data.get('target_user_id')
    user = repo.get_user(telegram_id)
    if not user:
        await message.answer("⚠️ المستخدم لم يعد موجوداً.")
        await state.clear()
        return

    raw = (message.text or '').strip().replace(',', '').replace('+', '')
    try:
        delta_mode = message.text.strip().startswith('-')
        value = int(raw.replace('-', ''))
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ الرجاء إدخال رقم صحيح:")
        return

    old_balance = int(user.get('bot_balance', 0))
    if delta_mode:
        new_balance = old_balance - value
        action = f"خصم {value:,}"
    else:
        new_balance = value
        action = f"تعيين إلى {value:,}"

    if new_balance < 0:
        await message.answer(f"❌ الرصيد الناتج سالب ({new_balance:,}). غير مسموح.")
        return

    repo.set_user_balance(telegram_id, new_balance)
    await message.answer(
        f"✅ <b>تم تحديث الرصيد بنجاح!</b>\n\n"
        f"👤 <code>{telegram_id}</code>\n"
        f"📝 <b>العملية:</b> {action} SYP\n"
        f"💎 <b>الرصيد القديم:</b> <code>{old_balance:,} SYP</code>\n"
        f"💎 <b>الرصيد الجديد:</b> <code>{new_balance:,} SYP</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔎 بحث عن مستخدم آخر", callback_data="adm_users_menu")],
            [InlineKeyboardButton(text="🏠 لوحة التحكم", callback_data="caesar_control_panel")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "adm_close_panel")
async def adm_close_panel_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_edit_text(callback.message, "🔒 تم إغلاق لوحة تحكم الأدمن بنجاح.")
    await safe_answer_callback(callback)


# ================================================================
# 👑 مسابقات القيصر - أزرار القبول/الرفض من القناة
# ================================================================

def get_contest_rejection_reason_keyboard(entry_id: int):
    """قائمة أسباب رفض المشاركة في المسابقة."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📸 صورة غير واضحة", callback_data=f"contest_reject_reason|receipt_unclear|{entry_id}"),
            InlineKeyboardButton(text="🔗 الرابط غير صحيح", callback_data=f"contest_reject_reason|wrong_link|{entry_id}")
        ],
        [
            InlineKeyboardButton(text="🚫 مشاركة مكررة", callback_data=f"contest_reject_reason|duplicate|{entry_id}"),
            InlineKeyboardButton(text="📝 محتوى غير مرتبط", callback_data=f"contest_reject_reason|irrelevant|{entry_id}")
        ],
        [InlineKeyboardButton(text="✍️ سبب مخصص (من المحادثة)", callback_data=f"contest_reject_reason|custom|{entry_id}")],
        [InlineKeyboardButton(text="🔙 إلغاء", callback_data="caesar_control_panel")]
    ])

def map_contest_rejection_reason(reason_code: str) -> str:
    """تحويل كود السبب إلى نص عربي."""
    return {
        'receipt_unclear': 'الصورة/الإثبات غير واضح.',
        'wrong_link': 'الرابط غير صحيح أو غير موجود.',
        'duplicate': 'مشاركة مكررة.',
        'irrelevant': 'المحتوى غير مرتبط بالمسابقة.',
    }.get(reason_code, 'تم رفض المشاركة.')


@router.callback_query(F.data.startswith("approve_contest_"))
async def approve_contest_callback(callback: CallbackQuery):
    """قبول مشاركة المسابقة من القناة."""
    if not await ensure_admin_callback(callback):
        return

    entry_id = int(callback.data.split("_")[-1])
    result = repo.approve_contest_entry(entry_id, reviewed_by=callback.from_user.id)

    if not result.get('ok'):
        reason = result.get('reason', 'unknown')
        reasons = {
            'entry_not_found': 'المشاركة غير موجودة.',
            'already_reviewed': 'تمت مراجعة هذه المشاركة مسبقاً.',
            'contest_not_found': 'المسابقة غير موجودة.',
            'winners_limit_reached': 'تم الوصول للحد الأقصى من الفائزين.',
        }
        await safe_answer_callback(callback, f"❌ {reasons.get(reason, 'تعذر قبول المشاركة.')}", show_alert=True)
        return

    # الرد على الـ callback فوراً
    await safe_answer_callback(callback, "✅ تم قبول المشاركة!")

    # تحديث رسالة القناة
    user_telegram_id = result.get('user_telegram_id')
    gift_code = result.get('gift_code')
    reward_amount = result.get('reward_amount')
    contest_id = result.get('contest_id')

    # تحديث نص الرسالة
    original_text = getattr(callback.message, 'html_text', None) or getattr(callback.message, 'caption', None) or ''
    status_text = "✅ <b>تم قبول المشاركة</b>"
    if gift_code:
        status_text += f"\n🎁 <b>كود الهدية:</b> <code>{gift_code}</code>"
    elif reward_amount and reward_amount > 0:
        status_text += f"\n💰 <b>تم إضافة:</b> <code>{reward_amount:,} SYP</code> إلى رصيد المستخدم"

    try:
        if getattr(callback.message, 'photo', None):
            await callback.message.edit_caption(
                caption=f"{original_text}\n\n{status_text}",
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                f"{original_text}\n\n{status_text}",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.warning(f"Could not update channel message after contest approval: {e}")

    # إشعار المستخدم بالقبول
    if user_telegram_id:
        contest = repo.get_contest(contest_id)
        contest_title = contest.get('title') if contest else 'المسابقة'

        user_text = (
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ <b>تم قبول مشاركتك!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👑 <b>المسابقة:</b> {contest_title}\n\n"
        )
        if gift_code:
            user_text += (
                f"🎁 <b>كود الجائزة:</b> <code>{gift_code}</code>\n\n"
                "💡 يمكنك استرداد الكود من زر <b>🎫 كود هدية</b> في القائمة الرئيسية."
            )
        elif reward_amount and reward_amount > 0:
            user = repo.get_user(user_telegram_id)
            new_balance = int(user.get('bot_balance') or 0) if user else 0
            user_text += (
                f"💰 <b>قيمة الجائزة:</b> <code>{reward_amount:,} SYP</code>\n\n"
                f"💎 <b>رصيدك الحالي:</b> <code>{new_balance:,} SYP</code>\n\n"
                "🎉 تهانينا! تم إضافة الجائزة إلى رصيدك."
            )
        else:
            user_text += "🎉 تهانينا! مشاركتك مقبولة ومؤهلة للفوز."

        user_text += "\n\n━━━━━━━━━━━━━━━━━━━━━━━"

        try:
            await callback.bot.send_message(
                chat_id=user_telegram_id,
                text=user_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Failed to notify user {user_telegram_id} about contest approval: {e}")

    # إرسال سجل
    await send_log_message(
        callback.bot,
        f"✅ <b>تم قبول مشاركة مسابقة</b>\n\n"
        f"📌 المشاركة: <code>#{entry_id}</code>\n"
        f"👑 المسابقة: <code>#{contest_id}</code>\n"
        f"👤 المشرف: <code>{callback.from_user.id}</code>"
    )


@router.callback_query(F.data.startswith("reject_contest_"))
async def reject_contest_callback(callback: CallbackQuery, state: FSMContext):
    """رفض مشاركة المسابقة - عرض قائمة الأسباب."""
    if not await ensure_admin_callback(callback):
        return

    entry_id = int(callback.data.split("_")[-1])

    await safe_edit_text(
        callback.message,
        f"❌ <b>اختر سبب رفض المشاركة</b> <code>#{entry_id}</code>",
        reply_markup=get_contest_rejection_reason_keyboard(entry_id),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


@router.callback_query(F.data.startswith("contest_reject_reason|"))
async def contest_reject_reason_callback(callback: CallbackQuery, state: FSMContext):
    """معالجة سبب رفض المشاركة."""
    if not await ensure_admin_callback(callback):
        return

    _, reason_code, entry_id_text = callback.data.split("|")
    entry_id = int(entry_id_text)

    if reason_code == 'custom':
        # حفظ الطلب المعلق للمعالجة من المحادثة الخاصة
        # سنستخدم نفس آلية pending_rejection مع نوع مختلف
        repo.set_pending_rejection(callback.from_user.id, entry_id, 'contest_entry', callback.message.chat.id, callback.message.message_id)
        await safe_edit_text(
            callback.message,
            f"✍️ <b>طلب رفض مخصص للمشاركة #{entry_id}</b>\n\n"
            "أرسل سبب الرفض في <b>محادثتك الخاصة</b> مع البوت الآن 👇",
            parse_mode="HTML"
        )
        try:
            await callback.bot.send_message(
                callback.from_user.id,
                f"✍️ <b>سبب رفض مخصص للمشاركة #{entry_id}</b>\n\n"
                "أرسل الآن نص السبب (سيُرسل للمستخدم فوراً):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ إلغاء الرفض المخصص", callback_data="adm_cancel_rejection")]]),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not DM admin for custom contest rejection: {e}")
        await safe_answer_callback(callback, "أرسل السبب في المحادثة الخاصة ✉️")
        return

    reason = map_contest_rejection_reason(reason_code)

    # الرد على الـ callback فوراً
    await safe_answer_callback(callback, "⏳ جاري رفض المشاركة...")

    # رفض المشاركة
    repo.reject_contest_entry(entry_id, reviewed_by=callback.from_user.id)

    # تحديث رسالة القناة
    original_text = getattr(callback.message, 'html_text', None) or getattr(callback.message, 'caption', None) or ''
    status_text = f"❌ <b>تم رفض المشاركة</b>\n📝 <b>السبب:</b> {reason}"

    try:
        if getattr(callback.message, 'photo', None):
            await callback.message.edit_caption(
                caption=f"{original_text}\n\n{status_text}",
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                f"{original_text}\n\n{status_text}",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.warning(f"Could not update channel message after contest rejection: {e}")

    # إشعار المستخدم بالرفض
    # نحتاج للبحث عن entry_id للحصول على user_telegram_id
    # نستخدم query مباشر
    try:
        entry_data = DatabaseManager.execute_query_dict(
            "SELECT * FROM contest_entries WHERE id = %s",
            (entry_id,),
            fetch='one'
        )
        if entry_data:
            user_telegram_id = entry_data.get('user_telegram_id')
            contest_id = entry_data.get('contest_id')
            contest = repo.get_contest(contest_id) if contest_id else None
            contest_title = contest.get('title') if contest else 'المسابقة'

            user_text = (
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "❌ <b>تم رفض مشاركتك</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👑 <b>المسابقة:</b> {contest_title}\n"
                f"📝 <b>السبب:</b> {reason}\n\n"
                "💡 يمكنك المحاولة مجدداً بمشاركة صحيحة.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━"
            )

            try:
                await callback.bot.send_message(
                    chat_id=user_telegram_id,
                    text=user_text,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Failed to notify user {user_telegram_id} about contest rejection: {e}")
    except Exception as e:
        logger.warning(f"Could not fetch entry data for notification: {e}")

    # إرسال سجل
    await send_log_message(
        callback.bot,
        f"❌ <b>تم رفض مشاركة مسابقة</b>\n\n"
        f"📌 المشاركة: <code>#{entry_id}</code>\n"
        f"📝 السبب: {reason}\n"
        f"👤 المشرف: <code>{callback.from_user.id}</code>"
    )


# ================================================================
# 🆕 (Update 18) قسم لوحة المتصدرين الأسبوعية (Turnover Leaderboard)
# ================================================================

def _lb_status_text():
    """نص لوحة إدارة المتصدرين: الحالة + الجوائز + آخر تحديث/تسوية."""
    feats = repo.get_user_features_settings()
    cfg = repo.get_lb_config()
    enabled = bool(feats.get('leaderboard_enabled', True))
    lb_type = str(feats.get('leaderboard_type') or 'all_time')
    type_label = {'weekly': '🗓️ أسبوعي (دوران مراهنات حقيقي)', 'monthly': '📆 شهري', 'all_time': '♾️ كلي (رصيد البوت)'}.get(lb_type, lb_type)
    tracked = repo.get_lb_tracked_count()
    last_refresh = repo.get_lb_last_refresh()
    refresh_txt = last_refresh.strftime('%Y-%m-%d %H:%M') if last_refresh else 'لم يتم بعد'
    last_done = cfg['last_settled_week'] or 'لا شيء بعد'
    qualified_hint = f"{cfg['min_weekly_turnover']:,}" if cfg['min_weekly_turnover'] > 0 else "بدون حد"

    return (
        "🏆 <b>إدارة لوحة المتصدرين الأسبوعية</b>\n\n"
        f"🔌 ظهور اللوحة للمستخدمين: <b>{'🟢 مفعّل' if enabled else '🔴 متوقف'}</b>\n"
        f"📊 نوع الترتيب: <b>{type_label}</b>\n"
        f"👥 لاعبون متتبَّعون: <code>{tracked}</code>\n"
        f"🕒 آخر تحديث إحصائيات: <code>{refresh_txt}</code>\n"
        f"🏁 آخر أسبوع مُسوّى: <code>{last_done}</code>\n\n"
        "🎁 <b>الجوائز (SYP):</b>\n"
        f"🥇 الأول: <code>{cfg['prize_1']:,}</code>\n"
        f"🥈 الثاني: <code>{cfg['prize_2']:,}</code>\n"
        f"🥉 الثالث: <code>{cfg['prize_3']:,}</code>\n"
        f"🎯 حد التأهل الأسبوعي: <code>{qualified_hint}</code>\n"
        f"🤖 قيد الجوائز تلقائياً: <b>{'🟢 مفعّل' if cfg['auto_credit'] else '🔴 متوقف (أرشفة فقط)'}</b>\n\n"
        "💡 الترتيب بالوضع الأسبوعي يعتمد على دوران المراهنات الفعلي في iChancy، "
        "وتُسوّى الجوائز آلياً اثنين 00:05 بتوقيت سوريا. التفعيل والنوع يُداران أيضاً "
        "من لوحة الميزات في الـ Mini App."
    )


def _lb_menu_keyboard():
    feats = repo.get_user_features_settings()
    cfg = repo.get_lb_config()
    enabled = bool(feats.get('leaderboard_enabled', True))
    weekly_mode = str(feats.get('leaderboard_type') or 'all_time') == 'weekly'
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔴 إيقاف الظهور" if enabled else "🟢 تفعيل الظهور",
                callback_data="adm_lb_toggle_visible"
            ),
            InlineKeyboardButton(
                text="🔀 إلغاء الوضع الأسبوعي" if weekly_mode else "🗓️ تفعيل الوضع الأسبوعي",
                callback_data="adm_lb_toggle_type"
            ),
        ],
        [
            InlineKeyboardButton(text="🥇 جائزة الأول", callback_data="adm_lb_set_p1"),
            InlineKeyboardButton(text="🥈 جائزة الثاني", callback_data="adm_lb_set_p2"),
            InlineKeyboardButton(text="🥉 جائزة الثالث", callback_data="adm_lb_set_p3"),
        ],
        [
            InlineKeyboardButton(text="🎯 حد التأهل", callback_data="adm_lb_set_min"),
            InlineKeyboardButton(
                text="🤖 إيقاف القيد التلقائي" if cfg['auto_credit'] else "🤖 تفعيل القيد التلقائي",
                callback_data="adm_lb_toggle_autocredit"
            ),
        ],
        [
            InlineKeyboardButton(text="🔄 تحديث الإحصائيات الآن", callback_data="adm_lb_refresh_now"),
            InlineKeyboardButton(text="🏅 تسوية الأسبوع الآن", callback_data="adm_lb_settle_now"),
        ],
        [InlineKeyboardButton(text="📜 آخر النتائج", callback_data="adm_lb_history")],
        [InlineKeyboardButton(text="🔙 لوحة التحكم", callback_data="caesar_control_panel")],
    ])


@router.callback_query(F.data == "adm_lb_menu")
async def adm_lb_menu_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    await state.clear()
    await safe_edit_text(callback.message, _lb_status_text(), reply_markup=_lb_menu_keyboard(), parse_mode="HTML")
    await safe_answer_callback(callback)


@router.callback_query(F.data == "adm_lb_toggle_visible")
async def adm_lb_toggle_visible_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    feats = repo.get_user_features_settings()
    repo.update_user_features_settings(leaderboard_enabled=not bool(feats.get('leaderboard_enabled', True)))
    await safe_answer_callback(callback, "تم تحديث حالة الظهور ✅")
    await adm_lb_menu_callback(callback, state)


@router.callback_query(F.data == "adm_lb_toggle_type")
async def adm_lb_toggle_type_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    feats = repo.get_user_features_settings()
    weekly_mode = str(feats.get('leaderboard_type') or 'all_time') == 'weekly'
    new_type = 'all_time' if weekly_mode else 'weekly'
    repo.update_user_features_settings(leaderboard_type=new_type)
    if new_type == 'weekly':
        await safe_answer_callback(callback, "🗓️ الوضع الأسبوعي مفعّل — سيبدأ التتبع عند أول تحديث إحصائيات")
    else:
        await safe_answer_callback(callback, "🔀 عدنا للترتيب الكلي (رصيد البوت)")
    await adm_lb_menu_callback(callback, state)


@router.callback_query(F.data == "adm_lb_toggle_autocredit")
async def adm_lb_toggle_autocredit_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    cfg = repo.get_lb_config()
    repo.update_lb_settings(auto_credit=not cfg['auto_credit'])
    await safe_answer_callback(callback, "تم تحديث القيد التلقائي 🤖")
    await adm_lb_menu_callback(callback, state)


@router.callback_query(F.data.in_({"adm_lb_set_p1", "adm_lb_set_p2", "adm_lb_set_p3", "adm_lb_set_min"}))
async def adm_lb_prompt_value_callback(callback: CallbackQuery, state: FSMContext):
    if not await ensure_admin_callback(callback):
        return
    prompts = {
        "adm_lb_set_p1": ("🥇 جائزة المركز الأول", AdminStates.entering_lb_prize_1),
        "adm_lb_set_p2": ("🥈 جائزة المركز الثاني", AdminStates.entering_lb_prize_2),
        "adm_lb_set_p3": ("🥉 جائزة المركز الثالث", AdminStates.entering_lb_prize_3),
        "adm_lb_set_min": ("🎯 الحد الأدنى لنقاط الدوران الأسبوعية للتأهل", AdminStates.entering_lb_min_turnover),
    }
    label, target_state = prompts[callback.data]
    await state.set_state(target_state)
    await safe_edit_text(
        callback.message,
        f"{label}\n\nأرسل القيمة الجديدة بالليرة السورية (رقم صحيح، 0 للإلغاء/بدون):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="adm_lb_menu")]
        ]),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)


async def _process_lb_value(message: Message, state: FSMContext, apply_func, success_label):
    if not is_admin_user(message.from_user.id):
        return
    try:
        value = int(str(message.text or "").strip().replace(",", ""))
        if value < 0:
            raise ValueError("negative")
    except (ValueError, TypeError):
        await message.answer("⚠️ أدخل رقماً صحيحاً موجباً فقط. أعد المحاولة أو /cancel")
        return
    apply_func(value)
    await state.clear()
    await message.answer(f"✅ {success_label}: <code>{value:,}</code>", parse_mode="HTML")


@router.message(AdminStates.entering_lb_prize_1)
async def process_lb_prize_1(message: Message, state: FSMContext):
    await _process_lb_value(message, state, lambda v: repo.update_lb_settings(prize_1=v), "🥇 جائزة المركز الأول")


@router.message(AdminStates.entering_lb_prize_2)
async def process_lb_prize_2(message: Message, state: FSMContext):
    await _process_lb_value(message, state, lambda v: repo.update_lb_settings(prize_2=v), "🥈 جائزة المركز الثاني")


@router.message(AdminStates.entering_lb_prize_3)
async def process_lb_prize_3(message: Message, state: FSMContext):
    await _process_lb_value(message, state, lambda v: repo.update_lb_settings(prize_3=v), "🥉 جائزة المركز الثالث")


@router.message(AdminStates.entering_lb_min_turnover)
async def process_lb_min_turnover(message: Message, state: FSMContext):
    await _process_lb_value(message, state, lambda v: repo.update_lb_settings(min_weekly_turnover=v), "🎯 حد التأهل الأسبوعي")


@router.callback_query(F.data == "adm_lb_refresh_now")
async def adm_lb_refresh_now_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_answer_callback(callback, "🔄 جاري جلب إحصائيات iChancy...")
    from telegram_bot.main import refresh_turnover_leaderboard  # استيراد مؤجّل لتفادي الدورية
    updated = await refresh_turnover_leaderboard()
    await safe_edit_text(
        callback.message,
        _lb_status_text() + f"\n\n✅ <b>آخر تحديث الآن:</b> <code>{updated}</code> سجل" if updated else
        _lb_status_text() + "\n\n⚠️ <b>فشل التحديث الآن</b> — راجع الجلسة/السجلات",
        reply_markup=_lb_menu_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "adm_lb_settle_now")
async def adm_lb_settle_now_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    await safe_answer_callback(callback, "🏅 جاري التسوية اليدوية للأسبوع الـمنصرم...")
    from telegram_bot.main import settle_weekly_leaderboard  # استيراد مؤجّل لتفادي الدورية
    result = await settle_weekly_leaderboard(bot=callback.bot, manual=True)
    if result.get('ok'):
        note = (
            f"✅ تمت التسوية — فائزون: {result.get('winners', 0)} | مدفوعات: {result.get('credited', 0)}"
            if not result.get('skipped') else
            f"ℹ️ الأسبوع {result.get('week_start')} مُسوّى مسبقاً — لا شيء جديد."
        )
    else:
        reasons = {
            'refresh_failed': 'تعذّر جلب الإحصائيات من iChancy (تحقق من الجلسة).',
            'no_participants': 'لا يوجد متتبَّعون في دورة الأسبوع الـمنصرم.',
        }
        note = f"⚠️ لم تكتمل التسوية: {reasons.get(result.get('reason'), result.get('reason') or 'خطأ غير متوقع')}"
    await safe_edit_text(
        callback.message,
        _lb_status_text() + f"\n\n<b>{note}</b>",
        reply_markup=_lb_menu_keyboard(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "adm_lb_history")
async def adm_lb_history_callback(callback: CallbackQuery):
    if not await ensure_admin_callback(callback):
        return
    rank_emoji = {1: '🥇', 2: '🥈', 3: '🥉'}
    last = repo.get_lb_last_results(limit=10)
    if not last.get('results'):
        text = "📜 <b>أرشيف المتصدرين</b>\n\nلا توجد أسابيع مؤرشفة بعد."
    else:
        wk = last['week_start']
        wk_txt = wk.strftime('%Y-%m-%d') if hasattr(wk, 'strftime') else str(wk)
        text = f"📜 <b>آخر نتائج مؤرشفة — أسبوع {wk_txt}</b>\n\n"
        for r in last['results']:
            medal = rank_emoji.get(r['rank'], f"#{r['rank']}")
            prize_txt = f" | 💰 {int(r['prize_syp']):,} SYP {'✅' if r.get('credited') else '⏳'}" if r.get('prize_syp') else ""
            text += f"{medal} {r['username']} — <code>{int(r['weekly_turnover']):,}</code>{prize_txt}\n"
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 قسم المتصدرين", callback_data="adm_lb_menu")]
        ]),
        parse_mode="HTML"
    )
    await safe_answer_callback(callback)
