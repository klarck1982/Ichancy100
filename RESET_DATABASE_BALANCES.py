"""
سكريبت تصفير جميع أرصدة قاعدة البيانات وتنظيف المعاملات المؤقتة والاختبارية
==========================================================================
شغل: python RESET_DATABASE_BALANCES.py
سيقوم بـ:
1. إعادة جميع أرصدة المستخدمين (bot_balance, bonus_balance, game_bonus_amount, bonus_base_balance) إلى 0.
2. تنظيف جميع السجلات والمعاملات المؤقتة (pending, sandbox_test, test).
3. الحفاظ على بيانات المستخدمين وقاعدة الحسابات الحقيقية (telegram_id, username, player_id).
"""

import sys
import os

# إضافة المسار الحالي للمشروع
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.connection import DatabaseManager

def reset_all_database_balances():
    print("🗑️ جاري الاتصال بقاعدة بيانات Neon وتصفير جميع الأرصدة...\n")
    try:
        DatabaseManager.execute_query("""
            UPDATE users SET 
                bot_balance = 0, 
                bonus_balance = 0, 
                game_bonus_amount = 0, 
                bonus_base_balance = 0, 
                cashback_pending_balance = 0, 
                checkin_pending_balance = 0;
        """)
        print("✅ تم تصفير جميع أرصدة المستخدمين (النقدية، المكافآت، بونص اللعب) إلى 0 ل.س بنجاح.")

        DatabaseManager.execute_query("""
            DELETE FROM transactions 
            WHERE status IN ('pending', 'sandbox_test', 'test') 
               OR payment_method IN ('game', 'test', 'sandbox', 'sandbox_test');
        """)
        print("✅ تم تنظيف المعاملات المؤقتة والاختبارية بنجاح.")

        print("\n🎉 قاعدة البيانات الآن نظيفة ومصفرة ومستعدة للتشغيل بنسبة 100%!")
    except Exception as e:
        print(f"❌ حدث خطأ أثناء تصفير قاعدة البيانات: {e}")

if __name__ == "__main__":
    reset_all_database_balances()
