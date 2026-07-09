import psycopg2
from psycopg2 import pool
import logging
import time
from config import settings

logger = logging.getLogger(__name__)


class DatabaseManager:
    _pool = None
    _last_activity = 0

    @classmethod
    def initialize_pool(cls):
        if not cls._pool:
            try:
                # ✅ FIXED: minconn=0 allows Neon to autosuspend (was 1 - prevented sleep)
                # ✅ FIXED: maxconn=5 instead of 20 (more than enough for Telegram bot, saves resources)
                cls._pool = pool.ThreadedConnectionPool(
                    minconn=0,  # كان 1 -> السبب الرئيسي لاستهلاك 50 ساعة في يومين
                    maxconn=5,  # كان 20 -> كثير جداً للخطة المجانية
                    dsn=settings.DATABASE_URL,
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=2,
                    connect_timeout=10,
                )
                cls._last_activity = time.time()
                logger.info("✅ Database pool optimized (min=0, max=5) - Neon CU fix applied")
                cls.create_tables()
            except Exception as e:
                logger.error(f"Error initializing database pool: {e}")
                raise e

    @classmethod
    def get_connection(cls):
        if not cls._pool:
            cls.initialize_pool()
        try:
            conn = cls._pool.getconn()
            # ✅ FIXED: Removed SELECT 1 ping - it was doubling queries
            # Old code did: cursor.execute("SELECT 1") on every get_connection
            cls._last_activity = time.time()
            return conn
        except Exception:
            logger.warning("Dead connection detected, recreating pool...")
            cls.recreate_pool()
            return cls._pool.getconn()

    @classmethod
    def put_connection(cls, conn):
        if cls._pool and conn:
            try:
                cls._pool.putconn(conn)
                cls._last_activity = time.time()
            except Exception:
                pass

    @classmethod
    def recreate_pool(cls):
        if cls._pool:
            try:
                cls._pool.closeall()
            except Exception:
                pass
        cls._pool = None
        cls.initialize_pool()

    @classmethod
    def execute_query(cls, query, params=None, fetch=None):
        conn = None
        cursor = None
        result = None
        retry_count = 0
        while retry_count < 2:  # كان 3
            try:
                conn = cls.get_connection()
                cursor = conn.cursor()
                cursor.execute(query, params or ())

                if fetch == 'one':
                    result = cursor.fetchone()
                elif fetch == 'all':
                    result = cursor.fetchall()

                conn.commit()
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as conn_err:
                logger.warning(f"DB connection lost: {conn_err}, retry {retry_count+1}")
                cls.recreate_pool()
                retry_count += 1
                time.sleep(0.5)
            except Exception as e:
                if conn:
                    conn.rollback()
                logger.error(f"Query error: {e}\nQuery: {query[:200]}")
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
        while retry_count < 2:
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
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as conn_err:
                logger.warning(f"DB connection lost: {conn_err}, retry")
                cls.recreate_pool()
                retry_count += 1
                time.sleep(0.5)
            except Exception as e:
                if conn:
                    conn.rollback()
                logger.error(f"Query error: {e}")
                raise e
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    cls.put_connection(conn)
        return result

    @classmethod
    def create_tables(cls):
        # نفس كود إنشاء الجداول الأصلي - لم يتغير
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
            turnover_field_name VARCHAR(80) DEFAULT 'totalBet'
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

        payment_settings_table = """
        CREATE TABLE IF NOT EXISTS payment_settings (
            payment_method VARCHAR(50) PRIMARY KEY,
            address TEXT,
            updated_by VARCHAR(50),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """

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

        alter_settings = [
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS agent_balance BIGINT DEFAULT 0;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS last_cookie_update TIMESTAMP WITH TIME ZONE;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS referrals_enabled BOOLEAN DEFAULT TRUE;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS game_min_deposit_syp BIGINT DEFAULT 20000;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS agent_revenue_percent NUMERIC(7, 2) DEFAULT 30;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS game_bonus_enabled BOOLEAN DEFAULT TRUE;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS game_bonus_apply_percent NUMERIC(7, 2) DEFAULT 10;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS bonus_rollover_multiplier NUMERIC(7, 2) DEFAULT 5;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS turnover_field_name VARCHAR(80) DEFAULT 'totalBet';",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS min_deposit_syp BIGINT DEFAULT 20000;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS min_deposit_usd INT DEFAULT 5;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS min_withdraw_syp BIGINT DEFAULT 25000;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS min_withdraw_usd INT DEFAULT 10;",
            "ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS syp_version VARCHAR(10) DEFAULT 'old';",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_balance BIGINT DEFAULT 0;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS game_bonus_amount BIGINT DEFAULT 0;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_base_balance BIGINT DEFAULT 0;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS affiliate_balance BIGINT DEFAULT 0;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS cashback_pending_balance BIGINT DEFAULT 0;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS checkin_pending_balance BIGINT DEFAULT 0;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS bonus_min_transfer BIGINT DEFAULT 20000;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS bonus_deposit_threshold BIGINT DEFAULT 100000;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS bonus_deposit_days INT DEFAULT 30;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS checkin_min_deposit BIGINT DEFAULT 50000;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS checkin_cycle_days INT DEFAULT 30;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS checkin_completion_reward BIGINT DEFAULT 20000;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS wheel_enabled BOOLEAN DEFAULT TRUE;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS wheel_segments_json TEXT DEFAULT '[[0,30],[2,20],[0,10],[5,15],[10,10],[0,5],[15,7],[25,3]]';",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS wheel_min_deposit BIGINT DEFAULT 50000;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS wheel_max_reward BIGINT DEFAULT 30000;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS vip_enabled BOOLEAN DEFAULT TRUE;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS vip_tiers_json TEXT DEFAULT '[[0,0,0],[500000,1,10000],[2000000,2,50000],[5000000,3,200000]]';",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS cashback_enabled BOOLEAN DEFAULT TRUE;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS cashback_pct NUMERIC(7,2) DEFAULT 5;",
            "ALTER TABLE user_features_settings ADD COLUMN IF NOT EXISTS cashback_min_loss BIGINT DEFAULT 50000;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_deposits BIGINT DEFAULT 0;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_tier INT DEFAULT 0;",
        ]

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

        insert_default_settings = """
        INSERT INTO bot_settings (id, exchange_rate, usd_buy_rate, usd_sell_rate, withdraw_commission, agent_balance)
        VALUES (1, 1000, 14000, 15000, 10, 0)
        ON CONFLICT (id) DO NOTHING;
        """

        table_queries = [
            users_table, referrals_table, transactions_table, *alter_transactions_columns,
            gifts_table, bot_settings_table, referral_commissions_table, bonus_rules_table,
            payment_settings_table, user_features_settings_table, *alter_settings,
            wheel_spins_table, cashback_payouts_table, affiliate_weekly_commissions_table,
            insert_default_settings, pending_rejections_table, broadcasts_table,
            support_tickets_table, prediction_cards_table, prediction_entries_table,
            contests_table, contest_entries_table, support_messages_table,
            daily_checkins_table, flash_bonuses_table,
        ]

        for table_query in table_queries:
            conn = None
            cursor = None
            try:
                conn = cls._pool.getconn()
                cursor = conn.cursor()
                cursor.execute(table_query)
                conn.commit()
            except Exception as e:
                if conn:
                    conn.rollback()
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    cls._pool.putconn(conn)

        logger.info("✅ Tables checked/created successfully (Neon optimized mode).")
