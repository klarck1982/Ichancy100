# Robert.vip — مراجعة الـEndpoints العامة في واجهة الموقع

**التاريخ:** 16 يوليو 2026  
**نوع الفحص:** تحليل سلبي وآمن لملفات HTML وJavaScript العامة التي يحملها المتصفح. لم يتم تسجيل الدخول، أو تخمين كلمات مرور، أو تجربة تجاوز حماية، أو إرسال معاملات إلى الـAPI.

## الخلاصة

نعم، توجد بنية API واضحة:

- Base API: `https://api.robert.vip/api/v1`
- WebSocket host: `ws.robert.vip`
- Authentication: Bearer token
- الواجهة تستخدم Axios مع `Authorization: Bearer <token>`.

هذه endpoints استُخرجت من ملفات الواجهة العامة، لكنها لا تُعد توثيقًا رسميًا ثابتًا. الربط المالي أو ربط الحسابات يحتاج موافقة/توثيق من فريق Robert.vip.

## Authentication

| Method | Endpoint |
|---|---|
| POST | `/auth/login` |
| POST | `/auth/register` |
| POST | `/auth/logout` |
| POST | `/auth/refresh` |
| POST | `/auth/password/forgot` |
| POST | `/auth/password/reset` |
| GET | `/user` |

## الملف الشخصي والتحقق

| Method | Endpoint |
|---|---|
| GET | `/profile` |
| PUT | `/profile` |
| GET | `/contacts` |
| POST | `/contacts` |
| PUT | `/contacts/{id}` |
| POST | `/contacts/{id}/otp/send` |
| POST | `/contacts/{id}/otp/verify` |
| POST | `/verification/submit` |
| GET | `/verification/{id}` |
| GET | `/verification/status` |

## المحفظة والمعاملات

| Method | Endpoint |
|---|---|
| GET | `/wallet` |
| GET | `/wallet/transactions` |
| GET | `/wallet/transactions/{id}` |
| POST | `/promo/redeem` |

## الألعاب

| Method | Endpoint |
|---|---|
| GET | `/games/inventory` |
| GET | `/games/{game}/session` |
| POST | `/games/{game}/start` |
| POST | `/games/{game}/open` |
| POST | `/games/{game}/{action}` |
| POST | `/games/{game}/close` |
| POST | `/games/{game}/end` |

## المحتوى العام

| Method | Endpoint |
|---|---|
| GET | `/banners` |
| GET | `/stories` |
| POST | `/stories/{id}/view` |
| GET | `/bet-with-robert` |
| POST | `/dreams` |

## المتجر

| Method | Endpoint |
|---|---|
| GET | `/products` |
| GET | `/products/{id}` |
| POST | `/purchases` |
| GET | `/purchases` |
| GET | `/purchases/{id}` |

## الإشعارات

| Method | Endpoint |
|---|---|
| GET | `/notifications` |
| GET | `/notifications/unread-count` |
| PATCH | `/notifications/{id}/read` |
| DELETE | `/notifications/{id}` |
| POST | `/notifications/mark-all-read` |
| GET | `/notifications/broadcasts` |
| POST | `/notifications/broadcasts/read` |

## بطاقات التوقع

| Method | Endpoint |
|---|---|
| GET | `/prediction-packages` |
| GET | `/prediction-packages/{id}` |
| GET | `/prediction-packages/{id}/submission/me` |
| POST | `/prediction-packages/{id}/submissions` |

## Realtime

- WebSocket: `ws.robert.vip`
- `/broadcasting/auth`
- `/broadcasting/user-auth`

## أفضل ربط آمن مع البوت

### المرحلة الأولى — دون API خاص

- زر فتح Robert.vip.
- إنشاء حساب وتسجيل الدخول عبر الموقع نفسه.
- روابط UTM للحملات.
- بوابة Mini App تعريفية.

### المرحلة الثانية — تحتاج اتفاق API رسمي

- ربط Telegram بحساب Robert.vip باستخدام one-time code.
- قراءة الملف الشخصي والمحفظة بصلاحيات محددة.
- عرض إشعارات الموقع في البوت.
- استرداد Promo code عبر endpoint رسمي.

### المطلوب من فريق Robert.vip

- API documentation أو OpenAPI.
- طريقة server-to-server authentication.
- OAuth أو one-time account-link token.
- Webhook events.
- Scopes وصلاحيات واضحة.
- Rate limits.
- سياسة CORS والأمان.

## تحذير أمني

لا ينبغي أن يطلب البوت كلمة مرور Robert.vip أو يخزن Bearer token المأخوذ من المتصفح. الربط الصحيح يكون بتوكن قصير العمر ومخصص للربط، أو OAuth/API key رسمي.
