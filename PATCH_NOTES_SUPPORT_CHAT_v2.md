# Caesar_Bot - Support Chat v2 + Contact Us Fix
التاريخ: 2026-06-28

## المشاكل التي تم إصلاحها

### 1. زر "رسالة للإدارة" 
**المشكلة القديمة:**
- كانت ترسل رسالة واحدة فقط ثم تغلق
- اسم المستخدم يظهر كنص `@username` فقط
- إذا المستخدم ما عنده username، يظهر الاسم الأول فقط غير قابل للنقر
- عند الضغط على اليوزر يفتح محادثة مع شخص آخر أحياناً
- لا يمكن الرد بسهولة

**الحل الجديد - محادثة دعم ثنائية كاملة:**
- ✅ محادثة مفتوحة: المستخدم يرسل عدد غير محدود من الرسائل (نص/صور/فيديو/ملفات/voice)
- ✅ رابط مضمون `tg://user?id=XXX` يفتح المحادثة الصحيحة دائماً حتى بدون @username
- ✅ عرض معلومات كاملة للأدمن: الاسم القابل للنقر + @username + Telegram ID + iChancy username + Player ID + رصيد البوت
- ✅ زر "↩️ رد عبر البوت" - الأدمن يرد من داخل البوت والمستخدم يستلم الرد فوراً
- ✅ زر "💬 فتح محادثة" - يفتح شات تيليجرام مباشرة مع المستخدم
- ✅ يمكن إنهاء المحادثة من الطرفين (المستخدم / الأدمن)
- ✅ جلسات الدعم محفوظة في قاعدة البيانات `support_chats` - لا تضيع مع إعادة تشغيل Render
- ✅ عداد رسائل + تاريخ آخر نشاط

### 2. زر "تواصل معنا"
**المشكلة القديمة:**
- كان placeholder فقط: "يمكنك الضغط على رسالة للإدارة..."
- لا توجد روابط خارجية

**الحل الجديد:**
- ✅ أزرار روابط ديناميكية بالكامل من متغيرات البيئة
- ✅ 4 روابط قابلة للتخصيص:
  - `CONTACT_TELEGRAM_URL` / `CONTACT_TELEGRAM_LABEL`
  - `CONTACT_WHATSAPP_URL` / `CONTACT_WHATSAPP_LABEL`
  - `CONTACT_CHANNEL_URL` / `CONTACT_CHANNEL_LABEL`
  - `CONTACT_WEBSITE_URL` / `CONTACT_WEBSITE_LABEL`
- ✅ أي رابط تتركه فارغ يختفي الزر تلقائياً
- ✅ روابط الموقع/التطبيق في القائمة الرئيسية أصبحت قابلة للتعديل عبر `CONTACT_WEBSITE_URL`
- ✅ Fallback تلقائي إلى `SUPPORT_LINK` للتوافق مع الإصدارات القديمة

## ملفات معدلة

1. `config/settings.py`
   - إضافة CONTACT_TELEGRAM/WHATSAPP/CHANNEL/WEBSITE_URL + LABEL
   - إضافة SUPPORT_CHAT_ID
   - إضافة SUPPORT_ALLOW_BOT_REPLY

2. `database/connection.py`
   - إضافة جدول `support_chats`

3. `database/repository.py`
   - `get_support_chat()`
   - `is_support_chat_open()`
   - `open_support_chat()`
   - `close_support_chat()`
   - `increment_support_message_count()`
   - `get_open_support_chats()`

4. `telegram_bot/keyboards/inline.py`
   - `get_contact_keyboard()` - جديدة، ديناميكية
   - `get_support_chat_keyboard()` - جديدة
   - `get_support_admin_keyboard()` - جديدة
   - تحديث `get_main_menu_keyboard()` - روابط الموقع أصبحت من env

5. `telegram_bot/handlers/menu.py`
   - نظام Support Chat v2 كامل:
     - `message_admin_callback()` - فتح محادثة
     - `support_chat_message_handler()` - استقبال رسائل المستخدم
     - `support_close_user_callback()` - إنهاء من المستخدم
     - `support_reply_start_callback()` - الأدمن يبدأ الرد
     - `support_reply_send_handler()` - إرسال رد الأدمن
     - `support_close_admin_callback()` - إنهاء من الأدمن
     - `forward_support_message_to_admin()` - مع tg://user?id=
   - `contact_us_callback()` - جديدة بالكامل، تعرض روابط من env

6. `env.example`
   - تحديث كامل ليتوافق مع settings.py الفعلي
   - إضافة كل متغيرات CONTACT_*
   - إضافة SUPPORT_CHAT_ID

7. `README.md` / `DEPLOY_RENDER.md`
   - تم التحديث للنسخة المقدمة من المستخدم (Webhook + AGENT_USERNAME)

## متغيرات Render الجديدة المطلوبة

أضف هذه المتغيرات في Render > Environment:

```
# إجباري - أين تصل رسائل الدعم
SUPPORT_CHAT_ID=123456789

# اختياري - روابط تواصل معنا (اترك فارغ لإخفاء الزر)
CONTACT_TELEGRAM_URL=https://t.me/YourSupport
CONTACT_TELEGRAM_LABEL=💬 دعم تيليجرام

CONTACT_WHATSAPP_URL=https://wa.me/9639XXXXXXXX
CONTACT_WHATSAPP_LABEL=📱 واتساب

CONTACT_CHANNEL_URL=https://t.me/YourChannel
CONTACT_CHANNEL_LABEL=📢 القناة الرسمية

CONTACT_WEBSITE_URL=https://www.ichancy.com
CONTACT_WEBSITE_LABEL=🌐 الموقع
```

إذا لم تضبط `SUPPORT_CHAT_ID`، ستصل رسائل الدعم تلقائياً إلى `ADMIN_TELEGRAM_ID`.

## اختبار سريع بعد Deploy

1. `/start` → اضغط "رسالة للإدارة"
2. أرسل نص + صورة → يجب أن تصل للأدمن مع رابط tg:// صحيح
3. من حساب الأدمن اضغط "↩️ رد عبر البوت" → أرسل رد → يجب أن يصل للمستخدم
4. جرب "🔴 إنهاء المحادثة" من الطرفين
5. اضغط "تواصل معنا" → يجب أن تظهر الأزرار التي ضبطتها في env فقط
6. إذا تركت كل روابط CONTACT فارغة، سيظهر زر "رسالة للإدارة" تلقائياً

---
تم بواسطة Arena Agent – 2026-06-28
