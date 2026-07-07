import os
import sys
import json
import hmac
import time
import hashlib
import logging
import asyncio
import urllib.parse
from datetime import datetime, timedelta
from decimal import Decimal
from contextlib import suppress
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeDefault
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
import aiohttp
from aiohttp import web

# إضافة المجلد الجذري للمشروع إلى مسار بايثون
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

WEBAPP_DIR = os.path.join(project_root, "webapp")

# إعداد مجلد السجلات
log_dir = os.path.join(project_root, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file_path = os.path.join(log_dir, "caesar_bot.log")

# إعداد التسجيل
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file_path, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# استيراد المكونات
from config import settings
from database.connection import DatabaseManager
from telegram_bot.middlewares.terms_check import TermsCheckMiddleware
from telegram_bot.handlers import start, menu, admin
from ichancy_api.client import ichancy_api_client
import database.repository as repo
from neon_metrics import get_neon_metrics
from render_metrics import get_render_metrics

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://ichancy100.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8080))

watchdog_task = None
ensure_webhook_task = None
daily_report_task = None
routers_registered = False
last_cookie_warning_sent = None  # 🆕 يمنع تكرار تنبيه الكوكيز
SERVER_START_TS = time.time()


async def cookie_watchdog_task(bot: Bot):
    """فحص دوري للويبهوك وجلسة iChancy"""
    logger.info("🍪 Watchdog started (interval: 5 min).")
    while True:
        try:
            try:
                webhook_info = await bot.get_webhook_info()
                if not webhook_info.url:
                    logger.warning("⚠️ Watchdog: Webhook is missing! Re-setting...")
                    await bot.delete_webhook(drop_pending_updates=True)
                    await bot.set_webhook(WEBHOOK_URL)
                    logger.info("✅ Watchdog: Webhook re-set.")
            except Exception as e:
                logger.error(f"Watchdog webhook check error: {e}")

            logger.info("🔍 Watchdog: checking session validity...")
            is_valid = await ichancy_api_client.check_session_validity()
            if not is_valid:
                logger.warning("💀 Watchdog: session DEAD! Attempting auto-login...")
                success = await ichancy_api_client.login_agent()
                if success:
                    logger.info("✅ Watchdog: session refreshed!")
                    repo.update_cookie_timestamp()
                    admin_balance = await ichancy_api_client.get_admin_balance()
                    if admin_balance is not None:
                        repo.update_bot_settings(agent_balance=admin_balance)
                else:
                    logger.error("❌ Watchdog: auto-login failed!")
            else:
                logger.info("🟢 Watchdog: session healthy.")
                admin_balance = await ichancy_api_client.get_admin_balance()
                if admin_balance is not None:
                    repo.update_bot_settings(agent_balance=admin_balance)
                    logger.info(f"💰 Watchdog: cached agent balance = {admin_balance:,} NSP")

            # 🆕 فحص رصيد الكاشيرة بشكل دوري
            await check_agent_balance_periodic(bot)

            # تنبيه ذكي: لا نطلب تحديث الكوكيز طالما الجلسة نشطة، حتى لو عمرها طويل.
            global last_cookie_warning_sent
            if not is_valid:
                warning_bucket = int(time.time() // 1800)  # كل 30 دقيقة كحد أقصى عند التعطل فقط
                if last_cookie_warning_sent != warning_bucket:
                    last_cookie_warning_sent = warning_bucket
                    admin_ids = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]
                    warn_text = (
                        "🔴 <b>جلسة iChancy غير نشطة</b>\n\n"
                        "فشل فحص الجلسة أو التجديد التلقائي. إذا توقفت العمليات، حدّث الكوكيز من لوحة الأدمن."
                    )
                    for admin_id in admin_ids:
                        try:
                            await bot.send_message(chat_id=admin_id, text=warn_text, parse_mode="HTML")
                        except Exception as e:
                            logger.warning(f"Cookie failure notification failed for {admin_id}: {e}")
        except asyncio.CancelledError:
            logger.info("🛑 Watchdog task cancelled.")
            raise
        except Exception as e:
            logger.error(f"⚠️ Watchdog error: {e}")

        await asyncio.sleep(300)


async def ensure_webhook(bot: Bot):
    """تنتظر 15 ثانية ثم تحاول تعيين الويبهوك لأول مرة."""
    await asyncio.sleep(15)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(WEBHOOK_URL)
        logger.info(f"🌐 Initial webhook set to {WEBHOOK_URL}")
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url == WEBHOOK_URL:
            logger.info("✅ Initial webhook verified.")
        else:
            logger.warning(f"⚠️ Initial webhook URL mismatch: {webhook_info.url}")
    except asyncio.CancelledError:
        logger.info("🛑 Initial webhook task cancelled.")
        raise
    except Exception as e:
        logger.error(f"❌ Initial webhook attempt failed: {e}")


# ================================================================
# 🆕 التقرير المالي اليومي + تنبيه رصيد الكاشيرة
# ================================================================

async def generate_daily_report(bot: Bot):
    """توليد وإرسال التقرير المالي اليومي للمشرفين."""
    try:
        admin_ids = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]

        # جمع الإحصائيات
        total_users = repo.get_total_users_count()
        new_users_today = repo.get_new_users_today()
        today_tx_count = repo.get_today_transactions_count()

        # إحصائيات الإيداعات اليوم
        deposits_today = DatabaseManager.execute_query_dict(
            """SELECT COUNT(*) as count, COALESCE(SUM(amount), 0) as total
            FROM transactions
            WHERE type = 'deposit_bot' AND status = 'approved'
            AND created_at::date = CURRENT_DATE""",
            fetch='one'
        )

        # إحصائيات السحوبات اليوم
        withdrawals_today = DatabaseManager.execute_query_dict(
            """SELECT COUNT(*) as count, COALESCE(SUM(amount), 0) as total
            FROM transactions
            WHERE type = 'withdraw_bot' AND status = 'approved'
            AND created_at::date = CURRENT_DATE""",
            fetch='one'
        )

        # إحصائيات الطلبات المعلقة
        pending_deposits = DatabaseManager.execute_query_dict(
            """SELECT COUNT(*) as count FROM transactions
            WHERE type = 'deposit_bot' AND status = 'pending'""",
            fetch='one'
        )
        pending_withdrawals = DatabaseManager.execute_query_dict(
            """SELECT COUNT(*) as count FROM transactions
            WHERE type = 'withdraw_bot' AND status = 'pending'""",
            fetch='one'
        )

        # إجمالي رصيد البوت
        total_bot_balance = await _get_total_bot_balance_cached()

        # رصيد الكاشيرة
        bot_settings = repo.get_bot_settings()
        agent_balance = int(bot_settings.get('agent_balance', 0))

        # حساب صافي الربح التقريبي
        deposit_total = float(deposits_today.get('total', 0)) if deposits_today else 0
        withdraw_total = float(withdrawals_today.get('total', 0)) if withdrawals_today else 0
        net_profit = deposit_total - withdraw_total

        # بناء التقرير
        report_text = (
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <b>التقرير المالي اليومي</b>\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d')}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            "👥 <b>══ المستخدمين ══</b>\n"
            f"├─ 📊 إجمالي المسجلين: <code>{total_users:,}</code>\n"
            f"└─ 🆕 جدد اليوم: <code>{new_users_today}</code>\n\n"

            "💰 <b>══ الإيداعات اليوم ══</b>\n"
            f"├─ 📥 عدد الطلبات: <code>{deposits_today.get('count', 0) if deposits_today else 0}</code>\n"
            f"└─ 💵 إجمالي المبالغ: <code>{deposit_total:,.0f} SYP</code>\n\n"

            "📤 <b>══ السحوبات اليوم ══</b>\n"
            f"├─ 📤 عدد الطلبات: <code>{withdrawals_today.get('count', 0) if withdrawals_today else 0}</code>\n"
            f"└─ 💵 إجمالي المبالغ: <code>{withdraw_total:,.0f} SYP</code>\n\n"

            "📈 <b>══ صافي الحركة ══</b>\n"
            f"├─ 💰 صافي اليوم: <code>{net_profit:,.0f} SYP</code>\n"
            f"{'├─ ✅ ربح' if net_profit > 0 else '├─ ❌ خسارة'}: <code>{abs(net_profit):,.0f} SYP</code>\n"
            f"└─ 🔄 معاملات اليوم: <code>{today_tx_count}</code>\n\n"

            "💎 <b>══ الأرصدة ══</b>\n"
            f"├─ 🏦 رصيد البوت (المستخدمين): <code>{total_bot_balance:,} SYP</code>\n"
            f"└─ 🎮 رصيد الكاشيرة: <code>{agent_balance:,} NSP</code>\n\n"

            "⏳ <b>══ الطلبات المعلقة ══</b>\n"
            f"├─ 📥 إيداعات معلقة: <code>{pending_deposits.get('count', 0) if pending_deposits else 0}</code>\n"
            f"└─ 📤 سحوبات معلقة: <code>{pending_withdrawals.get('count', 0) if pending_withdrawals else 0}</code>\n\n"

            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "👑 <i>Caesar_Bot - التقرير التلقائي</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━"
        )

        # إرسال التقرير للمشرفين
        for admin_id in admin_ids:
            try:
                await bot.send_message(chat_id=admin_id, text=report_text, parse_mode="HTML")
                logger.info(f"✅ Daily report sent to admin {admin_id}")
            except Exception as e:
                logger.warning(f"Failed to send daily report to {admin_id}: {e}")

        # إرسال سجل
        log_channel_id = getattr(settings, "LOG_CHANNEL_ID", None)
        if log_channel_id:
            try:
                await bot.send_message(chat_id=log_channel_id, text=report_text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to send daily report to log channel: {e}")

    except Exception as e:
        logger.error(f"❌ Daily report generation error: {e}", exc_info=True)


async def check_agent_balance_and_alert(bot: Bot):
    """فحص رصيد الكاشيرة وإرسال تنبيه إذا كان منخفضاً."""
    try:
        bot_settings = repo.get_bot_settings()
        agent_balance = int(bot_settings.get('agent_balance', 0))
        threshold = getattr(settings, 'AGENT_BALANCE_ALERT_THRESHOLD', 100000)

        if agent_balance < threshold:
            admin_ids = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]

            alert_text = (
                "━━━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ <b>تنبيه: رصيد الكاشيرة منخفض!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 <b>الرصيد الحالي:</b> <code>{agent_balance:,} NSP</code>\n"
                f"📉 <b>الحد الأدنى:</b> <code>{threshold:,} NSP</code>\n"
                f"⚠️ <b>النقص:</b> <code>{threshold - agent_balance:,} NSP</code>\n\n"
                f"⏰ <b>الوقت:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "💡 <b>الإجراء المطلوب:</b>\n"
                "قم بشحن رصيد الكاشيرة لضمان استمرارية العمليات.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━"
            )

            # 🆕 إرسال إلى قناة السجلات
            log_channel_id = getattr(settings, "LOG_CHANNEL_ID", None)
            if log_channel_id:
                try:
                    await bot.send_message(chat_id=log_channel_id, text=alert_text, parse_mode="HTML")
                    logger.info("✅ Agent balance low alert sent to log channel")
                except Exception as e:
                    logger.warning(f"Failed to send agent balance low alert to log channel: {e}")

            # إرسال إلى المشرفين
            for admin_id in admin_ids:
                try:
                    await bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
                    logger.warning(f"⚠️ Agent balance alert sent to admin {admin_id}: {agent_balance:,} NSP")
                except Exception as e:
                    logger.warning(f"Failed to send agent balance alert to {admin_id}: {e}")

    except Exception as e:
        logger.error(f"❌ Agent balance check error: {e}", exc_info=True)


async def daily_report_scheduler(bot: Bot):
    """مجدول التقرير المالي اليومي - يرسل التقرير في الساعة المحددة."""
    logger.info("📊 Daily report scheduler initialized.")

    while True:
        try:
            now = datetime.now()
            report_hour = getattr(settings, 'DAILY_REPORT_HOUR', 8)
            report_enabled = getattr(settings, 'DAILY_REPORT_ENABLED', True)

            # حساب الوقت المتبقي حتى موعد التقرير التالي
            if now.hour < report_hour:
                # التقرير اليوم لم يرسل بعد
                next_report = now.replace(hour=report_hour, minute=0, second=0, microsecond=0)
            else:
                # التقرير سيُرسل غداً
                next_report = (now + timedelta(days=1)).replace(hour=report_hour, minute=0, second=0, microsecond=0)

            wait_seconds = (next_report - now).total_seconds()
            logger.info(f"📊 Next daily report in {wait_seconds/3600:.1f} hours (at {next_report.strftime('%Y-%m-%d %H:%M')})")

            # انتظار حتى موعد التقرير
            await asyncio.sleep(wait_seconds)

            # إرسال التقرير إذا كان مفعلاً
            if report_enabled:
                logger.info("📊 Generating daily financial report...")
                await generate_daily_report(bot)

            # فحص رصيد الكاشيرة (يتم فحصه مرتين يومياً: مع التقرير وفي منتصف النهار)
            await check_agent_balance_and_alert(bot)

        except asyncio.CancelledError:
            logger.info("🛑 Daily report scheduler cancelled.")
            raise
        except Exception as e:
            logger.error(f"❌ Daily report scheduler error: {e}", exc_info=True)
            await asyncio.sleep(60)  # إعادة المحاولة بعد دقيقة


# مراقب رصيد الكاشيرة (يتم فحصه مع الـ watchdog الرئيسي كل 5 دقائق)
last_agent_balance_alert_sent = None
last_agent_balance_value = None  # 🆕 لتخزين آخر رصيد معروف


async def check_agent_balance_periodic(bot: Bot):
    """فحص رصيد الكاشيرة بشكل دوري (يُستدعى من الـ watchdog).
    
    يقوم بـ:
    1. فحص الزيادة في الرصيد وإرسال إشعار
    2. فحص النقص عن الحد الأدنى وإرسال تنبيه
    """
    global last_agent_balance_alert_sent, last_agent_balance_value

    try:
        bot_settings = repo.get_bot_settings()
        current_balance = int(bot_settings.get('agent_balance', 0))
        threshold = getattr(settings, 'AGENT_BALANCE_ALERT_THRESHOLD', 100000)

        # 🆕 فحص تغيّر الرصيد (زيادة)
        if last_agent_balance_value is not None and current_balance > last_agent_balance_value:
            # تم إضافة رصيد جديد!
            added_amount = current_balance - last_agent_balance_value
            await notify_agent_balance_increase(bot, last_agent_balance_value, current_balance, added_amount)
        elif last_agent_balance_value is not None and current_balance < last_agent_balance_value:
            # تم خصم رصيد (شحن للاعبين)
            decreased_amount = last_agent_balance_value - current_balance
            logger.info(f"📊 Agent balance decreased: {last_agent_balance_value:,} → {current_balance:,} (-{decreased_amount:,} NSP)")

        # تحديث آخر رصيد معروف
        last_agent_balance_value = current_balance

        # فحص النقص عن الحد الأدنى
        if current_balance < threshold:
            # إرسال تنبيه مرة واحدة فقط كل 6 ساعات لتجنب الإزعاج
            warning_bucket = int(time.time() // 21600)  # 6 ساعات
            if last_agent_balance_alert_sent != warning_bucket:
                last_agent_balance_alert_sent = warning_bucket
                await check_agent_balance_and_alert(bot)
    except Exception as e:
        logger.warning(f"Periodic agent balance check failed: {e}")


async def notify_agent_balance_increase(bot: Bot, old_balance: int, new_balance: int, added_amount: int):
    """إرسال إشعار عند زيادة رصيد الكاشيرة."""
    try:
        log_channel_id = getattr(settings, "LOG_CHANNEL_ID", None)

        notification_text = (
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💰 <b>تم تعبئة رصيد الكاشيرة!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 <b>الرصيد السابق:</b> <code>{old_balance:,} NSP</code>\n"
            f"➕ <b>القيمة المضافة:</b> <code>+{added_amount:,} NSP</code>\n"
            f"💰 <b>الرصيد الحالي:</b> <code>{new_balance:,} NSP</code>\n\n"
            f"⏰ <b>الوقت:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ <i>تم تحديث الرصيد بنجاح</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━"
        )

        # إرسال إلى قناة السجلات
        if log_channel_id:
            try:
                await bot.send_message(chat_id=log_channel_id, text=notification_text, parse_mode="HTML")
                logger.info("✅ Agent balance increase notification sent to log channel")
            except Exception as e:
                logger.warning(f"Failed to send agent balance increase notification to log channel: {e}")

        # إرسال إلى المشرفين
        admin_ids = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]
        for admin_id in admin_ids:
            try:
                await bot.send_message(chat_id=admin_id, text=notification_text, parse_mode="HTML")
                logger.info(f"✅ Agent balance increase notification sent to admin {admin_id}")
            except Exception as e:
                logger.warning(f"Failed to send agent balance increase notification to {admin_id}: {e}")

    except Exception as e:
        logger.error(f"❌ Failed to notify agent balance increase: {e}", exc_info=True)


async def on_startup(dispatcher: Dispatcher, bot: Bot):
    """تهيئة البوت عند بدء التشغيل."""
    global watchdog_task, ensure_webhook_task, daily_report_task, routers_registered

    logger.info("🚀 Caesar_Bot is starting...")

    # ✅ فحص الإعدادات بدون قتل العملية (المنفذ مربوط قبل هاد الكود)
    try:
        settings.validate_config()
    except ValueError as e:
        logger.critical(f"FATAL Configuration error: {e}")
        return  # منترك on_startup يخلص، المنفذ مفتوح، والمشكلة ظاهرة بلوغات

    try:
        DatabaseManager.initialize_pool()
    except Exception as e:
        logger.critical(f"FATAL Failed to connect to database: {e}")
        return  # منترك on_startup يخلص، والصحة تعمل، المشكلة بلوغات

    dispatcher.message.outer_middleware(TermsCheckMiddleware())
    dispatcher.callback_query.outer_middleware(TermsCheckMiddleware())

    if not routers_registered:
        dispatcher.include_router(start.router)
        dispatcher.include_router(menu.router)
        dispatcher.include_router(admin.router)
        routers_registered = True

    commands = [
        BotCommand(command="start", description="🏠 القائمة الرئيسية"),
        BotCommand(command="cancel", description="❌ إلغاء العملية الحالية"),
        BotCommand(command="home", description="🔙 العودة للقائمة الرئيسية"),
        BotCommand(command="delete", description="🗑️ حذف حسابي"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

    if watchdog_task is None or watchdog_task.done():
        watchdog_task = asyncio.create_task(cookie_watchdog_task(bot))

    if ensure_webhook_task is None or ensure_webhook_task.done():
        ensure_webhook_task = asyncio.create_task(ensure_webhook(bot))

    # 🆕 بدء مهمة التقرير المالي اليومي
    if daily_report_task is None or daily_report_task.done():
        daily_report_task = asyncio.create_task(daily_report_scheduler(bot))
        logger.info("📊 Daily financial report scheduler started.")


async def on_shutdown(dispatcher: Dispatcher, bot: Bot):
    """تنظيف الموارد عند إيقاف البوت."""
    logger.info("🛑 Shutting down...")

    for task in [watchdog_task, ensure_webhook_task, daily_report_task]:
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    with suppress(Exception):
        await bot.delete_webhook()
    with suppress(Exception):
        await bot.session.close()


def _verify_telegram_init_data(init_data_raw):
    """🔒 التحقق الرسمي من توقيع initData المرسل من Mini App (HMAC-SHA256).

    يتبع خوارزمية Telegram الرسمية لضمان أن الطلب صادر فعلاً من تيليجرام
    وغير مُزوّر. التزوير مستحيل رياضياً لأنه يتطلب BOT_TOKEN (سرّي).

    تعيد dict بيانات المستخدم (user) إذا كان التوقيع صحيحاً، أو None إذا كان مزوّراً/منتهي الصلاحية.
    مرجع: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data_raw:
        return None

    bot_token = settings.BOT_TOKEN
    if not bot_token:
        logger.error("BOT_TOKEN missing — cannot verify Mini App initData")
        return None

    try:
        # فكّ السلسلة إلى أزواج (مفتاح=قيمة)
        parsed = urllib.parse.parse_qs(init_data_raw, keep_blank_values=True)

        # استخراج التوقيع المُرسل من تيليجرام
        received_hash = parsed.get('hash', [None])[0]
        if not received_hash:
            return None

        # بناء data_check_string: كل المفاتيح (عدا hash) مرتّبة أبجدياً، كل مفتاح على سطر "key=value"
        # ملاحظة: يجب أن تكون القيمة غير مشفّرة URL (parse_qs يفك الترميز تلقائياً) كما تشترط تيليجرام
        vk_pairs = []
        for key in parsed:
            if key == 'hash':
                continue
            vk_pairs.append(f"{key}={parsed[key][0]}")
        vk_pairs.sort()
        data_check_string = '\n'.join(vk_pairs)

        # حساب secret_key = HMAC-SHA256(key="WebAppData", message=BOT_TOKEN)
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=bot_token.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()

        # حساب التوقيع = HMAC-SHA256(key=secret_key, message=data_check_string)
        calculated_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()

        # مقارنة آمنة ضد هجمات التوقيت (timing attack)
        if not hmac.compare_digest(calculated_hash, received_hash):
            logger.warning("🔒 Mini App initData hash mismatch — possible forgery attempt rejected.")
            return None

        # فحص إضافي: منع إعادة استخدام الطلبات القديمة (replay attack)
        # نرفض أي طلب أقدم من 24 ساعة
        auth_date_str = parsed.get('auth_date', [None])[0]
        if auth_date_str:
            try:
                auth_date = int(auth_date_str)
                if abs(time.time() - auth_date) > 86400:  # 24 ساعة
                    logger.warning(f"🔒 Mini App initData expired (auth_date={auth_date}) — rejected.")
                    return None
            except (ValueError, TypeError):
                logger.warning("🔒 Invalid auth_date in Mini App initData — rejected.")
                return None

        # استخراج بيانات المستخدم من الحقل user (JSON)
        user_json = parsed.get('user', [None])[0]
        if user_json:
            return json.loads(user_json)
        return {}
    except Exception as e:
        logger.warning(f"🔒 Mini App initData verification failed: {e}")
        return None


def _is_admin(init_data_raw):
    """🔒 التحقق من أن مستخدم Mini App هو أدمن — عبر التحقق الرسمي من توقيع Telegram أولاً."""
    user_obj = _verify_telegram_init_data(init_data_raw)
    if not user_obj:
        return False
    admin_ids = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]
    return str(user_obj.get('id', '')) in admin_ids



async def dashboard_api_handler(request):
    """🆕 API يغذّي لوحة التحكم بالبيانات (للمشرفين فقط)."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)

    try:
        bot_settings = repo.get_bot_settings()
        is_cookie_alive = await ichancy_api_client.check_session_validity()
        pending = repo.get_pending_requests()
        recent = repo.get_all_transactions(10)

        pending_deposits = [{
            'id': t['id'], 'amount': float(t['amount']),
            'created_at_str': t['created_at'].strftime('%m-%d %H:%M') if t.get('created_at') else ''
        } for t in pending if t['type'] == 'deposit_bot']
        pending_withdraws = [{
            'id': t['id'], 'amount': float(t['amount']),
            'created_at_str': t['created_at'].strftime('%m-%d %H:%M') if t.get('created_at') else ''
        } for t in pending if t['type'] == 'withdraw_bot']
        recent_transactions = [{
            'id': t['id'], 'type': t['type'], 'amount': float(t['amount']), 'status': t['status']
        } for t in recent[:5]]

        # 🆕 (Update 19) بيانات مالية حية للوحة القيادة
        today_deposits = DatabaseManager.execute_query_dict(
            """SELECT COALESCE(SUM(amount), 0) as total FROM transactions
               WHERE type = 'deposit_bot' AND status = 'approved'
               AND created_at::date = CURRENT_DATE""",
            fetch='one'
        )
        today_withdraws = DatabaseManager.execute_query_dict(
            """SELECT COALESCE(SUM(amount), 0) as total FROM transactions
               WHERE type = 'withdraw_bot' AND status = 'approved'
               AND created_at::date = CURRENT_DATE""",
            fetch='one'
        )
        today_game_deposits = DatabaseManager.execute_query_dict(
            """SELECT COALESCE(SUM(amount), 0) as total FROM transactions
               WHERE type = 'deposit_to_game' AND status IN ('completed', 'approved')
               AND created_at::date = CURRENT_DATE""",
            fetch='one'
        )
        today_bonus_paid = DatabaseManager.execute_query_dict(
            """SELECT COALESCE(SUM(amount), 0) as total FROM transactions
               WHERE type IN ('deposit_to_game', 'deposit_bot') AND status IN ('completed', 'approved')
               AND created_at::date = CURRENT_DATE
               AND transfer_number LIKE '%%Bonus%%'""",
            fetch='one'
        )
        dep_total = float(today_deposits.get('total', 0)) if today_deposits else 0
        wd_total = float(today_withdraws.get('total', 0)) if today_withdraws else 0
        game_dep = float(today_game_deposits.get('total', 0)) if today_game_deposits else 0
        bonus_paid = float(today_bonus_paid.get('total', 0)) if today_bonus_paid else 0
        agent_rev_pct = float(bot_settings.get('agent_revenue_percent') or 30)
        estimated_burn = game_dep * 0.70  # تقدير: 70% من شحنات اللعبة تُحرق
        estimated_revenue = estimated_burn * (agent_rev_pct / 100.0)
        net_profit = dep_total - wd_total - bonus_paid + estimated_revenue

        # 🆕 بيانات الرسوم البيانية (7 أيام)
        chart_data = DatabaseManager.execute_query_dict(
            """SELECT
                d::date as date,
                COALESCE(SUM(CASE WHEN t.type = 'deposit_bot' AND t.status = 'approved' THEN t.amount ELSE 0 END), 0) as deposits,
                COALESCE(SUM(CASE WHEN t.type = 'withdraw_bot' AND t.status = 'approved' THEN t.amount ELSE 0 END), 0) as withdraws
               FROM generate_series(CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, '1 day') as d
               LEFT JOIN transactions t ON t.created_at::date = d::date
               GROUP BY d::date ORDER BY d::date""",
            fetch='all'
        ) or []
        chart_labels = [r.get('date', '').strftime('%m-%d') if hasattr(r.get('date'), 'strftime') else str(r.get('date', '')) for r in chart_data]
        chart_deposits = [float(r.get('deposits') or 0) for r in chart_data]
        chart_withdraws = [float(r.get('withdraws') or 0) for r in chart_data]

        # 🆕 إحصائيات الميزات
        wheel_stats = {}
        try:
            wheel_stats = repo.get_wheel_stats()
        except Exception:
            pass

        cashback_stats = {}
        try:
            cashback_stats = repo.get_cashback_stats()
        except Exception:
            pass

        checkin_stats = {}
        try:
            checkin_stats = repo.get_checkin_stats()
        except Exception:
            pass

        # 🆕 عداد المستخدمين الخاملين
        inactive_users = DatabaseManager.execute_query(
            """SELECT COUNT(*) FROM users
               WHERE terms_accepted = TRUE
               AND telegram_id NOT IN (
                   SELECT DISTINCT user_telegram_id FROM transactions
                   WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
               )""",
            fetch='one'
        )
        inactive_count = int(inactive_users[0]) if inactive_users else 0

        agent_balance_alert = int(bot_settings.get('agent_balance', 0)) < int(getattr(settings, 'AGENT_BALANCE_ALERT_THRESHOLD', 100000))

        data = {
            'total_users': repo.get_total_users_count(),
            'new_users_today': repo.get_new_users_today(),
            'today_tx_count': repo.get_today_transactions_count(),
            'approved_volume': repo.get_transactions_volume('approved'),
            'total_bot_balance': await _get_total_bot_balance_cached(),
            'agent_balance': int(bot_settings.get('agent_balance', 0)),
            'usd_buy_rate': float(bot_settings['usd_buy_rate']),
            'usd_sell_rate': float(bot_settings['usd_sell_rate']),
            'exchange_rate': int(bot_settings['exchange_rate']),
            'withdraw_commission': float(bot_settings['withdraw_commission']),
            'game_min_deposit_syp': int(bot_settings.get('game_min_deposit_syp') or 20000),
            'agent_revenue_percent': agent_rev_pct,
            'min_deposit_syp': int(bot_settings.get('min_deposit_syp') or 20000),
            'min_deposit_usd': int(bot_settings.get('min_deposit_usd') or 5),
            'min_withdraw_syp': int(bot_settings.get('min_withdraw_syp') or 25000),
            'min_withdraw_usd': int(bot_settings.get('min_withdraw_usd') or 10),
            'syp_version': str(bot_settings.get('syp_version') or 'old'),
            'is_cookie_alive': is_cookie_alive,
            'cookie_age_minutes': repo.get_cookie_age_minutes(),
            'pending_deposits': pending_deposits,
            'pending_withdraws': pending_withdraws,
            'recent_transactions': recent_transactions,
            # 🆕 (Update 19)
            'today_deposits': dep_total,
            'today_withdraws': wd_total,
            'today_game_deposits': game_dep,
            'today_bonus_paid': bonus_paid,
            'estimated_burn': estimated_burn,
            'estimated_revenue': estimated_revenue,
            'net_profit': net_profit,
            'chart_labels': chart_labels,
            'chart_deposits': chart_deposits,
            'chart_withdraws': chart_withdraws,
            'wheel_stats': wheel_stats,
            'cashback_stats': cashback_stats,
            'checkin_stats': checkin_stats,
            'inactive_users': inactive_count,
            'agent_balance_alert': agent_balance_alert,
            'pending_count': len(pending_deposits) + len(pending_withdraws),
        }
        return web.json_response(data)
    except Exception as e:
        logger.error(f"Dashboard API error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)



async def admin_settings_get_handler(request):
    """API إعدادات الأدمن للـ Mini App."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    try:
        bot_settings = repo.get_bot_settings()
        return web.json_response({
            'exchange_rate': int(bot_settings.get('exchange_rate') or 1000),
            'usd_buy_rate': float(bot_settings.get('usd_buy_rate') or 0),
            'usd_sell_rate': float(bot_settings.get('usd_sell_rate') or 0),
            'withdraw_commission': float(bot_settings.get('withdraw_commission') or 0),
            'game_min_deposit_syp': int(bot_settings.get('game_min_deposit_syp') or 20000),
            'agent_revenue_percent': float(bot_settings.get('agent_revenue_percent') or 30),
            'min_deposit_syp': int(bot_settings.get('min_deposit_syp') or 20000),
            'min_deposit_usd': int(bot_settings.get('min_deposit_usd') or 5),
            'min_withdraw_syp': int(bot_settings.get('min_withdraw_syp') or 25000),
            'min_withdraw_usd': int(bot_settings.get('min_withdraw_usd') or 10),
            'syp_version': str(bot_settings.get('syp_version') or 'old'),
            'referrals_enabled': bool(bot_settings.get('referrals_enabled', True)),
            'payment_addresses': repo.get_all_payment_addresses(),
            'button_links': repo.get_all_button_links(),
        })
    except Exception as e:
        logger.error(f"Admin settings GET error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


async def admin_settings_post_handler(request):
    """حفظ إعدادات الأدمن من الـ Mini App."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    try:
        payload = await request.json()
        action = payload.get('action')

        if action == 'update_rates':
            exchange_rate = int(str(payload.get('exchange_rate', '')).replace(',', ''))
            usd_buy_rate = float(str(payload.get('usd_buy_rate', '')).replace(',', ''))
            usd_sell_rate = float(str(payload.get('usd_sell_rate', '')).replace(',', ''))
            withdraw_commission = float(str(payload.get('withdraw_commission', '')).replace('%', '').replace(',', ''))
            game_min_deposit_syp = int(str(payload.get('game_min_deposit_syp', '')).replace(',', ''))
            agent_revenue_percent = float(str(payload.get('agent_revenue_percent', '30')).replace('%', '').replace(',', ''))
            # 🆕 حدود الإيداع والسحب الدنيا
            min_deposit_syp = int(str(payload.get('min_deposit_syp', '')).replace(',', ''))
            min_deposit_usd = int(str(payload.get('min_deposit_usd', '')).replace(',', ''))
            min_withdraw_syp = int(str(payload.get('min_withdraw_syp', '')).replace(',', ''))
            min_withdraw_usd = int(str(payload.get('min_withdraw_usd', '')).replace(',', ''))
            syp_version = str(payload.get('syp_version', 'old')).strip()
            if syp_version not in ('old', 'new'):
                syp_version = 'old'
            referrals_enabled_raw = payload.get('referrals_enabled', True)
            referrals_enabled = str(referrals_enabled_raw).lower() in ('1', 'true', 'yes', 'on') if not isinstance(referrals_enabled_raw, bool) else referrals_enabled_raw
            if exchange_rate <= 0 or usd_buy_rate <= 0 or usd_sell_rate <= 0 or withdraw_commission < 0 or game_min_deposit_syp < 1 or agent_revenue_percent < 0 or min_deposit_syp < 1 or min_deposit_usd < 1 or min_withdraw_syp < 1 or min_withdraw_usd < 1:
                return web.json_response({'error': 'قيم غير صالحة'}, status=400)
            # 🔒 حماية السبريد (Update 9): سعر الإيداع يجب أن يكون أقل من سعر السحب
            # لمنع المراجحة المالية (المستخدم يودع دولار ثم يسحبه بربح).
            if usd_buy_rate >= usd_sell_rate:
                return web.json_response({'error': '⚠️ سعر الإيداع يجب أن يكون أقل من سعر السحب لتجنب المراجحة المالية. راجع الأسعار.'}, status=400)
            repo.update_bot_settings(
                exchange_rate=exchange_rate,
                usd_buy_rate=usd_buy_rate,
                usd_sell_rate=usd_sell_rate,
                withdraw_commission=withdraw_commission,
                game_min_deposit_syp=game_min_deposit_syp,
                agent_revenue_percent=agent_revenue_percent,
                referrals_enabled=referrals_enabled,
                min_deposit_syp=min_deposit_syp,
                min_deposit_usd=min_deposit_usd,
                min_withdraw_syp=min_withdraw_syp,
                min_withdraw_usd=min_withdraw_usd,
                syp_version=syp_version,
            )
            return web.json_response({'ok': True})

        if action == 'update_payment_address':
            method = payload.get('method')
            address = (payload.get('address') or '').strip()
            if method not in repo.PAYMENT_METHOD_LABELS:
                return web.json_response({'error': 'طريقة دفع غير معروفة'}, status=400)
            if not address:
                return web.json_response({'error': 'العنوان فارغ'}, status=400)
            if len(address) > 900:
                return web.json_response({'error': 'العنوان طويل جداً'}, status=400)
            repo.set_payment_address(method, address, updated_by='miniapp')
            return web.json_response({'ok': True})

        if action == 'reset_payment_address':
            method = payload.get('method')
            if method not in repo.PAYMENT_METHOD_LABELS:
                return web.json_response({'error': 'طريقة دفع غير معروفة'}, status=400)
            repo.reset_payment_address(method)
            return web.json_response({'ok': True})


        if action == 'update_button_link':
            key = payload.get('key')
            url = (payload.get('url') or '').strip()
            if key not in repo.BUTTON_LINK_LABELS:
                return web.json_response({'error': 'زر غير معروف'}, status=400)
            if not url:
                return web.json_response({'error': 'الرابط فارغ'}, status=400)
            if len(url) > 900 or not (url.startswith('http://') or url.startswith('https://')):
                return web.json_response({'error': 'الرابط غير صالح'}, status=400)
            repo.set_button_link(key, url, updated_by='miniapp')
            return web.json_response({'ok': True})

        if action == 'reset_button_link':
            key = payload.get('key')
            if key not in repo.BUTTON_LINK_LABELS:
                return web.json_response({'error': 'زر غير معروف'}, status=400)
            repo.reset_button_link(key)
            return web.json_response({'ok': True})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except ValueError:
        return web.json_response({'error': 'صيغة رقمية غير صحيحة'}, status=400)
    except Exception as e:
        logger.error(f"Admin settings POST error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)



def _parse_ichancy_date(value):
    if not value:
        return None
    text = str(value).replace('\xa0', ' ').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    return None


def _money(value):
    try:
        return Decimal(str(value).replace(',', '').strip())
    except Exception:
        return Decimal('0')


def _summarize_agent_transactions(records):
    players = {}
    total_to_players = Decimal('0')
    total_from_players = Decimal('0')
    bot_to_players = Decimal('0')
    bot_from_players = Decimal('0')
    manual_to_players = Decimal('0')
    manual_from_players = Decimal('0')
    latest = []

    for r in records or []:
        if str(r.get('transferStatus', '')).lower() != 'success':
            continue
        amount = _money(r.get('amount'))
        abs_amount = abs(amount)
        username = r.get('toUserName') or 'unknown'
        user_id = str(r.get('toUserId') or '')
        key = user_id or username
        comment = r.get('transferComment') or ''
        is_bot = 'Caesar_Bot' in comment
        direction = 'to_player' if amount > 0 else 'from_player'

        p = players.setdefault(key, {
            'key': key,
            'toUserName': username,
            'toUserId': user_id,
            'deposit_to_player': 0.0,
            'withdraw_from_player': 0.0,
            'net': 0.0,
            'operations_count': 0,
            'bot_operations_count': 0,
            'manual_operations_count': 0,
            'last_operation_at': '',
        })
        if direction == 'to_player':
            total_to_players += abs_amount
            p['deposit_to_player'] += float(abs_amount)
            if is_bot:
                bot_to_players += abs_amount
            else:
                manual_to_players += abs_amount
        else:
            total_from_players += abs_amount
            p['withdraw_from_player'] += float(abs_amount)
            if is_bot:
                bot_from_players += abs_amount
            else:
                manual_from_players += abs_amount
        p['net'] = p['withdraw_from_player'] - p['deposit_to_player']
        p['operations_count'] += 1
        if is_bot:
            p['bot_operations_count'] += 1
        else:
            p['manual_operations_count'] += 1
        dt = _parse_ichancy_date(r.get('date'))
        if dt and (not p['last_operation_at'] or dt > _parse_ichancy_date(p['last_operation_at'])):
            p['last_operation_at'] = dt.strftime('%Y-%m-%d %H:%M:%S')

        latest.append({
            'transactionId': r.get('transactionId'),
            'toUserName': username,
            'toUserId': user_id,
            'type': 'شحن للّاعب' if direction == 'to_player' else 'سحب من اللاعب',
            'amount': float(abs_amount),
            'signed_amount': float(amount),
            'date': str(r.get('date') or '').replace('\xa0', ' '),
            'source': 'bot' if is_bot else 'manual',
            'comment': comment,
            'beforeBalance': r.get('beforeBalance'),
            'afterBalance': r.get('afterBalance'),
        })

    players_list = sorted(players.values(), key=lambda x: x['operations_count'], reverse=True)
    latest.sort(key=lambda x: _parse_ichancy_date(x.get('date')) or datetime.min, reverse=True)
    return {
        'summary': {
            'total_deposit_to_players': float(total_to_players),
            'total_withdraw_from_players': float(total_from_players),
            'net_movement': float(total_from_players - total_to_players),
            'bot_deposit_to_players': float(bot_to_players),
            'bot_withdraw_from_players': float(bot_from_players),
            'manual_deposit_to_players': float(manual_to_players),
            'manual_withdraw_from_players': float(manual_from_players),
            'operations_count': len(latest),
            'players_count': len(players_list),
        },
        'players': players_list,
        'latest_transactions': latest[:50],
    }


async def admin_agent_finance_handler(request):
    """تقرير حركات الكاشيرة/الوكيل للـ Mini App."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        now = datetime.now()
        default_from = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        from_date = payload.get('from') or default_from.strftime('%Y/%m/%d %H:%M:%S')
        to_date = payload.get('to') or now.strftime('%Y/%m/%d %H:%M:%S')
        limit = min(int(payload.get('limit') or 1000), 1000)
        raw = await ichancy_api_client.get_agent_transaction_list(from_date, to_date, limit=limit, start=0, is_to_me=False)
        result = raw.get('result') if isinstance(raw, dict) else {}
        records = result.get('records', []) if isinstance(result, dict) else []
        data = _summarize_agent_transactions(records)
        bot_settings = repo.get_bot_settings()
        revenue_percent = float(bot_settings.get('agent_revenue_percent') or 30)

        # مطابقة لاعبي الكاشيرة مع مستخدمي البوت ثم جلب الرصيد الحالي لحساب الحرق و Revenue.
        matched_count = 0
        unmatched_count = 0
        current_balance_total = Decimal('0')
        burn_positive_total = Decimal('0')
        player_profit_total = Decimal('0')
        revenue_total = Decimal('0')

        # حد أمان حتى لا نضغط على iChancy إذا كان التقرير كبيراً جداً.
        max_balance_fetch = min(int(payload.get('balance_limit') or 60), 100)
        fetched_balances = 0

        for p in data.get('players', []):
            matched_user = repo.get_user_by_ichancy_identity(
                player_id=p.get('toUserId'),
                ichancy_username=p.get('toUserName')
            )
            if not matched_user:
                p['matched'] = False
                p['current_balance'] = None
                p['burn'] = None
                p['revenue'] = None
                unmatched_count += 1
                continue

            p['matched'] = True
            p['telegram_id'] = str(matched_user.get('telegram_id'))
            p['telegram_username'] = matched_user.get('telegram_username')
            p['bot_player_id'] = matched_user.get('player_id')
            matched_count += 1

            if fetched_balances >= max_balance_fetch:
                p['current_balance'] = int(matched_user.get('game_balance') or 0)
                p['balance_source'] = 'cached'
            else:
                current_balance = await ichancy_api_client.get_player_balance(matched_user.get('player_id'))
                repo.update_user_game_balance(matched_user.get('telegram_id'), current_balance)
                p['current_balance'] = int(current_balance or 0)
                p['balance_source'] = 'live'
                fetched_balances += 1

            deposit = Decimal(str(p.get('deposit_to_player') or 0))
            withdraw = Decimal(str(p.get('withdraw_from_player') or 0))
            balance = Decimal(str(p.get('current_balance') or 0))
            burn = deposit - withdraw - balance
            revenue = (burn * Decimal(str(revenue_percent)) / Decimal('100')) if burn > 0 else Decimal('0')

            p['burn'] = float(burn)
            p['revenue'] = float(revenue)

            current_balance_total += balance
            if burn > 0:
                burn_positive_total += burn
                revenue_total += revenue
            elif burn < 0:
                player_profit_total += abs(burn)

        data['agent_revenue_percent'] = revenue_percent
        data['burn_summary'] = {
            'matched_players': matched_count,
            'unmatched_players': unmatched_count,
            'balances_fetched_live': fetched_balances,
            'current_balance_total': float(current_balance_total),
            'burn_positive_total': float(burn_positive_total),
            'player_profit_total': float(player_profit_total),
            'net_burn': float(burn_positive_total - player_profit_total),
            'estimated_revenue': float(revenue_total),
        }
        data['from'] = from_date
        data['to'] = to_date
        data['total_records_count'] = int(result.get('totalRecordsCount') or len(records)) if isinstance(result, dict) else len(records)
        data['note'] = 'الحرق = الشحن - السحب - الرصيد الحالي. Revenue = الحرق الموجب × نسبة الوكيل.'
        return web.json_response(data)
    except Exception as e:
        logger.error(f"Admin agent finance error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)



def _format_uptime(seconds):
    seconds = int(seconds or 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days} يوم و {hours} ساعة"
    if hours:
        return f"{hours} ساعة و {minutes} دقيقة"
    return f"{minutes} دقيقة"


async def admin_health_handler(request):
    """حالة النظام: السيرفر، Neon، Webhook، iChancy، والكاشيرة."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)

    bot = request.app.get('bot')
    checks = {}

    # Server
    checks['server'] = {
        'ok': True,
        'uptime_seconds': int(time.time() - SERVER_START_TS),
        'uptime_text': _format_uptime(time.time() - SERVER_START_TS),
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    # Database latency
    conn = None
    cur = None
    try:
        t0 = time.perf_counter()
        conn = DatabaseManager.get_connection()
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        checks['database'] = {'ok': True, 'latency_ms': latency_ms}
    except Exception as e:
        checks['database'] = {'ok': False, 'error': str(e)[:300]}
    finally:
        if cur:
            cur.close()
        if conn:
            DatabaseManager.put_connection(conn)

    # Webhook
    try:
        if bot:
            wh = await bot.get_webhook_info()
            checks['webhook'] = {
                'ok': bool(wh.url),
                'url': wh.url,
                'pending_update_count': wh.pending_update_count,
                'last_error_date': wh.last_error_date,
                'last_error_message': wh.last_error_message,
            }
        else:
            checks['webhook'] = {'ok': False, 'error': 'bot unavailable'}
    except Exception as e:
        checks['webhook'] = {'ok': False, 'error': str(e)[:300]}

    # iChancy session
    try:
        is_valid = await ichancy_api_client.check_session_validity()
        cookie_age = repo.get_cookie_age_minutes()
        checks['ichancy'] = {
            'ok': bool(is_valid),
            'cookie_age_minutes': cookie_age,
            'cookie_age_text': '—' if cookie_age is None else _format_uptime(cookie_age * 60),
            'message': 'الجلسة نشطة، لا يلزم تحديث الكوكيز طالما العمليات تعمل.' if is_valid else 'الجلسة غير نشطة، قد تحتاج تحديث الكوكيز.',
        }
    except Exception as e:
        checks['ichancy'] = {'ok': False, 'error': str(e)[:300]}

    # Cashier endpoint quick ping
    try:
        now = datetime.now()
        from_date = (now - timedelta(days=1)).strftime('%Y/%m/%d %H:%M:%S')
        to_date = now.strftime('%Y/%m/%d %H:%M:%S')
        raw = await ichancy_api_client.get_agent_transaction_list(from_date, to_date, limit=1, start=0, is_to_me=False)
        result = raw.get('result') if isinstance(raw, dict) else {}
        checks['cashier'] = {
            'ok': bool(raw.get('status')) if isinstance(raw, dict) else False,
            'total_records_count': int(result.get('totalRecordsCount') or 0) if isinstance(result, dict) else 0,
        }
    except Exception as e:
        checks['cashier'] = {'ok': False, 'error': str(e)[:300]}

    # Quick DB stats
    try:
        checks['stats'] = {
            'total_users': repo.get_total_users_count(),
            'pending_requests': len(repo.get_pending_transactions()),
            'today_tx': repo.get_today_transactions_count(),
        }
    except Exception as e:
        checks['stats'] = {'error': str(e)[:300]}

    return web.json_response(checks)


async def admin_neon_handler(request):
    """📊 مقاييس Neon (الخطة المجانية) — endpoint منفصل بكاش 15 دقيقة.

    مفصول عمداً عن admin_health_handler كي لا يتسبّب بطء/تعطّل Neon الخارجي
    في إبطاء أو تعطيل فحص الصحة المحلي السريع.
    """
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)

    try:
        # نمرّر جلسة aiohttp المشتركة (يديرها aiogram) إن وُجدت لتفادي فتح جلسة لكل طلب
        session = request.app.get('neon_session')
        data = await get_neon_metrics(session=session)
        status = 200 if data.get('ok') else 502
        return web.json_response(data, status=status)
    except Exception as e:
        logger.error(f"Neon metrics handler error: {e}", exc_info=True)
        return web.json_response({'ok': False, 'error': 'خطأ داخلي'}, status=500)


async def admin_render_handler(request):
    """📊 حالة خدمة Render (الخطة المجانية) — endpoint منفصل بكاش 60 ثانية."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)

    try:
        # نعيد استخدام نفس جلسة aiohttp المشتركة (المُستخدمة لـ Neon)
        session = request.app.get('neon_session')
        data = await get_render_metrics(session=session)
        status = 200 if data.get('ok') else 502
        return web.json_response(data, status=status)
    except Exception as e:
        logger.error(f"Render metrics handler error: {e}", exc_info=True)
        return web.json_response({'ok': False, 'error': 'خطأ داخلي'}, status=500)


def _broadcast_text(title, message, message_type='announcement'):
    icons = {
        'announcement': '📢',
        'alert': '⚠️',
        'maintenance': '🛠️',
        'offer': '🎁',
        'update': '✨',
    }
    icon = icons.get(message_type, '📢')
    title = (title or 'تنبيه من الإدارة').strip()
    message = (message or '').strip()
    return f"{icon} {title}\n\n{message}"


async def admin_broadcast_handler(request):
    """إرسال جماعي آمن من لوحة الأدمن."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    bot = request.app.get('bot')
    if not bot:
        return web.json_response({'error': 'bot unavailable'}, status=500)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    action = payload.get('action') or 'count'
    audience = payload.get('audience') or 'all'
    title = (payload.get('title') or '').strip()
    message = (payload.get('message') or '').strip()
    message_type = payload.get('message_type') or 'announcement'

    if action == 'count':
        targets = repo.get_broadcast_targets(audience=audience, limit=20000)
        return web.json_response({'ok': True, 'count': len(targets)})

    if not message:
        return web.json_response({'error': 'نص الرسالة فارغ'}, status=400)
    if len(message) > 3500:
        return web.json_response({'error': 'الرسالة طويلة جداً'}, status=400)

    text = _broadcast_text(title, message, message_type)
    admin_ids = [item.strip() for item in str(getattr(settings, 'ADMIN_IDS', settings.ADMIN_ID)).split(',') if item.strip()]

    if action == 'test':
        sent = 0
        failed = 0
        errors = []
        for admin_id in admin_ids:
            try:
                await bot.send_message(chat_id=admin_id, text=text)
                sent += 1
            except Exception as e:
                failed += 1
                errors.append(str(e)[:200])
        return web.json_response({'ok': True, 'sent': sent, 'failed': failed, 'errors': errors[:3]})

    if action != 'send':
        return web.json_response({'error': 'إجراء غير معروف'}, status=400)

    targets = repo.get_broadcast_targets(audience=audience, limit=20000)
    broadcast_id = repo.create_broadcast(title, message, audience, message_type, created_by='miniapp', total_targets=len(targets))
    repo.update_broadcast_status(broadcast_id, 'sending', started=True)

    sent = 0
    failed = 0
    last_error = None
    # إرسال تدريجي لتفادي حدود Telegram
    for telegram_id in targets:
        try:
            await bot.send_message(chat_id=telegram_id, text=text)
            sent += 1
        except Exception as e:
            failed += 1
            last_error = str(e)[:300]
        if (sent + failed) % 25 == 0:
            repo.update_broadcast_status(broadcast_id, 'sending', sent_count=sent, failed_count=failed, last_error=last_error)
        await asyncio.sleep(0.18)

    repo.update_broadcast_status(broadcast_id, 'finished', sent_count=sent, failed_count=failed, last_error=last_error, finished=True)
    return web.json_response({'ok': True, 'broadcast_id': broadcast_id, 'total': len(targets), 'sent': sent, 'failed': failed, 'last_error': last_error})


async def admin_broadcasts_recent_handler(request):
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    rows = repo.get_recent_broadcasts(10)
    out = []
    for r in rows:
        out.append({
            'id': r.get('id'), 'title': r.get('title'), 'audience': r.get('audience'),
            'message_type': r.get('message_type'), 'status': r.get('status'),
            'total_targets': r.get('total_targets'), 'sent_count': r.get('sent_count'),
            'failed_count': r.get('failed_count'),
            'created_at': r.get('created_at').strftime('%Y-%m-%d %H:%M') if r.get('created_at') else '',
            'last_error': r.get('last_error')
        })
    return web.json_response({'items': out})



def _tx_to_json(tx):
    return {
        'id': tx.get('id'),
        'type': tx.get('type'),
        'payment_method': tx.get('payment_method'),
        'amount': float(tx.get('amount') or 0),
        'status': tx.get('status'),
        'created_at': tx.get('created_at').strftime('%Y-%m-%d %H:%M') if tx.get('created_at') else '',
        'original_amount': float(tx.get('original_amount') or 0) if tx.get('original_amount') is not None else None,
        'original_currency': tx.get('original_currency'),
        'description': tx.get('transfer_number'),
    }


def _user_to_json(user):
    if not user:
        return None
    return {
        'telegram_id': str(user.get('telegram_id')),
        'telegram_username': user.get('telegram_username'),
        'ichancy_username': user.get('ichancy_username'),
        'player_id': user.get('player_id'),
        'bot_balance': int(user.get('bot_balance') or 0),
        'game_balance': int(user.get('game_balance') or 0),
        'terms_accepted': bool(user.get('terms_accepted')),
        'created_at': user.get('created_at').strftime('%Y-%m-%d %H:%M') if user.get('created_at') else '',
    }


async def admin_users_handler(request):
    """مركز المستخدمين في Mini App: بحث، تفاصيل، رسالة، تعديل رصيد، تحديث رصيد اللعبة."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    bot = request.app.get('bot')
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get('action') or 'search'

    try:
        if action == 'search':
            query = (payload.get('query') or '').strip()
            if not query:
                return web.json_response({'users': []})
            users = repo.search_user(query)
            return web.json_response({'users': [_user_to_json(u) for u in users]})

        if action == 'detail':
            telegram_id = str(payload.get('telegram_id') or '').strip()
            user = repo.get_user(telegram_id)
            if not user:
                return web.json_response({'error': 'المستخدم غير موجود'}, status=404)
            history = repo.get_user_transactions_history(telegram_id, limit=20)
            stats = repo.get_transaction_stats_for_user(telegram_id)
            return web.json_response({
                'user': _user_to_json(user),
                'stats': stats,
                'history': [_tx_to_json(tx) for tx in history],
            })

        if action == 'message':
            telegram_id = str(payload.get('telegram_id') or '').strip()
            text = (payload.get('message') or '').strip()
            if not telegram_id or not text:
                return web.json_response({'error': 'بيانات ناقصة'}, status=400)
            if len(text) > 3500:
                return web.json_response({'error': 'الرسالة طويلة جداً'}, status=400)
            await bot.send_message(chat_id=telegram_id, text=f"📩 رسالة من الإدارة\n\n{text}")
            return web.json_response({'ok': True})

        if action == 'adjust_balance':
            telegram_id = str(payload.get('telegram_id') or '').strip()
            delta = int(str(payload.get('delta') or '0').replace(',', ''))
            if not telegram_id or delta == 0:
                return web.json_response({'error': 'أدخل قيمة موجبة أو سالبة'}, status=400)
            ok = repo.adjust_user_bot_balance(telegram_id, delta)
            if not ok:
                return web.json_response({'error': 'فشل تعديل الرصيد. قد يصبح الرصيد سالباً أو المستخدم غير موجود.'}, status=400)
            user = repo.get_user(telegram_id)
            try:
                sign = '+' if delta > 0 else ''
                await bot.send_message(chat_id=telegram_id, text=f"💎 تم تحديث رصيدك في البوت: {sign}{delta:,} SYP\nرصيدك الحالي: {int(user.get('bot_balance') or 0):,} SYP")
            except Exception:
                pass
            return web.json_response({'ok': True, 'user': _user_to_json(user)})

        if action == 'set_balance':
            telegram_id = str(payload.get('telegram_id') or '').strip()
            new_balance = int(str(payload.get('new_balance') or '0').replace(',', ''))
            if new_balance < 0:
                return web.json_response({'error': 'الرصيد لا يمكن أن يكون سالباً'}, status=400)
            ok = repo.set_user_balance(telegram_id, new_balance)
            if not ok:
                return web.json_response({'error': 'فشل تعيين الرصيد'}, status=400)
            return web.json_response({'ok': True, 'user': _user_to_json(repo.get_user(telegram_id))})

        if action == 'refresh_game_balance':
            telegram_id = str(payload.get('telegram_id') or '').strip()
            user = repo.get_user(telegram_id)
            if not user or not user.get('player_id'):
                return web.json_response({'error': 'المستخدم غير مرتبط بحساب iChancy'}, status=400)
            balance = await ichancy_api_client.get_player_balance(user.get('player_id'))
            repo.update_user_game_balance(telegram_id, balance)
            user = repo.get_user(telegram_id)
            return web.json_response({'ok': True, 'game_balance': int(balance or 0), 'user': _user_to_json(user)})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except ValueError:
        return web.json_response({'error': 'قيمة رقمية غير صحيحة'}, status=400)
    except Exception as e:
        logger.error(f"Admin users center error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)



def _request_tx_to_json(tx):
    user = repo.get_user(tx.get('user_telegram_id')) if tx and tx.get('user_telegram_id') else None
    return {
        'id': tx.get('id'),
        'user_telegram_id': str(tx.get('user_telegram_id')),
        'telegram_username': user.get('telegram_username') if user else None,
        'ichancy_username': user.get('ichancy_username') if user else None,
        'player_id': user.get('player_id') if user else None,
        'type': tx.get('type'),
        'payment_method': tx.get('payment_method'),
        'amount': float(tx.get('amount') or 0),
        'status': tx.get('status'),
        'transfer_number': tx.get('transfer_number'),
        'rejection_reason': tx.get('rejection_reason'),
        'reviewed_by': tx.get('reviewed_by'),
        'reviewed_at': tx.get('reviewed_at').strftime('%Y-%m-%d %H:%M') if tx.get('reviewed_at') else '',
        'created_at': tx.get('created_at').strftime('%Y-%m-%d %H:%M') if tx.get('created_at') else '',
        'original_amount': float(tx.get('original_amount') or 0) if tx.get('original_amount') is not None else None,
        'original_currency': tx.get('original_currency'),
        'converted_amount_syp': float(tx.get('converted_amount_syp') or 0) if tx.get('converted_amount_syp') is not None else None,
    }


async def admin_requests_handler(request):
    """مركز الطلبات في Mini App: عرض وبحث ومراسلة المستخدم. الاعتماد/الرفض يبقى من قناة المراجعة حالياً للأمان."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    bot = request.app.get('bot')
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get('action') or 'list'

    try:
        if action == 'list':
            pending = repo.get_pending_transactions()
            recent = repo.get_all_transactions(int(payload.get('limit') or 30))
            pending_deposits = [t for t in pending if t.get('type') == 'deposit_bot']
            pending_withdraws = [t for t in pending if t.get('type') == 'withdraw_bot']
            return web.json_response({
                'summary': {
                    'pending_total': len(pending),
                    'pending_deposits': len(pending_deposits),
                    'pending_withdraws': len(pending_withdraws),
                    'today_tx': repo.get_today_transactions_count(),
                    'approved_volume': repo.get_transactions_volume('approved'),
                },
                'pending': [_request_tx_to_json(t) for t in pending[:50]],
                'recent': [_request_tx_to_json(t) for t in recent[:50]],
            })

        if action == 'detail':
            tx_id = int(payload.get('tx_id'))
            tx = repo.get_transaction_by_id(tx_id)
            if not tx:
                return web.json_response({'error': 'الطلب غير موجود'}, status=404)
            user = repo.get_user(tx.get('user_telegram_id'))
            history = repo.get_user_transactions_history(tx.get('user_telegram_id'), limit=10) if user else []
            return web.json_response({
                'transaction': _request_tx_to_json(tx),
                'user': _user_to_json(user) if user else None,
                'history': [_tx_to_json(h) for h in history],
            })

        if action == 'search':
            q = str(payload.get('query') or '').strip().lstrip('#')
            if not q:
                return web.json_response({'items': []})
            try:
                tx_id = int(q)
                tx = repo.get_transaction_by_id(tx_id)
                return web.json_response({'items': [_request_tx_to_json(tx)] if tx else []})
            except ValueError:
                users = repo.search_user(q)
                items = []
                for u in users[:5]:
                    items.extend(repo.get_user_transactions_history(u['telegram_id'], limit=10) or [])
                return web.json_response({'items': [_request_tx_to_json(t) for t in items[:30]]})

        if action == 'message_user':
            telegram_id = str(payload.get('telegram_id') or '').strip()
            text = (payload.get('message') or '').strip()
            if not telegram_id or not text:
                return web.json_response({'error': 'بيانات ناقصة'}, status=400)
            await bot.send_message(chat_id=telegram_id, text=f"📩 رسالة من الإدارة بخصوص طلبك\n\n{text}")
            return web.json_response({'ok': True})

        if action == 'delete_transaction':
            tx_id = int(payload.get('tx_id'))
            refund_raw = payload.get('refund', False)
            refund = str(refund_raw).lower() in ('1', 'true', 'yes', 'on') if not isinstance(refund_raw, bool) else refund_raw
            result = repo.delete_transaction_safe(tx_id, refund=refund)
            if not result.get('ok'):
                return web.json_response({'error': 'تعذر حذف الطلب'}, status=400)
            return web.json_response({'ok': True, 'refunded': result.get('refunded', 0)})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except ValueError:
        return web.json_response({'error': 'قيمة رقمية غير صحيحة'}, status=400)
    except Exception as e:
        logger.error(f"Admin requests center error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)



def _bonus_rule_to_json(rule):
    return {
        'id': rule.get('id'),
        'title': rule.get('title'),
        'percent': float(rule.get('percent') or 0),
        'payment_method': rule.get('payment_method'),
        'min_amount_syp': float(rule.get('min_amount_syp') or 0),
        'max_bonus_syp': float(rule.get('max_bonus_syp') or 0),
        'is_active': bool(rule.get('is_active')),
        'created_by': rule.get('created_by'),
        'created_at': rule.get('created_at').strftime('%Y-%m-%d %H:%M') if rule.get('created_at') else '',
        'disabled_at': rule.get('disabled_at').strftime('%Y-%m-%d %H:%M') if rule.get('disabled_at') else '',
    }


def _ref_commission_to_json(item):
    return {
        'id': item.get('id'),
        'referrer_telegram_id': str(item.get('referrer_telegram_id')),
        'referrer_username': item.get('referrer_username'),
        'referrer_ichancy_username': item.get('referrer_ichancy_username'),
        'referred_telegram_id': str(item.get('referred_telegram_id')),
        'referred_username': item.get('referred_username'),
        'referred_ichancy_username': item.get('referred_ichancy_username'),
        'transaction_id': item.get('transaction_id'),
        'deposit_amount_syp': int(item.get('deposit_amount_syp') or 0),
        'active_referrals_count': int(item.get('active_referrals_count') or 0),
        'commission_percent': float(item.get('commission_percent') or 0),
        'commission_amount': int(item.get('commission_amount') or 0),
        'status': item.get('status'),
        'created_at': item.get('created_at').strftime('%Y-%m-%d %H:%M') if item.get('created_at') else '',
    }


async def admin_bonuses_handler(request):
    """إدارة البونصات والإحالات من Mini App."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get('action') or 'summary'

    try:
        if action == 'summary':
            bot_settings = repo.get_bot_settings()
            rules = repo.get_all_bonus_rules(limit=100)
            active_rules = [r for r in rules if r.get('is_active')]
            referral_summary = repo.get_referral_summary()
            top_referrers = repo.get_top_referrers(limit=int(payload.get('limit') or 10))
            recent_commissions = repo.get_recent_referral_commissions(limit=20)
            return web.json_response({
                'settings': {
                    'referrals_enabled': bool(bot_settings.get('referrals_enabled', True)),
                },
                'bonus_summary': {
                    'total_rules': len(rules),
                    'active_rules': len(active_rules),
                },
                'referral_summary': {
                    'total_referrals': int(referral_summary.get('total_referrals') or 0),
                    'active_referrals': int(referral_summary.get('active_referrals') or 0),
                    'top_percent_now': repo.get_referral_percent_by_active_count(int(referral_summary.get('active_referrals') or 0)),
                    'total_commissions_paid': repo.get_total_referral_commissions_sum(),
                },
                'bonus_rules': [_bonus_rule_to_json(r) for r in rules],
                'top_referrers': [{
                    'referrer_telegram_id': str(r.get('referrer_telegram_id')),
                    'telegram_username': r.get('telegram_username'),
                    'ichancy_username': r.get('ichancy_username'),
                    'player_id': r.get('player_id'),
                    'total_referrals': int(r.get('total_referrals') or 0),
                    'active_referrals': int(r.get('active_referrals') or 0),
                    'total_earnings': int(r.get('total_earnings') or 0),
                    'current_percent': repo.get_referral_percent_by_active_count(int(r.get('active_referrals') or 0)),
                } for r in top_referrers],
                'recent_commissions': [_ref_commission_to_json(r) for r in recent_commissions],
            })

        if action == 'create_bonus':
            title = (payload.get('title') or '').strip()
            percent = float(str(payload.get('percent') or '0').replace('%', '').replace(',', '.'))
            payment_method = (payload.get('payment_method') or 'all').strip()
            min_amount_syp = int(str(payload.get('min_amount_syp') or '0').replace(',', ''))
            max_bonus_syp = int(str(payload.get('max_bonus_syp') or '0').replace(',', ''))
            if len(title) < 3:
                return web.json_response({'error': 'اسم العرض قصير جداً'}, status=400)
            if percent <= 0 or percent > 100:
                return web.json_response({'error': 'نسبة البونص غير صالحة'}, status=400)
            if payment_method not in {'all','syriatel','mtn','sham_syp','sham_usd','usdt_trc','usdt_bep'}:
                return web.json_response({'error': 'طريقة دفع غير معروفة'}, status=400)
            if min_amount_syp < 0 or max_bonus_syp < 0:
                return web.json_response({'error': 'القيم المالية غير صالحة'}, status=400)
            # 🔒 حماية المراجحة (Update 9): البونص يجب ألا يتجاوز عمولة السحب
            withdraw_commission = float(repo.get_bot_settings().get('withdraw_commission') or 0)
            if percent > withdraw_commission:
                return web.json_response({'error': f'⚠️ نسبة البونص ({percent}%) أعلى من عمولة السحب ({withdraw_commission}%). يمكن للمستخدم الإيداع والسحب فوراً ليربح! يجب أن يكون البونص أقل من العمولة.'}, status=400)
            rule_id = repo.create_bonus_rule(title, percent, payment_method, min_amount_syp, max_bonus_syp, created_by='miniapp')
            return web.json_response({'ok': True, 'rule_id': rule_id})

        if action == 'disable_bonus':
            rule_id = int(payload.get('rule_id'))
            repo.disable_bonus_rule(rule_id)
            return web.json_response({'ok': True})

        if action == 'enable_bonus':
            rule_id = int(payload.get('rule_id'))
            repo.enable_bonus_rule(rule_id)
            return web.json_response({'ok': True})

        if action == 'delete_bonus':
            rule_id = int(payload.get('rule_id'))
            repo.delete_bonus_rule(rule_id)
            return web.json_response({'ok': True})

        if action == 'update_bonus':
            rule_id = int(payload.get('rule_id'))
            title = (payload.get('title') or '').strip()
            percent = float(str(payload.get('percent') or '0').replace('%', '').replace(',', '.'))
            payment_method = (payload.get('payment_method') or 'all').strip()
            min_amount_syp = int(str(payload.get('min_amount_syp') or '0').replace(',', ''))
            max_bonus_syp = int(str(payload.get('max_bonus_syp') or '0').replace(',', ''))
            if len(title) < 3:
                return web.json_response({'error': 'اسم العرض قصير جداً'}, status=400)
            if percent <= 0 or percent > 100:
                return web.json_response({'error': 'نسبة البونص غير صالحة'}, status=400)
            if payment_method not in {'all','syriatel','mtn','sham_syp','sham_usd','usdt_trc','usdt_bep'}:
                return web.json_response({'error': 'طريقة دفع غير معروفة'}, status=400)
            if min_amount_syp < 0 or max_bonus_syp < 0:
                return web.json_response({'error': 'القيم المالية غير صالحة'}, status=400)
            # 🔒 حماية المراجحة (Update 9): البونص يجب ألا يتجاوز عمولة السحب
            withdraw_commission = float(repo.get_bot_settings().get('withdraw_commission') or 0)
            if percent > withdraw_commission:
                return web.json_response({'error': f'⚠️ نسبة البونص ({percent}%) أعلى من عمولة السحب ({withdraw_commission}%). يمكن للمستخدم الإيداع والسحب فوراً ليربح! يجب أن يكون البونص أقل من العمولة.'}, status=400)
            repo.update_bonus_rule(rule_id, title=title, percent=percent, payment_method=payment_method, min_amount_syp=min_amount_syp, max_bonus_syp=max_bonus_syp)
            return web.json_response({'ok': True})

        if action == 'toggle_referrals':
            enabled_raw = payload.get('enabled', True)
            enabled = str(enabled_raw).lower() in ('1', 'true', 'yes', 'on') if not isinstance(enabled_raw, bool) else enabled_raw
            repo.set_referrals_enabled(enabled)
            return web.json_response({'ok': True, 'referrals_enabled': enabled})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except ValueError:
        return web.json_response({'error': 'قيمة رقمية غير صحيحة'}, status=400)
    except Exception as e:
        logger.error(f"Admin bonuses/referrals error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)




def _prediction_card_to_json(card):
    import json
    options = []
    try:
        options = json.loads(card.get('options_json') or '[]')
    except Exception:
        options = []
    summary = repo.get_prediction_card_summary(card.get('id'))
    return {
        'id': card.get('id'),
        'title': card.get('title'),
        'match_code': card.get('match_code'),
        'team_a': card.get('team_a'),
        'team_b': card.get('team_b'),
        'options': options,
        'max_predictions': int(card.get('max_predictions') or 0),
        'reward_syp': int(card.get('reward_syp') or 0),
        'status': card.get('status'),
        'closes_at': card.get('closes_at').strftime('%Y-%m-%d %H:%M') if card.get('closes_at') else '',
        'created_at': card.get('created_at').strftime('%Y-%m-%d %H:%M') if card.get('created_at') else '',
        'closed_at': card.get('closed_at').strftime('%Y-%m-%d %H:%M') if card.get('closed_at') else '',
        'settled_at': card.get('settled_at').strftime('%Y-%m-%d %H:%M') if card.get('settled_at') else '',
        'winning_option': card.get('winning_option'),
        'summary': [{'selected_option': x.get('selected_option'), 'count': int(x.get('count') or 0)} for x in summary],
        'entries_count': sum(int(x.get('count') or 0) for x in summary),
    }


def _prediction_entry_to_json(item):
    return {
        'id': item.get('id'),
        'user_telegram_id': str(item.get('user_telegram_id')),
        'telegram_username': item.get('telegram_username'),
        'ichancy_username': item.get('ichancy_username'),
        'player_id': item.get('player_id'),
        'selected_option': item.get('selected_option'),
        'reward_amount': int(item.get('reward_amount') or 0),
        'is_winner': bool(item.get('is_winner')),
        'created_at': item.get('created_at').strftime('%Y-%m-%d %H:%M') if item.get('created_at') else '',
    }


async def admin_predictions_handler(request):
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    app_bot = request.app.get('bot')
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get('action') or 'list'
    try:
        if action == 'list':
            status = payload.get('status') or 'all'
            cards = repo.get_prediction_cards(status=status, limit=int(payload.get('limit') or 50))
            return web.json_response({'cards': [_prediction_card_to_json(c) for c in cards]})

        if action == 'create':
            import json
            title = (payload.get('title') or '').strip()
            team_a = (payload.get('team_a') or '').strip()
            team_b = (payload.get('team_b') or '').strip()
            match_code = (payload.get('match_code') or '').strip()
            closes_at = (payload.get('closes_at') or '').strip() or None
            reward_syp = int(str(payload.get('reward_syp') or '0').replace(',', ''))
            max_predictions = int(str(payload.get('max_predictions') or '0').replace(',', ''))
            raw_options = payload.get('options') or []
            if isinstance(raw_options, str):
                options = [x.strip() for x in raw_options.splitlines() if x.strip()]
            else:
                options = [str(x).strip() for x in raw_options if str(x).strip()]
            if len(title) < 3 or not team_a or not team_b or len(options) < 2:
                return web.json_response({'error': 'بيانات البطاقة غير مكتملة'}, status=400)
            card_id = repo.create_prediction_card(
                title=title,
                team_a=team_a,
                team_b=team_b,
                options_json=json.dumps(options, ensure_ascii=False),
                max_predictions=max_predictions,
                reward_syp=reward_syp,
                closes_at=closes_at,
                created_by='miniapp',
                match_code=match_code,
            )
            return web.json_response({'ok': True, 'card_id': card_id})

        if action == 'detail':
            card_id = int(payload.get('card_id'))
            card = repo.get_prediction_card(card_id)
            if not card:
                return web.json_response({'error': 'البطاقة غير موجودة'}, status=404)
            entries = repo.get_prediction_entries(card_id)
            return web.json_response({'card': _prediction_card_to_json(card), 'entries': [_prediction_entry_to_json(e) for e in entries]})

        if action == 'close':
            card_id = int(payload.get('card_id'))
            repo.close_prediction_card(card_id)
            return web.json_response({'ok': True})

        if action == 'settle':
            card_id = int(payload.get('card_id'))
            winning_option = (payload.get('winning_option') or '').strip()
            if not winning_option:
                return web.json_response({'error': 'اختر النتيجة الفائزة'}, status=400)
            result = repo.settle_prediction_card(card_id, winning_option)
            if not result.get('ok'):
                return web.json_response({'error': 'تعذر تسوية البطاقة'}, status=400)
            card = repo.get_prediction_card(card_id)
            entries = repo.get_prediction_entries(card_id)
            for e in entries:
                if e.get('selected_option') == winning_option:
                    try:
                        if app_bot:
                            msg = (
                                f"🎉 مبروك! ربحت في بطاقة التوقع #{card_id}\n\n"
                                f"🏆 النتيجة الصحيحة: {winning_option}\n"
                                f"💰 الجائزة: {int(card.get('reward_syp') or 0):,} SYP"
                            )
                            await app_bot.send_message(chat_id=e.get('user_telegram_id'), text=msg)
                    except Exception:
                        pass
            return web.json_response({'ok': True, 'result': result})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except ValueError:
        return web.json_response({'error': 'قيمة رقمية غير صحيحة'}, status=400)
    except Exception as e:
        logger.error(f"Admin predictions error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)




def _contest_to_json(c):
    return {
        'id': c.get('id'),
        'title': c.get('title'),
        'description': c.get('description'),
        'contest_type': c.get('contest_type'),
        'reward_type': c.get('reward_type'),
        'reward_amount': int(c.get('reward_amount') or 0),
        'winners_limit': int(c.get('winners_limit') or 1),
        'requires_proof': bool(c.get('requires_proof')),
        'status': c.get('status'),
        'created_at': c.get('created_at').strftime('%Y-%m-%d %H:%M') if c.get('created_at') else '',
        'closed_at': c.get('closed_at').strftime('%Y-%m-%d %H:%M') if c.get('closed_at') else '',
    }


def _contest_entry_to_json(e):
    return {
        'id': e.get('id'),
        'contest_id': e.get('contest_id'),
        'user_telegram_id': str(e.get('user_telegram_id')),
        'telegram_username': e.get('telegram_username'),
        'ichancy_username': e.get('ichancy_username'),
        'player_id': e.get('player_id'),
        'proof_text': e.get('proof_text'),
        'proof_type': e.get('proof_type'),
        'status': e.get('status'),
        'gift_code': e.get('gift_code'),
        'reward_amount': int(e.get('reward_amount') or 0),
        'created_at': e.get('created_at').strftime('%Y-%m-%d %H:%M') if e.get('created_at') else '',
        'reviewed_at': e.get('reviewed_at').strftime('%Y-%m-%d %H:%M') if e.get('reviewed_at') else '',
    }


async def admin_contests_handler(request):
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    bot = request.app.get('bot')
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get('action') or 'list'
    try:
        if action == 'list':
            status = payload.get('status') or 'all'
            contests = repo.get_contests(status=status, limit=int(payload.get('limit') or 50))
            return web.json_response({'contests': [_contest_to_json(c) for c in contests]})

        if action == 'create':
            title = (payload.get('title') or '').strip()
            description = (payload.get('description') or '').strip()
            contest_type = (payload.get('contest_type') or 'first_approved').strip()
            reward_type = (payload.get('reward_type') or 'gift_code').strip()
            reward_amount = int(str(payload.get('reward_amount') or '0').replace(',', ''))
            winners_limit = int(str(payload.get('winners_limit') or '1').replace(',', ''))
            requires_proof_raw = payload.get('requires_proof', True)
            requires_proof = str(requires_proof_raw).lower() in ('1', 'true', 'yes', 'on') if not isinstance(requires_proof_raw, bool) else requires_proof_raw
            if contest_type not in {'first_approved', 'manual_review', 'random_draw'}:
                return web.json_response({'error': 'نوع المسابقة غير معروف'}, status=400)
            if reward_type not in {'gift_code', 'bot_balance'}:
                return web.json_response({'error': 'نوع الجائزة غير معروف'}, status=400)
            if len(title) < 3 or winners_limit < 1 or reward_amount < 0:
                return web.json_response({'error': 'بيانات المسابقة غير صالحة'}, status=400)
            contest_id = repo.create_contest(title, description, contest_type, reward_type, reward_amount, winners_limit, requires_proof, created_by='miniapp')
            return web.json_response({'ok': True, 'contest_id': contest_id})

        if action == 'detail':
            contest_id = int(payload.get('contest_id'))
            contest = repo.get_contest(contest_id)
            if not contest:
                return web.json_response({'error': 'المسابقة غير موجودة'}, status=404)
            entries = repo.get_contest_entries(contest_id, status=payload.get('entries_status') or 'all')
            return web.json_response({'contest': _contest_to_json(contest), 'entries': [_contest_entry_to_json(e) for e in entries]})

        if action == 'approve_entry':
            entry_id = int(payload.get('entry_id'))
            result = repo.approve_contest_entry(entry_id, reviewed_by='miniapp')
            if not result.get('ok'):
                return web.json_response({'error': 'تعذر اعتماد المشاركة'}, status=400)
            try:
                reward_amount = int(result.get('reward_amount') or 0)
                gift_code = result.get('gift_code')
                text = "🎉 مبروك! تم اعتماد مشاركتك في مسابقة القيصر.\n\n"
                if gift_code:
                    text += f"🎫 كود هديتك: <code>{gift_code}</code>\n💰 القيمة: <code>{reward_amount:,} SYP</code>"
                else:
                    text += f"💰 تمت إضافة الجائزة إلى رصيدك: <code>{reward_amount:,} SYP</code>"
                await bot.send_message(chat_id=result.get('user_telegram_id'), text=text, parse_mode='HTML')
            except Exception:
                pass
            return web.json_response({'ok': True, 'result': result})

        if action == 'reject_entry':
            entry_id = int(payload.get('entry_id'))
            repo.reject_contest_entry(entry_id, reviewed_by='miniapp')
            return web.json_response({'ok': True})

        if action == 'random_pick':
            contest_id = int(payload.get('contest_id'))
            winners_count = int(str(payload.get('winners_count') or '1').replace(',', ''))
            winners = repo.pick_random_contest_winners(contest_id, winners_count)
            return web.json_response({'ok': True, 'winners': [_contest_entry_to_json(w) for w in winners]})

        if action == 'close':
            contest_id = int(payload.get('contest_id'))
            repo.close_contest(contest_id)
            return web.json_response({'ok': True})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except ValueError:
        return web.json_response({'error': 'قيمة رقمية غير صحيحة'}, status=400)
    except Exception as e:
        logger.error(f"Admin contests error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


def _support_ticket_to_json(t):
    return {
        'id': t.get('id'),
        'user_telegram_id': str(t.get('user_telegram_id')),
        'telegram_username': t.get('telegram_username'),
        'ichancy_username': t.get('ichancy_username'),
        'player_id': t.get('player_id'),
        'bot_balance': int(t.get('bot_balance') or 0),
        'status': t.get('status'),
        'last_message': t.get('last_message'),
        'last_message_at': t.get('last_message_at').strftime('%Y-%m-%d %H:%M') if t.get('last_message_at') else '',
        'created_at': t.get('created_at').strftime('%Y-%m-%d %H:%M') if t.get('created_at') else '',
        'closed_at': t.get('closed_at').strftime('%Y-%m-%d %H:%M') if t.get('closed_at') else '',
    }


def _support_message_to_json(m):
    return {
        'id': m.get('id'),
        'ticket_id': m.get('ticket_id'),
        'sender_type': m.get('sender_type'),
        'sender_id': m.get('sender_id'),
        'message_text': m.get('message_text'),
        'content_type': m.get('content_type'),
        'created_at': m.get('created_at').strftime('%Y-%m-%d %H:%M') if m.get('created_at') else '',
    }


async def admin_support_handler(request):
    """مركز الدعم Tickets في Mini App."""
    init_data_raw = request.headers.get('X-Telegram-Init-Data', '')
    if not _is_admin(init_data_raw):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    bot = request.app.get('bot')
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get('action') or 'list'
    try:
        if action == 'list':
            status = payload.get('status') or 'open'
            tickets = repo.get_support_tickets(status=status, limit=int(payload.get('limit') or 50))
            return web.json_response({'tickets': [_support_ticket_to_json(t) for t in tickets]})

        if action == 'detail':
            ticket_id = int(payload.get('ticket_id'))
            ticket = repo.get_support_ticket(ticket_id)
            if not ticket:
                return web.json_response({'error': 'التذكرة غير موجودة'}, status=404)
            user = repo.get_user(ticket.get('user_telegram_id'))
            messages = repo.get_support_messages(ticket_id, limit=100)

            # مهم: لا ندمج dict التذكرة مع dict المستخدم مباشرة، لأن حقل id في users
            # كان يطغى على id التذكرة، فيرسل الـ Mini App ticket_id خاطئ عند الرد.
            ticket_json = _support_ticket_to_json(ticket)
            if user:
                ticket_json.update({
                    'telegram_username': user.get('telegram_username'),
                    'ichancy_username': user.get('ichancy_username'),
                    'player_id': user.get('player_id'),
                    'bot_balance': int(user.get('bot_balance') or 0),
                })

            return web.json_response({
                'ticket': ticket_json,
                'user': _user_to_json(user) if user else None,
                'messages': [_support_message_to_json(m) for m in messages]
            })

        if action == 'reply':
            ticket_id = int(payload.get('ticket_id'))
            text = (payload.get('message') or '').strip()
            if not text:
                return web.json_response({'error': 'نص الرد فارغ'}, status=400)
            ticket = repo.get_support_ticket(ticket_id)
            if not ticket:
                return web.json_response({'error': 'التذكرة غير موجودة'}, status=404)
            await bot.send_message(chat_id=ticket['user_telegram_id'], text=f"📩 رد من الإدارة\n\n{text}")
            repo.add_support_message(ticket_id, 'admin', 'miniapp', text, 'text', None)
            return web.json_response({'ok': True})

        if action == 'close':
            ticket_id = int(payload.get('ticket_id'))
            repo.close_support_ticket(ticket_id)
            ticket = repo.get_support_ticket(ticket_id)
            with suppress(Exception):
                await bot.send_message(chat_id=ticket['user_telegram_id'], text="✅ تم إغلاق تذكرة الدعم. يمكنك فتح محادثة جديدة في أي وقت من زر رسالة للإدارة.")
            return web.json_response({'ok': True})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except ValueError:
        return web.json_response({'error': 'قيمة رقمية غير صحيحة'}, status=400)
    except Exception as e:
        logger.error(f"Admin support center error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


async def _get_total_bot_balance_cached():
    conn = None
    cur = None
    try:
        conn = DatabaseManager.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(bot_balance), 0) FROM users")
        result = cur.fetchone()
        return int(result[0]) if result else 0
    except Exception as e:
        logger.error(f"Bot balance query error: {e}")
        return 0
    finally:
        if cur:
            cur.close()
        if conn:
            DatabaseManager.put_connection(conn)



async def serve_games_hub_html(request):
    html_path = os.path.join(WEBAPP_DIR, "games_hub.html")
    if not os.path.exists(html_path):
        return web.Response(text="Games hub not found", status=404)
    return web.FileResponse(html_path)


async def serve_site_hub_html(request):
    html_path = os.path.join(WEBAPP_DIR, "site_hub.html")
    if not os.path.exists(html_path):
        return web.Response(text="Site hub not found", status=404)
    return web.FileResponse(html_path)


async def public_links_handler(request):
    return web.json_response({
        'website_url': repo.get_button_link('website_url'),
        'app_download_url': repo.get_button_link('app_download_url'),
        'betting_url': repo.get_button_link('betting_url'),
        'games_url': repo.get_button_link('games_url'),
    })


async def serve_dashboard_html(request):
    """🆕 يقدّم صفحة HTML للوحة التحكم."""
    html_path = os.path.join(WEBAPP_DIR, "dashboard.html")
    if not os.path.exists(html_path):
        return web.Response(text="Dashboard not found", status=404)
    return web.FileResponse(html_path)


async def serve_user_app_html(request):
    """🆕 (Update 10) يقدّم Mini App للمستخدم العادي."""
    html_path = os.path.join(WEBAPP_DIR, "user_app.html")
    if not os.path.exists(html_path):
        return web.Response(text="App not found", status=404)
    return web.FileResponse(html_path)


async def user_me_api_handler(request):
    """🆕 (Update 10) API يغذّي Mini App للمستخدم بالبيانات."""
    user_obj = _verify_telegram_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    if not user_obj:
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    telegram_id = str(user_obj.get('id', ''))
    if not telegram_id:
        return web.json_response({'error': 'مستخدم غير صالح'}, status=400)

    try:
        user = repo.get_user(telegram_id)
        if not user:
            return web.json_response({'error': 'المستخدم غير موجود. استخدم /start أولاً.'}, status=404)

        bot_balance = int(user.get('bot_balance') or 0)
        game_balance = repo.get_user_game_balance(telegram_id)

        # آخر المعاملات
        history = repo.get_user_transactions_history(telegram_id, limit=30)
        recent_transactions = []
        for tx in history:
            recent_transactions.append({
                'id': tx.get('id'),
                'type': tx.get('type'),
                'amount': float(tx.get('amount') or 0),
                'status': tx.get('status'),
                'payment_method': tx.get('payment_method'),
                'created_at': tx.get('created_at').strftime('%Y-%m-%d %H:%M') if tx.get('created_at') else '',
            })

        # العروض النشطة
        active_offers = []
        try:
            method_labels = {'all':'كل الطرق','syriatel':'سيريتل','mtn':'MTN','sham_syp':'شام SYP','sham_usd':'شام USD','usdt_trc':'USDT','usdt_bep':'USDT BEP'}
            for rule in repo.get_active_bonus_rules()[:5]:
                active_offers.append({
                    'title': rule.get('title'),
                    'percent': float(rule.get('percent') or 0),
                    'payment_method': rule.get('payment_method') or 'all',
                    'method_label': method_labels.get(rule.get('payment_method'), 'كل الطرق'),
                })
        except Exception:
            pass

        # المسابقات المفتوحة
        open_contests = []
        try:
            for c in repo.get_open_contests(limit=5):
                open_contests.append({
                    'id': c.get('id'),
                    'title': c.get('title'),
                    'reward_type': c.get('reward_type'),
                    'reward_amount': int(c.get('reward_amount') or 0),
                })
        except Exception:
            pass

        # معلومات الإحالة
        referral = {}
        try:
            active_refs = repo.get_active_referrals_count(telegram_id)
            total_refs = repo.get_referrals_count(telegram_id)
            earnings = repo.get_total_referral_earnings(telegram_id)
            pct = repo.get_referral_percent_by_active_count(active_refs)
            bot_user = await request.app['bot'].get_me()
            ref_link = f"https://t.me/{bot_user.username}?start=ref_{telegram_id}"
            referral = {
                'active_referrals': active_refs,
                'total_referrals': total_refs,
                'percent': pct,
                'total_earnings': earnings,
                'ref_link': ref_link,
            }
        except Exception as e:
            logger.warning(f"user_me referral error: {e}")

        # 🆕 (Update 12) الحضور اليومي + فلاش البونص + لوحة الصدارة
        checkin = {}
        try:
            info = repo.get_checkin_info(telegram_id)
            # 🆕 شرط الأهلية: حد أدنى للإيداع خلال الشهر
            _feat = repo.get_user_features_settings()
            _min_dep = int(_feat.get('checkin_min_deposit') or 50000)
            _recent = repo.get_user_recent_deposits_total(telegram_id, 30) if _min_dep > 0 else 0
            _eligible = (_min_dep <= 0) or (_recent >= _min_dep)
            checkin = {
                'can_checkin': repo.can_checkin_today(telegram_id) and _eligible,
                'eligible': _eligible,
                'min_deposit': _min_dep,
                'recent_deposits': _recent,
                'current_streak': info['current_streak'],
                'total_checkins': info['total_checkins'],
                'total_rewards': info['total_rewards'],
            }
        except Exception as e:
            logger.warning(f"user_me checkin error: {e}")

        flash = None
        try:
            fb = repo.get_active_flash_bonus()
            if fb:
                flash = {
                    'id': fb.get('id'),
                    'percent': float(fb.get('percent') or 0),
                    'ends_at': fb.get('ends_at').isoformat() if fb.get('ends_at') else None,
                }
        except Exception as e:
            logger.warning(f"user_me flash error: {e}")

        leaderboard = {}
        try:
            lb = repo.get_leaderboard(limit=5, telegram_id=telegram_id)
            leaderboard = lb
        except Exception as e:
            logger.warning(f"user_me leaderboard error: {e}")

        # 🆕 (Update 13) تمرير إعدادات تفعيل الميزات للواجهة
        feat_settings = {}
        try:
            fs = repo.get_user_features_settings()
            feat_settings = {
                'checkin_enabled': fs.get('checkin_enabled', True),
                'leaderboard_enabled': fs.get('leaderboard_enabled', True),
            }
        except Exception:
            feat_settings = {'checkin_enabled': True, 'leaderboard_enabled': True}

        # 🆕 (Update 14) تمرير رصيد المكافآت وشروطه للواجهة
        bonus_balance = 0
        try:
            bonus_balance = repo.get_user_bonus_balance(telegram_id)
        except Exception:
            pass

        # 🆕 (Update 15) تمرير إعدادات العجلة
        try:
            ws = repo.get_wheel_settings()
            feat_settings['wheel_enabled'] = ws.get('wheel_enabled', True)
            feat_settings['wheel_segments'] = ws.get('segments', [])
        except Exception as e:
            logger.warning(f"user_me wheel error: {e}")

        bonus_eligibility = {}
        try:
            bonus_eligibility = repo.check_bonus_eligibility(telegram_id)
        except Exception as e:
            logger.warning(f"user_me bonus_eligibility error: {e}")

        # 🆕 (Update 15) الإيداعات المؤهلة للعجلة
        spun_ids = []
        try:
            spun_ids = repo.get_spun_deposit_ids(telegram_id)
            for tx in recent_transactions:
                if tx.get('type') == 'deposit_bot' and tx.get('status') == 'approved':
                    tx['can_spin_wheel'] = tx.get('id') not in spun_ids
                else:
                    tx['can_spin_wheel'] = False
        except Exception as e:
            logger.warning(f"user_me wheel eligibility error: {e}")

        # 🆕 (Update 16) نظام VIP
        vip_info = {}
        try:
            vip_settings = repo.get_vip_settings()
            feat_settings['vip_enabled'] = vip_settings.get('vip_enabled', True)
            feat_settings['vip_tiers'] = vip_settings.get('tiers', [])
            total_deposits = repo.get_user_total_deposits(telegram_id)
            vip_tier = repo.get_vip_tier_info(total_deposits, vip_settings.get('tiers', []))
            vip_info = {
                'total_deposits': total_deposits,
                'tier_name': vip_tier['tier_names'][vip_tier['current_index']],
                'tier_index': vip_tier['current_index'],
                'current_bonus_pct': vip_tier['current_bonus_pct'],
                'next_tier': vip_tier['next_tier'],
                'tier_names': vip_tier['tier_names'],
            }
        except Exception as e:
            logger.warning(f"user_me vip error: {e}")

        # 🆕 (Update 17) الكاش باك الأسبوعي
        cashback_info = {}
        try:
            cb_settings = repo.get_cashback_settings()
            feat_settings['cashback_enabled'] = cb_settings.get('cashback_enabled', True)
            feat_settings['cashback_pct'] = cb_settings.get('cashback_pct', 5)
            feat_settings['cashback_min_loss'] = cb_settings.get('cashback_min_loss', 50000)
            activity = repo.get_user_weekly_game_activity(telegram_id)
            already_paid = repo.has_cashback_this_week(telegram_id)
            expected = int(activity['net_loss'] * cb_settings.get('cashback_pct', 5) / 100.0)
            cashback_info = {
                'enabled': cb_settings.get('cashback_enabled', True),
                'pct': cb_settings.get('cashback_pct', 5),
                'min_loss': cb_settings.get('cashback_min_loss', 50000),
                'week_deposited': activity['deposited'],
                'week_withdrawn': activity['withdrawn'],
                'week_net_loss': activity['net_loss'],
                'expected_cashback': expected,
                'already_paid': already_paid,
            }
        except Exception as e:
            logger.warning(f"user_me cashback error: {e}")

        return web.json_response({
            'telegram_id': telegram_id,
            'username': user.get('telegram_username'),
            'bot_balance': bot_balance,
            'bonus_balance': bonus_balance,
            'game_balance': game_balance,
            'recent_transactions': recent_transactions,
            'active_offers': active_offers,
            'open_contests': open_contests,
            'referral': referral,
            'checkin': checkin,
            'flash_bonus': flash,
            'leaderboard': leaderboard,
            'features': feat_settings,
            'bonus_eligibility': bonus_eligibility,
            'vip': vip_info,
            'cashback': cashback_info,
        })
    except Exception as e:
        logger.error(f"user_me_api error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


async def user_checkin_handler(request):
    """🆕 (Update 12) تسجيل الحضور اليومي."""
    user_obj = _verify_telegram_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    if not user_obj:
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    telegram_id = str(user_obj.get('id', ''))
    try:
        result = repo.do_daily_checkin(telegram_id)
        if not result.get('ok'):
            if result.get('reason') == 'already_checked_in':
                return web.json_response({'error': 'لقد سجّلت حضورك اليوم بالفعل! عُد غداً.'}, status=400)
            if result.get('reason') == 'deposit_required':
                req = int(result.get('required') or 0)
                cur = int(result.get('current') or 0)
                return web.json_response({
                    'error': (
                        f'للمشاركة في مكافأة الحضور اليومي، يجب أن يكون إجمالي إيداعاتك '
                        f'خلال آخر 30 يوماً {req:,} ل.س على الأقل.\n'
                        f'إيداعاتك الحالية: {cur:,} ل.س.'
                    )
                }, status=403)
            return web.json_response({'error': 'تعذّر تسجيل الحضور'}, status=500)
        return web.json_response(result)
    except Exception as e:
        logger.error(f"user_checkin error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


async def user_bonus_to_game_handler(request):
    """🆕 (Update 14) شحن رصيد المكافآت للعبة."""
    user_obj = _verify_telegram_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    if not user_obj:
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    telegram_id = str(user_obj.get('id', ''))
    try:
        user = repo.get_user(telegram_id)
        if not user:
            return web.json_response({'error': 'المستخدم غير موجود'}, status=404)
        player_id = user.get('player_id')
        if not player_id:
            return web.json_response({'error': 'ليس لديك حساب iChancy مرتبط'}, status=400)

        payload = await request.json()
        amount = int(float(payload.get('amount') or 0))

        # فحص شروط المكافآت
        elig = repo.check_bonus_eligibility(telegram_id)
        if not elig.get('eligible'):
            return web.json_response({'error': f'يجب أن يكون إجمالي إيداعاتك في آخر {elig["days"]} يوماً لا يقل عن {elig["threshold"]:,} ل.س لفتح رصيد المكافآت.'}, status=403)

        feat = repo.get_user_features_settings()
        min_transfer = feat.get('bonus_min_transfer', 20000)
        if amount < min_transfer:
            return web.json_response({'error': f'الحد الأدنى لشحن المكافآت هو {min_transfer:,} ل.س'}, status=400)

        # حجز المبلغ
        reserve = repo.transfer_bonus_to_game_atomic(telegram_id, amount, player_id)
        if not reserve.get('success'):
            return web.json_response({'error': 'رصيد المكافآت غير كافٍ'}, status=400)

        tx_id = reserve['tx_id']

        # استدعاء iChancy API
        deposit_result = await ichancy_api_client.deposit_to_player(
            player_id=player_id,
            amount=int(amount)
        )

        if not deposit_result or not deposit_result.get('success'):
            # إعادة الرصيد للمكافآت عند الفشل
            repo.revert_game_transaction(tx_id)
            return web.json_response({'error': 'فشل الاتصال بـ iChancy، تم إعادة رصيد المكافآت'}, status=500)

        repo.confirm_game_transaction(tx_id)
        
        # تحديث رصيد اللعبة محلياً
        cached_game_balance = repo.get_user_game_balance(telegram_id)
        repo.update_user_game_balance(telegram_id, cached_game_balance + int(amount))

        return web.json_response({'ok': True, 'amount': amount, 'new_bonus_balance': reserve.get('new_bonus_balance', 0)})
    except Exception as e:
        logger.error(f"user_bonus_to_game error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


async def admin_flash_handler(request):
    """🆕 (Update 12) إدارة فلاش البونص من الداشبورد."""
    if not _is_admin(request.headers.get('X-Telegram-Init-Data', '')):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get('action') or 'status'
    try:
        if action == 'create':
            percent = float(str(payload.get('percent') or '0').replace('%', '').replace(',', '.'))
            duration = int(payload.get('duration_minutes') or 30)
            payment_method = (payload.get('payment_method') or 'all').strip()
            withdraw_commission = float(repo.get_bot_settings().get('withdraw_commission') or 0)
            if percent <= 0 or percent > withdraw_commission:
                return web.json_response({'error': f'النسبة يجب أن تكون أقل من عمولة السحب ({withdraw_commission}%)'}, status=400)
            if duration < 1 or duration > 1440:
                return web.json_response({'error': 'المدة غير صالحة (1-1440 دقيقة)'}, status=400)
            repo.stop_flash_bonus()  # إيقاف أي فلاش نشط سابق
            fb_id = repo.create_flash_bonus(percent, payment_method, duration, created_by='miniapp')
            return web.json_response({'ok': True, 'id': fb_id})

        if action == 'stop':
            repo.stop_flash_bonus()
            return web.json_response({'ok': True})

        if action == 'status':
            fb = repo.get_active_flash_bonus()
            if fb:
                return web.json_response({
                    'active': True,
                    'id': fb.get('id'),
                    'percent': float(fb.get('percent') or 0),
                    'ends_at': fb.get('ends_at').isoformat() if fb.get('ends_at') else None,
                })
            return web.json_response({'active': False})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except ValueError:
        return web.json_response({'error': 'قيمة رقمية غير صحيحة'}, status=400)
    except Exception as e:
        logger.error(f"admin_flash error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


async def user_spin_wheel_handler(request):
    """🆕 (Update 15) دوران عجلة الحظ على إيداع محدد."""
    user_obj = _verify_telegram_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    if not user_obj:
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    telegram_id = str(user_obj.get('id', ''))
    try:
        payload = await request.json()
        deposit_tx_id = int(payload.get('deposit_tx_id') or 0)
        if not deposit_tx_id:
            return web.json_response({'error': 'رقم الإيداع مطلوب'}, status=400)

        # التأكد أن العجلة مفعّلة
        ws = repo.get_wheel_settings()
        if not ws.get('wheel_enabled', True):
            return web.json_response({'error': 'عجلة الحظ متوقفة حالياً'}, status=403)

        # التأكد أن الإيداع مقبول وينتمي للمستخدم
        tx = repo.get_transaction_by_id(deposit_tx_id)
        if not tx or tx.get('user_telegram_id') != telegram_id:
            return web.json_response({'error': 'الإيداع غير صالح'}, status=400)
        if tx.get('status') != 'approved' or tx.get('type') != 'deposit_bot':
            return web.json_response({'error': 'هذا الإيداع غير مؤهل للعجلة'}, status=400)

        deposit_amount = int(float(tx.get('amount') or 0))

        # 🆕 حد أدنى للإيداع لاستحقاق الدورة (يمنع استغلال الإيداعات الصغيرة)
        wheel_min = int(ws.get('wheel_min_deposit') or 0)
        if wheel_min > 0 and deposit_amount < wheel_min:
            return web.json_response(
                {'error': f'عجلة الحظ متاحة للإيداعات من {wheel_min:,} ل.س فأكثر.'},
                status=403
            )

        # التأكد من عدم الدوران المزدوج
        if not repo.has_wheel_spin(telegram_id, deposit_tx_id):
            return web.json_response({'error': 'لقد دوّنت العجلة على هذا الإيداع بالفعل!'}, status=400)

        # تنفيذ الدوران
        result = repo.spin_wheel_atomic(telegram_id, deposit_tx_id, deposit_amount)
        if not result.get('ok'):
            return web.json_response({'error': 'تعذّر تنفيذ الدوران'}, status=500)

        return web.json_response(result)
    except Exception as e:
        logger.error(f"user_spin_wheel error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


async def admin_features_get_handler(request):
    """🆕 (Update 13) جلب إعدادات ميزات المستخدم وإحصائياتها."""
    if not _is_admin(request.headers.get('X-Telegram-Init-Data', '')):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    try:
        settings = repo.get_user_features_settings()
        stats = repo.get_checkin_stats()
        
        # 🆕 (Update 15) إعدادات وإحصائيات العجلة
        wheel_settings = repo.get_wheel_settings()
        settings['wheel_enabled'] = wheel_settings.get('wheel_enabled', True)
        settings['wheel_segments'] = wheel_settings.get('segments', [])
        wheel_stats = repo.get_wheel_stats()

        # 🆕 (Update 16) إعدادات VIP
        vip_settings = repo.get_vip_settings()
        settings['vip_enabled'] = vip_settings.get('vip_enabled', True)
        settings['vip_tiers'] = vip_settings.get('tiers', [])

        # 🆕 (Update 17) إعدادات الكاش باك
        cb_settings = repo.get_cashback_settings()
        settings['cashback_enabled'] = cb_settings.get('cashback_enabled', True)
        settings['cashback_pct'] = cb_settings.get('cashback_pct', 5)
        settings['cashback_min_loss'] = cb_settings.get('cashback_min_loss', 50000)
        cashback_stats = repo.get_cashback_stats()

        # جلب حالة فلاش البونص الحالي
        flash = repo.get_active_flash_bonus()
        flash_info = None
        if flash:
            flash_info = {
                'id': flash.get('id'),
                'percent': float(flash.get('percent') or 0),
                'ends_at': flash.get('ends_at').isoformat() if flash.get('ends_at') else None,
            }
        recent_flashes = repo.get_recent_flash_bonuses(limit=5)
        recent = []
        for f in recent_flashes:
            recent.append({
                'id': f.get('id'),
                'percent': float(f.get('percent') or 0),
                'ends_at': f.get('ends_at').strftime('%Y-%m-%d %H:%M') if f.get('ends_at') else '',
                'is_active': f.get('is_active'),
            })
        return web.json_response({
            'settings': settings,
            'checkin_stats': stats,
            'wheel_stats': wheel_stats,
            'cashback_stats': cashback_stats,
            'active_flash': flash_info,
            'recent_flashes': recent,
        })
    except Exception as e:
        logger.error(f"admin_features_get error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


async def admin_features_post_handler(request):
    """🆕 (Update 13) تحديث إعدادات ميزات المستخدم."""
    if not _is_admin(request.headers.get('X-Telegram-Init-Data', '')):
        return web.json_response({'error': 'غير مصرّح'}, status=403)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get('action')
    
    # 🆕 (Update 15 Fix) دالة مساعدة لتحويل القيم لـ boolean (تم نقلها لأعلى)
    def parse_bool(val):
        if val is None: return None
        if isinstance(val, bool): return val
        return str(val).lower() in ('1', 'true', 'yes', 'on')
        
    try:
        if action == 'update_features':
            # 🆕 (Update 14 Fix) تحويل القيم النصية لـ boolean بشكل صحيح
            checkin_enabled = parse_bool(payload.get('checkin_enabled'))
            checkin_rewards = payload.get('checkin_rewards')
            leaderboard_enabled = parse_bool(payload.get('leaderboard_enabled'))
            leaderboard_type = payload.get('leaderboard_type')
            bonus_min_transfer = payload.get('bonus_min_transfer')
            bonus_deposit_threshold = payload.get('bonus_deposit_threshold')
            bonus_deposit_days = payload.get('bonus_deposit_days')

            # تحقق من نوع leaderboard_type
            if leaderboard_type and leaderboard_type not in ('all_time', 'weekly', 'monthly'):
                leaderboard_type = 'all_time'

            # تحقق من قيم المكافآت
            if checkin_rewards is not None:
                if not isinstance(checkin_rewards, list) or len(checkin_rewards) != 8:
                    return web.json_response({'error': 'قيم المكافآت يجب أن تكون 8 أرقام (0 + 7 أيام)'}, status=400)
                try:
                    checkin_rewards = [int(float(x)) for x in checkin_rewards]
                except (ValueError, TypeError):
                    return web.json_response({'error': 'قيم المكافآت غير صالحة'}, status=400)

            repo.update_user_features_settings(
                checkin_enabled=checkin_enabled,
                checkin_rewards=checkin_rewards,
                leaderboard_enabled=leaderboard_enabled,
                leaderboard_type=leaderboard_type,
                bonus_min_transfer=bonus_min_transfer,
                bonus_deposit_threshold=bonus_deposit_threshold,
                bonus_deposit_days=bonus_deposit_days,
            )
            return web.json_response({'ok': True})

        if action == 'update_wheel':
            wheel_enabled = parse_bool(payload.get('wheel_enabled'))
            segments = payload.get('segments')
            if segments is not None:
                if not isinstance(segments, list) or len(segments) != 8:
                    return web.json_response({'error': 'يجب أن تكون 8 قطاعات'}, status=400)
                try:
                    segments = [[float(a), float(b)] for a, b in segments]
                    total_weight = sum(s[1] for s in segments)
                    if total_weight <= 0:
                        return web.json_response({'error': 'مجموع الاحتمالات يجب أن يكون أكبر من صفر'}, status=400)
                except (ValueError, TypeError):
                    return web.json_response({'error': 'قيم القطاعات غير صالحة'}, status=400)
            repo.update_wheel_settings(wheel_enabled=wheel_enabled, segments=segments)
            return web.json_response({'ok': True})

        if action == 'update_vip':
            vip_enabled = parse_bool(payload.get('vip_enabled'))
            tiers = payload.get('tiers')
            if tiers is not None:
                if not isinstance(tiers, list) or len(tiers) != 4:
                    return web.json_response({'error': 'يجب أن تكون 4 طبقات'}, status=400)
                try:
                    tiers = [[int(a), float(b), int(c)] for a, b, c in tiers]
                except (ValueError, TypeError):
                    return web.json_response({'error': 'قيم الطبقات غير صالحة'}, status=400)
            repo.update_vip_settings(vip_enabled=vip_enabled, tiers=tiers)
            return web.json_response({'ok': True})

        return web.json_response({'error': 'إجراء غير معروف'}, status=400)
    except Exception as e:
        logger.error(f"admin_features_post error: {e}", exc_info=True)
        return web.json_response({'error': 'خطأ داخلي'}, status=500)


def main():
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    app = web.Application()
    app['bot'] = bot

    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    async def health_check(request):
        return web.Response(text="Caesar_Bot Alive")

    app.router.add_get("/", health_check)
    # 🆕 مسارات Mini App
    app.router.add_get("/dashboard", serve_dashboard_html)
    app.router.add_get("/user-app", serve_user_app_html)
    app.router.add_get("/api/user/me", user_me_api_handler)
    app.router.add_post("/api/user/checkin", user_checkin_handler)
    app.router.add_post("/api/user/bonus-to-game", user_bonus_to_game_handler)
    app.router.add_post("/api/user/spin-wheel", user_spin_wheel_handler)
    app.router.add_post("/api/admin/flash", admin_flash_handler)
    app.router.add_get("/api/admin/features", admin_features_get_handler)
    app.router.add_post("/api/admin/features", admin_features_post_handler)
    app.router.add_get("/games-hub", serve_games_hub_html)
    app.router.add_get("/site-hub", serve_site_hub_html)
    app.router.add_get("/api/public/links", public_links_handler)
    app.router.add_get("/api/dashboard", dashboard_api_handler)
    app.router.add_get("/api/admin/settings", admin_settings_get_handler)
    app.router.add_post("/api/admin/settings", admin_settings_post_handler)
    app.router.add_post("/api/admin/agent-finance", admin_agent_finance_handler)
    app.router.add_get("/api/admin/health", admin_health_handler)
    app.router.add_get("/api/admin/neon", admin_neon_handler)
    app.router.add_get("/api/admin/render", admin_render_handler)
    app.router.add_post("/api/admin/broadcast", admin_broadcast_handler)
    app.router.add_get("/api/admin/broadcasts", admin_broadcasts_recent_handler)
    app.router.add_post("/api/admin/users", admin_users_handler)
    app.router.add_post("/api/admin/requests", admin_requests_handler)
    app.router.add_post("/api/admin/support", admin_support_handler)
    app.router.add_post("/api/admin/contests", admin_contests_handler)
    app.router.add_post("/api/admin/bonuses", admin_bonuses_handler)
    app.router.add_post("/api/admin/predictions", admin_predictions_handler)

    setup_application(app, dp, bot=bot)

    # 🆕 جلسة aiohttp مشتركة لطلبات Neon (تُنشأ مرة واحدة وتُغلق عند الإيقاف)
    async def _neon_session_startup(app):
        app['neon_session'] = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=6, connect=3)
        )

    async def _neon_session_cleanup(app):
        sess = app.get('neon_session')
        if sess:
            await sess.close()

    app.on_startup.append(_neon_session_startup)
    app.on_cleanup.append(_neon_session_cleanup)

    app.on_startup.append(lambda app: on_startup(dp, bot))
    app.on_shutdown.append(lambda app: on_shutdown(dp, bot))

    logger.info(f"Starting web server on {WEBAPP_HOST}:{WEBAPP_PORT}")
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)


if __name__ == "__main__":
    main()
