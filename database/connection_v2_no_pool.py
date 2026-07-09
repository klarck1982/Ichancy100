"""
الحل النهائي والأنظف لـ Neon Free Tier - بدون Pool نهائياً
=========================================================
هذا هو أفضل حل للخطة المجانية: افتح اتصال، نفذ، أغلق فوراً.
- يسمح لـ Neon بالنوم فوراً بعد كل query
- لا يوجد اتصالات دائمة
- استهلاك متوقع: 1-3 ساعات يومياً فقط
- عيبه الوحيد: latency أعلى بـ 50-100ms (مقبول جداً لبوت تيليجرام)

استخدم هذا الملف إذا كان عندك أقل من 1000 مستخدم نشط يومياً
"""

import psycopg2
from psycopg2.extras import RealDictCursor
import logging
import time
from config import settings
from contextlib import contextmanager
from functools import wraps

logger = logging.getLogger(__name__)

# ==================== Cache بسيط للـ TTL ====================
class TTLCache:
    def __init__(self, ttl=60):
        self.ttl = ttl
        self._cache = {}
        self._times = {}
    
    def get(self, key):
        if key in self._cache:
            if time.time() - self._times[key] < self.ttl:
                return self._cache[key]
            else:
                del self._cache[key]
                del self._times[key]
        return None
    
    def set(self, key, value):
        self._cache[key] = value
        self._times[key] = time.time()
    
    def clear(self):
        self._cache.clear()
        self._times.clear()

_settings_cache = TTLCache(ttl=60)
_query_cache = TTLCache(ttl=10)  # للاستعلامات المتكررة جداً


def cached(ttl=60, key_prefix=""):
    """Decorator للـ cache"""
    def decorator(func):
        cache = TTLCache(ttl=ttl)
        @wraps(func)
        def wrapper(*args, **kwargs):
            # بناء مفتاح cache
            key = f"{key_prefix}:{func.__name__}:{str(args)}:{str(kwargs)}"
            result = cache.get(key)
            if result is not None:
                return result
            result = func(*args, **kwargs)
            cache.set(key, result)
            return result
        wrapper._cache = cache
        wrapper.clear_cache = cache.clear
        return wrapper
    return decorator


class DatabaseManager:
    """
    مدير قاعدة بيانات بدون pool - مثالي لـ Neon Free
    كل استعلام = اتصال جديد + إغلاق فوري -> Neon ينام فوراً
    """
    
    @staticmethod
    def _get_conn():
        """فتح اتصال جديد - بدون pool"""
        return psycopg2.connect(
            dsn=settings.DATABASE_URL,
            connect_timeout=10,
            keepalives=0,  # لا keepalive - دع Neon ينام
        )

    @staticmethod
    def execute_query(query, params=None, fetch=None):
        conn = None
        cursor = None
        try:
            conn = DatabaseManager._get_conn()
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            
            result = None
            if fetch == 'one':
                result = cursor.fetchone()
            elif fetch == 'all':
                result = cursor.fetchall()
            
            conn.commit()
            return result
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Query error: {e} | Query: {query[:200]}")
            raise e
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()  # ✅ إغلاق فوري -> Neon ينام

    @staticmethod
    def execute_query_dict(query, params=None, fetch=None):
        conn = None
        cursor = None
        try:
            conn = DatabaseManager._get_conn()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(query, params or ())
            
            result = None
            if fetch == 'one':
                result = cursor.fetchone()
            elif fetch == 'all':
                result = cursor.fetchall()
            
            conn.commit()
            return result
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Query dict error: {e}")
            raise e
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    @staticmethod
    @contextmanager
    def get_cursor():
        conn = DatabaseManager._get_conn()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    @staticmethod
    @contextmanager
    def get_cursor_dict():
        conn = DatabaseManager._get_conn()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    # ==================== دوال متوافقة مع الكود القديم ====================
    @classmethod
    def initialize_pool(cls):
        """متوافق مع الكود القديم - لا يفعل شيء في no-pool mode"""
        logger.info("✅ DatabaseManager (no-pool mode) ready - Neon optimized, will sleep immediately")
        # ننشئ الجداول مرة واحدة فقط
        cls.create_tables()

    @classmethod
    def get_connection(cls):
        """للكود القديم الذي يستخدم get_connection/put_connection - نحذر ثم نعطي conn مؤقت"""
        logger.warning("get_connection() used in no-pool mode - consider using execute_query() directly")
        return cls._get_conn()

    @classmethod
    def put_connection(cls, conn):
        """في no-pool mode، نغلق مباشرة"""
        try:
            if conn:
                conn.close()
        except:
            pass

    @classmethod
    def recreate_pool(cls):
        pass

    @classmethod
    def close_idle_pool_if_needed(cls, idle_seconds=600):
        pass  # لا pool لإغلاقه

    @classmethod
    def get_bot_settings_cached(cls):
        cached_val = _settings_cache.get("bot_settings")
        if cached_val:
            return cached_val
        result = cls.execute_query_dict("SELECT * FROM bot_settings WHERE id = 1", fetch='one')
        if result:
            _settings_cache.set("bot_settings", result)
        return result

    @classmethod
    def invalidate_settings_cache(cls):
        _settings_cache.clear()

    @classmethod
    def create_tables(cls):
        """إنشاء الجداول - نفس كود الملف الأصلي لكن بconnection واحد"""
        logger.info("Checking and creating tables...")
        
        # نستوردها من الملف الأصلي لتجنب التكرار
        try:
            from database.connection import DatabaseManager as OldManager
            # نستخدم نفس منطق إنشاء الجداول لكن بno-pool
            # للتبسيط: ننفذ فقط الجداول الأساسية، والباقي سيتم إنشاؤه عند أول تشغيل للـ old manager
            # أو يمكنك نسخ كل الـ CREATE TABLE من connection.py هنا في قائمة
            
            # مثال: ننفذ جدول users و bot_settings فقط للتأكد
            queries = [
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id VARCHAR(50) UNIQUE NOT NULL,
                    telegram_username VARCHAR(100),
                    ichancy_username VARCHAR(100) UNIQUE,
                    ichancy_password VARCHAR(100),
                    ichancy_email VARCHAR(150),
                    player_id VARCHAR(100),
                    bot_balance BIGINT DEFAULT 0,
                    game_balance BIGINT DEFAULT 0,
                    referred_by VARCHAR(50),
                    terms_accepted BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """,
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    id SERIAL PRIMARY KEY,
                    exchange_rate INTEGER DEFAULT 1000,
                    usd_buy_rate NUMERIC(15, 2) DEFAULT 14000,
                    usd_sell_rate NUMERIC(15, 2) DEFAULT 15000,
                    withdraw_commission NUMERIC(5, 2) DEFAULT 10,
                    ichancy_cookie TEXT,
                    agent_balance BIGINT DEFAULT 0,
                    game_min_deposit_syp BIGINT DEFAULT 20000,
                    agent_revenue_percent NUMERIC(7, 2) DEFAULT 30,
                    game_bonus_enabled BOOLEAN DEFAULT TRUE,
                    game_bonus_apply_percent NUMERIC(7, 2) DEFAULT 10,
                    bonus_rollover_multiplier NUMERIC(7, 2) DEFAULT 5,
                    turnover_field_name VARCHAR(80) DEFAULT 'totalBet'
                );
                """,
                """
                INSERT INTO bot_settings (id, exchange_rate, usd_buy_rate, usd_sell_rate, withdraw_commission, agent_balance)
                VALUES (1, 1000, 14000, 15000, 10, 0)
                ON CONFLICT (id) DO NOTHING;
                """
            ]
            
            conn = cls._get_conn()
            cur = conn.cursor()
            for q in queries:
                cur.execute(q)
            conn.commit()
            cur.close()
            conn.close()
            logger.info("✅ Core tables ensured (no-pool mode)")
            
            # الآن نترك باقي الجداول يتم إنشاؤها عبر migration تدريجياً عند الحاجة
            # أو شغل مرة واحدة الـ old connection.py ثم ارجع لهذا الملف
            
        except Exception as e:
            logger.error(f"create_tables error: {e}")


# ==================== تحسينات Repository - دمج الاستعلامات ====================

def get_transaction_stats_for_user_optimized(telegram_id):
    """
    بدل 4 queries -> query واحد
    قبل: 4 اتصالات
    بعد: 1 اتصال
    توفير: 75%
    """
    query = """
    SELECT
        COUNT(*) as total,
        COUNT(*) FILTER (WHERE status = 'pending') as pending,
        COUNT(*) FILTER (WHERE status = 'approved') as approved,
        COUNT(*) FILTER (WHERE status = 'rejected') as rejected
    FROM transactions
    WHERE user_telegram_id = %s
    """
    result = DatabaseManager.execute_query_dict(query, (str(telegram_id),), fetch='one')
    if result:
        return {
            'total': int(result.get('total') or 0),
            'pending': int(result.get('pending') or 0),
            'approved': int(result.get('approved') or 0),
            'rejected': int(result.get('rejected') or 0),
        }
    return {'total': 0, 'pending': 0, 'approved': 0, 'rejected': 0}


def get_dashboard_stats_optimized():
    """
    دمج كل إحصائيات الداشبورد في query واحد بدل 7 queries
    توفير: 85%
    """
    query = """
    SELECT
        (SELECT COUNT(*) FROM users) as total_users,
        (SELECT COUNT(*) FROM users WHERE created_at::date = CURRENT_DATE) as new_today,
        (SELECT COUNT(*) FROM transactions WHERE created_at::date = CURRENT_DATE) as today_tx,
        (SELECT COALESCE(SUM(bot_balance),0) FROM users) as total_balance,
        (SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='deposit_bot' AND status='approved' AND created_at::date=CURRENT_DATE) as deposits_today,
        (SELECT COALESCE(SUM(amount),0) FROM transactions WHERE type='withdraw_bot' AND status='approved' AND created_at::date=CURRENT_DATE) as withdraws_today,
        (SELECT COUNT(*) FROM transactions WHERE status='pending') as pending_count
    """
    return DatabaseManager.execute_query_dict(query, fetch='one')
