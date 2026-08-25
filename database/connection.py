import psycopg2
from psycopg2 import pool
import logging
import time
import threading
from config import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    _pool = None
    _tables_checked = False
    _pool_lock = threading.Lock()
    _last_activity = time.time()
    _settings_cache = None
    _settings_cache_time = 0
    SETTINGS_CACHE_TTL = 60

    @classmethod
    def get_bot_settings_cached(cls):
        now = time.time()
        if cls._settings_cache and (now - cls._settings_cache_time) < cls.SETTINGS_CACHE_TTL:
            return cls._settings_cache
        result = cls.execute_query_dict("SELECT * FROM bot_settings WHERE id = 1", fetch='one')
        if result:
            cls._settings_cache = result
            cls._settings_cache_time = now
        return result

    @classmethod
    def invalidate_settings_cache(cls):
        cls._settings_cache = None
        cls._settings_cache_time = 0

    @classmethod
    def initialize_pool(cls):
        if not cls._pool:
            with cls._pool_lock:
                if not cls._pool:
                    try:
                        # ✅ تحسين لـ Neon Serverless الخطة المجانية:
                        # minconn=0 يتيح لقاعدة البيانات الدخول في وضع السكون (Sleep) عند الخمول
                        # maxconn=3 يمنع استنزاف المسبح وموارد السيرفر
                        minconn = int(getattr(settings, 'DB_POOL_MINCONN', 0) or 0)
                        maxconn = max(1, int(getattr(settings, 'DB_POOL_MAXCONN', 3) or 3))
                        cls._pool = pool.ThreadedConnectionPool(
                            minconn=minconn,
                            maxconn=maxconn,
                            dsn=settings.DATABASE_URL
                        )
                        logger.info(f"Database connection pool initialized successfully (min={minconn}, max={maxconn}).")
                        if not cls._tables_checked:
                            cls.create_tables()
                            cls._tables_checked = True
                    except Exception as e:
                        logger.error(f"Error initializing database pool: {e}")
                        raise e

    @classmethod
    def get_connection(cls):
        if not cls._pool:
            cls.initialize_pool()
        try:
            conn = cls._pool.getconn()
            if getattr(settings, 'DB_VALIDATE_CONNECTION', False):
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.close()
            return conn
        except Exception as e:
            logger.warning(f"Dead connection detected in pool ({e})! Recreating connection pool...")
            cls.recreate_pool()
            return cls._pool.getconn()

    @classmethod
    def put_connection(cls, conn):
        if cls._pool and conn:
            try:
                cls._pool.putconn(conn)
            except Exception:
                pass

    @classmethod
    def recreate_pool(cls):
        with cls._pool_lock:
            if cls._pool:
                try:
                    cls._pool.closeall()
                except Exception:
                    pass
                cls._pool = None
        cls.initialize_pool()

    @classmethod
    def close_idle_pool_if_needed(cls, idle_seconds=180):
        """إغلاق المسبح إذا لم يكن هناك نشاط لأكثر من 3 دقائق للسماح لقاعدة بيانات Neon بالدخول في وضع السكون السريع."""
        with cls._pool_lock:
            if not cls._pool:
                return
            if time.time() - cls._last_activity > idle_seconds:
                try:
                    logger.info(f"💤 No DB activity for {idle_seconds}s, closing pool to let Neon sleep.")
                    cls._pool.closeall()
                    cls._pool = None
                except Exception as e:
                    logger.warning(f"Failed to close idle pool: {e}")

    @classmethod
    def execute_query(cls, query, params=None, fetch=None):
        conn = None
        cursor = None
        result = None
        retry_count = 0
        while retry_count < 3:
            try:
                conn = cls.get_connection()
                cursor = conn.cursor()
                cursor.execute(query, params or ())

                if fetch == 'one':
                    result = cursor.fetchone()
                elif fetch == 'all':
                    result = cursor.fetchall()

                conn.commit()
                cls._last_activity = time.time()
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as conn_err:
                logger.warning(f"Database connection lost: {conn_err}. Retrying execution...")
                cls.recreate_pool()
                retry_count += 1
                time.sleep(0.5)
            except Exception as e:
                if conn:
                    conn.rollback()
                logger.error(f"Database query execution error: {e}\nQuery: {query}")
                raise e
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    cls.put_connection(conn)
        return result

    @classmethod
    def execute_query_dict(cls, query, params=None, fetch=None):
        conn = None
        cursor = None
        result = None
        retry_count = 0
        while retry_count < 3:
            try:
                conn = cls.get_connection()
                from psycopg2.extras import RealDictCursor
                cursor = conn.cursor(cursor_factory=RealDictCursor)
                cursor.execute(query, params or ())

                if fetch == 'one':
                    result = cursor.fetchone()
                elif fetch == 'all':
                    result = cursor.fetchall()

                conn.commit()
                cls._last_activity = time.time()
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as conn_err:
                logger.warning(f"Database connection lost: {conn_err}. Retrying execution...")
                cls.recreate_pool()
                retry_count += 1
                time.sleep(0.5)
            except Exception as e:
                if conn:
                    conn.rollback()
                logger.error(f"Database query execution error: {e}")
                raise e
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    cls.put_connection(conn)
        return result

    @classmethod
    def create_tables(cls):
        logger.info("Checking and creating tables if not exist...")

        users_table = """
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
        """

        referrals_table = """
        CREATE TABLE IF NOT EXISTS referrals (
            id SERIAL PRIMARY KEY,
            referrer_telegram_id VARCHAR(50) NOT NULL,
            referred_telegram_id VARCHAR(50) UNIQUE NOT NULL,
            is_active BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        transactions_table = """
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_telegram_id VARCHAR(50) NOT NULL,
            type VARCHAR(50) NOT NULL,
            payment_method VARCHAR(50),
            amount NUMERIC(15, 2) NOT NULL,
            transfer_number VARCHAR(255),
            status VARCHAR(50) DEFAULT 'pending',
            rejection_reason TEXT,
            reviewed_by VARCHAR(50),
            reviewed_at TIMESTAMP WITH TIME ZONE,
            original_amount NUMERIC(15, 2),
            original_currency VARCHAR(20),
            converted_amount_syp NUMERIC(15, 2),
            external_ref VARCHAR(255),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        alter_transactions_columns = [
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS rejection_reason TEXT;",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(50);",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP WITH TIME ZONE;",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS original_amount NUMERIC(15, 2);",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS original_currency VARCHAR(20);",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS converted_amount_syp NUMERIC(15, 2);",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS external_ref VARCHAR(255);",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cashback_amount_syp BIGINT DEFAULT 0;",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS checkin_amount_syp BIGINT DEFAULT 0;",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS bonus_base_added_syp BIGINT DEFAULT 0;",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cashier_profile_id INTEGER;",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cashier_profile_name VARCHAR(120);",
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_destination TEXT;",
        ]

        gifts_table = """
        CREATE TABLE IF NOT EXISTS gifts (
            id SERIAL PRIMARY KEY,
            sender_telegram_id VARCHAR(50) NOT NULL,
            receiver_telegram_id VARCHAR(50),
            code VARCHAR(100) UNIQUE NOT NULL,
            amount BIGINT NOT NULL,
            is_redeemed BOOLEAN DEFAULT FALSE,
            redeemed_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        gift_campaigns_table = """
        CREATE TABLE IF NOT EXISTS gift_campaigns (
            id SERIAL PRIMARY KEY,
            name VARCHAR(160) NOT NULL,
            reward_type VARCHAR(20) NOT NULL DEFAULT 'bonus',
            code_mode VARCHAR(20) NOT NULL DEFAULT 'unique',
            reward_amount BIGINT NOT NULL,
            max_redemptions INTEGER NOT NULL,
            requires_ichancy BOOLEAN DEFAULT TRUE,
            status VARCHAR(20) DEFAULT 'active',
            starts_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
            created_by VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        gift_campaign_codes_table = """
        CREATE TABLE IF NOT EXISTS gift_campaign_codes (
            id BIGSERIAL PRIMARY KEY,
            campaign_id INTEGER NOT NULL REFERENCES gift_campaigns(id) ON DELETE CASCADE,
            code VARCHAR(120) UNIQUE NOT NULL,
            max_redemptions INTEGER NOT NULL DEFAULT 1,
            redemptions_count INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        gift_campaign_redemptions_table = """
        CREATE TABLE IF NOT EXISTS gift_campaign_redemptions (
            id BIGSERIAL PRIMARY KEY,
            campaign_id INTEGER NOT NULL REFERENCES gift_campaigns(id) ON DELETE CASCADE,
            code_id BIGINT NOT NULL REFERENCES gift_campaign_codes(id) ON DELETE CASCADE,
            user_telegram_id VARCHAR(50) NOT NULL,
            reward_type VARCHAR(20) NOT NULL,
            reward_amount BIGINT NOT NULL,
            redeemed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(campaign_id, user_telegram_id)
        );
        """

        bot_settings_table = """
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
            turnover_field_name VARCHAR(80) DEFAULT 'totalBet',
            syriatel_auto_mode VARCHAR(30) DEFAULT 'off',
            syriatel_auto_channel_id VARCHAR(80)
        );
        """

        referral_commissions_table = """
        CREATE TABLE IF NOT EXISTS referral_commissions (
            id SERIAL PRIMARY KEY,
            referrer_telegram_id VARCHAR(50) NOT NULL,
            referred_telegram_id VARCHAR(50) NOT NULL,
            transaction_id INTEGER UNIQUE NOT NULL,
            deposit_amount_syp NUMERIC(15, 2) NOT NULL,
            active_referrals_count INTEGER DEFAULT 0,
            commission_percent NUMERIC(7, 2) NOT NULL,
            commission_amount BIGINT NOT NULL,
            status VARCHAR(30) DEFAULT 'credited',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        bonus_rules_table = """
        CREATE TABLE IF NOT EXISTS bonus_rules (
            id SERIAL PRIMARY KEY,
            title VARCHAR(150) NOT NULL,
            percent NUMERIC(7, 2) NOT NULL,
            payment_method VARCHAR(50) DEFAULT 'all',
            min_amount_syp NUMERIC(15, 2) DEFAULT 0,
            max_bonus_syp NUMERIC(15, 2) DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            created_by VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            disabled_at TIMESTAMP WITH TIME ZONE
        );
        """

        # 🆕 إعدادات عناوين الإيداع القابلة للتعديل من لوحة الأدمن
        payment_settings_table = """
        CREATE TABLE IF NOT EXISTS payment_settings (
            payment_method VARCHAR(50) PRIMARY KEY,
            address TEXT,
            updated_by VARCHAR(50),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        # ملفات المشرفين/الكاشير لعناوين الدفع المحلية.
        cashier_profiles_table = """
        CREATE TABLE IF NOT EXISTS cashier_profiles (
            id SERIAL PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            telegram_id VARCHAR(50),
            sham_syp_address TEXT NOT NULL,
            sham_usd_address TEXT NOT NULL,
            syriatel_address TEXT NOT NULL,
            mtn_address TEXT NOT NULL,
            is_enabled BOOLEAN DEFAULT TRUE,
            created_by VARCHAR(50),
            updated_by VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        cashier_switch_audit_table = """
        CREATE TABLE IF NOT EXISTS cashier_switch_audit (
            id BIGSERIAL PRIMARY KEY,
            previous_profile_id INTEGER REFERENCES cashier_profiles(id) ON DELETE SET NULL,
            new_profile_id INTEGER REFERENCES cashier_profiles(id) ON DELETE SET NULL,
            switched_by VARCHAR(50) NOT NULL,
            switched_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        # 🆕 جدول الرفض المخصص المؤقت (يحل مشكلة FSM عبر القنوات)
        pending_rejections_table = """
        CREATE TABLE IF NOT EXISTS pending_rejections (
            admin_id VARCHAR(50) PRIMARY KEY,
            tx_id INTEGER NOT NULL,
            tx_type VARCHAR(50) NOT NULL,
            channel_chat_id BIGINT,
            channel_message_id BIGINT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        broadcasts_table = """
        CREATE TABLE IF NOT EXISTS broadcasts (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200),
            message TEXT NOT NULL,
            audience VARCHAR(50) DEFAULT 'all',
            message_type VARCHAR(50) DEFAULT 'announcement',
            created_by VARCHAR(50),
            status VARCHAR(30) DEFAULT 'draft',
            total_targets INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP WITH TIME ZONE,
            finished_at TIMESTAMP WITH TIME ZONE
        );
        """

        support_tickets_table = """
        CREATE TABLE IF NOT EXISTS support_tickets (
            id SERIAL PRIMARY KEY,
            user_telegram_id VARCHAR(50) NOT NULL,
            status VARCHAR(30) DEFAULT 'open',
            last_message TEXT,
            last_message_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP WITH TIME ZONE
        );
        """



        prediction_cards_table = """
        CREATE TABLE IF NOT EXISTS prediction_cards (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            match_code VARCHAR(100),
            team_a VARCHAR(120) NOT NULL,
            team_b VARCHAR(120) NOT NULL,
            options_json TEXT NOT NULL,
            max_predictions INTEGER DEFAULT 0,
            reward_syp BIGINT DEFAULT 0,
            status VARCHAR(30) DEFAULT 'open',
            closes_at TIMESTAMP WITH TIME ZONE,
            created_by VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP WITH TIME ZONE,
            settled_at TIMESTAMP WITH TIME ZONE,
            winning_option VARCHAR(120)
        );
        """

        prediction_entries_table = """
        CREATE TABLE IF NOT EXISTS prediction_entries (
            id SERIAL PRIMARY KEY,
            card_id INTEGER REFERENCES prediction_cards(id) ON DELETE CASCADE,
            user_telegram_id VARCHAR(50) NOT NULL,
            selected_option VARCHAR(120) NOT NULL,
            reward_amount BIGINT DEFAULT 0,
            is_winner BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(card_id, user_telegram_id)
        );
        """


        contests_table = """
        CREATE TABLE IF NOT EXISTS contests (
            id SERIAL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            description TEXT,
            contest_type VARCHAR(50) DEFAULT 'first_approved',
            reward_type VARCHAR(30) DEFAULT 'gift_code',
            reward_amount BIGINT DEFAULT 0,
            winners_limit INTEGER DEFAULT 1,
            requires_proof BOOLEAN DEFAULT TRUE,
            status VARCHAR(30) DEFAULT 'open',
            created_by VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP WITH TIME ZONE
        );
        """

        contest_entries_table = """
        CREATE TABLE IF NOT EXISTS contest_entries (
            id SERIAL PRIMARY KEY,
            contest_id INTEGER REFERENCES contests(id) ON DELETE CASCADE,
            user_telegram_id VARCHAR(50) NOT NULL,
            proof_text TEXT,
            proof_type VARCHAR(50) DEFAULT 'text',
            proof_file_id TEXT,
            status VARCHAR(30) DEFAULT 'pending',
            gift_code VARCHAR(120),
            reward_amount BIGINT DEFAULT 0,
            reviewed_by VARCHAR(50),
            reviewed_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contest_id, user_telegram_id)
        );
        """
        support_messages_table = """
        CREATE TABLE IF NOT EXISTS support_messages (
            id SERIAL PRIMARY KEY,
            ticket_id INTEGER REFERENCES support_tickets(id) ON DELETE CASCADE,
            sender_type VARCHAR(30) NOT NULL,
            sender_id VARCHAR(50),
            message_text TEXT,
            content_type VARCHAR(50),
            telegram_message_id BIGINT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        # 🆕 (Update 12) جدول الحضور اليومي (Daily Check-in Streak)
        daily_checkins_table = """
        CREATE TABLE IF NOT EXISTS daily_checkins (
            id SERIAL PRIMARY KEY,
            telegram_id VARCHAR(50) UNIQUE NOT NULL,
            last_checkin_date DATE NOT NULL,
            current_streak INTEGER DEFAULT 0,
            total_checkins INTEGER DEFAULT 0,
            total_rewards BIGINT DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        # 🆕 (Update 12) جدول فلاش البونص (Flash Bonus - محدود الوقت)
        flash_bonuses_table = """
        CREATE TABLE IF NOT EXISTS flash_bonuses (
            id SERIAL PRIMARY KEY,
            percent NUMERIC(7, 2) NOT NULL,
            payment_method VARCHAR(50) DEFAULT 'all',
            starts_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            ends_at TIMESTAMP WITH TIME ZONE NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_by VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        # 🆕 (Update 13) جدول إعدادات ميزات المستخدم (قيم المكافآت، التفعيل)
        user_features_settings_table = """
        CREATE TABLE IF NOT EXISTS user_features_settings (
            id SERIAL PRIMARY KEY,
            checkin_enabled BOOLEAN DEFAULT TRUE,
            checkin_rewards_json TEXT DEFAULT '[0, 500, 1000, 1500, 2000, 2500, 3000, 10000]',
            checkin_min_deposit BIGINT DEFAULT 50000,
            leaderboard_enabled BOOLEAN DEFAULT TRUE,
            leaderboard_type VARCHAR(20) DEFAULT 'all_time',
            bonus_min_transfer BIGINT DEFAULT 20000,
            bonus_deposit_threshold BIGINT DEFAULT 100000,
            bonus_deposit_days INT DEFAULT 30,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        alter_settings_agent_balance = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS agent_balance BIGINT DEFAULT 0;"
        alter_settings_cookie_update = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS last_cookie_update TIMESTAMP WITH TIME ZONE;"
        alter_settings_referrals_enabled = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS referrals_enabled BOOLEAN DEFAULT TRUE;"
        alter_settings_game_min_deposit = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS game_min_deposit_syp BIGINT DEFAULT 20000;"
        alter_settings_agent_revenue = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS agent_revenue_percent NUMERIC(7, 2) DEFAULT 30;"
        # 🎁 إعدادات إرفاق بونص اللعب عند شحن حساب iChancy (معزولة عن Flash Bonus)
        alter_settings_game_bonus_enabled = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS game_bonus_enabled BOOLEAN DEFAULT TRUE;"
        alter_settings_game_bonus_apply_percent = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS game_bonus_apply_percent NUMERIC(7, 2) DEFAULT 10;"
        # أعمدة قديمة للتوافق فقط — لم نعد نعتمد على التدوير في نظام البونص الجديد
        alter_settings_bonus_rollover = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS bonus_rollover_multiplier NUMERIC(7, 2) DEFAULT 5;"
        alter_settings_turnover_field = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS turnover_field_name VARCHAR(80) DEFAULT 'totalBet';"
        # إعدادات أتمتة إيداع Syriatel Cash من لوحة المشرف
        alter_settings_syriatel_auto_mode = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS syriatel_auto_mode VARCHAR(30) DEFAULT 'off';"
        alter_settings_syriatel_auto_channel = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS syriatel_auto_channel_id VARCHAR(80);"
        # 🆕 حدود الإيداع والسحب الدنيا — يمكن للمشرف تعديلها من Dashboard
        alter_settings_min_deposit_syp = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS min_deposit_syp BIGINT DEFAULT 20000;"
        alter_settings_min_deposit_usd = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS min_deposit_usd INT DEFAULT 5;"
        alter_settings_min_withdraw_syp = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS min_withdraw_syp BIGINT DEFAULT 25000;"
        alter_settings_min_withdraw_usd = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS min_withdraw_usd INT DEFAULT 10;"
        # 🆕 نسخة الليرة السورية (old = قديمة, new = جديدة ÷100)
        alter_settings_syp_version = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS syp_version VARCHAR(10) DEFAULT 'old';"
        alter_settings_alert_threshold = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS agent_balance_alert_threshold BIGINT DEFAULT 100000;"
        alter_settings_active_cashier = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS active_cashier_profile_id INTEGER;"
        alter_settings_maintenance = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS maintenance_mode BOOLEAN DEFAULT FALSE;"
        alter_settings_deposits_enabled = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS deposits_enabled BOOLEAN DEFAULT TRUE;"
        alter_settings_withdrawals_enabled = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS withdrawals_enabled BOOLEAN DEFAULT TRUE;"
        alter_settings_game_transfers_enabled = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS game_transfers_enabled BOOLEAN DEFAULT TRUE;"

        # 🆕 (Update 14) رصيد المكافآت للمستخدم (غير قابل للسحب)
        alter_users_bonus_balance = "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_balance BIGINT DEFAULT 0;"
        # 🆕 مبلغ البونص المحوّل إلى اللعبة والذي يحتاج تدوير قبل السحب
        alter_users_game_bonus_amount = "ALTER TABLE users ADD COLUMN IF NOT EXISTS game_bonus_amount BIGINT DEFAULT 0;"
        # قاعدة الرصيد النقدي التي يرتبط بها رصيد البونص (لصرفه نسبياً عند شحن اللعبة)
        alter_users_bonus_base_balance = "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_base_balance BIGINT DEFAULT 0;"
        # رصيد أرباح الإحالات القابل للسحب (Revenue Share)
        alter_users_affiliate_balance = "ALTER TABLE users ADD COLUMN IF NOT EXISTS affiliate_balance BIGINT DEFAULT 0;"
        # كاش باك مستحق ينتظر أول شحن لعبة ناجح ليُضاف إلى iChancy
        alter_users_cashback_pending_balance = "ALTER TABLE users ADD COLUMN IF NOT EXISTS cashback_pending_balance BIGINT DEFAULT 0;"
        # مكافأة حضور مستحقة تنتظر أول شحن لعبة ناجح
        alter_users_checkin_pending_balance = "ALTER TABLE users ADD COLUMN IF NOT EXISTS checkin_pending_balance BIGINT DEFAULT 0;"
        
        # 🆕 (Update 14 Fix) ALTER TABLE لإضافة أعمدة شروط المكافآت لجدول موجود
        alter_feat_bonus_min = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS bonus_min_transfer BIGINT DEFAULT 20000;"
        alter_feat_bonus_threshold = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS bonus_deposit_threshold BIGINT DEFAULT 100000;"
        alter_feat_bonus_days = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS bonus_deposit_days INT DEFAULT 30;"
        alter_feat_checkin_min_deposit = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS checkin_min_deposit BIGINT DEFAULT 50000;"
        alter_feat_checkin_cycle_days = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS checkin_cycle_days INT DEFAULT 30;"
        alter_feat_checkin_completion_reward = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS checkin_completion_reward BIGINT DEFAULT 20000;"

        # 🆕 (Update 18) لوحة المتصدرين الأسبوعية حسب حجم المراهنات (Turnover Leaderboard)
        # لقطات دورية من iChancy: baseline = الإجمالي عند بداية أسبوع القياس، last = آخر إجمالي مرصود.
        turnover_snapshots_table = """
        CREATE TABLE IF NOT EXISTS turnover_leaderboard_snapshots (
            player_id VARCHAR(100) PRIMARY KEY,
            telegram_id VARCHAR(50) NOT NULL,
            username VARCHAR(100),
            baseline_turnover BIGINT DEFAULT 0,
            last_turnover BIGINT DEFAULT 0,
            cycle_start DATE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

        # أرشيف نتائج الأسابيع + دفع الجوائز (UNIQUE يمنع أرشفة/دفع نفس الأسبوع مرتين)
        turnover_results_table = """
        CREATE TABLE IF NOT EXISTS turnover_leaderboard_results (
            id SERIAL PRIMARY KEY,
            week_start DATE NOT NULL,
            rank INTEGER NOT NULL,
            player_id VARCHAR(100),
            telegram_id VARCHAR(50) NOT NULL,
            username VARCHAR(100),
            weekly_turnover BIGINT DEFAULT 0,
            prize_syp BIGINT DEFAULT 0,
            credited BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(week_start, telegram_id)
        );
        """

        alter_settings_lb_prize_1 = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS lb_prize_1 BIGINT DEFAULT 0;"
        alter_settings_lb_prize_2 = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS lb_prize_2 BIGINT DEFAULT 0;"
        alter_settings_lb_prize_3 = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS lb_prize_3 BIGINT DEFAULT 0;"
        alter_settings_lb_min_weekly = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS lb_min_weekly_turnover BIGINT DEFAULT 0;"
        alter_settings_lb_auto_credit = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS lb_auto_credit BOOLEAN DEFAULT TRUE;"
        alter_settings_lb_last_settled = "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS lb_last_settled_week VARCHAR(20) DEFAULT ''"

        # 🆕 (Update 20 / Performance) مؤشرات الأداء — IF NOT EXISTS آمنة للتكرار عند كل إقلاع.
        # ملاحظة: لا نستخدم مؤشراً وظيفياً على created_at::date لأنه غير IMMUTABLE على timestamptz —
        # بدلاً منه مسند نطاقي (created_at >= day AND < day+1) يستفيد من هذا المؤشر B-tree القياسي.
        idx_tx_created_at = "CREATE INDEX IF NOT EXISTS idx_tx_created_at ON transactions(created_at);"
        idx_tx_type_status = "CREATE INDEX IF NOT EXISTS idx_tx_type_status ON transactions(type, status);"
        idx_tx_user = "CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_telegram_id);"
        idx_support_status = "CREATE INDEX IF NOT EXISTS idx_support_status ON support_tickets(status);"
        idx_tlb_snap_cycle = "CREATE INDEX IF NOT EXISTS idx_tlb_snap_cycle ON turnover_leaderboard_snapshots(cycle_start);"
        idx_tlb_results_week = "CREATE INDEX IF NOT EXISTS idx_tlb_results_week ON turnover_leaderboard_results(week_start);"

        # 🆕 (Update 15) عجلة الحظ - جدول تتبع الدورات
        wheel_spins_table = """
        CREATE TABLE IF NOT EXISTS wheel_spins (
            id SERIAL PRIMARY KEY,
            telegram_id VARCHAR(50) NOT NULL,
            deposit_tx_id INTEGER,
            segment_index INT NOT NULL,
            reward_percent NUMERIC(7,2) DEFAULT 0,
            reward_amount BIGINT DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(deposit_tx_id)
        );
        """

        # 🆕 (Update 15) إعدادات العجلة
        alter_feat_wheel_enabled = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS wheel_enabled BOOLEAN DEFAULT TRUE;"
        alter_feat_wheel_segments = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS wheel_segments_json TEXT DEFAULT '[[0,30],[2,20],[0,10],[5,15],[10,10],[0,5],[15,7],[25,3]]';"
        alter_feat_wheel_min_deposit = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS wheel_min_deposit BIGINT DEFAULT 50000;"
        alter_feat_wheel_max_reward = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS wheel_max_reward BIGINT DEFAULT 30000;"

        # 🆕 (Update 16) نظام VIP
        alter_feat_vip_enabled = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS vip_enabled BOOLEAN DEFAULT TRUE;"
        alter_feat_vip_tiers_json = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS vip_tiers_json TEXT DEFAULT '[[0,0,0],[500000,1,10000],[2000000,2,50000],[5000000,3,200000]]';"

        # 🆕 (Update 17) الكاش باك الأسبوعي
        alter_feat_cashback_enabled = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS cashback_enabled BOOLEAN DEFAULT TRUE;"
        alter_feat_cashback_pct = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS cashback_pct NUMERIC(7,2) DEFAULT 5;"
        alter_feat_cashback_min = "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS cashback_min_loss BIGINT DEFAULT 50000;"

        cashback_payouts_table = """
        CREATE TABLE IF NOT EXISTS cashback_payouts (
            id SERIAL PRIMARY KEY,
            telegram_id VARCHAR(50) NOT NULL,
            week_start DATE NOT NULL,
            week_end DATE NOT NULL,
            total_deposited BIGINT DEFAULT 0,
            total_withdrawn BIGINT DEFAULT 0,
            net_loss BIGINT DEFAULT 0,
            cashback_amount BIGINT DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(telegram_id, week_start)
        );
        """

        affiliate_weekly_commissions_table = """
        CREATE TABLE IF NOT EXISTS affiliate_weekly_commissions (
            id SERIAL PRIMARY KEY,
            referrer_telegram_id VARCHAR(50) NOT NULL,
            referred_telegram_id VARCHAR(50) NOT NULL,
            week_start DATE NOT NULL,
            week_end DATE NOT NULL,
            total_deposited BIGINT DEFAULT 0,
            total_withdrawn BIGINT DEFAULT 0,
            ending_game_balance BIGINT DEFAULT 0,
            net_loss BIGINT DEFAULT 0,
            commission_percent NUMERIC(7,2) DEFAULT 0,
            commission_amount BIGINT DEFAULT 0,
            status VARCHAR(30) DEFAULT 'paid',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(referrer_telegram_id, referred_telegram_id, week_start)
        );
        """
        alter_users_total_deposits = "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_deposits BIGINT DEFAULT 0;"
        alter_users_vip_tier = "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_tier INT DEFAULT 0;"

        insert_default_settings = """
        INSERT INTO bot_settings (id, exchange_rate, usd_buy_rate, usd_sell_rate, withdraw_commission, agent_balance)
        VALUES (1, 1000, 14000, 15000, 10, 0)
        ON CONFLICT (id) DO NOTHING;
        """

        update_game_bonus_defaults = """
        UPDATE bot_settings
        SET game_bonus_enabled = COALESCE(game_bonus_enabled, TRUE),
            game_bonus_apply_percent = COALESCE(game_bonus_apply_percent, 10)
        WHERE id = 1;
        """

        table_queries = [
            users_table,
            referrals_table,
            transactions_table,
            *alter_transactions_columns,
            gifts_table,
            gift_campaigns_table,
            gift_campaign_codes_table,
            gift_campaign_redemptions_table,
            bot_settings_table,
            referral_commissions_table,
            bonus_rules_table,
            payment_settings_table,
            cashier_profiles_table,
            cashier_switch_audit_table,
            # يجب إنشاء جدول إعدادات الميزات قبل أي ALTER عليه
            user_features_settings_table,
            alter_settings_agent_balance,
            alter_settings_cookie_update,
            alter_settings_referrals_enabled,
            alter_settings_game_min_deposit,
            alter_settings_agent_revenue,
            alter_settings_game_bonus_enabled,
            alter_settings_game_bonus_apply_percent,
            alter_settings_bonus_rollover,
            alter_settings_turnover_field,
            alter_settings_syriatel_auto_mode,
            alter_settings_syriatel_auto_channel,
            alter_settings_min_deposit_syp,
            alter_settings_min_deposit_usd,
            alter_settings_min_withdraw_syp,
            alter_settings_min_withdraw_usd,
            alter_settings_syp_version,
            alter_settings_alert_threshold,
            alter_settings_active_cashier,
            alter_settings_maintenance,
            alter_settings_deposits_enabled,
            alter_settings_withdrawals_enabled,
            alter_settings_game_transfers_enabled,
            alter_users_bonus_balance,
            alter_users_game_bonus_amount,
            alter_users_bonus_base_balance,
            alter_users_affiliate_balance,
            alter_users_cashback_pending_balance,
            alter_users_checkin_pending_balance,
            alter_feat_bonus_min,
            alter_feat_bonus_threshold,
            alter_feat_bonus_days,
            alter_feat_checkin_min_deposit,
            alter_feat_checkin_cycle_days,
            alter_feat_checkin_completion_reward,
            # 🆕 (Update 18) لوحة المتصدرين الأسبوعية
            turnover_snapshots_table,
            turnover_results_table,
            alter_settings_lb_prize_1,
            alter_settings_lb_prize_2,
            alter_settings_lb_prize_3,
            alter_settings_lb_min_weekly,
            alter_settings_lb_auto_credit,
            alter_settings_lb_last_settled,
            wheel_spins_table,
            alter_feat_wheel_enabled,
            alter_feat_wheel_segments,
            alter_feat_wheel_min_deposit,
            alter_feat_wheel_max_reward,
            alter_feat_vip_enabled,
            alter_feat_vip_tiers_json,
            alter_feat_cashback_enabled,
            alter_feat_cashback_pct,
            alter_feat_cashback_min,
            cashback_payouts_table,
            affiliate_weekly_commissions_table,
            alter_users_total_deposits,
            alter_users_vip_tier,
            insert_default_settings,
            update_game_bonus_defaults,
            pending_rejections_table,
            broadcasts_table,
            support_tickets_table,
            prediction_cards_table,
            prediction_entries_table,
            contests_table,
            contest_entries_table,
            support_messages_table,
            daily_checkins_table,
            flash_bonuses_table,
            # 🆕 (Update 20 / Performance) أول مؤشرات في قاعدة البيانات:
            # دونها كل مجاميع اليوم والرسم البياني و«المستخدمون الخاملون» فحص تسلسلي كامل.
            # المؤشر الوظيفي على created_at::date يحصر مجاميع «اليوم» في صفوف اليوم فقط.
            idx_tx_created_at,
            idx_tx_type_status,
            idx_tx_user,
            idx_support_status,
            idx_tlb_snap_cycle,
            idx_tlb_results_week,
        ]

        conn = None
        cursor = None
        try:
            conn = cls._pool.getconn()
            cursor = conn.cursor()
            for table_query in table_queries:
                try:
                    cursor.execute(table_query)
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Error executing table creation query: {e}")
            conn.commit()
            logger.info("Tables checked/created successfully in a single batched connection.")
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Error creating tables: {e}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                cls._pool.putconn(conn)
