# 🛠️ تحديث قاعدة البيانات لنظام "تدوير البونص" (Rollover)

من أجل تفعيل نظام حماية السيولة الجديد ومنع "غسل المكافآت"، يرجى تنفيذ الأوامر التالية في قاعدة بيانات Neon الخاصة بك (عبر Neon Console SQL Editor):

### 1. إضافة حقل تتبع البونص للمستخدمين
هذا الحقل يسجل مقدار البونص الذي تم تحويله للعبة ولم يتم تدويره بعد.
```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS game_bonus_amount INTEGER DEFAULT 0;
```

### 2. إضافة إعدادات التدوير في جدول الإعدادات
هذا يسمح لك بالتحكم في "صعوبة" السحب (مثلاً 5 أضعاف) واسم الحقل القادم من iChancy.
```sql
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS bonus_rollover_multiplier FLOAT DEFAULT 5.0;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS turnover_field_name TEXT DEFAULT 'totalBet';
```

---
**💡 ملاحظة للمشرف:**
- **bonus_rollover_multiplier**: إذا جعلته `1.0` يكون السحب سهلاً، وإذا جعلته `10.0` يكون السحب صعباً جداً. (الموصى به: `5.0`).
- **turnover_field_name**: هذا هو اسم الحقل في API iChancy. قمنا بضبطه افتراضياً على `totalBet`. إذا لاحظت من سجلات البوت أن القيمة تعود دائماً بصفر، يرجى إبلاغي لتصحيح اسم الحقل.
