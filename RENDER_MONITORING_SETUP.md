# ☁️ إعداد مراقبة Render في لوحة المشرف

يعرض بطاقة **☁️ Render** داخل تبويب «حالة النظام»: حالة الخدمة (نشطة/معلّقة)، الخطة، آخر نشر، والنطاق (إن توفّر).

## الخطوات

### 1. أنشئ مفتاح Render API
**Render Dashboard → Account Settings → API Keys → Create API Key**

### 2. احصل على معرّف الخدمة (Service ID)
من رابط لوحة خدمتك، الجزء `srv-xxxxxxxxxxxx`:
```
https://dashboard.render.com/web/srv-xxxxxxxxxxxxxxxxxxxx
```

### 3. أضف المتغيرات في Render → Environment
```env
RENDER_API_KEY=your_render_api_key_here
RENDER_SERVICE_ID=srv-xxxxxxxxxxxxxxxxxxxx
RENDER_ENABLE_PROBE=false
```
ثم أعد النشر.

## ماذا يُعرض؟

| العنصر | المصدر | متاح على المجاني؟ |
|---|---|---|
| حالة الخدمة (نشطة/معلّقة) | `GET /v1/services/{id}` | ✅ نعم |
| الخطة والمنطقة | `serviceDetails` | ✅ نعم |
| آخر نشر وحالته | `GET /v1/services/{id}/deploys` | ✅ نعم |
| النطاق المستهلك (24 ساعة) | `GET /v1/metrics/bandwidth` | ⚠️ قد لا يتوفّر (يظهر تنبيه) |
| فحص الاستيقاظ (cold start) | probe لرابط الخدمة | اختياري (يوقظ الخدمة) |

## قيود مهمّة (بخلاف Neon)

- **الساعات المتبقية (750/شهر)** ودقائق البناء **غير متاحة عبر API** — تُراجع من لوحة Render (Billing) فقط.
- **«معلّقة» (`suspended`)** تعني تعليقاً صارماً (نفاد الساعات/تعليق يدوي)، **وليس** التوقّف بالخمول.
- الخطة المجانية توقف الخدمة بعد ~15 دقيقة خمول وتستيقظ خلال ~دقيقة (cold start). لا يوجد حدث API رسمي للخمول — يُكتشف فقط عبر `RENDER_ENABLE_PROBE=true` (لكنه يوقظ الخدمة ويستهلك من الساعات، لذا اتركه معطّلاً إلا للفحص اليدوي).

## الأمان
- مفتاح Render يمنح صلاحية كاملة على الحساب (لا يوجد read-only scoping) — احفظه في Render Environment فقط، ولا تكتبه في المستودع.
