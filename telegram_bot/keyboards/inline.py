from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from config import settings
import database.repository as repo


def get_terms_keyboard():
    keyboard = [
        [InlineKeyboardButton(text="✅ موافق", callback_data="accept_terms")],
        [InlineKeyboardButton(text="📌 قراءة الشروط", callback_data="show_terms_only")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="reject_terms")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_user_app_url():
    """🆕 Mini App للمستخدم عبر مسار جديد لكسر كاش Telegram WebView نهائياً."""
    base = getattr(settings, 'RENDER_EXTERNAL_URL', 'https://ichancy100.onrender.com')
    return f"{base}/user-app-pingo?v=caesar-handoff-v8-20260715"


def get_robert_vip_hub_url():
    base = getattr(settings, 'RENDER_EXTERNAL_URL', 'https://ichancy100.onrender.com')
    return f"{base}/robert-vip?v=robert-hub-v2-20260719"


def get_guides_url():
    """💭 رابط Mini App الشروحات (مع cache-buster لكسر كاش Telegram WebView)."""
    base = getattr(settings, 'RENDER_EXTERNAL_URL', 'https://ichancy100.onrender.com')
    return f"{base}/guides.html?v=guides-miniapp-v2-20260801"


def get_main_menu_keyboard(is_admin=False):
    keyboard = []

    # 🆕 (Update 10) الزر العريض المبهر: Mini App للمستخدم
    keyboard.append([
        InlineKeyboardButton(text="👑 لوحة القيصر", web_app=WebAppInfo(url=get_user_app_url()))
    ])
    keyboard.append([
        InlineKeyboardButton(text="👑 Robert.VIP", web_app=WebAppInfo(url=get_robert_vip_hub_url()))
    ])

    if is_admin:
        keyboard.append([
            InlineKeyboardButton(text="⚙️ لوحة تحكم الإدارة", callback_data="admin_panel")
        ])

    keyboard.extend([
        [InlineKeyboardButton(text="⚡ حساب iChancy", callback_data="ichancy_menu")],
        [
            InlineKeyboardButton(text="📥 شحن رصيد", callback_data="deposit_bot"),
            InlineKeyboardButton(text="📤 سحب رصيد", callback_data="withdraw_bot")
        ],
        [
            InlineKeyboardButton(text="🎁 إهداء رصيد", callback_data="gift_send"),
            InlineKeyboardButton(text="🎫 كود هدية", callback_data="gift_redeem")
        ],
        [
            InlineKeyboardButton(text="💰 الإحالات", callback_data="referral_menu"),
            InlineKeyboardButton(text="🔄 السجل", callback_data="history_menu")
        ],
        [
            InlineKeyboardButton(text="🗳️ رسالة للإدارة", callback_data="message_admin"),
            InlineKeyboardButton(text="✉️ تواصل معنا", callback_data="contact_us")
        ],
        [
            InlineKeyboardButton(text="📌 الشروط", callback_data="show_terms_only"),
            InlineKeyboardButton(text="💭 الشروحات", web_app=WebAppInfo(url=get_guides_url()))
        ],
        [
            InlineKeyboardButton(text="👑 مسابقات القيصر", callback_data="contests_menu"),
            InlineKeyboardButton(text="🎮 ألعاب iChancy", web_app=WebAppInfo(url=repo.get_button_link('games_url')))
        ],
        [InlineKeyboardButton(text="🎁 العروض والبونصات", callback_data="offers_menu")],
        [InlineKeyboardButton(text="🏆 المتصدرون الأسبوعيون", callback_data="weekly_leaderboard_menu")],
        [InlineKeyboardButton(text="🎫 بطاقات التوقع", callback_data="prediction_cards_menu")],
        [
            InlineKeyboardButton(text="🌐 فتح الموقع", web_app=WebAppInfo(url=repo.get_button_link('website_url'))),
            InlineKeyboardButton(text="📱 تحميل التطبيق", url=repo.get_button_link('app_download_url'))
        ],
        [InlineKeyboardButton(text="📘 Facebook البوت", url=repo.get_button_link('betting_url'))]
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_ichancy_submenu(has_account=False):
    keyboard = []

    if not has_account:
        keyboard.append([
            InlineKeyboardButton(text="🆕 إنشاء حساب جديد", callback_data="create_ichancy_account")
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(text="📥 شحن حساب اللعبة", callback_data="deposit_game_acc"),
            InlineKeyboardButton(text="📤 سحب من حساب اللعبة", callback_data="withdraw_game_acc")
        ])

    keyboard.append([
        InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="back_to_main_menu")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_prediction_card_options_keyboard(card_id, options, user_id=None):
    rows = []
    for opt in options:
        rows.append([InlineKeyboardButton(text=f"🎯 {opt}", callback_data=f"predict_select:{card_id}:{opt}")])
    rows.append([InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="back_to_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_prediction_cards_list_keyboard(cards):
    rows = []
    for c in cards:
        label = f"🎫 #{c.get('id')} {c.get('team_a')} × {c.get('team_b')}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"prediction_card_detail:{c.get('id')}")])
    rows.append([InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="back_to_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_contests_list_keyboard(contests):
    rows = []
    for c in contests:
        rows.append([InlineKeyboardButton(text=f"👑 #{c.get('id')} {c.get('title')}"[:64], callback_data=f"contest_detail:{c.get('id')}")])
    rows.append([InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="back_to_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_contest_submit_keyboard(contest_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ إرسال المشاركة", callback_data=f"contest_submit:{contest_id}")],
        [InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="back_to_main_menu")],
    ])
