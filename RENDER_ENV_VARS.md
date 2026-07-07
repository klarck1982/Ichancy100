# 🚀 Caesar_Bot / Ichancy100 — جميع متغيرات بيئة Render

> **انسخ هذه القيم إلى: Render → Environment → Environment Variables**  
> الملف المرجعي: `env.example` في المستودع (للاستخدام المحلي فقط)

---

## 🔴 إجبارية (البوت لن يعمل بدونها)

| المتغير | الوصف | مثال للقيمة |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | توكن البوت من [@BotFather](https://t.me/BotFather) | `1234567890:ABCdefGHI...` |
| `ADMIN_TELEGRAM_ID` | معرفك الرقمي في تيليجرام (المشرف الأساسي) | `987654321` |
| `DATABASE_URL` | رابط اتصال PostgreSQL من Neon | `postgresql://user:pass@ep-xxx.us-east-2.aws.neon.tech/neondb?sslmode=require` |
| `AGENT_USERNAME` | اسم مستخدم الوكيل في iChancy | `your_agent_user` |
| `AGENT_PASSWORD` | كلمة مرور الوكيل في iChancy | `your_agent_pass` |
| `PARENT_ID` | Parent/Affiliate ID الخاص بالوكيل | `12345` |

---

## 🟡 موصى بها بشدة

| المتغير | الوصف | مثال للقيمة |
|---|---|---|
| `ADMIN_IDS` | عدة مشرفين مفصولين بفواصل | `987654321,111222333,444555666` |
| `RENDER_EXTERNAL_URL` | رابط خدمة Render العام | `https://ichancy100.onrender.com` |
| `AGENT_ID` | Agent ID (غالباً نفس PARENT_ID) | `12345` |
| `ICHANCY_AGENT_BASE_URL` | رابط لوحة الوكيل | `https://agents.ichancy100.com` |
| `DEPOSIT_CHANNEL_ID` | قناة مراجعة طلبات الإيداع (تبدأ بـ `-100`) | `-1001234567890` |
| `WITHDRAWAL_CHANNEL_ID` | قناة مراجعة طلبات السحب (تبدأ بـ `-100`) | `-1001234567890` |

---

## 🟢 اختيارية (لكنها مفيدة)

### القنوات والسجلات
| المتغير | الوصف | مثال للقيمة |
|---|---|---|
| `LOG_CHANNEL_ID` | قناة السجلات العامة (تبدأ بـ `-100`) | `-1001234567890` |
| `CONTEST_CHANNEL_ID` | قناة المسابقات والتوقعات | `-1001234567890` |
| `SUPPORT_CHAT_ID` | أين تصل رسائل الدعم | `123456789` |

### التقارير والتنبيهات
| المتغير | الوصف | الافتراضي |
|---|---|---|
| `DAILY_REPORT_HOUR` | ساعة التقرير (UTC) | `5` |
| `DAILY_REPORT_ENABLED` | تفعيل التقرير اليومي | `true` |
| `AGENT_BALANCE_ALERT_THRESHOLD` | حد تنبيه رصيد الكاشيرة (NSP) | `100000` |

### الإعدادات العامة
| المتغير | الوصف | الافتراضي |
|---|---|---|
| `PORT` | منفذ الخادم | `8080` |
| `USER_AGENT` | User-Agent للطلبات | (افتراضي Chrome) |

### روابط الموقع
| المتغير | الافتراضي |
|---|---|
| `WEBSITE_URL` | `https://www.ichancy100.com` |
| `APP_DOWNLOAD_URL` | `https://www.ichancy100.com` |
| `BETTING_URL` | `https://facebook.com/your-bot-page` |
| `GAMES_URL` | `https://ichancy100.com/games` |

### تواصل معنا
| المتغير | الوصف |
|---|---|
| `SUPPORT_LINK` | رابط الدعم الأساسي |
| `SUPPORT_USERNAME` | يوزر الدعم |
| `CONTACT_TELEGRAM_URL` | زر تيليجرام |
| `CONTACT_TELEGRAM_LABEL` | عنوان زر تيليجرام |
| `CONTACT_WHATSAPP_URL` | زر واتساب (فارغ = مخفي) |
| `CONTACT_WHATSAPP_LABEL` | عنوان زر واتساب |
| `CONTACT_CHANNEL_URL` | زر القناة (فارغ = مخفي) |
| `CONTACT_CHANNEL_LABEL` | عنوان زر القناة |
| `CONTACT_WEBSITE_URL` | زر الموقع |
| `CONTACT_WEBSITE_LABEL` | عنوان زر الموقع |

### عناوين الدفع
| المتغير | الوصف |
|---|---|
| `SYRIATEL_CASH_NUMBERS` | أرقام سيريتل كاش |
| `MTN_CASH_NUMBER` | رقم MTN كاش |
| `SHAM_CASH_SYP_ADDRESS` | شام كاش ليرة |
| `SHAM_CASH_USD_ADDRESS` | شام كاش دولار |
| `USDT_TRC20_ADDRESS` | USDT TRC-20 |
| `USDT_BEP20_ADDRESS` | USDT BEP-20 |

### 🆕 حدود الإيداع والسحب (احتياطية — تدار من Dashboard)
| المتغير | الافتراضي | الوصف |
|---|---|---|
| `MIN_DEPOSIT_SYP_DEFAULT` | `20000` | أدنى إيداع ليرة |
| `MIN_DEPOSIT_USD_DEFAULT` | `5` | أدنى إيداع دولار |
| `MIN_WITHDRAW_SYP_DEFAULT` | `25000` | أدنى سحب ليرة |
| `MIN_WITHDRAW_USD_DEFAULT` | `10` | أدنى سحب دولار (احتياطي) |

### 🆕 نسخة الليرة السورية
| المتغير | الوصف | الافتراضي |
|---|---|---|
| `SYP_VERSION` | نسخة الليرة ببوابات الدفع (`old` قديمة أو `new` جديدة) | `old` |

> ⚠️ **هذا المتغير احتياطي فقط!** الإدارة الفعلية من: **Dashboard → الأسعار والعمولات → نسخة الليرة ببوابات الدفع**

### أسعار الصرف (قديمة / احتياطية)
| المتغير | الافتراضي |
|---|---|
| `EXCHANGE_RATE_BUY` | `87500` |
| `EXCHANGE_RATE_SELL` | `92500` |

---

## 📝 ملاحظات هامة

1. **لا ترفع `.env` إلى GitHub** — المتغيرات في Render Dashboard فقط.
2. أي تعديل على متغيرات البيئة يحتاج **Restart** للخدمة.
3. القنوات تبدأ بـ `-100` متبوعاً بمعرف القناة.
4. `DATABASE_URL` يجب أن ينتهي بـ `?sslmode=require`.
5. **الأسعار وحدود الإيداع/السحب الفعلية تُدار من Dashboard → الأسعار والعمولات**.
