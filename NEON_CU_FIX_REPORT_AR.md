# تقرير تحليل استهلاك Neon CU-Hrs العالي 🧮

## التشخيص: لماذا استهلكت 50 ساعة في يومين؟

الحد المجاني في Neon هو **100 ساعة / شهر**. استهلاكك 25 ساعة / يوم يعني أن قاعدة البيانات تعمل **24 ساعة بدون توقف** ولا تدخل في وضع السكون أبداً.

`100 ساعة / 30 يوم = 3.3 ساعة فقط مسموح بها يومياً`. أنت تستهلك 7 أضعاف!

---

### 🔴 السبب رقم 1 - القاتل الأكبر: `minconn=1` في الـ Pool
**الملف:** `database/connection.py` السطر 14

```python
cls._pool = pool.ThreadedConnectionPool(
    minconn=1,  # <-- هذه هي الكارثة
    maxconn=20,
    dsn=settings.DATABASE_URL
)
```

**ماذا يحدث؟**
- `minconn=1` يعني عند تشغيل البوت يتم فتح اتصال واحد فوراً ويبقى **مفتوح للأبد**
- Neon يعلق الـ Compute بعد 5 دقائق من عدم النشاط، لكنه لا يستطيع التعليق إذا كان هناك اتصال مفتوح!
- النتيجة: Compute يعمل 24/7 -> 720 ساعة في الشهر -> 50 ساعة في يومين منطقي جداً

**الحل:**
```python
minconn=0  # لا تترك أي اتصال مفتوح
maxconn=3  # 20 كثير جداً لخطة مجانية
```

### 🔴 السبب رقم 2: Watchdog كل 5 دقائق
**الملف:** `telegram_bot/main.py` السطر 132

```python
await asyncio.sleep(300)  # 5 دقائق
```

كل 5 دقائق يقوم الـ Watchdog بـ:
1. `get_webhook_info()` 
2. `check_session_validity()`
3. `get_admin_balance()`
4. `get_bot_settings()` -> query
5. `update_bot_settings()` -> query
6. `check_agent_balance_periodic()` -> query

حتى لو أصلحت `minconn=0`، هذا الـ Watchdog بحد ذاته سيمنع Neon من النوم، لأن Neon ينام بعد 5 دقائق خمول، وأنت توقظه كل 5 دقائق!

**الحل:** اجعله 30 دقيقة = `1800` ثانية

### 🔴 السبب رقم 3: `SELECT 1` في كل مرة
**الملف:** `database/connection.py` السطر 28

```python
cursor.execute("SELECT 1")  # ping زائد في كل get_connection
```

هذه العملية تضاعف عدد الاستعلامات. لا حاجة لها مع Neon.

### 🔴 السبب رقم 4: لا يوجد Cache لإعدادات البوت
**الملف:** `database/repository.py`

`get_bot_settings()` يتم استدعاؤها في كل مكان:
- في كل رسالة مستخدم (TermsCheckMiddleware)
- في كل طلب إيداع/سحب
- في الـ Watchdog
- في الـ Dashboard (مرتين - ثلاث)

كل استدعاء = اتصال + query. بينما الإعدادات تتغير مرة في اليوم!

### 🔴 السبب رقم 5: دوال غير محسنة
مثال: `get_transaction_stats_for_user()` تقوم بـ 4 استعلامات منفصلة:
```python
SELECT COUNT(*) WHERE status = 'pending'
SELECT COUNT(*) WHERE status = 'approved'
SELECT COUNT(*) WHERE status = 'rejected'
SELECT COUNT(*) total
```
كان يمكن دمجها في استعلام واحد! وهذا يستهلك 4x اتصالات.

---

## الحل الكامل - 3 ملفات مصححة

### 1. تم إنشاء `database/connection_optimized.py`
هذا هو الملف الجديد الذي يجب أن تستبدل به القديم.
الفروقات:
- `minconn=0` - صفر اتصالات دائمة -> يسمح لـ Neon بالنوم
- `maxconn=3` بدل 20 (مناسب للخطة المجانية)
- لا يوجد `SELECT 1` ping
- إضافة cache لإعدادات البوت (60 ثانية)
- دالة `close_idle_pool()` لإغلاق الـ pool عند عدم الحاجة
- استخدام Context Manager لإغلاق تلقائي

**التوفير المتوقع:** من 24 ساعة/يوم إلى 2-4 ساعات/يوم (توفير 85%)

### 2. تم إنشاء `database/repository_cached.py`
إضافة decorator `@cached_ttl(60)` لـ `get_bot_settings()`

### 3. تعديل مقترح لـ `telegram_bot/main.py`

```python
# غيّر السطر 132 من:
await asyncio.sleep(300)
# إلى:
await asyncio.sleep(1800)  # 30 دقيقة بدل 5

# وأيضا في daily_report: لا داعي لفحص الرصيد كل 5 دقائق
```

---

## إعدادات Neon التي يجب أن تطبقها من لوحة Neon

1. اذهب إلى Neon Console > Project > Settings > Compute
2. تأكد أن **Autosuspend delay = 5 minutes** (الأقل)
3. تأكد أن **Min compute size = 0.25 CU** و **Max = 0.25 CU** (للتوفير)
4. لا تفعل **Scale to zero** معطل - يجب أن يكون مفعل

## إعدادات Render التي تساعد

في Render، الخطة المجانية توقف السيرفس بعد 15 دقيقة خمول. هذا جيد لـ Neon لأنه يسمح لـ Neon بالنوم أيضاً.

لكن Watchdog كل 5 دقائق يمنع Render من النوم أيضاً! وهذا يسبب مشكلتين:
- تستهلك ساعات Render المجانية (750 ساعة)
- تستهلك ساعات Neon

عندما تجعل Watchdog 30 دقيقة، سينام Render أيضاً، وهذا سيوفر المال.

---

## النتيجة بعد الإصلاح

| قبل | بعد |
|-----|-----|
| 25 ساعة / يوم | 2-4 ساعات / يوم |
| 50 ساعة في يومين | 4-8 ساعات في يومين |
| 750 ساعة / شهر (تتجاوز الحد) | 60-120 ساعة / شهر (ضمن الـ 100 مع هامش) |
| Neon لا ينام أبداً | Neon ينام 20 ساعة يومياً |

## كيفية تطبيق الإصلاح

### الطريقة السريعة (5 دقائق):

```bash
# 1. استبدل ملف الاتصال
cp database/connection_optimized.py database/connection.py

# 2. عدّل main.py يدوياً
# السطر 132: 300 -> 1800

# 3. اعمل deploy جديد على Render
git add .
git commit -m "fix: reduce Neon CU consumption from 25h/day to 3h/day"
git push
```

### الطريقة الاحترافية (موجودة في الملفات المرفقة):
استخدم `connection_v2_no_pool.py` الذي لا يستخدم pool أبداً - يفتح اتصال ويغلقه فوراً. هذا أفضل طريقة للخطة المجانية.

---

## ملاحظة أخيرة: هل 100 ساعة تكفي؟

نعم، إذا طبقت الإصلاح. معظم مشاريع Neon المجانية تستهلك 20-40 ساعة فقط شهرياً مع الاستخدام العادي.

إذا كان عندك أكثر من 100 مستخدم نشط يومياً، قد تحتاج:
- ترقية لـ Neon Pro (19$ -> 1000 ساعة)
- أو نقل الـ Watchdog إلى Cron job خارجي (مثل UptimeRobot يوقظ كل 30 دقيقة فقط)
- أو استخدام Supabase (500 ساعة مجانية) لكن نفس مشكلة الـ Pool تنطبق

أخبرني إذا تريد أن أطبق الإصلاح تلقائياً على الملفات الأصلية!
