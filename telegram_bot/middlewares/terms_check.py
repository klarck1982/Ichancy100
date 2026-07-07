from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from typing import Callable, Dict, Any, Awaitable
import database.repository as repo
from config import settings


# 🔧 إصلاح: قائمة الأدمن ليتجاوزوا فحص الشروط
ADMIN_IDS = [item.strip() for item in str(getattr(settings, "ADMIN_IDS", settings.ADMIN_ID)).split(",") if item.strip()]


def _is_admin(user_id) -> bool:
    return str(user_id) in ADMIN_IDS


def get_terms_text() -> str:
    return (
        "📜 <b>الشروط والأحكام</b>\n\n"
        "🚨 <b>يجب عليك الموافقة على الشروط قبل استخدام البوت:</b>\n\n"
        "1️⃣ <b>المتابعة تعني الموافقة على الشروط:</b> أنت تقر بأنك قرأت الشروط وتوافق عليها بالكامل.\n"
        "2️⃣ <b>تبديل طرق الدفع غير مسموح:</b> لا يسمح بشحن رصيد وسحبه بطرق مختلفة بغرض التلاعب.\n"
        "3️⃣ <b>أرباح الإحالات:</b> تحتسب فقط بعد تسجيل 3 إحالات نشطة.\n"
        "4️⃣ <b>المسؤولية:</b> أي محاولة احتيال أو تلاعب تؤدي إلى حظر الحساب ومصادرة الرصيد.\n\n"
        "⚠️ بمجرد المتابعة بعد الموافقة، فأنت تقر بأنك قرأت الشروط ووافقت عليها."
    )


class TermsCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get('event_from_user')
        if not user:
            return await handler(event, data)

        # 🔧 إصلاح: المشرفون يتجاوزون فحص الشروط بالكامل
        # (ضروري لأن أزرار الموافقة/الرفض تُنقر من داخل القنوات)
        if _is_admin(user.id):
            return await handler(event, data)

        telegram_id = str(user.id)
        username = user.username

        db_user = repo.get_user(telegram_id)
        if not db_user:
            repo.create_user(telegram_id, username)
            db_user = repo.get_user(telegram_id)

        is_bypass = False

        if isinstance(event, Message):
            if event.text and (
                event.text.startswith('/start') or
                event.text.startswith('/admin') or
                event.text.startswith('/delete') or
                event.text.startswith('/cancel') or
                event.text.startswith('/home')
            ):
                is_bypass = True
        elif isinstance(event, CallbackQuery):
            if event.data in [
                'accept_terms',
                'reject_terms',
                'show_terms_only',
                'delete_confirm',
                'delete_cancel',
                'back_to_main_menu'
            ]:
                is_bypass = True

        if not is_bypass and db_user and not db_user.get('terms_accepted'):
            from telegram_bot.keyboards.inline import get_terms_keyboard
            terms_text = get_terms_text()

            if isinstance(event, Message):
                await event.answer(terms_text, reply_markup=get_terms_keyboard(), parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                await event.message.answer(terms_text, reply_markup=get_terms_keyboard(), parse_mode="HTML")
                await event.answer("يرجى الموافقة على الشروط أولاً!", show_alert=True)
            return

        return await handler(event, data)
