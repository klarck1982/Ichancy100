# دليل النشر على Render — Caesar_Bot

هذا المشروع يعمل حالياً بنظام:

```text
Render Web Service + aiohttp Webhook
```

وليس Background Service/Polling.

---

## 1. تجهيز المستودع

ارفع ملفات المشروع إلى GitHub.

تأكد أن الملفات الأساسية موجودة:

```text
requirements.txt
telegram_bot/main.py
config/settings.py
database/connection.py
```

---

## 2. إنشاء خدمة Render

من Render:

```text
New +
Web Service
```

ثم اختر مستودع GitHub.

---

## 3. إعدادات الخدمة

استخدم:

```text
Language: Python 3
Build Command: pip install -r requirements.txt
Start Command: python telegram_bot/main.py
```

Render سيعطيك رابطاً مثل:

```text
https://your-service-name.onrender.com
```

ضعه في متغير البيئة:

```env
RENDER_EXTERNAL_URL=https://your-service-name.onrender.com
```

---

## 4. متغيرات البيئة

أضف القيم الموجودة في:

```text
env.example
```

داخل:

```text
Render > Environment
```

الأهم:

```env
TELEGRAM_BOT_TOKEN
ADMIN_TELEGRAM_ID
ADMIN_IDS
DATABASE_URL
RENDER_EXTERNAL_URL
AGENT_USERNAME
AGENT_PASSWORD
PARENT_ID
```

ومستحسن إضافة:

```env
DEPOSIT_CHANNEL_ID
WITHDRAWAL_CHANNEL_ID
LOG_CHANNEL_ID
```

---

## 5. قاعدة البيانات Neon

في Neon:

1. أنشئ Project.
2. انسخ Connection String.
3. تأكد من وجود:

```text
sslmode=require
```

مثال:

```env
DATABASE_URL=postgresql://user:password@ep-xxxx.region.aws.neon.tech/neondb?sslmode=require
```

عند أول تشغيل، البوت ينشئ الجداول تلقائياً.

---

## 6. بعد أول Deploy

افتح Render Logs وتأكد من ظهور رسائل مثل:

```text
Caesar_Bot is starting...
Database connection pool initialized successfully.
Tables checked/created successfully.
Starting web server on 0.0.0.0:PORT
Initial webhook set
```

ثم جرّب في تيليجرام:

```text
/start
/admin
```

---

## 7. اختبار سريع بعد كل Deploy

### المستخدم

- `/start`
- فتح القائمة الرئيسية
- عرض الرصيد
- صفحة العروض
- صفحة الإحالات

### المشرف

- `/admin`
- أسعار الصرف
- البونصات والعروض
- إنشاء كود هدية
- تفعيل/إيقاف الإحالات

### العمليات المالية

- إيداع SYP
- إيداع USD
- قبول إيداع
- سحب رصيد
- شحن حساب اللعبة
- سحب من حساب اللعبة

---

## 8. ملاحظات مهمة

- لا تستخدم Background Service لأن الكود الحالي يحتاج Webhook ومسار HTTP.
- لا تضع أسرار `.env` في GitHub العام.
- تحديث README أو env.example لا يحتاج Deploy للبوت، لكنه مفيد للتوثيق.
- أي تعديل على ملفات Python يحتاج Deploy جديد.
