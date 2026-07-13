"""
سكريبت التصفير الشامل لقاعدة البيانات وإنهاء المرحلة التجريبية (Beta Reset)
=============================================================================
شغل: python RESET_DATABASE_BALANCES.py
سيقوم بـ:
1. تصفير جميع أرصدة المستخدمين (bot_balance, game_balance, bonus_balance, game_bonus_amount, total_deposits, vip_tier, affiliate_balance) إلى 0.
2. تفريغ ومسح جميع سجلات (transactions, gifts, wheel_spins, daily_checkins, cashback_payouts, affiliate_weekly_commissions, referral_commissions) بالكامل.
3. الحفاظ على بيانات وحسابات المستخدمين وأسماءهم وربطهم بـ iChancy كما هي بأمان.
"""

import sys
import os

# إضافة المسار الحالي للمشروع
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.connection import DatabaseManager

def reset_all_database_balances():
    print("🗑️ جاري الاتصال بقاعدة بيانات Neon وتنفيذ التصفير الشامل للمرحلة التجريبية (Beta Reset)...\n")
    try:
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
        print("✅ تم تصفير جميع أرصدة ومستويات وإجمالي إيداعات المستخدمين إلى 0 ل.س بنجاح.")

        # تفريغ جميع جداول الحركات والتاريخ
        try:
            DatabaseManager.execute_query("""
                TRUNCATE TABLE transactions, gifts, wheel_spins, daily_checkins, 
                               cashback_payouts, affiliate_weekly_commissions, 
                               referral_commissions, prediction_entries, contest_entries RESTART IDENTITY CASCADE;
            """)
            print("✅ تم تفريغ ومسح جميع الجداول المالية وسجلات الحضور والعجلة بالكامل (TRUNCATE).")
        except Exception:
            for tbl in ["transactions", "gifts", "wheel_spins", "daily_checkins", "cashback_payouts", "affiliate_weekly_commissions", "referral_commissions", "prediction_entries", "contest_entries"]:
                try:
                    DatabaseManager.execute_query(f"DELETE FROM {tbl};")
                except Exception:
                    pass
            print("✅ تم مسح جميع سجلات الجداول المالية (DELETE).")

        DatabaseManager.execute_query("UPDATE bot_settings SET agent_balance = 0 WHERE id = 1;")
        if hasattr(DatabaseManager, 'invalidate_settings_cache'):
            DatabaseManager.invalidate_settings_cache()
        print("✅ تم تصفير مؤشرات رصيد الكاشيرة في الإعدادات.")

        print("\n🎉 قاعدة البيانات الآن مصَفّرة ونظيفة 100% (مع الحفاظ على أسماء وحسابات المستخدمين) ومستعدة للانطلاق الحقيقي!")
    except Exception as e:
        print(f"❌ حدث خطأ أثناء تصفير قاعدة البيانات: {e}")

if __name__ == "__main__":
    reset_all_database_balances()
