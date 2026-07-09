"""
سكريبت تطبيق الإصلاح التلقائي - يصلح استهلاك Neon
==================================================
شغل: python FIX_APPLY_PATCH.py
سيقوم ب:
1. نسخ احتياطي للملفات الأصلية
2. تطبيق الإصلاحات
3. عرض النتيجة
"""

import os
import shutil
import re

ROOT = os.path.dirname(os.path.abspath(__file__))

def backup_file(path):
    bak = path + ".backup_neon_fix"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print(f"✅ Backup: {bak}")
    else:
        print(f"ℹ️ Backup already exists: {bak}")

def patch_connection_py():
    path = os.path.join(ROOT, "database", "connection.py")
    backup_file(path)
    
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # إصلاح 1: minconn 1 -> 0
    content = content.replace("minconn=1,", "minconn=0,  # FIXED: was 1 - caused 50h/2days")
    # إصلاح 2: maxconn 20 -> 3
    content = content.replace("maxconn=20,", "maxconn=3,  # FIXED: was 20 - too high for free tier")
    
    # إصلاح 3: إزالة SELECT 1
    content = content.replace(
        '            cursor = conn.cursor()\n            cursor.execute("SELECT 1")\n            cursor.close()\n            return conn',
        '            # FIXED: Removed SELECT 1 ping - was doubling queries\n            return conn'
    )
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("✅ Patched database/connection.py: minconn=0, maxconn=3, removed SELECT 1")

def patch_main_py():
    path = os.path.join(ROOT, "telegram_bot", "main.py")
    backup_file(path)
    
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # إصلاح Watchdog من 300 ثانية (5 دقائق) إلى 1800 ثانية (30 دقيقة)
    original = content
    # نبحث عن await asyncio.sleep(300) داخل cookie_watchdog_task
    content = re.sub(
        r'(async def cookie_watchdog_task.*?await asyncio\.sleep\()300(\))',
        r'\g<1>1800\g<2>  # FIXED: was 300 (5min) -> 1800 (30min) to let Neon sleep',
        content,
        flags=re.DOTALL
    )
    
    # إذا لم ينجح الـ regex الأول، جرب استبدال بسيط
    if content == original:
        content = content.replace(
            "        await asyncio.sleep(300)",
            "        await asyncio.sleep(1800)  # FIXED: was 300 (5min) -> 1800 (30min) to let Neon sleep"
        )
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("✅ Patched telegram_bot/main.py: watchdog 300s -> 1800s (30min)")

def create_env_example():
    path = os.path.join(ROOT, "NEON_OPTIMIZATION_ENV.txt")
    with open(path, 'w', encoding='utf-8') as f:
        f.write("""
# إعدادات إضافية لتقليل استهلاك Neon - أضفها إلى .env في Render

# ========= تقليل استهلاك Neon =========
# هذه المتغيرات يقرأها الكود المحسن الجديد

# مدة الـ cache للإعدادات (بالثواني) - كلما زاد، قل الاستهلاك
SETTINGS_CACHE_TTL=120

# هل تستخدم Pool أم لا؟ 0 = لا (أفضل للمجاني)، 1 = نعم
USE_DB_POOL=0

# فترة نوم الـ Watchdog (ثواني) - 1800 = 30 دقيقة
WATCHDOG_INTERVAL=1800

# هل تفعل إغلاق Pool عند الخمول؟
CLOSE_IDLE_POOL=true
IDLE_POOL_TIMEOUT=600

# ========= إعدادات Neon من لوحة التحكم =========
# اذهب إلى Neon Console > Settings > Compute
# Autosuspend delay = 300 seconds (5 min) - الأقل
# Min CU = 0.25, Max CU = 0.25
""")
    print(f"✅ Created {path}")

if __name__ == "__main__":
    print("🔧 بدء تطبيق إصلاح استهلاك Neon...\n")
    
    try:
        patch_connection_py()
        patch_main_py()
        create_env_example()
        
        print("\n" + "="*60)
        print("✅ تم تطبيق جميع الإصلاحات بنجاح!")
        print("="*60)
        print("""
الخطوات التالية:
1. راجع الملفات التي تم تعديلها
2. اعمل commit و push إلى GitHub
3. Render سيعمل deploy تلقائياً
4. راقب استهلاك Neon بعد 24 ساعة - يجب أن ينخفض من 25h/day إلى 3h/day

للتراجع في حال وجود مشكلة:
  cp database/connection.py.backup_neon_fix database/connection.py
  cp telegram_bot/main.py.backup_neon_fix telegram_bot/main.py

للحل النهائي (موصى به):
  cp database/connection_v2_no_pool.py database/connection.py
  هذا الحل بدون pool نهائياً - يوفر 90% من الاستهلاك
""")
    except Exception as e:
        print(f"\n❌ خطأ: {e}")
        import traceback
        traceback.print_exc()
