import os
from dotenv import load_dotenv
from decimal import Decimal

# Load .env file
load_dotenv()

# Telegram Configuration
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_TELEGRAM_ID')
ADMIN_IDS = os.getenv('ADMIN_IDS', ADMIN_ID or '')

# Channels Configuration for manual deposits and withdrawals
DEPOSIT_CHANNEL_ID = os.getenv('DEPOSIT_CHANNEL_ID')
WITHDRAWAL_CHANNEL_ID = os.getenv('WITHDRAWAL_CHANNEL_ID')

# 🆕 جديد - قناة السجلات
LOG_CHANNEL_ID = os.getenv('LOG_CHANNEL_ID')

# 🆕 قناة المسابقات والتوقعات
CONTEST_CHANNEL_ID = os.getenv('CONTEST_CHANNEL_ID')

# 🆕 إعدادات التقرير المالي اليومي
# ملاحظة: Render يستخدم UTC (غرينتش)
# الساعة 5 صباحاً UTC = الساعة 8 صباحاً بتوقيت سوريا (UTC+3)
DAILY_REPORT_HOUR = int(os.getenv('DAILY_REPORT_HOUR', '5'))
DAILY_REPORT_ENABLED = os.getenv('DAILY_REPORT_ENABLED', 'true').lower() == 'true'

# 🆕 حد التنبيه لرصيد الكاشيرة (NSP)
AGENT_BALANCE_ALERT_THRESHOLD = int(os.getenv('AGENT_BALANCE_ALERT_THRESHOLD', '100000'))

# Support Handles
SUPPORT_LINK = os.getenv('SUPPORT_LINK', 'https://t.me/Caesar_Support')
SUPPORT_USERNAME = os.getenv('SUPPORT_USERNAME', '@Caesar_Support')

# Public iChancy Links
WEBSITE_URL = os.getenv('WEBSITE_URL', 'https://www.ichancy100.com')
APP_DOWNLOAD_URL = os.getenv('APP_DOWNLOAD_URL', 'https://www.ichancy100.com')
BETTING_URL = os.getenv('BETTING_URL', 'https://facebook.com/your-bot-page')
GAMES_URL = os.getenv('GAMES_URL', 'https://ichancy100.com/games')

RENDER_EXTERNAL_URL = os.getenv('RENDER_EXTERNAL_URL', 'https://ichancy100.onrender.com')

# Database Configuration
DATABASE_URL = os.getenv('DATABASE_URL')

# iChancy Configuration
PARENT_ID = os.getenv('PARENT_ID')
AGENT_ID = os.getenv('AGENT_ID', PARENT_ID or '')
ICHANCY_AGENT_BASE_URL = os.getenv('ICHANCY_AGENT_BASE_URL', 'https://agents.ichancy100.com')
USER_AGENT = os.getenv('USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# iChancy Agent Login Credentials (for automated dynamic cookie generation)
AGENT_USERNAME = os.getenv('AGENT_USERNAME')
AGENT_PASSWORD = os.getenv('AGENT_PASSWORD')

# 🆕 جديد - أسعار الصرف
try:
    EXCHANGE_RATE_BUY = Decimal(os.getenv('EXCHANGE_RATE_BUY', '87.5'))
    EXCHANGE_RATE_SELL = Decimal(os.getenv('EXCHANGE_RATE_SELL', '92.5'))
except:
    EXCHANGE_RATE_BUY = Decimal('87.5')
    EXCHANGE_RATE_SELL = Decimal('92.5')

# 🆕 حدود الإيداع والسحب الدنيا — قيم افتراضية احتياطية (المصدر الأساسي: لوحة Dashboard)
# يمكن ضبطها من Render Environment إذا أردت قيماً افتراضية مختلفة، لكن الإدارة الفعلية من Dashboard
MIN_DEPOSIT_SYP_DEFAULT = int(os.getenv('MIN_DEPOSIT_SYP_DEFAULT', '20000'))
MIN_DEPOSIT_USD_DEFAULT = int(os.getenv('MIN_DEPOSIT_USD_DEFAULT', '5'))
MIN_WITHDRAW_SYP_DEFAULT = int(os.getenv('MIN_WITHDRAW_SYP_DEFAULT', '25000'))
MIN_WITHDRAW_USD_DEFAULT = int(os.getenv('MIN_WITHDRAW_USD_DEFAULT', '10'))
SYP_VERSION = os.getenv('SYP_VERSION', 'old')  # 'old' = قديمة, 'new' = جديدة (مقسومة على 100)

# Configurable Payment Addresses (Environment Variables with fallbacks)
SYRIATEL_CASH_NUMBERS = os.getenv('SYRIATEL_CASH_NUMBERS', '83935571\n00229271')
MTN_CASH_NUMBER = os.getenv('MTN_CASH_NUMBER', '098xxxxxxx')
SHAM_CASH_SYP_ADDRESS = os.getenv('SHAM_CASH_SYP_ADDRESS', 'SHAM-SYP-1092')
SHAM_CASH_USD_ADDRESS = os.getenv('SHAM_CASH_USD_ADDRESS', 'SHAM-USD-5093')
USDT_TRC20_ADDRESS = os.getenv('USDT_TRC20_ADDRESS', 'TYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
USDT_BEP20_ADDRESS = os.getenv('USDT_BEP20_ADDRESS', '0xuxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')

def validate_config():
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in environment variables")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set in environment variables")
    if not ADMIN_ID:
        raise ValueError("ADMIN_TELEGRAM_ID is not set in environment variables")
    if not AGENT_USERNAME or not AGENT_PASSWORD:
        raise ValueError("AGENT_USERNAME or AGENT_PASSWORD is not set in environment variables for automatic login")
    
    # 🆕 جديد - تحذير للـ Log Channel (اختياري)
    if not LOG_CHANNEL_ID:
        print("⚠️ WARNING: LOG_CHANNEL_ID not set - logging disabled")
    
    # 🆕 جديد - تحذير لأسعار الصرف
    if not EXCHANGE_RATE_BUY or not EXCHANGE_RATE_SELL:
        print("⚠️ WARNING: Exchange rates not properly configured")
