import logging
import secrets
from datetime import datetime, timezone, timedelta
from database.connection import DatabaseManager

logger = logging.getLogger(__name__)

# 🆕 (Update 14) توقيت سوريا الدقيق (UTC+3) لتوحيد كل العمليات
SYRIA_TZ = timezone(timedelta(hours=3))

def get_syria_now():
    """إرجاع التوقيت الحالي بتوقيت سوريا بدقة، بغضّ النظر عن توقيت الخادم."""
    return datetime.now(SYRIA_TZ)


# ==================== دوال المستخدمين ====================

def get_user(telegram_id):
    query = "SELECT * FROM users WHERE telegram_id = %s"
    return DatabaseManager.execute_query_dict(query, (str(telegram_id),), fetch='one')


def create_user(telegram_id, username=None, referred_by=None):
    user = get_user(telegram_id)
    if not user:
        query = """
        INSERT INTO users (telegram_id, telegram_username, referred_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_id) DO NOTHING
        """
        DatabaseManager.execute_query(query, (str(telegram_id), username, referred_by))

        if referred_by and str(referred_by) != str(telegram_id):
            add_referral(referred_by, telegram_id)

        return True
    return False


def update_user_terms(telegram_id, accepted=True):
    query = "UPDATE users SET terms_accepted = %s WHERE telegram_id = %s"
    DatabaseManager.execute_query(query, (accepted, str(telegram_id)))


def update_user_ichancy_details(telegram_id, username, password, email, player_id):
    query = """
    UPDATE users
    SET ichancy_username = %s, ichancy_password = %s, ichancy_email = %s, player_id = %s
    WHERE telegram_id = %s
    """
    DatabaseManager.execute_query(query, (username, password, email, str(player_id), str(telegram_id)))


def update_user_bot_balance(telegram_id, new_balance):
    query = "UPDATE users SET bot_balance = %s WHERE telegram_id = %s"
    DatabaseManager.execute_query(query, (int(new_balance), str(telegram_id)))


def adjust_user_bot_balance(telegram_id, delta):
    """تعديل رصيد البوت بقيمة موجبة/سالبة بشكل ذري آمن (FOR UPDATE).

    🔒 آمن ضد 'الكتابة فوق' (Lost Update) عند التزامن.
    تُضاف/تُخصم القيمة على الرصيد الفعلي داخل قاعدة البيانات مباشرة،
    فلا يُطرح أي تغيير متزامن آخر.
    """
    conn = None
    cursor = None
    tid = str(telegram_id)
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        # قفل الصف لمنع التزامن، ثم تعديل ذري على القيمة الفعلية
        cursor.execute("SELECT bot_balance FROM users WHERE telegram_id = %s FOR UPDATE", (tid,))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False
        current_balance = int(row[0] or 0)
        if current_balance + int(delta) < 0:
            conn.rollback()
            return False
        cursor.execute(
            "UPDATE users SET bot_balance = bot_balance + %s WHERE telegram_id = %s RETURNING bot_balance",
            (int(delta), tid)
        )
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"adjust_user_bot_balance atomic error: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def update_user_game_balance(telegram_id, new_balance):
    if new_balance is None:
        return
    try:
        val = int(new_balance)
    except (ValueError, TypeError):
        return
    query = "UPDATE users SET game_balance = %s WHERE telegram_id = %s"
    DatabaseManager.execute_query(query, (val, str(telegram_id)))


def get_user_game_balance(telegram_id):
    """جلب رصيد اللعبة المخزن محلياً للمستخدم."""
    user = get_user(telegram_id)
    if user and user.get('game_balance') is not None:
        return int(user['game_balance'])
    return 0


# 🔧 إصلاح: دوال كانت مستدعاة في menu.py لكنها غير معرّفة

def register_user(telegram_id, username=None, phone=None, ichancy_username=None, ichancy_password=None):
    """تسجيل مستخدم جديد بكامل بياناته (مع توافق تام مع جدول users)."""
    tid = str(telegram_id)
    if get_user(tid):
        return False
    query = """
    INSERT INTO users (telegram_id, telegram_username, ichancy_username, ichancy_password)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (telegram_id) DO NOTHING
    """
    DatabaseManager.execute_query(query, (tid, username, ichancy_username, ichancy_password))
    return True


def update_user_balance(telegram_id, balance_change, transaction_type=None, status=None):
    """تعديل رصيد البوت بقيمة موجبة/سالبة مع منع السالب."""
    tid = str(telegram_id)
    user = get_user(tid)
    if not user:
        return False
    new_balance = int(user['bot_balance']) + int(balance_change)
    if new_balance < 0:
        return False
    update_user_bot_balance(tid, new_balance)
    return True


def get_bot_balance(telegram_id=None):
    """رصيد البوت: مجموع أرصدة المستخدمين (أو رصيد مستخدم محدد)."""
    if telegram_id:
        user = get_user(telegram_id)
        return int(user['bot_balance']) if user and user.get('bot_balance') is not None else 0
    result = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(bot_balance), 0) FROM users",
        fetch='one'
    )
    return int(result[0]) if result else 0


def insert_transaction_log(user_id, transaction_type, amount, status, gateway=None, description=None):
    """إدراج سجل معاملة (لعمليات اللعبة التلقائية)."""
    tid = str(user_id)
    query = """
    INSERT INTO transactions (user_telegram_id, type, payment_method, amount, transfer_number, status)
    VALUES (%s, %s, %s, %s, %s, %s)
    """
    DatabaseManager.execute_query(query, (tid, transaction_type, gateway, amount, description, status))


def get_user_game_bonus(telegram_id):
    """جلب مقدار البونص المحجوز حالياً في اللعبة للمستخدم."""
    user = get_user(telegram_id)
    if user and user.get('game_bonus_amount') is not None:
        return int(user['game_bonus_amount'])
    return 0


def update_user_game_bonus(telegram_id, amount):
    """تحديث مقدار البونص المحجوز في اللعبة (لأغراض التدوير)."""
    query = "UPDATE users SET game_bonus_amount = %s WHERE telegram_id = %s"
    DatabaseManager.execute_query(query, (int(amount), str(telegram_id)))


def reduce_user_game_bonus(telegram_id, amount):
    """خصم مبلغ من رصيد البونص المحجوز في اللعبة (عند السحب)."""
    query = """
    UPDATE users
    SET game_bonus_amount = GREATEST(0, COALESCE(game_bonus_amount, 0) - %s)
    WHERE telegram_id = %s
    """
    DatabaseManager.execute_query(query, (int(amount), str(telegram_id)))


def delete_user_completely(telegram_id):
    DatabaseManager.execute_query(
        "DELETE FROM referrals WHERE referrer_telegram_id = %s OR referred_telegram_id = %s",
        (str(telegram_id), str(telegram_id))
    )
    DatabaseManager.execute_query("DELETE FROM transactions WHERE user_telegram_id = %s", (str(telegram_id),))
    DatabaseManager.execute_query(
        "DELETE FROM gifts WHERE sender_telegram_id = %s OR receiver_telegram_id = %s",
        (str(telegram_id), str(telegram_id))
    )
    DatabaseManager.execute_query("DELETE FROM users WHERE telegram_id = %s", (str(telegram_id),))


# ==================== الإحالات ====================

def add_referral(referrer_id, referred_id):
    query = """
    INSERT INTO referrals (referrer_telegram_id, referred_telegram_id, is_active)
    VALUES (%s, %s, FALSE)
    ON CONFLICT (referred_telegram_id) DO NOTHING
    """
    DatabaseManager.execute_query(query, (str(referrer_id), str(referred_id)))


def get_active_referrals_count(referrer_id):
    query = "SELECT COUNT(*) FROM referrals WHERE referrer_telegram_id = %s AND is_active = TRUE"
    result = DatabaseManager.execute_query(query, (str(referrer_id),), fetch='one')
    return result[0] if result else 0


def mark_referral_active(referred_id):
    query = "UPDATE referrals SET is_active = TRUE WHERE referred_telegram_id = %s"
    DatabaseManager.execute_query(query, (str(referred_id),))



def get_referrals_count(referrer_id):
    query = "SELECT COUNT(*) FROM referrals WHERE referrer_telegram_id = %s"
    result = DatabaseManager.execute_query(query, (str(referrer_id),), fetch='one')
    return int(result[0]) if result else 0


def get_referral_percent_by_active_count(active_count):
    """شرائح عمولة الإحالات: 3=3%, 5=5%, 10=10%."""
    active_count = int(active_count or 0)
    if active_count >= 10:
        return 10
    if active_count >= 5:
        return 5
    if active_count >= 3:
        return 3
    return 0


def activate_referral_if_needed(referred_id):
    """تفعيل إحالة المستخدم عند أول إيداع مقبول.

    تعيد referrer_telegram_id إذا تم تفعيل إحالة جديدة فعلاً، وإلا None.
    """
    query = """
    UPDATE referrals
    SET is_active = TRUE
    WHERE referred_telegram_id = %s AND is_active = FALSE
    RETURNING referrer_telegram_id
    """
    result = DatabaseManager.execute_query(query, (str(referred_id),), fetch='one')
    return str(result[0]) if result else None


def get_referrer_for_referred(referred_id):
    query = "SELECT referrer_telegram_id FROM referrals WHERE referred_telegram_id = %s"
    result = DatabaseManager.execute_query(query, (str(referred_id),), fetch='one')
    return str(result[0]) if result else None


def are_referrals_enabled():
    settings_dict = get_bot_settings()
    return bool(settings_dict and settings_dict.get('referrals_enabled', True))


def set_referrals_enabled(enabled=True):
    DatabaseManager.execute_query(
        "UPDATE bot_settings SET referrals_enabled = %s WHERE id = 1",
        (bool(enabled),)
    )




def get_referral_summary():
    result = DatabaseManager.execute_query(
        """
        SELECT
            COUNT(*) AS total_referrals,
            COALESCE(SUM(CASE WHEN is_active = TRUE THEN 1 ELSE 0 END), 0) AS active_referrals
        FROM referrals
        """,
        fetch='one'
    )
    total = int(result[0]) if result else 0
    active = int(result[1]) if result else 0
    return {
        'total_referrals': total,
        'active_referrals': active,
    }


def get_top_referrers(limit=10):
    query = """
    SELECT
        r.referrer_telegram_id,
        u.telegram_username,
        u.ichancy_username,
        u.player_id,
        COUNT(*) AS total_referrals,
        COALESCE(SUM(CASE WHEN r.is_active = TRUE THEN 1 ELSE 0 END), 0) AS active_referrals,
        COALESCE(SUM(awc.commission_amount), 0) AS total_earnings
    FROM referrals r
    LEFT JOIN users u ON u.telegram_id = r.referrer_telegram_id
    LEFT JOIN affiliate_weekly_commissions awc ON awc.referrer_telegram_id = r.referrer_telegram_id
    GROUP BY r.referrer_telegram_id, u.telegram_username, u.ichancy_username, u.player_id
    ORDER BY total_earnings DESC, active_referrals DESC, total_referrals DESC
    LIMIT %s
    """
    return DatabaseManager.execute_query_dict(query, (int(limit),), fetch='all') or []


def get_recent_referral_commissions(limit=20):
    query = """
    SELECT
        rc.*,
        ru.telegram_username AS referrer_username,
        ru.ichancy_username AS referrer_ichancy_username,
        uu.telegram_username AS referred_username,
        uu.ichancy_username AS referred_ichancy_username
    FROM referral_commissions rc
    LEFT JOIN users ru ON ru.telegram_id = rc.referrer_telegram_id
    LEFT JOIN users uu ON uu.telegram_id = rc.referred_telegram_id
    ORDER BY rc.created_at DESC
    LIMIT %s
    """
    return DatabaseManager.execute_query_dict(query, (int(limit),), fetch='all') or []

def get_total_referral_commissions_sum():
    result = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(commission_amount), 0) FROM referral_commissions",
        fetch='one'
    )
    return int(result[0]) if result else 0


def get_total_referral_earnings(referrer_id):
    result = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(commission_amount), 0) FROM affiliate_weekly_commissions WHERE referrer_telegram_id = %s",
        (str(referrer_id),), fetch='one'
    )
    return int(result[0]) if result else 0


def credit_referral_commission_if_applicable(referrer_id, referred_id, transaction_id, deposit_amount_syp):
    """إضافة عمولة الإحالة فوراً لرصيد صاحب الإحالة إذا كان مؤهلاً.

    العمولة تُحسب على مبلغ الإيداع الأساسي فقط، بدون البونصات.
    إذا كان لدى صاحب الإحالة أقل من 3 إحالات نشطة، لا تُضاف عمولة.
    """
    conn = None
    cursor = None
    referrer_id = str(referrer_id)
    referred_id = str(referred_id)
    amount_int = int(float(deposit_amount_syp or 0))

    if amount_int <= 0:
        return {'credited': False, 'reason': 'invalid_amount'}

    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_telegram_id = %s AND is_active = TRUE",
            (referrer_id,)
        )
        active_count = int(cursor.fetchone()[0] or 0)
        percent = get_referral_percent_by_active_count(active_count)
        if percent <= 0:
            conn.rollback()
            return {'credited': False, 'reason': 'not_eligible', 'active_count': active_count, 'percent': 0}

        commission = int(amount_int * (percent / 100.0))
        if commission <= 0:
            conn.rollback()
            return {'credited': False, 'reason': 'zero_commission', 'active_count': active_count, 'percent': percent}

        # منع تكرار العمولة لنفس معاملة الإيداع
        cursor.execute(
            "SELECT id FROM referral_commissions WHERE transaction_id = %s",
            (transaction_id,)
        )
        if cursor.fetchone():
            conn.rollback()
            return {'credited': False, 'reason': 'already_credited'}

        # قفل رصيد المكافآت لصاحب الإحالة قبل الإضافة
        # 🎯 العمولة تذهب إلى bonus_balance (مقيّد، لعب فقط) بدل bot_balance النقدي —
        #    يمنع تجميع عمولات وسحبها نقداً، ويُجبرها على المرور عبر اللعبة (حافة البيت).
        cursor.execute(
            "SELECT bonus_balance FROM users WHERE telegram_id = %s FOR UPDATE",
            (referrer_id,)
        )
        if not cursor.fetchone():
            conn.rollback()
            return {'credited': False, 'reason': 'referrer_not_found'}

        cursor.execute(
            "UPDATE users SET bonus_balance = COALESCE(bonus_balance, 0) + %s WHERE telegram_id = %s",
            (commission, referrer_id)
        )
        cursor.execute(
            """
            INSERT INTO referral_commissions (
                referrer_telegram_id, referred_telegram_id, transaction_id,
                deposit_amount_syp, active_referrals_count, commission_percent,
                commission_amount, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'credited')
            RETURNING id
            """,
            (referrer_id, referred_id, transaction_id, amount_int, active_count, percent, commission)
        )
        commission_id = cursor.fetchone()[0]
        conn.commit()
        return {
            'credited': True,
            'commission_id': commission_id,
            'amount': commission,
            'percent': percent,
            'active_count': active_count,
        }
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"credit_referral_commission_if_applicable error: {e}")
        return {'credited': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


# ==================== المعاملات ====================

def create_transaction(
    telegram_id,
    tx_type,
    amount,
    payment_method=None,
    transfer_number=None,
    status='pending',
    original_amount=None,
    original_currency=None,
    converted_amount_syp=None,
    external_ref=None,
    cashier_profile_id=None,
    cashier_profile_name=None,
    payment_destination=None,
):
    query = """
    INSERT INTO transactions (
        user_telegram_id, type, payment_method, amount, transfer_number, status,
        original_amount, original_currency, converted_amount_syp, external_ref,
        cashier_profile_id, cashier_profile_name, payment_destination
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING id
    """
    result = DatabaseManager.execute_query(
        query,
        (
            str(telegram_id),
            tx_type,
            payment_method,
            amount,
            transfer_number,
            status,
            original_amount,
            original_currency,
            converted_amount_syp,
            external_ref,
            int(cashier_profile_id) if cashier_profile_id is not None else None,
            str(cashier_profile_name)[:120] if cashier_profile_name else None,
            str(payment_destination)[:2000] if payment_destination else None,
        ),
        fetch='one'
    )
    return result[0] if result else None


def create_withdraw_transaction_atomic(telegram_id, amount, payment_method=None, transfer_number=None):
    """إنشاء طلب سحب مع خصم الرصيد بشكل ذري وآمن.

    تنفّذ هذه الدالة الخطوات الحساسة داخل transaction واحدة:
    1) قفل صف المستخدم لمنع الضغط المزدوج/التوازي.
    2) التأكد من عدم وجود طلب سحب معلّق لنفس المستخدم.
    3) التأكد من كفاية الرصيد.
    4) خصم الرصيد.
    5) إنشاء سجل معاملة السحب بحالة pending.

    تعيد dict بالشكل:
    {'success': True, 'tx_id': int, 'old_balance': int, 'new_balance': int}
    أو {'success': False, 'reason': 'not_found'|'pending'|'insufficient'|'error', 'message': str}
    """
    conn = None
    cursor = None
    tid = str(telegram_id)
    amount_int = int(float(amount))

    if amount_int <= 0:
        return {'success': False, 'reason': 'invalid_amount', 'message': 'Invalid withdraw amount'}

    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        # قفل صف المستخدم حتى لا تمر عمليتا سحب بنفس اللحظة لنفس الرصيد
        cursor.execute(
            "SELECT bot_balance FROM users WHERE telegram_id = %s FOR UPDATE",
            (tid,)
        )
        user_row = cursor.fetchone()
        if not user_row:
            conn.rollback()
            return {'success': False, 'reason': 'not_found', 'message': 'User not found'}

        old_balance = int(user_row[0] or 0)

        # فحص الطلب المعلّق داخل نفس القفل/المعاملة
        cursor.execute(
            """
            SELECT id FROM transactions
            WHERE user_telegram_id = %s AND type = 'withdraw_bot' AND status = 'pending'
            LIMIT 1
            """,
            (tid,)
        )
        if cursor.fetchone():
            conn.rollback()
            return {'success': False, 'reason': 'pending', 'message': 'Pending withdraw already exists'}

        if old_balance < amount_int:
            conn.rollback()
            return {
                'success': False,
                'reason': 'insufficient',
                'message': 'Insufficient balance',
                'old_balance': old_balance,
            }

        cursor.execute(
            """
            UPDATE users
            SET bot_balance = bot_balance - %s
            WHERE telegram_id = %s AND bot_balance >= %s
            RETURNING bot_balance
            """,
            (amount_int, tid, amount_int)
        )
        updated = cursor.fetchone()
        if not updated:
            conn.rollback()
            return {
                'success': False,
                'reason': 'insufficient',
                'message': 'Insufficient balance',
                'old_balance': old_balance,
            }

        new_balance = int(updated[0] or 0)

        cursor.execute(
            """
            INSERT INTO transactions (user_telegram_id, type, payment_method, amount, transfer_number, status)
            VALUES (%s, 'withdraw_bot', %s, %s, %s, 'pending')
            RETURNING id
            """,
            (tid, payment_method, amount_int, transfer_number)
        )
        tx_row = cursor.fetchone()
        tx_id = tx_row[0]

        conn.commit()
        return {
            'success': True,
            'tx_id': tx_id,
            'old_balance': old_balance,
            'new_balance': new_balance,
            'amount': amount_int,
        }
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Atomic withdraw transaction error: {e}")
        return {'success': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def calculate_game_bonus_for_deposit(amount, available_bonus=None, bonus_base_balance=None):
    """حساب بونص اللعب المرفق عند شحن حساب iChancy.

    إذا كان البونص مرتبطاً بإيداع: شحن كامل مبلغ الإيداع يصرف كامل البونص،
    وشحن نصفه يصرف نصف البونص. إذا لم توجد قاعدة ربط نستخدم نسبة الإرفاق من الإعدادات كاحتياط.
    """
    amount_int = int(float(amount or 0))
    available_bonus_int = int(float(available_bonus or 0)) if available_bonus is not None else 0
    base_int = int(float(bonus_base_balance or 0)) if bonus_base_balance is not None else 0
    if amount_int <= 0 or available_bonus_int <= 0:
        return 0
    settings = get_bot_settings() or {}
    enabled = True if settings.get('game_bonus_enabled') is None else bool(settings.get('game_bonus_enabled'))
    if not enabled:
        return 0
    if base_int > 0:
        bonus = int(amount_int * available_bonus_int / base_int)
    else:
        # 🔧 إصلاح جوهري: عندما يكون رصيد قاعدة البونص (base_int) صفراً (مثل مكافآت الكاش باك، الهدايا، والحضور)،
        # يتم إرفاق البونص المتاح (available_bonus_int) مع الإيداع النقدي (حتى 100% من قيمة الإيداع النقدي)
        # لضمان وصول رصيد المكافآت إلى حساب اللعبة الفعلي وعدم تجميده بنسبة 10% فقط.
        pct = float(settings.get('game_bonus_apply_percent', 100))
        if pct <= 0:
            pct = 100.0
        pct_bonus = int(amount_int * (pct / 100.0))
        bonus = max(pct_bonus, min(available_bonus_int, amount_int))
    return max(0, min(available_bonus_int, bonus))




def reserve_game_deposit_atomic(telegram_id, amount, player_id):
    """حجز شحن لعبة ذري مع إرفاق بونص اللعب تلقائياً."""
    conn = None
    cursor = None
    tid = str(telegram_id)
    cash_amount = int(float(amount))

    if cash_amount <= 0:
        return {'success': False, 'reason': 'invalid_amount', 'message': 'Invalid amount'}

    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT bot_balance, bonus_balance, bonus_base_balance, cashback_pending_balance, checkin_pending_balance FROM users WHERE telegram_id = %s FOR UPDATE",
            (tid,)
        )
        user_row = cursor.fetchone()
        if not user_row:
            conn.rollback()
            return {'success': False, 'reason': 'not_found', 'message': 'User not found'}

        old_balance = int(user_row[0] or 0)
        old_bonus_balance = int(user_row[1] or 0)
        old_bonus_base_balance = int(user_row[2] or 0)
        cashback_amount = int(user_row[3] or 0)
        checkin_amount = int(user_row[4] or 0)
        bonus_amount = calculate_game_bonus_for_deposit(cash_amount, old_bonus_balance, old_bonus_base_balance)
        base_consumed = min(cash_amount, old_bonus_base_balance) if bonus_amount > 0 else 0
        total_to_game = cash_amount + bonus_amount + cashback_amount + checkin_amount

        cursor.execute(
            """
            UPDATE users
            SET bot_balance = COALESCE(bot_balance, 0) - %s,
                bonus_balance = COALESCE(bonus_balance, 0) - %s,
                bonus_base_balance = GREATEST(0, COALESCE(bonus_base_balance, 0) - %s),
                cashback_pending_balance = 0,
                checkin_pending_balance = 0
            WHERE telegram_id = %s AND COALESCE(bot_balance, 0) >= %s AND COALESCE(bonus_balance, 0) >= %s
            RETURNING bot_balance, bonus_balance, bonus_base_balance, cashback_pending_balance
            """,
            (cash_amount, bonus_amount, base_consumed, tid, cash_amount, bonus_amount)
        )
        updated = cursor.fetchone()
        if not updated:
            conn.rollback()
            return {
                'success': False,
                'reason': 'insufficient',
                'message': 'Insufficient balance',
                'old_balance': old_balance,
                'old_bonus_balance': old_bonus_balance,
            }

        new_balance = int(updated[0] or 0)
        new_bonus_balance = int(updated[1] or 0)
        cursor.execute(
            """
            INSERT INTO transactions (
                user_telegram_id, type, payment_method, amount, transfer_number, status,
                original_amount, original_currency, converted_amount_syp, external_ref, cashback_amount_syp, checkin_amount_syp
            )
            VALUES (%s, 'deposit_to_game', 'game', %s, %s, 'pending', %s, 'cash_syp', %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tid,
                total_to_game,
                f'Game deposit for player {player_id} | cash={cash_amount} | bonus={bonus_amount} | cashback={cashback_amount} | checkin={checkin_amount} | total={total_to_game}',
                cash_amount,
                bonus_amount,
                str(base_consumed),
                cashback_amount,
                checkin_amount,
            )
        )
        tx_id = cursor.fetchone()[0]
        conn.commit()
        return {
            'success': True,
            'tx_id': tx_id,
            'old_balance': old_balance,
            'new_balance': new_balance,
            'old_bonus_balance': old_bonus_balance,
            'new_bonus_balance': new_bonus_balance,
            'old_bonus_base_balance': old_bonus_base_balance,
            'new_bonus_base_balance': int(updated[2] or 0) if len(updated) > 2 else 0,
            'bonus_base_consumed': base_consumed,
            'cash_amount': cash_amount,
            'bonus_amount': bonus_amount,
            'cashback_amount': cashback_amount,
            'checkin_amount': checkin_amount,
            'total_to_game': total_to_game,
            'amount': cash_amount,
        }
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"reserve_game_deposit_atomic error: {e}")
        return {'success': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def confirm_reserved_game_deposit(tx_id):
    """تأكيد شحن اللعبة بعد نجاح API، وتفعيل البونص المرفق كبونص نشط داخل اللعبة."""
    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_telegram_id, status, COALESCE(converted_amount_syp, 0), COALESCE(cashback_amount_syp, 0), COALESCE(checkin_amount_syp, 0) FROM transactions WHERE id = %s FOR UPDATE",
            (int(tx_id),)
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False
        user_telegram_id, status, bonus_amount, cashback_amount, checkin_amount = row
        if status != 'pending':
            conn.rollback()
            return True
        bonus_int = int(float(bonus_amount or 0))
        cashback_int = int(float(cashback_amount or 0))
        checkin_int = int(float(checkin_amount or 0))
        active_bonus_to_add = bonus_int + cashback_int + checkin_int
        if active_bonus_to_add > 0:
            cursor.execute(
                "UPDATE users SET game_bonus_amount = COALESCE(game_bonus_amount, 0) + %s WHERE telegram_id = %s",
                (active_bonus_to_add, str(user_telegram_id))
            )
        cursor.execute(
            "UPDATE transactions SET status = 'completed', reviewed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (int(tx_id),)
        )
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"confirm_reserved_game_deposit error: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def confirm_game_transaction(tx_id):
    """تأكيد عملية لعب بعد نجاح iChancy API: تحديث الحالة إلى completed."""
    try:
        DatabaseManager.execute_query(
            "UPDATE transactions SET status = 'completed', reviewed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (int(tx_id),)
        )
        return True
    except Exception as e:
        logger.error(f"confirm_game_transaction error: {e}")
        return False


def revert_game_transaction(tx_id):
    """إرجاع أرصدة عملية لعبة فاشلة حسب نوعها بدون تحويل البونص إلى كاش."""
    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """SELECT user_telegram_id, type, amount, status, original_amount, converted_amount_syp, external_ref, COALESCE(cashback_amount_syp, 0), COALESCE(checkin_amount_syp, 0)
               FROM transactions WHERE id = %s FOR UPDATE""",
            (int(tx_id),)
        )
        tx_row = cursor.fetchone()
        if not tx_row:
            conn.rollback()
            return False

        user_telegram_id, tx_type, amount, status, original_amount, converted_amount_syp, external_ref, cashback_amount_syp, checkin_amount_syp = tx_row
        if status == 'pending':
            if tx_type == 'deposit_to_game':
                cash_refund = int(float(original_amount or amount or 0))
                bonus_refund = int(float(converted_amount_syp or 0))
                cashback_refund = int(float(cashback_amount_syp or 0))
                checkin_refund = int(float(checkin_amount_syp or 0))
                try:
                    base_refund = int(float(external_ref or 0))
                except Exception:
                    base_refund = 0
                cursor.execute(
                    "UPDATE users SET bot_balance = COALESCE(bot_balance, 0) + %s, bonus_balance = COALESCE(bonus_balance, 0) + %s, bonus_base_balance = COALESCE(bonus_base_balance, 0) + %s, cashback_pending_balance = COALESCE(cashback_pending_balance, 0) + %s, checkin_pending_balance = COALESCE(checkin_pending_balance, 0) + %s WHERE telegram_id = %s",
                    (cash_refund, bonus_refund, base_refund, cashback_refund, checkin_refund, str(user_telegram_id))
                )
            elif tx_type == 'bonus_to_game':
                bonus_refund = int(float(amount or 0))
                cursor.execute(
                    "UPDATE users SET bonus_balance = COALESCE(bonus_balance, 0) + %s, game_bonus_amount = GREATEST(0, COALESCE(game_bonus_amount, 0) - %s) WHERE telegram_id = %s",
                    (bonus_refund, bonus_refund, str(user_telegram_id))
                )
            else:
                amount_int = int(float(amount or 0))
                if amount_int > 0:
                    cursor.execute(
                        "UPDATE users SET bot_balance = COALESCE(bot_balance, 0) + %s WHERE telegram_id = %s",
                        (amount_int, str(user_telegram_id))
                    )
        cursor.execute(
            "UPDATE transactions SET status = 'failed', rejection_reason = 'iChancy API failed - balance refunded', reviewed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (int(tx_id),)
        )
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"revert_game_transaction error: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def credit_balance_atomic(telegram_id, amount):
    """إضافة مبلغ لرصيد المستخدم بشكل ذري آمن (FOR UPDATE + increment).

    🔒 تحلّ مشكلة 'الكتابة فوق' (Lost Update) عند التزامن.
    تُستخدم لإعادة رصيد السحب المرفوض، أو أي إضافة رصيد آمنة.

    تعيد الرصيد الجديد بعد الإضافة، أو None عند الفشل.
    """
    conn = None
    cursor = None
    tid = str(telegram_id)
    amount_int = int(amount)
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT bot_balance FROM users WHERE telegram_id = %s FOR UPDATE", (tid,))
        if not cursor.fetchone():
            conn.rollback()
            return None
        cursor.execute(
            "UPDATE users SET bot_balance = bot_balance + %s WHERE telegram_id = %s RETURNING bot_balance",
            (amount_int, tid)
        )
        row = cursor.fetchone()
        conn.commit()
        return int(row[0]) if row else None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"credit_balance_atomic error: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def settle_game_withdraw_with_active_bonus(telegram_id, withdraw_amount, tx_id=None):
    """تسوية سحب اللعبة: خصم بونص اللعبة النشط أولاً، ثم إضافة الصافي إلى bot_balance."""
    conn = None
    cursor = None
    tid = str(telegram_id)
    amount_int = int(float(withdraw_amount or 0))
    if amount_int <= 0:
        return {'ok': False, 'reason': 'invalid_amount'}
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT bot_balance, game_bonus_amount FROM users WHERE telegram_id = %s FOR UPDATE", (tid,))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {'ok': False, 'reason': 'user_not_found'}
        old_balance = int(row[0] or 0)
        active_bonus = int(row[1] or 0)
        consumed_bonus = min(active_bonus, amount_int)
        cash_to_credit = max(0, amount_int - consumed_bonus)
        remaining_bonus = max(0, active_bonus - consumed_bonus)
        cursor.execute(
            "UPDATE users SET bot_balance = bot_balance + %s, game_bonus_amount = %s WHERE telegram_id = %s RETURNING bot_balance",
            (cash_to_credit, remaining_bonus, tid)
        )
        new_balance = int(cursor.fetchone()[0] or 0)
        if tx_id:
            cursor.execute(
                """UPDATE transactions
                   SET status = 'completed', reviewed_at = CURRENT_TIMESTAMP,
                       original_amount = %s, converted_amount_syp = %s,
                       transfer_number = COALESCE(transfer_number, '') || %s
                   WHERE id = %s""",
                (amount_int, cash_to_credit, f' | active_bonus_deducted={consumed_bonus} | credited={cash_to_credit}', int(tx_id))
            )
        conn.commit()
        return {
            'ok': True,
            'withdraw_amount': amount_int,
            'active_bonus_before': active_bonus,
            'bonus_deducted': consumed_bonus,
            'active_bonus_after': remaining_bonus,
            'cash_credited': cash_to_credit,
            'old_balance': old_balance,
            'new_balance': new_balance,
        }
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"settle_game_withdraw_with_active_bonus error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def approve_deposit_atomic(telegram_id, deposit_amount, bonus_amount, tx_id, reviewed_by=None, new_vip_tier=None):
    """اعتماد إيداع بشكل ذري كامل: القفل + الإضافة + التعليم.

    تنفّذ داخل معاملة واحدة لا تُخترق:
    1) قفل سجل المعاملة والتأكد أنه لا يزال pending (منع القبول المزدوج).
    2) قفل صف المستخدم.
    3) إضافة الإيداع النقدي إلى bot_balance (قابل للسحب)،
       وإضافة البونص إلى bonus_balance (رصيد مكافآت مقيّد، لا يُسحب نقداً).
       🎯 هذا يمنع استخراج البونص نقداً ويُجبره على المرور عبر اللعبة (حافة البيت).
    4) تعليم المعاملة approved.

    تعيد dict: {'ok': True, 'new_balance': int, 'new_bonus_balance': int,
                'deposit_added': int, 'bonus_added': int, 'already_approved': bool}
    أو {'ok': False, 'reason': str}
    """
    conn = None
    cursor = None
    tid = str(telegram_id)
    deposit_added = int(deposit_amount)
    bonus_added = int(bonus_amount or 0)
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        # 1) قفل سجل المعاملة والتأكد من حالته (منع قبول مزدوج للأدمنين المتزامنين)
        cursor.execute("SELECT status FROM transactions WHERE id = %s FOR UPDATE", (int(tx_id),))
        tx_row = cursor.fetchone()
        if not tx_row:
            conn.rollback()
            return {'ok': False, 'reason': 'tx_not_found'}
        current_status = tx_row[0]
        if current_status != 'pending':
            conn.rollback()
            return {'ok': True, 'already_approved': True, 'new_balance': None}

        # 2) قفل صف المستخدم
        cursor.execute("SELECT bot_balance FROM users WHERE telegram_id = %s FOR UPDATE", (tid,))
        if not cursor.fetchone():
            conn.rollback()
            return {'ok': False, 'reason': 'user_not_found'}

        # 3) إضافة ذرّية: الإيداع النقدي → bot_balance ، والبونص → bonus_balance (مقيّد)
        # ونضيف deposit_added إلى bonus_base_balance فقط إذا وُجد بونص، ليُصرف البونص نسبياً عند شحن اللعبة.
        bonus_base_added = deposit_added if bonus_added > 0 else 0
        cursor.execute(
            """UPDATE users
               SET bot_balance = COALESCE(bot_balance, 0) + %s,
                   bonus_balance = COALESCE(bonus_balance, 0) + %s,
                   bonus_base_balance = COALESCE(bonus_base_balance, 0) + %s,
                   vip_tier = COALESCE(%s, vip_tier)
               WHERE telegram_id = %s
               RETURNING bot_balance, bonus_balance""",
            (deposit_added, bonus_added, bonus_base_added, new_vip_tier, tid)
        )
        new_row = cursor.fetchone()
        new_balance = int(new_row[0]) if new_row else 0
        new_bonus_balance = int(new_row[1]) if new_row and len(new_row) > 1 else 0

        # 4) تعليم المعاملة approved
        cursor.execute(
            """UPDATE transactions
               SET status = 'approved',
                   reviewed_by = COALESCE(%s, reviewed_by),
                   reviewed_at = CURRENT_TIMESTAMP,
                   bonus_base_added_syp = COALESCE(bonus_base_added_syp, 0) + %s
               WHERE id = %s""",
            (str(reviewed_by) if reviewed_by else None, bonus_base_added, int(tx_id))
        )

        conn.commit()
        return {
            'ok': True,
            'new_balance': new_balance,
            'new_bonus_balance': new_bonus_balance,
            'deposit_added': deposit_added,
            'bonus_added': bonus_added,
            'already_approved': False,
        }
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"approve_deposit_atomic error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def get_transaction_by_id(tx_id):
    query = "SELECT * FROM transactions WHERE id = %s"
    return DatabaseManager.execute_query_dict(query, (tx_id,), fetch='one')


def get_pending_transactions():
    query = "SELECT * FROM transactions WHERE status = 'pending' ORDER BY created_at DESC"
    return DatabaseManager.execute_query_dict(query, fetch='all')


def get_pending_requests():
    query = """
    SELECT id, user_telegram_id, type, amount, payment_method, status, created_at,
           original_amount, original_currency, converted_amount_syp
    FROM transactions
    WHERE status = 'pending'
    ORDER BY created_at DESC
    LIMIT 10
    """
    return DatabaseManager.execute_query_dict(query, fetch='all')


def get_all_transactions(limit=20):
    query = """
    SELECT id, user_telegram_id, type, amount, payment_method, status, created_at,
           original_amount, original_currency, converted_amount_syp
    FROM transactions
    ORDER BY created_at DESC
    LIMIT %s
    """
    return DatabaseManager.execute_query_dict(query, (limit,), fetch='all')


def search_transactions(query):
    try:
        q_int = int(query)
        sql = """
        SELECT id, user_telegram_id, type, amount, payment_method, status, created_at,
               original_amount, original_currency, converted_amount_syp
        FROM transactions
        WHERE id = %s OR user_telegram_id = %s
        ORDER BY created_at DESC
        LIMIT 10
        """
        return DatabaseManager.execute_query_dict(sql, (q_int, str(q_int)), fetch='all')
    except ValueError:
        sql = """
        SELECT id, user_telegram_id, type, amount, payment_method, status, created_at,
               original_amount, original_currency, converted_amount_syp
        FROM transactions
        WHERE user_telegram_id = %s
        ORDER BY created_at DESC
        LIMIT 10
        """
        return DatabaseManager.execute_query_dict(sql, (query,), fetch='all')


def get_user_pending_transactions(telegram_id, tx_type=None):
    if tx_type:
        query = """
        SELECT * FROM transactions
        WHERE user_telegram_id = %s AND type = %s AND status = 'pending'
        ORDER BY created_at DESC
        """
        return DatabaseManager.execute_query_dict(query, (str(telegram_id), tx_type), fetch='all')
    query = """
    SELECT * FROM transactions
    WHERE user_telegram_id = %s AND status = 'pending'
    ORDER BY created_at DESC
    """
    return DatabaseManager.execute_query_dict(query, (str(telegram_id),), fetch='all')


def has_pending_transaction(telegram_id, tx_type=None):
    rows = get_user_pending_transactions(telegram_id, tx_type=tx_type)
    return len(rows) > 0

def is_external_ref_used(external_ref, exclude_tx_id=None):
    ref = str(external_ref or '').strip()
    if not ref:
        return False
    if exclude_tx_id:
        result = DatabaseManager.execute_query(
            """SELECT id FROM transactions
               WHERE external_ref = %s AND id <> %s AND status IN ('approved','completed')
               LIMIT 1""",
            (ref, int(exclude_tx_id)), fetch='one'
        )
    else:
        result = DatabaseManager.execute_query(
            """SELECT id FROM transactions
               WHERE external_ref = %s AND status IN ('approved','completed')
               LIMIT 1""",
            (ref,), fetch='one'
        )
    return result is not None


def set_transaction_external_ref(tx_id, external_ref):
    DatabaseManager.execute_query(
        "UPDATE transactions SET external_ref = %s WHERE id = %s",
        (str(external_ref), int(tx_id))
    )


def update_transaction_status(tx_id, status, reviewed_by=None):
    query = """
    UPDATE transactions
    SET status = %s,
        reviewed_by = COALESCE(%s, reviewed_by),
        reviewed_at = CURRENT_TIMESTAMP
    WHERE id = %s
    """
    DatabaseManager.execute_query(query, (status, str(reviewed_by) if reviewed_by else None, tx_id))


def update_transaction_rejection_reason(tx_id, reason):
    query = "UPDATE transactions SET rejection_reason = %s WHERE id = %s"
    DatabaseManager.execute_query(query, (reason, tx_id))


def is_transaction_pending(tx_id):
    tx = get_transaction_by_id(tx_id)
    return bool(tx and tx.get('status') == 'pending')


def delete_transaction_safe(tx_id, refund=False):
    """حذف طلب نهائياً مع خيار إعادة الرصيد للمستخدم.

    - refund=True: يعيد المبلغ لرصيد المستخدم قبل الحذف (مناسب لطلبات السحب المعلّقة).
    - refund=False: يحذف فقط دون لمس الرصيد.
    تعيد dict بالنتيجة.
    """
    tx = get_transaction_by_id(tx_id)
    if not tx:
        return {'ok': False, 'reason': 'not_found'}

    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        refunded = 0
        if refund:
            amount = int(float(tx.get('amount') or 0))
            user_tid = str(tx.get('user_telegram_id'))
            tx_type = tx.get('type')
            # إعادة الرصيد منطقياً بأمان:
            if tx_type == 'withdraw_bot' and amount > 0:
                cursor.execute(
                    "UPDATE users SET bot_balance = bot_balance + %s WHERE telegram_id = %s",
                    (amount, user_tid)
                )
                refunded = amount
            elif tx_type in ('deposit_to_game', 'bonus_to_game') and amount > 0:
                # شحن لعبة معلّق (محجوز) → نفصله ونعيد الكاش والبونص بشكل مستقل عبر الدالة المخصصة
                revert_game_transaction(tx_id)
                refunded = amount

        cursor.execute("DELETE FROM transactions WHERE id = %s", (int(tx_id),))
        conn.commit()
        return {'ok': True, 'refunded': refunded, 'tx_type': tx.get('type')}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"delete_transaction_safe error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def get_user_transactions_history(telegram_id, limit=10):
    query = "SELECT * FROM transactions WHERE user_telegram_id = %s ORDER BY created_at DESC LIMIT %s"
    return DatabaseManager.execute_query_dict(query, (str(telegram_id), limit), fetch='all')


def get_transaction_stats_for_user(telegram_id):
    total_result = DatabaseManager.execute_query(
        "SELECT COUNT(*) FROM transactions WHERE user_telegram_id = %s",
        (str(telegram_id),),
        fetch='one'
    )
    pending_result = DatabaseManager.execute_query(
        "SELECT COUNT(*) FROM transactions WHERE user_telegram_id = %s AND status = 'pending'",
        (str(telegram_id),),
        fetch='one'
    )
    approved_result = DatabaseManager.execute_query(
        "SELECT COUNT(*) FROM transactions WHERE user_telegram_id = %s AND status = 'approved'",
        (str(telegram_id),),
        fetch='one'
    )
    rejected_result = DatabaseManager.execute_query(
        "SELECT COUNT(*) FROM transactions WHERE user_telegram_id = %s AND status = 'rejected'",
        (str(telegram_id),),
        fetch='one'
    )

    return {
        'total': total_result[0] if total_result else 0,
        'pending': pending_result[0] if pending_result else 0,
        'approved': approved_result[0] if approved_result else 0,
        'rejected': rejected_result[0] if rejected_result else 0,
    }


# ==================== بطاقات الهدايا ====================

def create_gift(sender_id, amount, code, receiver_id=None):
    """إنشاء كود إهداء رصيد من مستخدم لمستخدم بشكل ذري وآمن.

    يخصم المبلغ من رصيد المُرسل وينشئ الكود داخل transaction واحدة،
    حتى لا يتم خصم الرصيد بدون إنشاء الكود ولا يمر ضغط مزدوج بنفس الرصيد.
    """
    conn = None
    cursor = None
    sender_tid = str(sender_id)
    receiver_tid = str(receiver_id) if receiver_id else None
    amount_int = int(amount)

    if amount_int <= 0:
        return False, "قيمة كود الهدية غير صالحة!"

    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        # قفل رصيد المُرسل لمنع إنشاء أكثر من كود بنفس الرصيد عند الضغط المتزامن
        cursor.execute(
            "SELECT bot_balance FROM users WHERE telegram_id = %s FOR UPDATE",
            (sender_tid,)
        )
        sender_row = cursor.fetchone()
        if not sender_row:
            conn.rollback()
            return False, "لم يتم العثور على حسابك. الرجاء استخدام /start أولاً."

        sender_balance = int(sender_row[0] or 0)
        if sender_balance < amount_int:
            conn.rollback()
            return False, "رصيدك غير كافٍ لتوليد هذا الكود!"

        cursor.execute(
            """
            UPDATE users
            SET bot_balance = bot_balance - %s
            WHERE telegram_id = %s AND bot_balance >= %s
            RETURNING bot_balance
            """,
            (amount_int, sender_tid, amount_int)
        )
        if not cursor.fetchone():
            conn.rollback()
            return False, "رصيدك غير كافٍ لتوليد هذا الكود!"

        cursor.execute(
            """
            INSERT INTO gifts (sender_telegram_id, receiver_telegram_id, code, amount, is_redeemed)
            VALUES (%s, %s, %s, %s, FALSE)
            """,
            (sender_tid, receiver_tid, code, amount_int)
        )

        conn.commit()
        return True, "تم إنشاء الكود بنجاح!"
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"create_gift atomic error: {e}")
        return False, "تعذر إنشاء كود الهدية. يرجى المحاولة مجدداً."
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def get_gift_by_code(code):
    normalized = str(code).strip().upper()
    query = "SELECT * FROM gifts WHERE UPPER(code) = %s"
    return DatabaseManager.execute_query_dict(query, (normalized,), fetch='one')


def create_bot_gift(admin_id, amount, code):
    """إنشاء كود هدية من البوت/الأدمن بدون خصم رصيد أي مستخدم.

    الكود يستخدم مرة واحدة فقط، ويُخزن في جدول gifts بنفس آلية الاسترداد.
    sender_telegram_id يكون بصيغة ADMIN:<id> للتمييز عن إهداء المستخدمين.
    """
    amount_int = int(amount)
    if amount_int <= 0:
        return False, "قيمة كود الهدية غير صالحة."

    query = """
    INSERT INTO gifts (sender_telegram_id, receiver_telegram_id, code, amount, is_redeemed)
    VALUES (%s, NULL, %s, %s, FALSE)
    """
    try:
        DatabaseManager.execute_query(query, (f"ADMIN:{admin_id}", code, amount_int))
        return True, "تم إنشاء كود الهدية بنجاح."
    except Exception as e:
        logger.error(f"create_bot_gift error: {e}")
        return False, "تعذر إنشاء كود الهدية. يرجى المحاولة مجدداً."


def calculate_campaign_distribution(input_mode, input_value, max_redemptions):
    """Calculate per-redemption value and actual maximum campaign spend."""
    count = int(max_redemptions or 0)
    value = int(input_value or 0)
    if count <= 0 or value <= 0:
        return {'ok': False, 'reward_amount': 0, 'total_budget': 0, 'remainder': 0}
    if str(input_mode or 'per_code') == 'total_budget':
        reward = value // count
        actual = reward * count
        return {'ok': reward > 0, 'reward_amount': reward, 'total_budget': actual, 'remainder': value - actual}
    return {'ok': True, 'reward_amount': value, 'total_budget': value * count, 'remainder': 0}


def create_gift_campaign(name, reward_type, code_mode, reward_amount, max_redemptions, duration_hours, requires_ichancy=True, created_by=None):
    """Create a campaign and its unique batch/shared code inside one transaction."""
    reward_type = str(reward_type or '').strip().lower()
    code_mode = str(code_mode or '').strip().lower()
    amount = int(reward_amount or 0)
    max_uses = int(max_redemptions or 0)
    hours = int(duration_hours or 0)
    if reward_type not in ('bonus', 'cash') or code_mode not in ('unique', 'shared'):
        return {'ok': False, 'reason': 'invalid_type'}
    if amount < 1 or max_uses < 1 or max_uses > 1000 or hours < 1 or hours > 2160:
        return {'ok': False, 'reason': 'invalid_values'}
    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO gift_campaigns (name, reward_type, code_mode, reward_amount,
                max_redemptions, requires_ichancy, status, starts_at, ends_at, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP + (%s * INTERVAL '1 hour'), %s)
            RETURNING id
            """,
            (str(name).strip(), reward_type, code_mode, amount, max_uses, bool(requires_ichancy), hours, str(created_by or ''))
        )
        row = cursor.fetchone()
        campaign_id = int(row[0])
        prefix = 'CAESAR-BONUS' if reward_type == 'bonus' else 'CAESAR-CASH'
        code_count = max_uses if code_mode == 'unique' else 1
        per_code_limit = 1 if code_mode == 'unique' else max_uses
        codes = []
        for _ in range(code_count):
            code = f"{prefix}-C{campaign_id}-{secrets.token_hex(4).upper()}"
            cursor.execute(
                """
                INSERT INTO gift_campaign_codes (campaign_id, code, max_redemptions, redemptions_count, is_active)
                VALUES (%s, %s, %s, 0, TRUE)
                """,
                (campaign_id, code, per_code_limit)
            )
            codes.append(code)
        conn.commit()
        return {
            'ok': True,
            'campaign_id': campaign_id,
            'codes': codes,
            'reward_amount': amount,
            'max_redemptions': max_uses,
            'total_budget': amount * max_uses,
        }
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"create_gift_campaign error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def get_gift_campaigns(limit=100):
    return DatabaseManager.execute_query_dict(
        """
        SELECT c.*,
            COALESCE((SELECT COUNT(*) FROM gift_campaign_redemptions r WHERE r.campaign_id=c.id),0) AS redeemed_count,
            COALESCE((SELECT COUNT(*) FROM gift_campaign_codes cc WHERE cc.campaign_id=c.id),0) AS codes_count
        FROM gift_campaigns c ORDER BY c.created_at DESC LIMIT %s
        """,
        (int(limit),), fetch='all'
    ) or []


def get_gift_campaign(campaign_id):
    return DatabaseManager.execute_query_dict(
        """
        SELECT c.*,
            COALESCE((SELECT COUNT(*) FROM gift_campaign_redemptions r WHERE r.campaign_id=c.id),0) AS redeemed_count,
            COALESCE((SELECT COUNT(*) FROM gift_campaign_codes cc WHERE cc.campaign_id=c.id),0) AS codes_count
        FROM gift_campaigns c WHERE c.id=%s
        """,
        (int(campaign_id),), fetch='one'
    )


def get_gift_campaign_codes(campaign_id):
    return DatabaseManager.execute_query_dict(
        "SELECT * FROM gift_campaign_codes WHERE campaign_id=%s ORDER BY id ASC",
        (int(campaign_id),), fetch='all'
    ) or []


def get_gift_campaign_redemptions(campaign_id, limit=200):
    return DatabaseManager.execute_query_dict(
        """
        SELECT r.*, u.telegram_username, u.ichancy_username
        FROM gift_campaign_redemptions r
        LEFT JOIN users u ON u.telegram_id=r.user_telegram_id
        WHERE r.campaign_id=%s ORDER BY r.redeemed_at DESC LIMIT %s
        """,
        (int(campaign_id), int(limit)), fetch='all'
    ) or []


def set_gift_campaign_status(campaign_id, status):
    status = str(status or '').strip().lower()
    if status not in ('active', 'paused', 'closed'):
        return False
    DatabaseManager.execute_query(
        "UPDATE gift_campaigns SET status=%s WHERE id=%s",
        (status, int(campaign_id))
    )
    return True


def get_campaign_code_info(code):
    return DatabaseManager.execute_query_dict(
        """
        SELECT cc.id AS code_id, cc.code, cc.campaign_id, c.name AS campaign_name,
               c.reward_type, c.reward_amount, c.reward_amount AS amount,
               ('CAMPAIGN:' || c.id::text) AS sender_telegram_id, c.status, c.ends_at
        FROM gift_campaign_codes cc JOIN gift_campaigns c ON c.id=cc.campaign_id
        WHERE UPPER(cc.code)=UPPER(%s)
        """,
        (str(code).strip(),), fetch='one'
    )


def redeem_campaign_code(code, receiver_id):
    """Redeem a campaign code atomically, enforcing one reward per user/campaign."""
    conn = None
    cursor = None
    normalized = str(code or '').strip().upper()
    receiver_tid = str(receiver_id)
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT cc.id, cc.campaign_id, cc.max_redemptions, cc.redemptions_count, cc.is_active,
                   c.name, c.reward_type, c.reward_amount, c.max_redemptions,
                   c.requires_ichancy, c.status, c.starts_at, c.ends_at
            FROM gift_campaign_codes cc
            JOIN gift_campaigns c ON c.id=cc.campaign_id
            WHERE UPPER(cc.code)=%s
            FOR UPDATE OF cc, c
            """,
            (normalized,)
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {'found': False, 'ok': False, 'reason': 'not_found'}
        (code_id, campaign_id, code_max, code_used, code_active, campaign_name,
         reward_type, reward_amount, campaign_max, requires_ichancy, status,
         starts_at, ends_at) = row
        now = datetime.now(timezone.utc)
        if status != 'active' or not code_active or (starts_at and now < starts_at) or (ends_at and now >= ends_at):
            conn.rollback()
            return {'found': True, 'ok': False, 'reason': 'inactive', 'message': 'هذه الحملة غير نشطة أو انتهت صلاحيتها.'}
        if int(code_used or 0) >= int(code_max or 0):
            conn.rollback()
            return {'found': True, 'ok': False, 'reason': 'code_exhausted', 'message': 'تم استخدام هذا الكود بالكامل.'}
        cursor.execute("SELECT bot_balance, player_id FROM users WHERE telegram_id=%s FOR UPDATE", (receiver_tid,))
        user = cursor.fetchone()
        if not user:
            conn.rollback()
            return {'found': True, 'ok': False, 'reason': 'user_not_found', 'message': 'استخدم /start أولاً ثم حاول مجددًا.'}
        if requires_ichancy and not user[1]:
            conn.rollback()
            return {'found': True, 'ok': False, 'reason': 'ichancy_required', 'message': 'هذه الحملة متاحة للمستخدمين الذين لديهم حساب iChancy مرتبط.'}
        cursor.execute(
            "SELECT id FROM gift_campaign_redemptions WHERE campaign_id=%s AND user_telegram_id=%s",
            (campaign_id, receiver_tid)
        )
        if cursor.fetchone():
            conn.rollback()
            return {'found': True, 'ok': False, 'reason': 'user_limit', 'message': 'لقد حصلت على مكافأتك من هذه الحملة مسبقًا.'}
        cursor.execute("SELECT COUNT(*) FROM gift_campaign_redemptions WHERE campaign_id=%s", (campaign_id,))
        campaign_used = int((cursor.fetchone() or [0])[0] or 0)
        if campaign_used >= int(campaign_max or 0):
            conn.rollback()
            return {'found': True, 'ok': False, 'reason': 'campaign_exhausted', 'message': 'اكتمل عدد المستفيدين من هذه الحملة.'}
        amount_int = int(reward_amount or 0)
        cursor.execute(
            """
            INSERT INTO gift_campaign_redemptions
                (campaign_id, code_id, user_telegram_id, reward_type, reward_amount)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (campaign_id, code_id, receiver_tid, reward_type, amount_int)
        )
        cursor.execute(
            """
            UPDATE gift_campaign_codes SET redemptions_count=redemptions_count+1,
                is_active=(redemptions_count+1 < max_redemptions)
            WHERE id=%s
            """,
            (code_id,)
        )
        if reward_type == 'bonus':
            cursor.execute("UPDATE users SET bonus_balance=COALESCE(bonus_balance,0)+%s WHERE telegram_id=%s", (amount_int, receiver_tid))
            message = f"تمت إضافة {amount_int:,} ل.س إلى رصيد مكافآت اللعب من حملة {campaign_name}."
        else:
            cursor.execute("UPDATE users SET bot_balance=COALESCE(bot_balance,0)+%s WHERE telegram_id=%s", (amount_int, receiver_tid))
            message = f"تمت إضافة {amount_int:,} ل.س إلى رصيدك القابل للسحب من حملة {campaign_name}."
        conn.commit()
        return {'found': True, 'ok': True, 'campaign_id': int(campaign_id), 'amount': amount_int, 'reward_type': reward_type, 'message': message}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"redeem_campaign_code error: {e}")
        return {'found': True, 'ok': False, 'reason': 'error', 'message': 'تعذر استرداد كود الحملة حاليًا.'}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def redeem_gift(code, receiver_id):
    """استرداد كود هدية بشكل ذري وآمن.

    يقفل سجل الهدية أثناء الاسترداد حتى لا يتم استخدام نفس الكود مرتين،
    ثم يعلّم الكود كمستخدم ويضيف الرصيد للمستلم داخل transaction واحدة.
    """
    conn = None
    cursor = None
    receiver_tid = str(receiver_id)
    normalized_code = str(code).strip().upper()

    code_parts = normalized_code.split('-')
    is_campaign_format = len(code_parts) >= 4 and code_parts[2].startswith('C') and code_parts[2][1:].isdigit()
    if is_campaign_format:
        campaign_result = redeem_campaign_code(normalized_code, receiver_tid)
        if campaign_result.get('found'):
            return bool(campaign_result.get('ok')), campaign_result.get('message') or 'تعذر استرداد كود الحملة.'

    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT id, sender_telegram_id, receiver_telegram_id, amount, is_redeemed
            FROM gifts
            WHERE UPPER(code) = %s
            FOR UPDATE
            """,
            (normalized_code,)
        )
        gift = cursor.fetchone()
        if not gift:
            conn.rollback()
            return False, "كود الهدية هذا غير موجود!"

        gift_id, sender_telegram_id, dedicated_receiver, amount, is_redeemed = gift
        amount_int = int(amount)
        code_upper = normalized_code.upper()
        is_bonus_gift = code_upper.startswith('CAESAR-BONUS-')

        if is_redeemed:
            conn.rollback()
            return False, "لقد تم استخدام هذا الكود مسبقاً!"

        if dedicated_receiver and str(dedicated_receiver) != receiver_tid:
            conn.rollback()
            return False, "عذراً، هذا الكود مخصص لشخص آخر فقط!"

        # 🔒 حماية عروض السوشال ميديا (Anti-Greed Guard): منع لاعب واحد من التهام جميع أكواد البونص المنشورة معاً
        # إذا كان الكود بونص عام صادر من المشرف (ADMIN:...) ومتاح للجميع (بدون تخصيص شخصي)،
        # نفحص هل قام هذا المستخدم باسترداد كود بونص عام آخر خلال آخر 24 ساعة.
        if str(sender_telegram_id or '').startswith('ADMIN:') and not dedicated_receiver and is_bonus_gift:
            cursor.execute(
                """
                SELECT id FROM gifts
                WHERE receiver_telegram_id = %s
                  AND sender_telegram_id LIKE 'ADMIN:%%'
                  AND UPPER(code) LIKE 'CAESAR-BONUS-%%'
                  AND redeemed_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'
                LIMIT 1
                """,
                (receiver_tid,)
            )
            if cursor.fetchone():
                conn.rollback()
                return False, "⏳ عذراً! لقد قمت باسترداد كود بونص مجاني اليوم بالفعل. لضمان تكافؤ الفرص واستفادة أكبر عدد من اللاعبين من عروض السوشال ميديا، يُسمح باسترداد كود بونص عام واحد لكل لاعب خلال 24 ساعة. تابعنا للحصول على العروض القادمة!"

        # قفل صف المستلم أيضاً لضمان تحديث الرصيد بأمان
        cursor.execute(
            "SELECT bot_balance FROM users WHERE telegram_id = %s FOR UPDATE",
            (receiver_tid,)
        )
        receiver_row = cursor.fetchone()
        if not receiver_row:
            conn.rollback()
            return False, "لم يتم العثور على حسابك. الرجاء استخدام /start أولاً ثم حاول مجدداً."

        cursor.execute(
            """
            UPDATE gifts
            SET is_redeemed = TRUE,
                receiver_telegram_id = %s,
                redeemed_at = CURRENT_TIMESTAMP
            WHERE id = %s AND is_redeemed = FALSE
            """,
            (receiver_tid, gift_id)
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return False, "لقد تم استخدام هذا الكود مسبقاً!"

        if is_bonus_gift:
            cursor.execute(
                "UPDATE users SET bonus_balance = COALESCE(bonus_balance, 0) + %s WHERE telegram_id = %s",
                (amount_int, receiver_tid)
            )
            success_msg = f"تهانينا! تم تفعيل كود بونص بنجاح وإضافة {amount_int:,} ل.س إلى رصيد مكافآت اللعب."
        else:
            cursor.execute(
                "UPDATE users SET bot_balance = COALESCE(bot_balance, 0) + %s WHERE telegram_id = %s",
                (amount_int, receiver_tid)
            )
            success_msg = f"تهانينا! تم تفعيل كود كاش بنجاح وإضافة {amount_int:,} ل.س إلى رصيدك القابل للسحب."

        conn.commit()
        return True, success_msg
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"redeem_gift atomic error: {e}")
        return False, "تعذر استرداد الكود حالياً. يرجى المحاولة مجدداً."
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


# ==================== إعدادات البوت الديناميكية ====================

def get_bot_settings():
    if hasattr(DatabaseManager, 'get_bot_settings_cached'):
        settings_dict = DatabaseManager.get_bot_settings_cached()
    else:
        settings_dict = DatabaseManager.execute_query_dict("SELECT * FROM bot_settings WHERE id = 1", fetch='one')
    if not settings_dict:
        # 🛡️ حماية: ضمان وجود السجل الافتراضي قبل أي استخدام
        DatabaseManager.execute_query(
            "INSERT INTO bot_settings (id, exchange_rate, usd_buy_rate, usd_sell_rate, withdraw_commission, agent_balance, game_min_deposit_syp, agent_revenue_percent, min_deposit_syp, min_deposit_usd, min_withdraw_syp, min_withdraw_usd, syp_version) "
            "VALUES (1, 1000, 14000, 15000, 10, 0, 20000, 30, 20000, 5, 25000, 10, 'old') ON CONFLICT (id) DO NOTHING;"
        )
        settings_dict = DatabaseManager.execute_query_dict("SELECT * FROM bot_settings WHERE id = 1", fetch='one')
    
    # 🆕 قيم افتراضية لإعدادات التدوير (Rollover)
    if settings_dict:
        settings_dict['bonus_rollover_multiplier'] = settings_dict.get('bonus_rollover_multiplier') or 5.0
        settings_dict['turnover_field_name'] = settings_dict.get('turnover_field_name') or 'totalBet'
        # مهم للمشاريع القائمة: إذا أُضيفت الأعمدة لاحقاً وكانت NULL نعتبرها مفعلة و10% افتراضياً.
        settings_dict['game_bonus_enabled'] = True if settings_dict.get('game_bonus_enabled') is None else bool(settings_dict.get('game_bonus_enabled'))
        settings_dict['game_bonus_apply_percent'] = 10 if settings_dict.get('game_bonus_apply_percent') is None else settings_dict.get('game_bonus_apply_percent')
        settings_dict['syriatel_auto_mode'] = settings_dict.get('syriatel_auto_mode') or 'off'
        settings_dict['syriatel_auto_channel_id'] = settings_dict.get('syriatel_auto_channel_id') or ''
        settings_dict['maintenance_mode'] = bool(settings_dict.get('maintenance_mode', False))
        settings_dict['deposits_enabled'] = True if settings_dict.get('deposits_enabled') is None else bool(settings_dict.get('deposits_enabled'))
        settings_dict['withdrawals_enabled'] = True if settings_dict.get('withdrawals_enabled') is None else bool(settings_dict.get('withdrawals_enabled'))
        settings_dict['game_transfers_enabled'] = True if settings_dict.get('game_transfers_enabled') is None else bool(settings_dict.get('game_transfers_enabled'))
        
    return settings_dict


def update_bot_settings(exchange_rate=None, usd_buy_rate=None, usd_sell_rate=None, withdraw_commission=None, ichancy_cookie=None, agent_balance=None, referrals_enabled=None, game_min_deposit_syp=None, agent_revenue_percent=None, min_deposit_syp=None, min_deposit_usd=None, min_withdraw_syp=None, min_withdraw_usd=None, syp_version=None, bonus_rollover_multiplier=None, turnover_field_name=None, game_bonus_enabled=None, game_bonus_apply_percent=None, syriatel_auto_mode=None, syriatel_auto_channel_id=None, agent_balance_alert_threshold=None):
    settings_dict = get_bot_settings()
    if not settings_dict:
        DatabaseManager.execute_query("INSERT INTO bot_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;")
        settings_dict = get_bot_settings()

    new_exchange_rate = exchange_rate if exchange_rate is not None else settings_dict['exchange_rate']
    new_usd_buy_rate = usd_buy_rate if usd_buy_rate is not None else settings_dict['usd_buy_rate']
    new_usd_sell_rate = usd_sell_rate if usd_sell_rate is not None else settings_dict['usd_sell_rate']
    new_withdraw_commission = withdraw_commission if withdraw_commission is not None else settings_dict['withdraw_commission']
    new_cookie = ichancy_cookie if ichancy_cookie is not None else settings_dict['ichancy_cookie']
    new_agent_balance = agent_balance if agent_balance is not None else settings_dict.get('agent_balance', 0)
    new_referrals_enabled = referrals_enabled if referrals_enabled is not None else settings_dict.get('referrals_enabled', True)
    new_game_min_deposit_syp = game_min_deposit_syp if game_min_deposit_syp is not None else settings_dict.get('game_min_deposit_syp', 20000)
    new_agent_revenue_percent = agent_revenue_percent if agent_revenue_percent is not None else settings_dict.get('agent_revenue_percent', 30)
    new_min_deposit_syp = min_deposit_syp if min_deposit_syp is not None else settings_dict.get('min_deposit_syp', 20000)
    new_min_deposit_usd = min_deposit_usd if min_deposit_usd is not None else settings_dict.get('min_deposit_usd', 5)
    new_min_withdraw_syp = min_withdraw_syp if min_withdraw_syp is not None else settings_dict.get('min_withdraw_syp', 25000)
    new_min_withdraw_usd = min_withdraw_usd if min_withdraw_usd is not None else settings_dict.get('min_withdraw_usd', 10)
    new_syp_version = syp_version if syp_version is not None else settings_dict.get('syp_version', 'old')
    new_rollover = bonus_rollover_multiplier if bonus_rollover_multiplier is not None else settings_dict.get('bonus_rollover_multiplier', 5.0)
    new_field = turnover_field_name if turnover_field_name is not None else settings_dict.get('turnover_field_name', 'totalBet')
    new_game_bonus_enabled = game_bonus_enabled if game_bonus_enabled is not None else settings_dict.get('game_bonus_enabled', True)
    new_game_bonus_apply_percent = game_bonus_apply_percent if game_bonus_apply_percent is not None else settings_dict.get('game_bonus_apply_percent', 10)
    new_syriatel_auto_mode = syriatel_auto_mode if syriatel_auto_mode is not None else settings_dict.get('syriatel_auto_mode', 'off')
    new_syriatel_auto_channel_id = syriatel_auto_channel_id if syriatel_auto_channel_id is not None else settings_dict.get('syriatel_auto_channel_id', '')
    new_alert_thresh = agent_balance_alert_threshold if agent_balance_alert_threshold is not None else settings_dict.get('agent_balance_alert_threshold', 100000)

    query = """
    UPDATE bot_settings
    SET exchange_rate = %s, usd_buy_rate = %s, usd_sell_rate = %s, withdraw_commission = %s, ichancy_cookie = %s, agent_balance = %s, referrals_enabled = %s, game_min_deposit_syp = %s, agent_revenue_percent = %s, min_deposit_syp = %s, min_deposit_usd = %s, min_withdraw_syp = %s, min_withdraw_usd = %s, syp_version = %s, bonus_rollover_multiplier = %s, turnover_field_name = %s, game_bonus_enabled = %s, game_bonus_apply_percent = %s, syriatel_auto_mode = %s, syriatel_auto_channel_id = %s, agent_balance_alert_threshold = %s
    WHERE id = 1
    """
    DatabaseManager.execute_query(
        query,
        (
            int(new_exchange_rate),
            float(new_usd_buy_rate),
            float(new_usd_sell_rate),
            float(new_withdraw_commission),
            new_cookie,
            int(new_agent_balance),
            bool(new_referrals_enabled),
            int(new_game_min_deposit_syp),
            float(new_agent_revenue_percent),
            int(new_min_deposit_syp),
            int(new_min_deposit_usd),
            int(new_min_withdraw_syp),
            int(new_min_withdraw_usd),
            str(new_syp_version),
            float(new_rollover),
            str(new_field),
            bool(new_game_bonus_enabled),
            float(new_game_bonus_apply_percent),
            str(new_syriatel_auto_mode or 'off'),
            str(new_syriatel_auto_channel_id or ''),
            int(new_alert_thresh)
        )
    )
    if hasattr(DatabaseManager, 'invalidate_settings_cache'):
        DatabaseManager.invalidate_settings_cache()


def get_service_gates():
    settings = get_bot_settings() or {}
    return {
        'maintenance_mode': bool(settings.get('maintenance_mode', False)),
        'deposits_enabled': True if settings.get('deposits_enabled') is None else bool(settings.get('deposits_enabled')),
        'withdrawals_enabled': True if settings.get('withdrawals_enabled') is None else bool(settings.get('withdrawals_enabled')),
        'game_transfers_enabled': True if settings.get('game_transfers_enabled') is None else bool(settings.get('game_transfers_enabled')),
    }


def update_service_gates(maintenance_mode=None, deposits_enabled=None, withdrawals_enabled=None, game_transfers_enabled=None):
    current = get_service_gates()
    values = {
        'maintenance_mode': current['maintenance_mode'] if maintenance_mode is None else bool(maintenance_mode),
        'deposits_enabled': current['deposits_enabled'] if deposits_enabled is None else bool(deposits_enabled),
        'withdrawals_enabled': current['withdrawals_enabled'] if withdrawals_enabled is None else bool(withdrawals_enabled),
        'game_transfers_enabled': current['game_transfers_enabled'] if game_transfers_enabled is None else bool(game_transfers_enabled),
    }
    DatabaseManager.execute_query(
        """
        UPDATE bot_settings SET maintenance_mode=%s, deposits_enabled=%s,
            withdrawals_enabled=%s, game_transfers_enabled=%s WHERE id=1
        """,
        (
            values['maintenance_mode'], values['deposits_enabled'],
            values['withdrawals_enabled'], values['game_transfers_enabled'],
        )
    )
    if hasattr(DatabaseManager, 'invalidate_settings_cache'):
        DatabaseManager.invalidate_settings_cache()
    return values


def service_gate_status(service):
    gates = get_service_gates()
    if gates['maintenance_mode']:
        return False, 'البوت في وضع الصيانة حالياً. يرجى المحاولة لاحقاً.'
    key = {
        'deposit': 'deposits_enabled',
        'withdraw': 'withdrawals_enabled',
        'game': 'game_transfers_enabled',
    }.get(service)
    if key and not gates[key]:
        messages = {
            'deposit': 'خدمة الإيداع متوقفة مؤقتاً من الإدارة.',
            'withdraw': 'خدمة السحب متوقفة مؤقتاً من الإدارة.',
            'game': 'تحويلات حساب اللعبة متوقفة مؤقتاً من الإدارة.',
        }
        return False, messages[service]
    return True, None


# ==================== دوال إضافية للوحة الأدمن ====================

def get_user_details(telegram_id):
    user = get_user(telegram_id)
    if not user:
        return None

    stats = get_transaction_stats_for_user(telegram_id)
    ref_count_result = DatabaseManager.execute_query(
        "SELECT COUNT(*) FROM referrals WHERE referrer_telegram_id = %s AND is_active = TRUE",
        (str(telegram_id),),
        fetch='one'
    )

    return {
        **user,
        'tx_count': stats['total'],
        'pending_count': stats['pending'],
        'approved_count': stats['approved'],
        'rejected_count': stats['rejected'],
        'ref_count': ref_count_result[0] if ref_count_result else 0,
    }


# ==================== 🤝 عمولات الإحالات القابلة للسحب (Revenue Share) ====================

def get_affiliate_percent_by_active_count(active_count):
    """شرائح أرباح الإحالات من خسارة المحالين الأسبوعية — قابلة للسحب."""
    active_count = int(active_count or 0)
    if active_count >= 10:
        return 2.0
    if active_count >= 5:
        return 1.5
    if active_count >= 3:
        return 1.0
    return 0.0


def has_affiliate_commission_for_week(referrer_id, referred_id, week_start):
    result = DatabaseManager.execute_query(
        """SELECT id FROM affiliate_weekly_commissions
           WHERE referrer_telegram_id = %s AND referred_telegram_id = %s AND week_start = %s""",
        (str(referrer_id), str(referred_id), week_start), fetch='one'
    )
    return result is not None


def get_affiliate_commissions_summary(limit=20):
    rows = DatabaseManager.execute_query_dict(
        """SELECT awc.*, ru.telegram_username AS referrer_username, uu.telegram_username AS referred_username
           FROM affiliate_weekly_commissions awc
           LEFT JOIN users ru ON ru.telegram_id = awc.referrer_telegram_id
           LEFT JOIN users uu ON uu.telegram_id = awc.referred_telegram_id
           ORDER BY awc.created_at DESC
           LIMIT %s""",
        (int(limit),), fetch='all'
    ) or []
    total = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(commission_amount),0) FROM affiliate_weekly_commissions",
        fetch='one'
    )
    week_start = get_syria_now().date() - timedelta(days=get_syria_now().weekday())
    pending_refs = DatabaseManager.execute_query(
        """SELECT COUNT(*) FROM referrals r
           WHERE r.is_active = TRUE
           AND NOT EXISTS (
             SELECT 1 FROM affiliate_weekly_commissions awc
             WHERE awc.referrer_telegram_id = r.referrer_telegram_id
             AND awc.referred_telegram_id = r.referred_telegram_id
             AND awc.week_start = %s
           )""",
        (week_start,), fetch='one'
    )
    return {
        'total_paid': int(total[0]) if total else 0,
        'pending_active_referrals': int(pending_refs[0]) if pending_refs else 0,
        'recent': rows,
    }


def credit_affiliate_weekly_commission(referrer_id, referred_id, activity, percent):
    """إضافة عمولة إحالة أسبوعية قابلة للسحب إلى affiliate_balance."""
    referrer_id = str(referrer_id)
    referred_id = str(referred_id)
    net_loss = int(activity.get('net_loss') or 0)
    pct = float(percent or 0)
    if net_loss <= 0 or pct <= 0:
        return {'ok': False, 'reason': 'no_loss_or_percent', 'net_loss': net_loss, 'percent': pct}
    commission = int(net_loss * pct / 100.0)
    if commission <= 0:
        return {'ok': False, 'reason': 'zero_commission', 'net_loss': net_loss, 'percent': pct}
    now = get_syria_now()
    week_start = now.date() - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6)
    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT affiliate_balance FROM users WHERE telegram_id = %s FOR UPDATE", (referrer_id,))
        if not cursor.fetchone():
            conn.rollback()
            return {'ok': False, 'reason': 'referrer_not_found'}
        cursor.execute(
            """INSERT INTO affiliate_weekly_commissions (
                   referrer_telegram_id, referred_telegram_id, week_start, week_end,
                   total_deposited, total_withdrawn, ending_game_balance, net_loss,
                   commission_percent, commission_amount, status
               ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'paid')
               ON CONFLICT (referrer_telegram_id, referred_telegram_id, week_start) DO NOTHING
               RETURNING id""",
            (
                referrer_id, referred_id, week_start, week_end,
                int(activity.get('deposited') or 0), int(activity.get('withdrawn') or 0),
                int(activity.get('game_balance') or 0), net_loss, pct, commission
            )
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {'ok': False, 'reason': 'already_paid'}
        cursor.execute(
            "UPDATE users SET affiliate_balance = COALESCE(affiliate_balance,0) + %s WHERE telegram_id = %s RETURNING affiliate_balance",
            (commission, referrer_id)
        )
        new_balance = int(cursor.fetchone()[0] or 0)
        conn.commit()
        return {'ok': True, 'commission': commission, 'net_loss': net_loss, 'percent': pct, 'new_affiliate_balance': new_balance}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"credit_affiliate_weekly_commission error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


async def process_weekly_affiliate_commissions(bot=None):
    """صرف عمولات الإحالات الأسبوعية على أساس خسارة المحالين في اللعبة."""
    rows = DatabaseManager.execute_query_dict(
        """SELECT r.referrer_telegram_id, r.referred_telegram_id, u.player_id
           FROM referrals r
           JOIN users u ON u.telegram_id = r.referred_telegram_id
           WHERE r.is_active = TRUE""",
        fetch='all'
    ) or []
    from ichancy_api.client import ichancy_api_client
    checked = 0
    paid_count = 0
    total_loss = 0
    total_paid = 0
    results = []
    for row in rows:
        referrer_id = str(row.get('referrer_telegram_id'))
        referred_id = str(row.get('referred_telegram_id'))
        active_count = get_active_referrals_count(referrer_id)
        pct = get_affiliate_percent_by_active_count(active_count)
        if pct <= 0:
            continue
        live_balance = None
        try:
            pid = row.get('player_id')
            if pid:
                raw_bal = await ichancy_api_client.get_player_balance(pid)
                if raw_bal is not None:
                    live_balance = int(raw_bal)
                else:
                    live_balance = get_user_game_balance(referred_id)
        except Exception as e:
            logger.warning(f"affiliate: failed live balance for {referred_id}: {e}")
            live_balance = get_user_game_balance(referred_id)
        activity = get_user_weekly_game_activity(referred_id, current_game_balance=live_balance)
        checked += 1
        if int(activity.get('net_loss') or 0) <= 0:
            continue
        res = credit_affiliate_weekly_commission(referrer_id, referred_id, activity, pct)
        if res.get('ok'):
            paid_count += 1
            total_loss += int(res.get('net_loss') or 0)
            total_paid += int(res.get('commission') or 0)
            results.append({'referrer_id': referrer_id, 'referred_id': referred_id, **res})
            if bot:
                try:
                    await bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            "🤝 <b>أرباح إحالة أسبوعية!</b>\n\n"
                            f"📊 خسارة أحد المحالين لديك: <code>{int(res.get('net_loss')):,} ل.س</code>\n"
                            f"📈 نسبتك: <code>{float(res.get('percent')):g}%</code>\n"
                            f"💵 أرباحك القابلة للسحب: <code>{int(res.get('commission')):,} ل.س</code>\n\n"
                            "تمت إضافتها إلى رصيد أرباح الإحالات."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    return {'ok': True, 'checked': checked, 'paid_count': paid_count, 'total_loss': total_loss, 'total_paid': total_paid, 'items': results[:50]}


def transfer_affiliate_balance_to_bot(telegram_id):
    """تحويل رصيد أرباح الإحالات القابل للسحب إلى رصيد البوت النقدي."""
    conn = None
    cursor = None
    tid = str(telegram_id)
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT affiliate_balance FROM users WHERE telegram_id = %s FOR UPDATE", (tid,))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {'ok': False, 'reason': 'user_not_found'}
        amount = int(row[0] or 0)
        if amount <= 0:
            conn.rollback()
            return {'ok': False, 'reason': 'empty'}
        cursor.execute(
            "UPDATE users SET affiliate_balance = 0, bot_balance = COALESCE(bot_balance,0) + %s WHERE telegram_id = %s RETURNING bot_balance",
            (amount, tid)
        )
        new_bot_balance = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            """INSERT INTO transactions (user_telegram_id, type, payment_method, amount, transfer_number, status)
               VALUES (%s, 'affiliate_to_bot', 'affiliate', %s, 'Affiliate earnings transferred to bot balance', 'completed')""",
            (tid, amount)
        )
        conn.commit()
        return {'ok': True, 'amount': amount, 'new_bot_balance': new_bot_balance}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"transfer_affiliate_balance_to_bot error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


# ==================== 🎁 البونصات والعروض ====================

def create_bonus_rule(title, percent, payment_method='all', min_amount_syp=0, max_bonus_syp=0, created_by=None):
    query = """
    INSERT INTO bonus_rules (title, percent, payment_method, min_amount_syp, max_bonus_syp, is_active, created_by)
    VALUES (%s, %s, %s, %s, %s, TRUE, %s)
    RETURNING id
    """
    result = DatabaseManager.execute_query(
        query,
        (title, float(percent), payment_method or 'all', float(min_amount_syp or 0), float(max_bonus_syp or 0), str(created_by) if created_by else None),
        fetch='one'
    )
    return result[0] if result else None


def get_active_bonus_rules():
    query = """
    SELECT * FROM bonus_rules
    WHERE is_active = TRUE
    ORDER BY created_at DESC
    """
    return DatabaseManager.execute_query_dict(query, fetch='all') or []


def get_all_bonus_rules(limit=20):
    query = """
    SELECT * FROM bonus_rules
    ORDER BY created_at DESC
    LIMIT %s
    """
    return DatabaseManager.execute_query_dict(query, (limit,), fetch='all') or []


def disable_bonus_rule(rule_id):
    query = """
    UPDATE bonus_rules
    SET is_active = FALSE, disabled_at = CURRENT_TIMESTAMP
    WHERE id = %s AND is_active = TRUE
    """
    DatabaseManager.execute_query(query, (rule_id,))
    return True


def enable_bonus_rule(rule_id):
    """إعادة تفعيل عرض بونص موقوف."""
    query = """
    UPDATE bonus_rules
    SET is_active = TRUE, disabled_at = NULL
    WHERE id = %s AND is_active = FALSE
    """
    DatabaseManager.execute_query(query, (rule_id,))
    return True


def delete_bonus_rule(rule_id):
    """حذف عرض بونص نهائياً."""
    DatabaseManager.execute_query("DELETE FROM bonus_rules WHERE id = %s", (rule_id,))
    return True


def get_bonus_rule(rule_id):
    """جلب عرض بونص واحد."""
    return DatabaseManager.execute_query_dict(
        "SELECT * FROM bonus_rules WHERE id = %s",
        (rule_id,), fetch='one'
    )


def update_bonus_rule(rule_id, title=None, percent=None, payment_method=None, min_amount_syp=None, max_bonus_syp=None):
    """تحديث عرض بونص موجود."""
    rule = get_bonus_rule(rule_id)
    if not rule:
        return False
    new_title = title if title is not None else rule.get('title')
    new_percent = percent if percent is not None else rule.get('percent')
    new_payment_method = payment_method if payment_method is not None else rule.get('payment_method')
    new_min = min_amount_syp if min_amount_syp is not None else rule.get('min_amount_syp')
    new_max = max_bonus_syp if max_bonus_syp is not None else rule.get('max_bonus_syp')
    query = """
    UPDATE bonus_rules
    SET title = %s, percent = %s, payment_method = %s, min_amount_syp = %s, max_bonus_syp = %s
    WHERE id = %s
    """
    DatabaseManager.execute_query(
        query,
        (new_title, float(new_percent), new_payment_method or 'all', float(new_min or 0), float(new_max or 0), rule_id)
    )
    return True


def calculate_best_deposit_bonus(amount_syp, payment_method):
    """حساب أفضل بونص ينطبق على الإيداع.

    البونصات تطبق فقط حسب طريقة الإيداع والحد الأدنى، وإذا انطبق أكثر من عرض
    يحصل المستخدم على أعلى قيمة بونص فقط.

    🔒 تطبيع المفاتيح (Update 8): تتقبل الصيغتين القديمة والجديدة تلقائياً
    حتى البونصات المحفوظة بمفاتيح قديمة (مثل sham_cash_syp) تعمل مع الإيداع
    الحديث (sham_syp) دون الحاجة لحذفها.
    """
    # قاموس التطبيع: يحوّل أي صيغة مفتاح للصيغة الموحدة القصيرة
    METHOD_NORMALIZE = {
        'syriatel_cash': 'syriatel',
        'mtn_cash': 'mtn',
        'sham_cash_syp': 'sham_syp',
        'sham_cash_usd': 'sham_usd',
        'usdt_trc20': 'usdt_trc',
        'usdt_bep20': 'usdt_bep',
    }

    def normalize(m):
        """تحويل أي مفتاح للصيغة الموحدة القصيرة."""
        if not m:
            return 'all'
        return METHOD_NORMALIZE.get(m, m)

    amount = float(amount_syp or 0)
    method = normalize(payment_method)
    best = {
        'bonus_amount': 0,
        'rule': None,
        'total_amount': int(amount),
    }

    for rule in get_active_bonus_rules():
        rule_method = normalize(rule.get('payment_method'))
        if rule_method != 'all' and rule_method != method:
            continue

        min_amount = float(rule.get('min_amount_syp') or 0)
        if amount < min_amount:
            continue

        percent = float(rule.get('percent') or 0)
        if percent <= 0:
            continue

        bonus = amount * (percent / 100.0)
        max_bonus = float(rule.get('max_bonus_syp') or 0)
        if max_bonus > 0 and bonus > max_bonus:
            bonus = max_bonus

        bonus_int = int(bonus)
        if bonus_int > best['bonus_amount']:
            best = {
                'bonus_amount': bonus_int,
                'rule': rule,
                'total_amount': int(amount) + bonus_int,
            }

    # ⚡ Flash Bonus: يعامل كعرض بونص عادي مؤقت، وينافس العروض العادية.
    # لا يتراكم مع البونص العادي؛ المستخدم يأخذ أعلى بونص منطبق فقط.
    try:
        flash = get_active_flash_bonus()
        if flash:
            flash_method = normalize(flash.get('payment_method'))
            if flash_method == 'all' or flash_method == method:
                flash_percent = float(flash.get('percent') or 0)
                if flash_percent > 0:
                    flash_bonus_int = int(amount * (flash_percent / 100.0))
                    if flash_bonus_int > best['bonus_amount']:
                        best = {
                            'bonus_amount': flash_bonus_int,
                            'rule': {
                                'id': flash.get('id'),
                                'title': f"Flash Bonus {flash_percent:g}%",
                                'percent': flash_percent,
                                'payment_method': flash.get('payment_method') or 'all',
                                'min_amount_syp': 0,
                                'max_bonus_syp': 0,
                                'is_flash': True,
                            },
                            'total_amount': int(amount) + flash_bonus_int,
                        }
    except Exception as e:
        logger.error(f"calculate_best_deposit_bonus flash error: {e}")

    return best


# ==================== الرفض المخصص المؤقت (يعبر مشكلة FSM عبر القنوات) ====================

def set_pending_rejection(admin_id, tx_id, tx_type, channel_chat_id=None, channel_message_id=None):
    """حفظ طلب رفض مخصص مؤقتاً مع مرجع رسالة القناة لتحديثها لاحقاً."""
    query = """
    INSERT INTO pending_rejections (admin_id, tx_id, tx_type, channel_chat_id, channel_message_id)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (admin_id) DO UPDATE SET
        tx_id = EXCLUDED.tx_id,
        tx_type = EXCLUDED.tx_type,
        channel_chat_id = EXCLUDED.channel_chat_id,
        channel_message_id = EXCLUDED.channel_message_id,
        created_at = CURRENT_TIMESTAMP
    """
    DatabaseManager.execute_query(query, (str(admin_id), tx_id, tx_type, channel_chat_id, channel_message_id))


def get_pending_rejection(admin_id):
    """جلب الرفض المخصص المعلّق (صالح فقط خلال 5 دقائق لمنع التراكم)."""
    query = """
    SELECT * FROM pending_rejections
    WHERE admin_id = %s AND created_at > NOW() - INTERVAL '5 minutes'
    """
    return DatabaseManager.execute_query_dict(query, (str(admin_id),), fetch='one')


def clear_pending_rejection(admin_id):
    """حذف الرفض المخصص المعلّق بعد المعالجة."""
    DatabaseManager.execute_query("DELETE FROM pending_rejections WHERE admin_id = %s", (str(admin_id),))


# ==================== 🆕 إحصائيات وإدارة المستخدمين (لوحة الأدمن) ====================

def get_total_users_count():
    """إجمالي عدد المستخدمين المسجّلين."""
    result = DatabaseManager.execute_query("SELECT COUNT(*) FROM users", fetch='one')
    return int(result[0]) if result else 0


def get_new_users_today():
    """عدد المستخدمين الجدد اليوم."""
    result = DatabaseManager.execute_query(
        "SELECT COUNT(*) FROM users WHERE created_at::date = CURRENT_DATE", fetch='one'
    )
    return int(result[0]) if result else 0


def get_today_transactions_count():
    """عدد المعاملات المنفّذة اليوم."""
    result = DatabaseManager.execute_query(
        "SELECT COUNT(*) FROM transactions WHERE created_at::date = CURRENT_DATE", fetch='one'
    )
    return int(result[0]) if result else 0


def get_transactions_volume(status='approved'):
    """إجمالي قيمة المعاملات بحالة معيّنة."""
    result = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE status = %s",
        (status,), fetch='one'
    )
    return int(float(result[0])) if result else 0


def update_cookie_timestamp():
    """تسجيل وقت آخر تحديث للكوكيز."""
    DatabaseManager.execute_query(
        "UPDATE bot_settings SET last_cookie_update = CURRENT_TIMESTAMP WHERE id = 1"
    )


def get_cookie_age_minutes():
    """عمر آخر تحديث للكوكيز بالدقائق (None إذا لم يُحدّث أبداً)."""
    result = DatabaseManager.execute_query_dict(
        "SELECT EXTRACT(EPOCH FROM (NOW() - last_cookie_update))/60 AS age_minutes "
        "FROM bot_settings WHERE id = 1 AND last_cookie_update IS NOT NULL",
        fetch='one'
    )
    return int(result['age_minutes']) if result and result['age_minutes'] is not None else None


def search_user(query):
    """البحث عن مستخدم بالـ Telegram ID أو username أو iChancy username أو Player ID."""
    q = str(query).strip().lstrip('@')
    try:
        int(q)
        sql = """
        SELECT telegram_id, telegram_username, ichancy_username, player_id,
               bot_balance, game_balance, terms_accepted, created_at
        FROM users
        WHERE telegram_id = %s
           OR player_id = %s
           OR telegram_username ILIKE %s
           OR ichancy_username ILIKE %s
        ORDER BY created_at DESC LIMIT 10
        """
        like = f"%{q}%"
        return DatabaseManager.execute_query_dict(sql, (q, q, like, like), fetch='all')
    except ValueError:
        sql = """
        SELECT telegram_id, telegram_username, ichancy_username, player_id,
               bot_balance, game_balance, terms_accepted, created_at
        FROM users
        WHERE telegram_username ILIKE %s
           OR ichancy_username ILIKE %s
           OR player_id ILIKE %s
        ORDER BY created_at DESC LIMIT 10
        """
        like = f"%{q}%"
        return DatabaseManager.execute_query_dict(sql, (like, like, like), fetch='all')


def set_user_balance(telegram_id, new_balance):
    """تعيين رصيد مستخدم محدّد (للأدمن)."""
    if new_balance < 0:
        return False
    DatabaseManager.execute_query(
        "UPDATE users SET bot_balance = %s WHERE telegram_id = %s",
        (int(new_balance), str(telegram_id))
    )
    return True


# ==================== إعدادات عناوين الإيداع ====================

PAYMENT_ADDRESS_FALLBACKS = {
    'syriatel': lambda: __import__('config.settings', fromlist=['settings']).SYRIATEL_CASH_NUMBERS,
    'mtn': lambda: __import__('config.settings', fromlist=['settings']).MTN_CASH_NUMBER,
    'sham_syp': lambda: __import__('config.settings', fromlist=['settings']).SHAM_CASH_SYP_ADDRESS,
    'sham_usd': lambda: __import__('config.settings', fromlist=['settings']).SHAM_CASH_USD_ADDRESS,
    'usdt_trc': lambda: __import__('config.settings', fromlist=['settings']).USDT_TRC20_ADDRESS,
    'usdt_bep': lambda: __import__('config.settings', fromlist=['settings']).USDT_BEP20_ADDRESS,
}



BUTTON_LINK_LABELS = {
    'website_url': '🌐 فتح الموقع',
    'app_download_url': '📱 تحميل التطبيق',
    'betting_url': '📘 Facebook البوت',
    'games_url': '🎮 ألعاب iChancy',
    'robert_vip_url': '🌟 Robert VIP — المنصة',
    'robert_vip_register_url': '🆕 Robert VIP — التسجيل',
    'robert_vip_login_url': '🔐 Robert VIP — الدخول',
    'robert_vip_bet_url': '⚽ Robert VIP — راهن مع روبيرت',
    'robert_vip_predictions_url': '🎫 Robert VIP — باقات التوقعات',
}


def get_button_link_fallback(key):
    from config import settings
    mapping = {
        'website_url': getattr(settings, 'WEBSITE_URL', ''),
        'app_download_url': getattr(settings, 'APP_DOWNLOAD_URL', ''),
        'betting_url': getattr(settings, 'BETTING_URL', ''),
        'games_url': getattr(settings, 'GAMES_URL', ''),
        'robert_vip_url': getattr(settings, 'ROBERT_VIP_URL', 'https://robert.vip/dashboard/games'),
        'robert_vip_register_url': getattr(settings, 'ROBERT_VIP_REGISTER_URL', 'https://robert.vip/register'),
        'robert_vip_login_url': getattr(settings, 'ROBERT_VIP_LOGIN_URL', 'https://robert.vip/login?redirect=%2Fdashboard%2Fgames'),
        'robert_vip_bet_url': getattr(settings, 'ROBERT_VIP_BET_URL', 'https://robert.vip/dashboard/bet-with-robert'),
        'robert_vip_predictions_url': getattr(settings, 'ROBERT_VIP_PREDICTIONS_URL', 'https://robert.vip/dashboard/prediction-packages'),
    }
    return mapping.get(key, '')


def get_button_link(key):
    row = DatabaseManager.execute_query_dict(
        "SELECT address FROM payment_settings WHERE payment_method = %s",
        (key,),
        fetch='one'
    )
    if row and row.get('address'):
        return row['address']
    return get_button_link_fallback(key)


def get_button_link_source(key):
    row = DatabaseManager.execute_query_dict(
        "SELECT address FROM payment_settings WHERE payment_method = %s",
        (key,),
        fetch='one'
    )
    if row and row.get('address'):
        return 'database'
    return 'render'


def get_all_button_links():
    return [
        {
            'key': key,
            'label': BUTTON_LINK_LABELS.get(key, key),
            'url': get_button_link(key),
            'source': get_button_link_source(key),
            'fallback': get_button_link_fallback(key),
        }
        for key in BUTTON_LINK_LABELS.keys()
    ]


def set_button_link(key, url, updated_by=None):
    if key not in BUTTON_LINK_LABELS:
        return False
    DatabaseManager.execute_query(
        """
        INSERT INTO payment_settings (payment_method, address, updated_by, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (payment_method)
        DO UPDATE SET address = EXCLUDED.address,
                      updated_by = EXCLUDED.updated_by,
                      updated_at = CURRENT_TIMESTAMP
        """,
        (key, url.strip(), str(updated_by) if updated_by else None)
    )
    return True


def reset_button_link(key):
    if key not in BUTTON_LINK_LABELS:
        return False
    DatabaseManager.execute_query(
        "DELETE FROM payment_settings WHERE payment_method = %s",
        (key,)
    )
    return True
PAYMENT_METHOD_LABELS = {
    'syriatel': '🟢 سيريتل كاش',
    'mtn': '🟡 MTN كاش',
    'sham_syp': '📱 شام كاش SYP',
    'sham_usd': '💵 شام كاش USD',
    'usdt_trc': '🪙 USDT TRC20',
    'usdt_bep': '🪙 USDT BEP20',
}


def get_payment_address_fallback(payment_method):
    getter = PAYMENT_ADDRESS_FALLBACKS.get(payment_method)
    if not getter:
        return ''
    try:
        return getter() or ''
    except Exception:
        return ''


CASHIER_METHOD_COLUMNS = {
    'syriatel': 'syriatel_address',
    'mtn': 'mtn_address',
    'sham_syp': 'sham_syp_address',
    'sham_usd': 'sham_usd_address',
}


def _cashier_profile_to_dict(row):
    if not row:
        return None
    data = dict(row)
    for key in ('id',):
        if data.get(key) is not None:
            data[key] = int(data[key])
    data['is_enabled'] = bool(data.get('is_enabled'))
    return data


def list_cashier_profiles(include_disabled=True):
    where = '' if include_disabled else 'WHERE is_enabled = TRUE'
    rows = DatabaseManager.execute_query_dict(
        f"SELECT * FROM cashier_profiles {where} ORDER BY is_enabled DESC, name ASC, id ASC",
        fetch='all'
    ) or []
    return [_cashier_profile_to_dict(row) for row in rows]


def get_cashier_profile(profile_id):
    if profile_id in (None, ''):
        return None
    row = DatabaseManager.execute_query_dict(
        "SELECT * FROM cashier_profiles WHERE id = %s",
        (int(profile_id),), fetch='one'
    )
    return _cashier_profile_to_dict(row)


def create_cashier_profile(name, telegram_id, sham_syp_address, sham_usd_address, syriatel_address, mtn_address, created_by=None):
    values = [str(v or '').strip() for v in (name, sham_syp_address, sham_usd_address, syriatel_address, mtn_address)]
    if any(not value for value in values):
        return None
    result = DatabaseManager.execute_query(
        """
        INSERT INTO cashier_profiles (
            name, telegram_id, sham_syp_address, sham_usd_address,
            syriatel_address, mtn_address, created_by, updated_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            values[0], str(telegram_id or '').strip() or None,
            values[1], values[2], values[3], values[4],
            str(created_by or '').strip() or None,
            str(created_by or '').strip() or None,
        ), fetch='one'
    )
    return int(result[0]) if result else None


def update_cashier_profile(profile_id, name, telegram_id, sham_syp_address, sham_usd_address, syriatel_address, mtn_address, updated_by=None, is_enabled=True):
    values = [str(v or '').strip() for v in (name, sham_syp_address, sham_usd_address, syriatel_address, mtn_address)]
    if any(not value for value in values):
        return False
    DatabaseManager.execute_query(
        """
        UPDATE cashier_profiles
        SET name=%s, telegram_id=%s, sham_syp_address=%s, sham_usd_address=%s,
            syriatel_address=%s, mtn_address=%s, is_enabled=%s,
            updated_by=%s, updated_at=CURRENT_TIMESTAMP
        WHERE id=%s
        """,
        (
            values[0], str(telegram_id or '').strip() or None,
            values[1], values[2], values[3], values[4], bool(is_enabled),
            str(updated_by or '').strip() or None, int(profile_id),
        )
    )
    return True


def delete_cashier_profile(profile_id):
    settings = get_bot_settings() or {}
    if int(settings.get('active_cashier_profile_id') or 0) == int(profile_id):
        return {'ok': False, 'reason': 'active_profile'}
    DatabaseManager.execute_query("DELETE FROM cashier_profiles WHERE id = %s", (int(profile_id),))
    return {'ok': True}


def get_active_cashier_profile():
    settings = get_bot_settings() or {}
    profile_id = settings.get('active_cashier_profile_id')
    profile = get_cashier_profile(profile_id) if profile_id else None
    return profile if profile and profile.get('is_enabled') else None


def activate_cashier_profile(profile_id, switched_by):
    """Atomically switch the active cashier and write an audit row."""
    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, name, sham_syp_address, sham_usd_address, syriatel_address, mtn_address
            FROM cashier_profiles WHERE id = %s AND is_enabled = TRUE FOR UPDATE
            """,
            (int(profile_id),)
        )
        profile = cursor.fetchone()
        if not profile or any(not str(v or '').strip() for v in profile[2:]):
            conn.rollback()
            return {'ok': False, 'reason': 'invalid_profile'}
        cursor.execute("SELECT active_cashier_profile_id FROM bot_settings WHERE id = 1 FOR UPDATE")
        previous_row = cursor.fetchone()
        previous_id = int(previous_row[0]) if previous_row and previous_row[0] is not None else None
        cursor.execute(
            "UPDATE bot_settings SET active_cashier_profile_id = %s WHERE id = 1",
            (int(profile_id),)
        )
        cursor.execute(
            """
            INSERT INTO cashier_switch_audit (previous_profile_id, new_profile_id, switched_by)
            VALUES (%s, %s, %s)
            """,
            (previous_id, int(profile_id), str(switched_by))
        )
        conn.commit()
        if hasattr(DatabaseManager, 'invalidate_settings_cache'):
            DatabaseManager.invalidate_settings_cache()
        return {'ok': True, 'previous_profile_id': previous_id, 'profile_id': int(profile_id), 'profile_name': profile[1]}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"activate_cashier_profile error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def get_cashier_switch_audit(limit=20):
    return DatabaseManager.execute_query_dict(
        """
        SELECT a.id, a.previous_profile_id, p1.name AS previous_profile_name,
               a.new_profile_id, p2.name AS new_profile_name,
               a.switched_by, a.switched_at
        FROM cashier_switch_audit a
        LEFT JOIN cashier_profiles p1 ON p1.id = a.previous_profile_id
        LEFT JOIN cashier_profiles p2 ON p2.id = a.new_profile_id
        ORDER BY a.switched_at DESC LIMIT %s
        """,
        (int(limit),), fetch='all'
    ) or []


def resolve_cashier_payment_route(payment_method, active_profile, legacy_address, legacy_source='database'):
    """Pure routing helper used by runtime and offline simulations."""
    method = str(payment_method or '').strip()
    column = CASHIER_METHOD_COLUMNS.get(method)
    if active_profile and active_profile.get('is_enabled') and column:
        address = str(active_profile.get(column) or '').strip()
        if address:
            return {
                'address': address,
                'source': 'cashier_profile',
                'cashier_profile_id': int(active_profile.get('id')),
                'cashier_profile_name': active_profile.get('name') or '',
            }
    return {
        'address': str(legacy_address or ''),
        'source': legacy_source,
        'cashier_profile_id': None,
        'cashier_profile_name': None,
    }


def _get_legacy_payment_route(payment_method):
    row = DatabaseManager.execute_query_dict(
        "SELECT address FROM payment_settings WHERE payment_method = %s",
        (payment_method,), fetch='one'
    )
    if row and row.get('address'):
        return row['address'], 'database'
    return get_payment_address_fallback(payment_method), 'render'


def get_payment_routing_context(payment_method):
    legacy_address, legacy_source = _get_legacy_payment_route(payment_method)
    return resolve_cashier_payment_route(
        payment_method,
        get_active_cashier_profile(),
        legacy_address,
        legacy_source,
    )


def get_payment_address(payment_method):
    """Return active cashier address for local cash methods, then legacy fallback."""
    return get_payment_routing_context(payment_method)['address']


def get_payment_address_source(payment_method):
    return get_payment_routing_context(payment_method)['source']


def get_all_payment_addresses():
    result = []
    active_profile = get_active_cashier_profile()
    for method in PAYMENT_METHOD_LABELS.keys():
        legacy_address, legacy_source = _get_legacy_payment_route(method)
        route = resolve_cashier_payment_route(method, active_profile, legacy_address, legacy_source)
        result.append({
            'method': method,
            'label': PAYMENT_METHOD_LABELS.get(method, method),
            'address': route['address'],
            'source': route['source'],
            'cashier_profile_id': route.get('cashier_profile_id'),
            'cashier_profile_name': route.get('cashier_profile_name'),
            'fallback': get_payment_address_fallback(method),
        })
    return result


def set_payment_address(payment_method, address, updated_by=None):
    if payment_method not in PAYMENT_METHOD_LABELS:
        return False
    DatabaseManager.execute_query(
        """
        INSERT INTO payment_settings (payment_method, address, updated_by, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (payment_method)
        DO UPDATE SET address = EXCLUDED.address,
                      updated_by = EXCLUDED.updated_by,
                      updated_at = CURRENT_TIMESTAMP
        """,
        (payment_method, address.strip(), str(updated_by) if updated_by else None)
    )
    return True


def reset_payment_address(payment_method):
    if payment_method not in PAYMENT_METHOD_LABELS:
        return False
    DatabaseManager.execute_query(
        "DELETE FROM payment_settings WHERE payment_method = %s",
        (payment_method,)
    )
    return True


# ==================== البث الجماعي والتنبيهات ====================

def get_broadcast_targets(audience='all', limit=10000):
    audience = audience or 'all'
    if audience == 'ichancy':
        sql = "SELECT telegram_id FROM users WHERE player_id IS NOT NULL ORDER BY created_at DESC LIMIT %s"
        params = (limit,)
    elif audience == 'balance':
        sql = "SELECT telegram_id FROM users WHERE COALESCE(bot_balance, 0) > 0 ORDER BY created_at DESC LIMIT %s"
        params = (limit,)
    elif audience == 'accepted_terms':
        sql = "SELECT telegram_id FROM users WHERE terms_accepted = TRUE ORDER BY created_at DESC LIMIT %s"
        params = (limit,)
    else:
        sql = "SELECT telegram_id FROM users ORDER BY created_at DESC LIMIT %s"
        params = (limit,)
    rows = DatabaseManager.execute_query_dict(sql, params, fetch='all') or []
    return [str(r['telegram_id']) for r in rows if r.get('telegram_id')]


def create_broadcast(title, message, audience='all', message_type='announcement', created_by=None, total_targets=0):
    query = """
    INSERT INTO broadcasts (title, message, audience, message_type, created_by, status, total_targets)
    VALUES (%s, %s, %s, %s, %s, 'created', %s)
    RETURNING id
    """
    result = DatabaseManager.execute_query(query, (title, message, audience, message_type, str(created_by) if created_by else None, int(total_targets)), fetch='one')
    return int(result[0]) if result else None


def update_broadcast_status(broadcast_id, status, sent_count=None, failed_count=None, last_error=None, started=False, finished=False):
    parts = ["status = %s"]
    params = [status]
    if sent_count is not None:
        parts.append("sent_count = %s")
        params.append(int(sent_count))
    if failed_count is not None:
        parts.append("failed_count = %s")
        params.append(int(failed_count))
    if last_error is not None:
        parts.append("last_error = %s")
        params.append(str(last_error)[:1000])
    if started:
        parts.append("started_at = CURRENT_TIMESTAMP")
    if finished:
        parts.append("finished_at = CURRENT_TIMESTAMP")
    params.append(int(broadcast_id))
    DatabaseManager.execute_query(f"UPDATE broadcasts SET {', '.join(parts)} WHERE id = %s", tuple(params))


def get_recent_broadcasts(limit=10):
    return DatabaseManager.execute_query_dict(
        """
        SELECT id, title, audience, message_type, status, total_targets, sent_count, failed_count,
               created_at, started_at, finished_at, last_error
        FROM broadcasts
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
        fetch='all'
    ) or []


def get_user_by_ichancy_identity(player_id=None, ichancy_username=None):
    """مطابقة مستخدم البوت مع سجل iChancy عبر player_id أو اسم اللاعب."""
    if player_id:
        row = DatabaseManager.execute_query_dict(
            """
            SELECT * FROM users
            WHERE player_id = %s
            LIMIT 1
            """,
            (str(player_id),),
            fetch='one'
        )
        if row:
            return row
    if ichancy_username:
        row = DatabaseManager.execute_query_dict(
            """
            SELECT * FROM users
            WHERE LOWER(ichancy_username) = LOWER(%s)
            LIMIT 1
            """,
            (str(ichancy_username).strip(),),
            fetch='one'
        )
        if row:
            return row
    return None


def get_all_player_ids():
    """جلب قائمة بجميع معرفات اللاعبين المرتبطين بالبوت."""
    query = "SELECT telegram_id, player_id FROM users WHERE player_id IS NOT NULL"
    return DatabaseManager.execute_query_dict(query, fetch='all') or []


def get_total_game_balances():
    """حساب إجمالي أرصدة اللاعبين المخزنة محلياً في اللعبة."""
    result = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(game_balance), 0) FROM users",
        fetch='one'
    )
    return int(result[0]) if result else 0


def get_total_bot_balances():
    """حساب إجمالي أرصدة جميع المستخدمين داخل البوت."""
    result = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(bot_balance), 0) FROM users",
        fetch='one'
    )
    return int(result[0]) if result else 0


# ==================== مركز الدعم / Tickets ====================

def get_or_create_support_ticket(user_telegram_id):
    tid = str(user_telegram_id)
    row = DatabaseManager.execute_query_dict(
        """
        SELECT * FROM support_tickets
        WHERE user_telegram_id = %s AND status = 'open'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (tid,),
        fetch='one'
    )
    if row:
        return row
    result = DatabaseManager.execute_query(
        """
        INSERT INTO support_tickets (user_telegram_id, status)
        VALUES (%s, 'open')
        RETURNING id
        """,
        (tid,),
        fetch='one'
    )
    ticket_id = int(result[0]) if result else None
    return get_support_ticket(ticket_id) if ticket_id else None


def get_support_ticket(ticket_id):
    return DatabaseManager.execute_query_dict(
        "SELECT * FROM support_tickets WHERE id = %s",
        (int(ticket_id),),
        fetch='one'
    )


def add_support_message(ticket_id, sender_type, sender_id=None, message_text=None, content_type=None, telegram_message_id=None):
    DatabaseManager.execute_query(
        """
        INSERT INTO support_messages (ticket_id, sender_type, sender_id, message_text, content_type, telegram_message_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (int(ticket_id), sender_type, str(sender_id) if sender_id else None, message_text, content_type, telegram_message_id)
    )
    DatabaseManager.execute_query(
        """
        UPDATE support_tickets
        SET last_message = %s,
            last_message_at = CURRENT_TIMESTAMP,
            status = CASE WHEN status = 'closed' AND %s = 'user' THEN 'open' ELSE status END
        WHERE id = %s
        """,
        ((message_text or content_type or '')[:1000], sender_type, int(ticket_id))
    )


def close_support_ticket(ticket_id):
    DatabaseManager.execute_query(
        "UPDATE support_tickets SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = %s",
        (int(ticket_id),)
    )


def close_open_support_ticket_for_user(user_telegram_id):
    DatabaseManager.execute_query(
        """
        UPDATE support_tickets
        SET status = 'closed', closed_at = CURRENT_TIMESTAMP
        WHERE user_telegram_id = %s AND status = 'open'
        """,
        (str(user_telegram_id),)
    )


def get_support_tickets(status=None, limit=50):
    if status and status != 'all':
        sql = """
        SELECT st.*, u.telegram_username, u.ichancy_username, u.player_id, u.bot_balance
        FROM support_tickets st
        LEFT JOIN users u ON u.telegram_id = st.user_telegram_id
        WHERE st.status = %s
        ORDER BY st.last_message_at DESC NULLS LAST, st.created_at DESC
        LIMIT %s
        """
        params = (status, int(limit))
    else:
        sql = """
        SELECT st.*, u.telegram_username, u.ichancy_username, u.player_id, u.bot_balance
        FROM support_tickets st
        LEFT JOIN users u ON u.telegram_id = st.user_telegram_id
        ORDER BY st.last_message_at DESC NULLS LAST, st.created_at DESC
        LIMIT %s
        """
        params = (int(limit),)
    return DatabaseManager.execute_query_dict(sql, params, fetch='all') or []


def get_support_messages(ticket_id, limit=100):
    return DatabaseManager.execute_query_dict(
        """
        SELECT * FROM support_messages
        WHERE ticket_id = %s
        ORDER BY created_at ASC
        LIMIT %s
        """,
        (int(ticket_id), int(limit)),
        fetch='all'
    ) or []


# ==================== بطاقات التوقع ====================

def create_prediction_card(title, team_a, team_b, options_json, max_predictions=0, reward_syp=0, closes_at=None, created_by=None, match_code=None):
    query = """
    INSERT INTO prediction_cards (
        title, match_code, team_a, team_b, options_json,
        max_predictions, reward_syp, status, closes_at, created_by
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s)
    RETURNING id
    """
    result = DatabaseManager.execute_query(
        query,
        (title, match_code, team_a, team_b, options_json, int(max_predictions or 0), int(reward_syp or 0), closes_at, str(created_by) if created_by else None),
        fetch='one'
    )
    return int(result[0]) if result else None


def get_prediction_card(card_id):
    return DatabaseManager.execute_query_dict("SELECT * FROM prediction_cards WHERE id = %s", (int(card_id),), fetch='one')


def get_prediction_cards(status='all', limit=50):
    if status == 'all':
        query = "SELECT * FROM prediction_cards ORDER BY id DESC LIMIT %s"
        return DatabaseManager.execute_query_dict(query, (int(limit),), fetch='all') or []
    query = "SELECT * FROM prediction_cards WHERE status = %s ORDER BY id DESC LIMIT %s"
    return DatabaseManager.execute_query_dict(query, (status, int(limit)), fetch='all') or []


def close_prediction_card(card_id):
    DatabaseManager.execute_query(
        "UPDATE prediction_cards SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = %s AND status = 'open'",
        (int(card_id),)
    )


def add_prediction_entry(card_id, user_telegram_id, selected_option):
    query = """
    INSERT INTO prediction_entries (card_id, user_telegram_id, selected_option)
    VALUES (%s, %s, %s)
    ON CONFLICT (card_id, user_telegram_id) DO NOTHING
    RETURNING id
    """
    result = DatabaseManager.execute_query(query, (int(card_id), str(user_telegram_id), selected_option), fetch='one')
    return int(result[0]) if result else None


def get_prediction_entries(card_id):
    query = """
    SELECT pe.*, u.telegram_username, u.ichancy_username, u.player_id
    FROM prediction_entries pe
    LEFT JOIN users u ON u.telegram_id = pe.user_telegram_id
    WHERE pe.card_id = %s
    ORDER BY pe.id ASC
    """
    return DatabaseManager.execute_query_dict(query, (int(card_id),), fetch='all') or []


def get_prediction_card_summary(card_id):
    query = """
    SELECT selected_option, COUNT(*) AS count
    FROM prediction_entries
    WHERE card_id = %s
    GROUP BY selected_option
    ORDER BY count DESC, selected_option ASC
    """
    return DatabaseManager.execute_query_dict(query, (int(card_id),), fetch='all') or []


def settle_prediction_card(card_id, winning_option):
    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM prediction_cards WHERE id = %s FOR UPDATE", (int(card_id),))
        card = cursor.fetchone()
        if not card:
            conn.rollback()
            return {'ok': False, 'reason': 'card_not_found'}

        card_dict = get_prediction_card(card_id)
        if not card_dict:
            conn.rollback()
            return {'ok': False, 'reason': 'card_not_found'}
        if str(card_dict.get('status')) == 'settled':
            conn.rollback()
            return {'ok': False, 'reason': 'already_settled'}

        reward = int(card_dict.get('reward_syp') or 0)
        cursor.execute(
            "SELECT user_telegram_id FROM prediction_entries WHERE card_id = %s AND selected_option = %s",
            (int(card_id), winning_option)
        )
        winners = [str(r[0]) for r in (cursor.fetchall() or [])]
        if reward > 0 and winners:
            for telegram_id in winners:
                cursor.execute(
                    "UPDATE users SET bot_balance = COALESCE(bot_balance,0) + %s WHERE telegram_id = %s",
                    (reward, telegram_id)
                )
            cursor.execute(
                "UPDATE prediction_entries SET is_winner = TRUE, reward_amount = %s WHERE card_id = %s AND selected_option = %s",
                (reward, int(card_id), winning_option)
            )
        cursor.execute(
            "UPDATE prediction_cards SET status = 'settled', settled_at = CURRENT_TIMESTAMP, winning_option = %s WHERE id = %s",
            (winning_option, int(card_id))
        )
        conn.commit()
        return {'ok': True, 'winners_count': len(winners), 'reward_syp': reward}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"settle_prediction_card error: {e}")
        return {'ok': False, 'reason': 'exception'}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def get_open_prediction_cards(limit=10):
    query = """
    SELECT * FROM prediction_cards
    WHERE status = 'open'
      AND (closes_at IS NULL OR closes_at > CURRENT_TIMESTAMP)
    ORDER BY id DESC
    LIMIT %s
    """
    return DatabaseManager.execute_query_dict(query, (int(limit),), fetch='all') or []


def get_user_prediction_entry(card_id, user_telegram_id):
    query = "SELECT * FROM prediction_entries WHERE card_id = %s AND user_telegram_id = %s"
    return DatabaseManager.execute_query_dict(query, (int(card_id), str(user_telegram_id)), fetch='one')


def add_prediction_entry_safe(card_id, user_telegram_id, selected_option):
    card = get_prediction_card(card_id)
    if not card:
        return {'ok': False, 'reason': 'card_not_found'}
    if str(card.get('status')) != 'open':
        return {'ok': False, 'reason': 'card_closed'}
    closes_at = card.get('closes_at')
    if closes_at and datetime.now(closes_at.tzinfo) >= closes_at:
        close_prediction_card(card_id)
        return {'ok': False, 'reason': 'card_closed'}
    if get_user_prediction_entry(card_id, user_telegram_id):
        return {'ok': False, 'reason': 'already_predicted'}
    summary = get_prediction_card_summary(card_id)
    total_entries = sum(int(x.get('count') or 0) for x in summary)
    max_predictions = int(card.get('max_predictions') or 0)
    if max_predictions > 0 and total_entries >= max_predictions:
        return {'ok': False, 'reason': 'limit_reached'}
    import json
    try:
        options = json.loads(card.get('options_json') or '[]')
    except Exception:
        options = []
    if selected_option not in options:
        return {'ok': False, 'reason': 'invalid_option'}
    entry_id = add_prediction_entry(card_id, user_telegram_id, selected_option)
    if not entry_id:
        return {'ok': False, 'reason': 'not_saved'}
    return {'ok': True, 'entry_id': entry_id}

# ==================== مسابقات القيصر ====================

def create_contest(title, description, contest_type='first_approved', reward_type='gift_code', reward_amount=0, winners_limit=1, requires_proof=True, created_by=None):
    query = """
    INSERT INTO contests (
        title, description, contest_type, reward_type, reward_amount,
        winners_limit, requires_proof, status, created_by
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s)
    RETURNING id
    """
    result = DatabaseManager.execute_query(
        query,
        (title, description, contest_type, reward_type, int(reward_amount or 0), int(winners_limit or 1), bool(requires_proof), str(created_by) if created_by else None),
        fetch='one'
    )
    return int(result[0]) if result else None


def get_contest(contest_id):
    return DatabaseManager.execute_query_dict("SELECT * FROM contests WHERE id = %s", (int(contest_id),), fetch='one')


def get_contests(status='all', limit=50):
    if status == 'all':
        return DatabaseManager.execute_query_dict("SELECT * FROM contests ORDER BY id DESC LIMIT %s", (int(limit),), fetch='all') or []
    return DatabaseManager.execute_query_dict("SELECT * FROM contests WHERE status = %s ORDER BY id DESC LIMIT %s", (status, int(limit)), fetch='all') or []


def close_contest(contest_id):
    DatabaseManager.execute_query("UPDATE contests SET status = 'closed', closed_at = CURRENT_TIMESTAMP WHERE id = %s", (int(contest_id),))


def get_open_contests(limit=10):
    return DatabaseManager.execute_query_dict("SELECT * FROM contests WHERE status = 'open' ORDER BY id DESC LIMIT %s", (int(limit),), fetch='all') or []


def get_contest_entry(contest_id, user_telegram_id):
    return DatabaseManager.execute_query_dict(
        "SELECT * FROM contest_entries WHERE contest_id = %s AND user_telegram_id = %s",
        (int(contest_id), str(user_telegram_id)),
        fetch='one'
    )


def add_contest_entry(contest_id, user_telegram_id, proof_text=None, proof_type='text', proof_file_id=None):
    contest = get_contest(contest_id)
    if not contest:
        return {'ok': False, 'reason': 'contest_not_found'}
    if str(contest.get('status')) != 'open':
        return {'ok': False, 'reason': 'contest_closed'}
    if get_contest_entry(contest_id, user_telegram_id):
        return {'ok': False, 'reason': 'already_joined'}
    result = DatabaseManager.execute_query(
        """
        INSERT INTO contest_entries (contest_id, user_telegram_id, proof_text, proof_type, proof_file_id, status)
        VALUES (%s, %s, %s, %s, %s, 'pending')
        RETURNING id
        """,
        (int(contest_id), str(user_telegram_id), proof_text, proof_type, proof_file_id),
        fetch='one'
    )
    return {'ok': True, 'entry_id': int(result[0])} if result else {'ok': False, 'reason': 'not_saved'}


def get_contest_entries(contest_id, status='all'):
    if status == 'all':
        query = """
        SELECT ce.*, u.telegram_username, u.ichancy_username, u.player_id
        FROM contest_entries ce
        LEFT JOIN users u ON u.telegram_id = ce.user_telegram_id
        WHERE ce.contest_id = %s
        ORDER BY ce.id ASC
        """
        return DatabaseManager.execute_query_dict(query, (int(contest_id),), fetch='all') or []
    query = """
    SELECT ce.*, u.telegram_username, u.ichancy_username, u.player_id
    FROM contest_entries ce
    LEFT JOIN users u ON u.telegram_id = ce.user_telegram_id
    WHERE ce.contest_id = %s AND ce.status = %s
    ORDER BY ce.id ASC
    """
    return DatabaseManager.execute_query_dict(query, (int(contest_id), status), fetch='all') or []


def approve_contest_entry(entry_id, reviewed_by=None):
    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM contest_entries WHERE id = %s FOR UPDATE", (int(entry_id),))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return {'ok': False, 'reason': 'entry_not_found'}

        entry = DatabaseManager.execute_query_dict("SELECT * FROM contest_entries WHERE id = %s", (int(entry_id),), fetch='one')
        if not entry or entry.get('status') != 'pending':
            conn.rollback()
            return {'ok': False, 'reason': 'already_reviewed'}

        contest = get_contest(entry.get('contest_id'))
        if not contest:
            conn.rollback()
            return {'ok': False, 'reason': 'contest_not_found'}

        approved_count = DatabaseManager.execute_query(
            "SELECT COUNT(*) FROM contest_entries WHERE contest_id = %s AND status = 'approved'",
            (int(entry.get('contest_id')),),
            fetch='one'
        )
        approved_count = int(approved_count[0] or 0) if approved_count else 0
        winners_limit = int(contest.get('winners_limit') or 1)
        if approved_count >= winners_limit:
            conn.rollback()
            return {'ok': False, 'reason': 'winners_limit_reached'}

        gift_code = None
        reward_amount = int(contest.get('reward_amount') or 0)
        reward_type = str(contest.get('reward_type') or 'bonus_code')
        if reward_type in ('bonus_code', 'gift_code'):
            import secrets
            gift_code = f"CAESAR-BONUS-{secrets.token_hex(4).upper()}"
            cursor.execute(
                "INSERT INTO gifts (sender_telegram_id, receiver_telegram_id, code, amount, is_redeemed) VALUES (%s, %s, %s, %s, FALSE)",
                (f"CONTEST:{contest.get('id')}:BONUS", str(entry.get('user_telegram_id')), gift_code, reward_amount)
            )
        elif reward_type == 'cash_code':
            import secrets
            gift_code = f"CAESAR-CASH-{secrets.token_hex(4).upper()}"
            cursor.execute(
                "INSERT INTO gifts (sender_telegram_id, receiver_telegram_id, code, amount, is_redeemed) VALUES (%s, %s, %s, %s, FALSE)",
                (f"CONTEST:{contest.get('id')}:CASH", str(entry.get('user_telegram_id')), gift_code, reward_amount)
            )
        elif reward_type == 'bot_balance' and reward_amount > 0:
            cursor.execute(
                "UPDATE users SET bot_balance = COALESCE(bot_balance, 0) + %s WHERE telegram_id = %s",
                (reward_amount, str(entry.get('user_telegram_id')))
            )
        elif reward_type == 'bonus_balance' and reward_amount > 0:
            cursor.execute(
                "UPDATE users SET bonus_balance = COALESCE(bonus_balance, 0) + %s WHERE telegram_id = %s",
                (reward_amount, str(entry.get('user_telegram_id')))
            )

        cursor.execute(
            """
            UPDATE contest_entries
            SET status = 'approved', gift_code = %s, reward_amount = %s,
                reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (gift_code, reward_amount, str(reviewed_by) if reviewed_by else None, int(entry_id))
        )
        conn.commit()
        return {'ok': True, 'gift_code': gift_code, 'reward_amount': reward_amount, 'user_telegram_id': str(entry.get('user_telegram_id')), 'contest_id': int(entry.get('contest_id'))}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"approve_contest_entry error: {e}")
        return {'ok': False, 'reason': 'exception'}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def reject_contest_entry(entry_id, reviewed_by=None):
    DatabaseManager.execute_query(
        "UPDATE contest_entries SET status = 'rejected', reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP WHERE id = %s AND status = 'pending'",
        (str(reviewed_by) if reviewed_by else None, int(entry_id))
    )
    return True


def pick_random_contest_winners(contest_id, winners_count=1):
    rows = DatabaseManager.execute_query_dict(
        """
        SELECT ce.*, u.telegram_username, u.ichancy_username, u.player_id
        FROM contest_entries ce
        LEFT JOIN users u ON u.telegram_id = ce.user_telegram_id
        WHERE ce.contest_id = %s AND ce.status = 'pending'
        ORDER BY RANDOM()
        LIMIT %s
        """,
        (int(contest_id), int(winners_count)),
        fetch='all'
    ) or []
    return rows


# ==================== 📊 لوحة الصدارة (Leaderboard) ====================

def get_leaderboard(limit=10, telegram_id=None):
    """جلب أعلى المستخدمين حسب رصيد البوت مع ترتيب المستخدم الحالي.

    دالة خفيفة لإزالة تحذير user_me، وتستخدم الرصيد النقدي فقط.
    """
    rows = DatabaseManager.execute_query_dict(
        """SELECT telegram_id, telegram_username, bot_balance
           FROM users
           WHERE terms_accepted = TRUE
           ORDER BY bot_balance DESC NULLS LAST
           LIMIT %s""",
        (int(limit),), fetch='all'
    ) or []
    top = []
    for idx, row in enumerate(rows, start=1):
        top.append({
            'rank': idx,
            'telegram_id': str(row.get('telegram_id')),
            'username': row.get('telegram_username') or str(row.get('telegram_id')),
            'balance': int(row.get('bot_balance') or 0),
        })
    result = {'top': top}
    if telegram_id:
        rank_row = DatabaseManager.execute_query(
            """SELECT COUNT(*) + 1 FROM users
               WHERE terms_accepted = TRUE
               AND COALESCE(bot_balance, 0) > COALESCE((SELECT bot_balance FROM users WHERE telegram_id = %s), 0)""",
            (str(telegram_id),), fetch='one'
        )
        my_user = get_user(telegram_id)
        result['my_rank'] = int(rank_row[0] or 0) if rank_row else None
        result['my_balance'] = int((my_user or {}).get('bot_balance') or 0)
    return result


# ==================== 📅 الحضور اليومي (Daily Check-in) ====================

def get_checkin_info(telegram_id):
    """جلب معلومات الحضور للمستخدم."""
    tid = str(telegram_id)
    row = DatabaseManager.execute_query_dict(
        "SELECT * FROM daily_checkins WHERE telegram_id = %s",
        (tid,), fetch='one'
    )
    if not row:
        return {'current_streak': 0, 'total_checkins': 0, 'total_rewards': 0, 'last_checkin_date': None}
    return {
        'current_streak': int(row.get('current_streak') or 0),
        'total_checkins': int(row.get('total_checkins') or 0),
        'total_rewards': int(row.get('total_rewards') or 0),
        'last_checkin_date': row.get('last_checkin_date'),
    }


def can_checkin_today(telegram_id):
    """هل يمكن للمستخدم تسجيل الحضور اليوم؟ (بتوقيت سوريا)"""
    info = get_checkin_info(telegram_id)
    if not info.get('last_checkin_date'):
        return True
    last = info['last_checkin_date']
    if hasattr(last, 'year'):
        last_date = last if hasattr(last, 'day') and not hasattr(last, 'hour') else last.date()
    else:
        try:
            last_date = datetime.strptime(str(last).split(' ')[0], '%Y-%m-%d').date()
        except Exception:
            return True
    today = get_syria_now().date()  # 🆕 (Update 14) توقيت سوريا
    return last_date < today


def do_daily_checkin(telegram_id):
    """تسجيل الحضور اليومي بنظام تقدّم بصري 30 يوم.

    - يتفعل بعد أول إيداع مقبول أياً كانت قيمته.
    - لا تُصرف مكافآت يومية.
    - عند إكمال الدورة دون انقطاع، ومع تحقق حد إيداعات آخر 30 يوم، تُضاف مكافأة مستحقة
      إلى checkin_pending_balance لتُضاف تلقائياً عند أول شحن لعبة ناجح.
    """
    tid = str(telegram_id)
    today = get_syria_now().date()
    info = get_checkin_info(tid)
    feat_settings = get_user_features_settings()
    cycle_days = int(feat_settings.get('checkin_cycle_days') or 30)
    completion_reward = int(feat_settings.get('checkin_completion_reward') or 20000)
    min_deposit = int(feat_settings.get('checkin_min_deposit') or 50000)
    if cycle_days < 1:
        cycle_days = 30

    if not can_checkin_today(tid):
        return {'ok': False, 'reason': 'already_checked_in'}

    # التفعيل بعد أول إيداع مقبول مهما كانت قيمته
    any_deposit = DatabaseManager.execute_query(
        "SELECT id FROM transactions WHERE user_telegram_id = %s AND type = 'deposit_bot' AND status = 'approved' LIMIT 1",
        (tid,), fetch='one'
    )
    if not any_deposit:
        return {'ok': False, 'reason': 'first_deposit_required'}

    last_date = info.get('last_checkin_date')
    if last_date:
        last = last_date if hasattr(last_date, 'year') else datetime.strptime(str(last_date), '%Y-%m-%d').date()
        diff = (today - last).days
        new_streak = info['current_streak'] + 1 if diff == 1 else 1
    else:
        new_streak = 1

    recent_deposits = get_user_recent_deposits_total(tid, 30)
    cycle_completed = new_streak >= cycle_days
    qualified = recent_deposits >= min_deposit
    reward = completion_reward if (cycle_completed and qualified) else 0
    stored_streak = 0 if cycle_completed else new_streak

    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT checkin_pending_balance FROM users WHERE telegram_id = %s FOR UPDATE", (tid,))
        if not cursor.fetchone():
            conn.rollback()
            return {'ok': False, 'reason': 'user_not_found'}
        if reward > 0:
            cursor.execute(
                "UPDATE users SET checkin_pending_balance = COALESCE(checkin_pending_balance, 0) + %s WHERE telegram_id = %s",
                (reward, tid)
            )
        cursor.execute("""
            INSERT INTO daily_checkins (telegram_id, last_checkin_date, current_streak, total_checkins, total_rewards)
            VALUES (%s, %s, %s, 1, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET
                last_checkin_date = EXCLUDED.last_checkin_date,
                current_streak = EXCLUDED.current_streak,
                total_checkins = daily_checkins.total_checkins + 1,
                total_rewards = daily_checkins.total_rewards + EXCLUDED.total_rewards
        """, (tid, today, stored_streak, reward))
        conn.commit()
        return {
            'ok': True,
            'reward': reward,
            'streak': new_streak,
            'stored_streak': stored_streak,
            'cycle_days': cycle_days,
            'cycle_completed': cycle_completed,
            'qualified': qualified,
            'recent_deposits': recent_deposits,
            'required_deposits': min_deposit,
            'pending_reward': reward,
        }
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"do_daily_checkin error: {e}")
        return {'ok': False, 'reason': 'error'}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


# ==================== ⚙️ إعدادات ميزات المستخدم (Update 13) ====================

import json as _json

WHEEL_FIXED_SEGMENTS = [
    [0, 1, 'PINGO'],
    [0, 1, '0%'],
    [1, 1, '+1%'],
    [2, 1, '+2%'],
    [0, 1, '0%'],
    [3, 1, '+3%'],
    [4, 1, '+4%'],
    [0, 1, '0%'],
]


def _fixed_wheel_segments_with_weights(saved=None):
    """إرجاع قطاعات العجلة الثابتة مع أوزان قابلة للتعديل فقط."""
    weights = []
    if isinstance(saved, str):
        try:
            saved = _json.loads(saved or '[]')
        except Exception:
            saved = []
    if isinstance(saved, list):
        for i, item in enumerate(saved[:8]):
            try:
                # ندعم [pct, weight] أو [pct, weight, label] أو وزن مباشر
                w = item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else item
                weights.append(float(w))
            except Exception:
                weights.append(1.0)
    out = []
    for i, fixed in enumerate(WHEEL_FIXED_SEGMENTS):
        w = weights[i] if i < len(weights) else fixed[1]
        out.append([fixed[0], float(w), fixed[2]])
    return out

def get_user_features_settings():
    """جلب إعدادات ميزات المستخدم (الحضور، الصدارة، شروط المكافآت)."""
    row = DatabaseManager.execute_query_dict("SELECT * FROM user_features_settings WHERE id = 1", fetch='one')
    if not row:
        DatabaseManager.execute_query(
            "INSERT INTO user_features_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;"
        )
        row = DatabaseManager.execute_query_dict("SELECT * FROM user_features_settings WHERE id = 1", fetch='one')
    # 🆕 الجدول الافتراضي = دورة شهرية (30 يوماً) مجموعها 20,000 ل.س بالضبط.
    rewards = [
        200, 200, 300, 300, 400, 400, 1200,
        300, 400, 400, 400, 500, 500, 600,
        2000,
        400, 500, 500, 500, 600, 600, 700,
        500, 600, 600, 700, 700, 800,
        1000, 3200,
    ]
    try:
        _saved = _json.loads(row.get('checkin_rewards_json') or '[]') if row else []
        if _saved:
            rewards = _saved
    except Exception:
        pass
    return {
        'checkin_enabled': row.get('checkin_enabled', True) if row else True,
        'checkin_rewards': rewards,
        'checkin_rewards_json': row.get('checkin_rewards_json') if row else None,
        'checkin_min_deposit': int(row.get('checkin_min_deposit') or 50000) if row else 50000,
        'checkin_cycle_days': int(row.get('checkin_cycle_days') or 30) if row else 30,
        'checkin_completion_reward': int(row.get('checkin_completion_reward') or 20000) if row else 20000,
        'leaderboard_enabled': row.get('leaderboard_enabled', True) if row else True,
        'leaderboard_type': row.get('leaderboard_type', 'all_time') if row else 'all_time',
        'bonus_min_transfer': int(row.get('bonus_min_transfer') or 20000) if row else 20000,
        'bonus_deposit_threshold': int(row.get('bonus_deposit_threshold') or 100000) if row else 100000,
        'bonus_deposit_days': int(row.get('bonus_deposit_days') or 30) if row else 30,
        # إعدادات العجلة/VIP/الكاش باك تُحفظ في نفس الجدول؛ إرجاعها هنا ضروري كي لا تُهمل القيم المحفوظة.
        'wheel_enabled': row.get('wheel_enabled', True) if row else True,
        'wheel_segments_json': row.get('wheel_segments_json') if row else None,
        'wheel_min_deposit': int(row.get('wheel_min_deposit') or 50000) if row else 50000,
        'wheel_max_reward': int(row.get('wheel_max_reward') or 30000) if row else 30000,
        'vip_enabled': row.get('vip_enabled', True) if row else True,
        'vip_tiers_json': row.get('vip_tiers_json') if row else None,
        'cashback_enabled': row.get('cashback_enabled', True) if row else True,
        'cashback_pct': float(row.get('cashback_pct') or 5) if row else 5,
        'cashback_min_loss': int(row.get('cashback_min_loss') or 50000) if row else 50000,
    }


def update_user_features_settings(checkin_enabled=None, checkin_rewards=None, leaderboard_enabled=None, leaderboard_type=None, bonus_min_transfer=None, bonus_deposit_threshold=None, bonus_deposit_days=None, checkin_min_deposit=None, checkin_cycle_days=None, checkin_completion_reward=None):
    """تحديث إعدادات ميزات المستخدم."""
    current = get_user_features_settings()
    new_checkin_enabled = checkin_enabled if checkin_enabled is not None else current['checkin_enabled']
    new_checkin_rewards = checkin_rewards if checkin_rewards is not None else current['checkin_rewards']
    new_lb_enabled = leaderboard_enabled if leaderboard_enabled is not None else current['leaderboard_enabled']
    new_lb_type = leaderboard_type if leaderboard_type is not None else current['leaderboard_type']
    new_checkin_min = int(checkin_min_deposit) if checkin_min_deposit is not None else current.get('checkin_min_deposit', 50000)
    new_checkin_cycle = int(checkin_cycle_days) if checkin_cycle_days is not None else current.get('checkin_cycle_days', 30)
    new_checkin_reward = int(checkin_completion_reward) if checkin_completion_reward is not None else current.get('checkin_completion_reward', 20000)
    
    # 🆕 (Update 14) شروط المكافآت
    new_bonus_min = int(bonus_min_transfer) if bonus_min_transfer is not None else current['bonus_min_transfer']
    new_bonus_threshold = int(bonus_deposit_threshold) if bonus_deposit_threshold is not None else current['bonus_deposit_threshold']
    new_bonus_days = int(bonus_deposit_days) if bonus_deposit_days is not None else current['bonus_deposit_days']

    rewards_json = _json.dumps([int(x) for x in new_checkin_rewards])
    DatabaseManager.execute_query("""
        UPDATE user_features_settings
        SET checkin_enabled = %s, checkin_rewards_json = %s, leaderboard_enabled = %s, leaderboard_type = %s,
            checkin_min_deposit = %s, checkin_cycle_days = %s, checkin_completion_reward = %s,
            bonus_min_transfer = %s, bonus_deposit_threshold = %s, bonus_deposit_days = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    """, (bool(new_checkin_enabled), rewards_json, bool(new_lb_enabled), str(new_lb_type), new_checkin_min, new_checkin_cycle, new_checkin_reward, new_bonus_min, new_bonus_threshold, new_bonus_days))


def get_user_recent_deposits_total(telegram_id, days=30):
    """حساب إجمالي إيداعات المستخدم النقدية المقبولة في آخر X يوم."""
    tid = str(telegram_id)
    result = DatabaseManager.execute_query(
        """SELECT COALESCE(SUM(amount), 0) FROM transactions 
           WHERE user_telegram_id = %s AND type = 'deposit_bot' AND status = 'approved'
           AND created_at >= CURRENT_DATE - INTERVAL '%s days'""",
        (tid, int(days)), fetch='one'
    )
    return int(result[0]) if result else 0


def check_bonus_eligibility(telegram_id):
    """فحص هل المستخدم مؤهل لاستخدام رصيد المكافآت في اللعبة؟"""
    settings = get_user_features_settings()
    threshold = settings.get('bonus_deposit_threshold', 100000)
    days = settings.get('bonus_deposit_days', 30)
    recent_deposits = get_user_recent_deposits_total(telegram_id, days)
    
    return {
        'eligible': recent_deposits >= threshold,
        'recent_deposits': recent_deposits,
        'threshold': threshold,
        'days': days
    }


def transfer_bonus_to_game_atomic(telegram_id, amount, player_id):
    """شحن رصيد المكافآت للعبة بشكل ذري (بدون لمس رصيد البوت الحقيقي)."""
    conn = None
    cursor = None
    tid = str(telegram_id)
    amount_int = int(float(amount))

    if amount_int <= 0:
        return {'success': False, 'reason': 'invalid_amount'}

    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT bonus_balance FROM users WHERE telegram_id = %s FOR UPDATE", (tid,))
        user_row = cursor.fetchone()
        if not user_row:
            conn.rollback()
            return {'success': False, 'reason': 'not_found'}

        current_bonus = int(user_row[0] or 0)
        if current_bonus < amount_int:
            conn.rollback()
            return {'success': False, 'reason': 'insufficient_bonus', 'current_bonus': current_bonus}

        cursor.execute(
            "UPDATE users SET bonus_balance = COALESCE(bonus_balance, 0) - %s WHERE telegram_id = %s RETURNING bonus_balance",
            (amount_int, tid)
        )
        new_bonus = int(cursor.fetchone()[0] or 0)

        # 🆕 تسجيل مبلغ البونص المحجوز في اللعبة لغرض التدوير (Rollover)
        cursor.execute(
            "UPDATE users SET game_bonus_amount = COALESCE(game_bonus_amount, 0) + %s WHERE telegram_id = %s",
            (amount_int, tid)
        )

        cursor.execute(
            """
            INSERT INTO transactions (user_telegram_id, type, payment_method, amount, transfer_number, status)
            VALUES (%s, 'bonus_to_game', 'game', %s, %s, 'pending')
            RETURNING id
            """,
            (tid, amount_int, f'Bonus transfer to player {player_id}')
        )
        tx_row = cursor.fetchone()
        tx_id = tx_row[0]

        conn.commit()
        return {'success': True, 'tx_id': tx_id, 'new_bonus_balance': new_bonus, 'amount': amount_int}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"transfer_bonus_to_game_atomic error: {e}")
        return {'success': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def adjust_user_bonus_balance(telegram_id, new_balance):
    """تعديل رصيد المكافآت (للأدمن)."""
    DatabaseManager.execute_query(
        "UPDATE users SET bonus_balance = %s WHERE telegram_id = %s",
        (int(new_balance), str(telegram_id))
    )
    return True


def get_user_bonus_balance(telegram_id):
    """جلب رصيد المكافآت الحالي."""
    user = get_user(telegram_id)
    if user and user.get('bonus_balance') is not None:
        return int(user['bonus_balance'])
    return 0


def get_checkin_stats():
    """إحصائيات الحضور اليومي."""
    today_count = DatabaseManager.execute_query(
        "SELECT COUNT(*) FROM daily_checkins WHERE last_checkin_date = CURRENT_DATE", fetch='one'
    )
    total_users = DatabaseManager.execute_query("SELECT COUNT(*) FROM daily_checkins", fetch='one')
    total_rewards = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(total_rewards), 0) FROM daily_checkins", fetch='one'
    )
    avg_streak = DatabaseManager.execute_query(
        "SELECT COALESCE(AVG(current_streak), 0) FROM daily_checkins", fetch='one'
    )
    return {
        'today_checkins': int(today_count[0]) if today_count else 0,
        'total_checkin_users': int(total_users[0]) if total_users else 0,
        'total_rewards_paid': int(total_rewards[0]) if total_rewards else 0,
        'avg_streak': round(float(avg_streak[0]), 1) if avg_streak else 0,
    }


# ==================== 🎰 عجلة الحظ (Deposit Booster Wheel) ====================

def get_wheel_settings():
    """جلب إعدادات العجلة (التفعيل + القطاعات + الحدود)."""
    feat = get_user_features_settings()
    # الخانات ثابتة: PINGO - 0% - 1% - 2% - 0% - 3% - 4% - 0%
    # المتغير الوحيد من لوحة التحكم هو الوزن/الاحتمال لكل خانة.
    segments = _fixed_wheel_segments_with_weights(feat.get('wheel_segments_json'))
    bot_settings = get_bot_settings() or {}
    return {
        'wheel_enabled': feat.get('wheel_enabled', True),
        'segments': segments,
        # أهلية العجلة تتبع الحد الأدنى للإيداع في البوت.
        'wheel_min_deposit': int(bot_settings.get('min_deposit_syp') or 20000),
        # 🆕 سقف أقصى لجائزة الدورة الواحدة (افتراضي 30,000 ل.س) — يحمي من الإيداعات الكبيرة
        'wheel_max_reward': int(feat.get('wheel_max_reward') or 30000),
    }


def update_wheel_settings(wheel_enabled=None, segments=None):
    """تحديث إعدادات العجلة: الخانات ثابتة، والأوزان فقط قابلة للتعديل."""
    current = get_user_features_settings()
    new_enabled = wheel_enabled if wheel_enabled is not None else current.get('wheel_enabled', True)
    if segments is not None:
        fixed_segments = _fixed_wheel_segments_with_weights(segments)
        seg_json = _json.dumps(fixed_segments, ensure_ascii=False)
    else:
        seg_json = current.get('wheel_segments_json') or _json.dumps(WHEEL_FIXED_SEGMENTS, ensure_ascii=False)
    DatabaseManager.execute_query("""
        UPDATE user_features_settings
        SET wheel_enabled = %s, wheel_segments_json = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    """, (bool(new_enabled), seg_json))


def can_spin_wheel_for_deposit(telegram_id, deposit_tx_id):
    """هل يحق للمستخدم الدوران على هذا الإيداع؟ (True إذا لم يدُر سابقاً)"""
    tid = str(telegram_id)
    result = DatabaseManager.execute_query(
        "SELECT id FROM wheel_spins WHERE telegram_id = %s AND deposit_tx_id = %s",
        (tid, int(deposit_tx_id)), fetch='one'
    )
    return result is None


def has_wheel_spin(telegram_id, deposit_tx_id):
    """(Alias/Backward Compatibility) هل يحق للمستخدم لفة على هذا الإيداع؟"""
    return can_spin_wheel_for_deposit(telegram_id, deposit_tx_id)


def get_spun_deposit_ids(telegram_id):
    """جلب كل معرّفات الإيداعات التي تم دوران العجلة عليها."""
    tid = str(telegram_id)
    rows = DatabaseManager.execute_query(
        "SELECT deposit_tx_id FROM wheel_spins WHERE telegram_id = %s",
        (tid,), fetch='all'
    )
    return [int(r[0]) for r in rows] if rows else []


def spin_wheel_atomic(telegram_id, deposit_tx_id, deposit_amount):
    """تنفيذ دوران العجلة ذرياً.

    تختار القطاع عشوائياً (وزنياً)، تحفظ النتيجة، وتضيف الجائزة لرصيد المكافآت.
    """
    import random
    tid = str(telegram_id)

    # جلب القطاعات
    settings = get_wheel_settings()
    segments = settings.get('segments', [[0,30],[2,20],[0,10],[5,15],[10,10],[0,5],[15,7],[25,3]])

    # اختيار وزني للقطاع
    weights = [seg[1] for seg in segments]
    total_weight = sum(weights)
    if total_weight <= 0:
        return {'ok': False, 'reason': 'invalid_segments'}

    # 🔒 حماية مالية مزدوجة: نتخطّى صراحةً خانات "الزينة" (وزن 0) فلا تُصاب أبداً
    #    حتى عند الحالة الحدّية r=0 أو أي ترتيب. الخانات ذات الوزن 0 للعرض فقط.
    r = random.uniform(0, total_weight)
    cumulative = 0
    selected_index = None
    for i, w in enumerate(weights):
        if w <= 0:
            continue  # خانة زينة — تخطٍّ تام (مستحيلة الإصابة)
        cumulative += w
        if r <= cumulative:
            selected_index = i
            break
    # أمان إضافي: إن لم يُختَر شيء (حالة حدّية)، اختر أول خانة حقيقية (وزن > 0)
    if selected_index is None:
        selected_index = next((i for i, w in enumerate(weights) if w > 0), 0)

    reward_percent = float(segments[selected_index][0])
    reward_amount = int(deposit_amount * reward_percent / 100.0)

    # 🆕 سقف أقصى للجائزة — يحمي من الالتزام المفتوح على الإيداعات الكبيرة
    max_reward = int(settings.get('wheel_max_reward') or 0)
    if max_reward > 0 and reward_amount > max_reward:
        reward_amount = max_reward

    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        # التأكد من عدم الدوران المزدوج
        cursor.execute(
            "SELECT id FROM wheel_spins WHERE telegram_id = %s AND deposit_tx_id = %s FOR UPDATE",
            (tid, int(deposit_tx_id))
        )
        if cursor.fetchone():
            conn.rollback()
            return {'ok': False, 'reason': 'already_spun'}

        # إضافة الجائزة لرصيد المكافآت.
        # لا نكرر bonus_base_balance إذا كان نفس الإيداع أضاف قاعدة صرف مسبقاً (بونص/فلاش/VIP).
        # إذا لم يكن للإيداع أي بونص سابق، نضيف قاعدة الإيداع هنا حتى تُصرف جائزة العجلة مع شحن نفس مبلغ الإيداع.
        if reward_amount > 0:
            cursor.execute(
                "SELECT COALESCE(bonus_base_added_syp, 0) FROM transactions WHERE id = %s FOR UPDATE",
                (int(deposit_tx_id),)
            )
            tx_base_row = cursor.fetchone()
            already_added_base = int(tx_base_row[0] or 0) if tx_base_row else 0
            base_to_add = 0 if already_added_base > 0 else int(deposit_amount)
            cursor.execute(
                """UPDATE users
                   SET bonus_balance = COALESCE(bonus_balance, 0) + %s,
                       bonus_base_balance = COALESCE(bonus_base_balance, 0) + %s
                   WHERE telegram_id = %s""",
                (reward_amount, base_to_add, tid)
            )
            if base_to_add > 0:
                cursor.execute(
                    "UPDATE transactions SET bonus_base_added_syp = COALESCE(bonus_base_added_syp, 0) + %s WHERE id = %s",
                    (base_to_add, int(deposit_tx_id))
                )

        # حفظ الدوران
        cursor.execute(
            """
            INSERT INTO wheel_spins (telegram_id, deposit_tx_id, segment_index, reward_percent, reward_amount)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (tid, int(deposit_tx_id), selected_index, reward_percent, reward_amount)
        )

        conn.commit()
        return {
            'ok': True,
            'segment_index': selected_index,
            'reward_percent': reward_percent,
            'reward_amount': reward_amount,
            # التسمية المعروضة (إن وُجدت) — تُطابق ما يراه اللاعب على العجلة
            'label': (segments[selected_index][2] if len(segments[selected_index]) > 2 else None),
        }
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"spin_wheel_atomic error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


def get_wheel_stats():
    """إحصائيات العجلة."""
    today = DatabaseManager.execute_query(
        "SELECT COUNT(*), COALESCE(SUM(reward_amount), 0) FROM wheel_spins WHERE created_at::date = CURRENT_DATE",
        fetch='one'
    )
    total = DatabaseManager.execute_query(
        "SELECT COUNT(*), COALESCE(SUM(reward_amount), 0) FROM wheel_spins",
        fetch='one'
    )
    return {
        'today_spins': int(today[0]) if today else 0,
        'today_rewards': int(today[1]) if today else 0,
        'total_spins': int(total[0]) if total else 0,
        'total_rewards': int(total[1]) if total else 0,
    }


# ==================== 🏆 نظام VIP (الطبقات الملكية) ====================

def get_vip_settings():
    """جلب إعدادات نظام VIP."""
    feat = get_user_features_settings()
    tiers = [[0,0,0],[500000,1,10000],[2000000,2,50000],[5000000,3,200000]]
    try:
        tiers = _json.loads(feat.get('vip_tiers_json') or '[]') or tiers
    except Exception:
        pass
    return {
        'vip_enabled': feat.get('vip_enabled', True),
        'tiers': tiers,
    }


def update_vip_settings(vip_enabled=None, tiers=None):
    """تحديث إعدادات VIP."""
    current = get_user_features_settings()
    new_enabled = vip_enabled if vip_enabled is not None else current.get('vip_enabled', True)
    if tiers is not None:
        tiers_json = _json.dumps([[int(a), int(b), int(c)] for a, b, c in tiers])
    else:
        tiers_json = current.get('vip_tiers_json', '[[0,0,0],[500000,1,10000],[2000000,2,50000],[5000000,3,200000]]')
    DatabaseManager.execute_query("""
        UPDATE user_features_settings
        SET vip_enabled = %s, vip_tiers_json = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    """, (bool(new_enabled), tiers_json))


def get_user_total_deposits(telegram_id):
    """إجمالي إيداعات المستخدم (Lifetime)."""
    tid = str(telegram_id)
    result = DatabaseManager.execute_query(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_telegram_id = %s AND type = 'deposit_bot' AND status = 'approved'",
        (tid,), fetch='one'
    )
    return int(result[0]) if result else 0


def get_vip_tier_info(total_deposits, tiers):
    """تحديد طبقة المستخدم بناءً على إجمالي إيداعاته."""
    current_tier_index = 0
    next_tier = None
    
    for i, tier in enumerate(tiers):
        threshold, bonus_pct, reward = tier
        if total_deposits >= threshold:
            current_tier_index = i
    
    # البحث عن الطبقة التالية
    if current_tier_index < len(tiers) - 1:
        next_threshold = tiers[current_tier_index + 1][0]
        if next_threshold > total_deposits:
            next_tier = {
                'threshold': next_threshold,
                'remaining': next_threshold - total_deposits,
                'index': current_tier_index + 1
            }
    
    current = tiers[current_tier_index]
    return {
        'current_index': current_tier_index,
        'current_threshold': current[0],
        'current_bonus_pct': current[1],
        'current_reward': current[2],
        'next_tier': next_tier,
        'tier_names': ['🥉 مواطن', '🥈 فارس', '🥇 نبيل', '👑 قيصر']
    }


def check_and_process_vip_upgrade(telegram_id, new_deposit_amount):
    """فحص إذا كان الإيداع الجديد يسبب ترقية طبقة، ومعالجتها."""
    tid = str(telegram_id)
    tiers_settings = get_vip_settings()
    if not tiers_settings.get('vip_enabled', True):
        return {'upgraded': False}
    
    tiers = tiers_settings.get('tiers', [])
    total_deposits = get_user_total_deposits(tid)
    
    # الطبقة القديمة
    old_info = get_vip_tier_info(total_deposits - new_deposit_amount, tiers)
    # الطبقة الجديدة
    new_info = get_vip_tier_info(total_deposits, tiers)
    
    if new_info['current_index'] > old_info['current_index']:
        # حدثت ترقية!
        reward = new_info['current_reward']
        if reward > 0:
            # إضافة مكافأة الترقية لرصيد المكافآت وربطها بالإيداع الذي سبب الترقية
            # حتى تُصرف نسبياً عند شحن اللعبة مثل البونص العادي والفلاش والعجلة.
            DatabaseManager.execute_query(
                """UPDATE users
                   SET bonus_balance = COALESCE(bonus_balance, 0) + %s,
                       bonus_base_balance = COALESCE(bonus_base_balance, 0) + %s,
                       vip_tier = %s
                   WHERE telegram_id = %s""",
                (reward, int(float(new_deposit_amount or 0)), new_info['current_index'], tid)
            )
        else:
            DatabaseManager.execute_query(
                "UPDATE users SET vip_tier = %s WHERE telegram_id = %s",
                (new_info['current_index'], tid)
            )
        return {
            'upgraded': True,
            'new_tier': new_info['tier_names'][new_info['current_index']],
            'reward': reward
        }
    return {'upgraded': False}


# ==================== 💸 الكاش باك الأسبوعي (Weekly Cashback) ====================

def get_cashback_settings():
    """جلب إعدادات الكاش باك."""
    feat = get_user_features_settings()
    return {
        'cashback_enabled': feat.get('cashback_enabled', True),
        'cashback_pct': float(feat.get('cashback_pct') or 3),
        'cashback_min_loss': int(feat.get('cashback_min_loss') or 50000),
    }


def update_cashback_settings(enabled=None, pct=None, min_loss=None):
    """تحديث إعدادات الكاش باك."""
    current = get_user_features_settings()
    new_enabled = enabled if enabled is not None else current.get('cashback_enabled', True)
    new_pct = pct if pct is not None else current.get('cashback_pct', 5)
    new_min = min_loss if min_loss is not None else current.get('cashback_min_loss', 50000)
    DatabaseManager.execute_query("""
        UPDATE user_features_settings
        SET cashback_enabled = %s, cashback_pct = %s, cashback_min_loss = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    """, (bool(new_enabled), float(new_pct), int(new_min)))


def get_user_weekly_game_activity(telegram_id, current_game_balance=None):
    """حساب نشاط المستخدم في اللعبة هذا الأسبوع على المال الحقيقي فقط.

    مهم جداً بعد نظام البونصات:
    - إيداع اللعبة `amount` قد يشمل بونص/كاش باك/حضور؛ لذلك نستخدم `original_amount` كقيمة الشحن النقدي الحقيقي.
    - سحب اللعبة `amount` هو الإجمالي المسحوب من iChancy، لكن الصافي الذي عاد للمستخدم بعد خصم البونص النشط محفوظ في `converted_amount_syp`.

    الحرق الحقيقي = الشحن النقدي الحقيقي − الصافي العائد للمستخدم − الرصيد المتبقي في اللعبة.
    """
    tid = str(telegram_id)
    deposits = DatabaseManager.execute_query(
        """SELECT COALESCE(SUM(COALESCE(original_amount, amount)), 0) FROM transactions
           WHERE user_telegram_id = %s AND type = 'deposit_to_game'
           AND status IN ('completed', 'approved')
           AND created_at >= CURRENT_DATE - INTERVAL '7 days'""",
        (tid,), fetch='one'
    )
    withdrawals = DatabaseManager.execute_query(
        """SELECT COALESCE(SUM(COALESCE(converted_amount_syp, amount)), 0) FROM transactions
           WHERE user_telegram_id = %s AND type = 'withdraw_from_game'
           AND status IN ('completed', 'approved')
           AND created_at >= CURRENT_DATE - INTERVAL '7 days'""",
        (tid,), fetch='one'
    )
    dep = int(float(deposits[0] or 0)) if deposits else 0
    wd = int(float(withdrawals[0] or 0)) if withdrawals else 0
    if current_game_balance is not None:
        net_loss = dep - wd - int(current_game_balance)
    else:
        # بدون رصيد حي لا يمكن تقدير الحرق بدقة، لكن نبقي fallback محافظاً.
        net_loss = dep - wd
    return {
        'deposited': dep,
        'withdrawn': wd,
        'game_balance': int(current_game_balance) if current_game_balance is not None else None,
        'net_loss': max(net_loss, 0),
    }


def has_cashback_this_week(telegram_id):
    """هل حصل المستخدم على كاش باك هذا الأسبوع؟"""
    tid = str(telegram_id)
    week_start = get_syria_now().date() - timedelta(days=get_syria_now().weekday())
    result = DatabaseManager.execute_query(
        "SELECT id FROM cashback_payouts WHERE telegram_id = %s AND week_start >= %s",
        (tid, week_start), fetch='one'
    )
    return result is not None


def process_weekly_cashback_for_user(telegram_id, current_game_balance=None):
    """معالجة الكاش باك لمستخدم واحد.

    current_game_balance: الرصيد الحيّ في اللعبة (يُجلب من iChancy API في الطبقة async)
    لحساب الخسارة الحقيقية (الحرق). إن كان None يُستخدم الحساب المبسّط (شحن−سحب).
    """
    tid = str(telegram_id)
    settings = get_cashback_settings()
    if not settings.get('cashback_enabled', True):
        return {'ok': False, 'reason': 'disabled'}

    if has_cashback_this_week(tid):
        return {'ok': False, 'reason': 'already_paid'}

    activity = get_user_weekly_game_activity(tid, current_game_balance=current_game_balance)
    net_loss = activity['net_loss']
    min_loss = settings.get('cashback_min_loss', 50000)

    if net_loss < min_loss:
        return {'ok': False, 'reason': 'below_min', 'net_loss': net_loss, 'min': min_loss}

    pct = settings.get('cashback_pct', 5)
    cashback = int(net_loss * pct / 100.0)
    if cashback <= 0:
        return {'ok': False, 'reason': 'zero_cashback'}

    now = get_syria_now()
    week_start = now.date() - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6)

    conn = None
    cursor = None
    try:
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT cashback_pending_balance FROM users WHERE telegram_id = %s FOR UPDATE", (tid,))
        if not cursor.fetchone():
            conn.rollback()
            return {'ok': False, 'reason': 'user_not_found'}
        cursor.execute(
            "UPDATE users SET cashback_pending_balance = COALESCE(cashback_pending_balance, 0) + %s WHERE telegram_id = %s",
            (cashback, tid)
        )
        cursor.execute(
            """INSERT INTO cashback_payouts (telegram_id, week_start, week_end, total_deposited, total_withdrawn, net_loss, cashback_amount)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (tid, week_start, week_end, activity['deposited'], activity['withdrawn'], net_loss, cashback)
        )
        conn.commit()
        return {'ok': True, 'cashback': cashback, 'net_loss': net_loss, 'pct': pct}
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"process_weekly_cashback_for_user error: {e}")
        return {'ok': False, 'reason': 'error', 'message': str(e)}
    finally:
        if cursor:
            cursor.close()
        if conn:
            DatabaseManager.put_connection(conn)


async def process_all_weekly_cashbacks(bot=None):
    """معالجة الكاش باك لجميع المستخدمين المؤهلين (تُستدعى أسبوعياً)."""
    settings = get_cashback_settings()
    if not settings.get('cashback_enabled', True):
        return {'processed': 0, 'skipped': 'disabled'}

    # جلب كل المستخدمين الذين لديهم نشاط في اللعبة هذا الأسبوع
    rows = DatabaseManager.execute_query_dict(
        """SELECT DISTINCT t.user_telegram_id FROM transactions t
           WHERE t.type IN ('deposit_to_game', 'withdraw_from_game')
           AND t.status IN ('completed', 'approved')
           AND t.created_at >= CURRENT_DATE - INTERVAL '7 days'""",
        fetch='all'
    ) or []

    # نجلب الرصيد الحيّ من iChancy لحساب الخسارة الحقيقية (الحرق)
    from ichancy_api.client import ichancy_api_client

    processed = 0
    total_paid = 0
    for row in rows:
        tid = row.get('user_telegram_id')
        if not tid:
            continue
        # جلب الرصيد الحيّ في اللعبة (إن أمكن) لحساب الخسارة الحقيقية
        live_balance = None
        try:
            u = get_user(tid)
            pid = u.get('player_id') if u else None
            if pid:
                raw_bal = await ichancy_api_client.get_player_balance(pid)
                if raw_bal is not None:
                    live_balance = int(raw_bal)
                else:
                    live_balance = get_user_game_balance(tid)
        except Exception as e:
            logger.warning(f"cashback: failed to fetch live balance for {tid}: {e}")
            live_balance = get_user_game_balance(tid)
        result = process_weekly_cashback_for_user(tid, current_game_balance=live_balance)
        if result.get('ok'):
            processed += 1
            total_paid += result['cashback']
            # إشعار المستخدم
            if bot:
                try:
                    await bot.send_message(
                        chat_id=tid,
                        text=(
                            f"💸 <b>كاش باك أسبوعي!</b>\n\n"
                            f"📊 خسارتك هذا الأسبوع: <code>{result['net_loss']:,} ل.س</code>\n"
                            f"💰 نسبة الاسترجاع: <code>{result['pct']}%</code>\n"
                            f"💸 تم تسجيل كاش باك مستحق: <code>{result['cashback']:,} ل.س</code>\n"
                            f"🎮 سيُضاف تلقائياً إلى حساب اللعبة بعد أول عملية شحن ناجحة."
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    logger.info(f"Weekly cashback: processed {processed} users, paid {total_paid} total.")
    return {'processed': processed, 'total_paid': total_paid}


def get_cashback_stats():
    """إحصائيات الكاش باك."""
    today = DatabaseManager.execute_query(
        """SELECT COUNT(*), COALESCE(SUM(cashback_amount), 0), COALESCE(SUM(net_loss), 0)
           FROM cashback_payouts WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'""",
        fetch='one'
    )
    total = DatabaseManager.execute_query(
        "SELECT COUNT(*), COALESCE(SUM(cashback_amount), 0) FROM cashback_payouts",
        fetch='one'
    )
    return {
        'week_count': int(today[0]) if today else 0,
        'week_paid': int(today[1]) if today else 0,
        'week_losses': int(today[2]) if today else 0,
        'total_count': int(total[0]) if total else 0,
        'total_paid': int(total[1]) if total else 0,
    }


# ==================== ⚡ فلاش البونص (Flash Bonus) ====================

def create_flash_bonus(percent, payment_method='all', duration_minutes=30, created_by=None):
    """إنشاء فلاش بونص محدود الوقت."""
    query = """
    INSERT INTO flash_bonuses (percent, payment_method, starts_at, ends_at, is_active, created_by)
    VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '%s minutes', TRUE, %s)
    RETURNING id
    """
    result = DatabaseManager.execute_query(
        query, (float(percent), payment_method, int(duration_minutes), str(created_by) if created_by else None),
        fetch='one'
    )
    return int(result[0]) if result else None


def get_active_flash_bonus():
    """جلب فلاش البونص النشط حالياً (لو يوجد)."""
    DatabaseManager.execute_query(
        "UPDATE flash_bonuses SET is_active = FALSE WHERE ends_at < CURRENT_TIMESTAMP AND is_active = TRUE"
    )
    return DatabaseManager.execute_query_dict(
        "SELECT * FROM flash_bonuses WHERE is_active = TRUE AND ends_at > CURRENT_TIMESTAMP ORDER BY id DESC LIMIT 1",
        fetch='one'
    )


def stop_flash_bonus(bonus_id=None):
    """إيقاف فلاش بونص."""
    if bonus_id:
        DatabaseManager.execute_query("UPDATE flash_bonuses SET is_active = FALSE WHERE id = %s", (int(bonus_id),))
    else:
        DatabaseManager.execute_query("UPDATE flash_bonuses SET is_active = FALSE WHERE is_active = TRUE")


def get_recent_flash_bonuses(limit=10):
    return DatabaseManager.execute_query_dict(
        "SELECT * FROM flash_bonuses ORDER BY id DESC LIMIT %s",
        (int(limit),), fetch='all'
    ) or []
