"""
Render Service Metrics Client (Free Plan)
=========================================
يجلب حالة خدمة Render لعرضها في لوحة المشرف، مصمّم للخطة المجانية.

الحقائق التي بُني عليها (من api-docs.render.com):
- Base URL: https://api.render.com/v1  (مصادقة: Bearer <RENDER_API_KEY>)
- حالة الخدمة:  GET /v1/services/{serviceId}
    → service.suspended ("not_suspended" | "suspended")
    → service.serviceDetails.plan ("free" ...)، region، url
- آخر عمليات النشر: GET /v1/services/{serviceId}/deploys?limit=1
- المقاييس (CPU/الذاكرة/النطاق): GET /v1/metrics/...  — توفّرها على الخطة
  المجانية غير مؤكّد (قد تُرجع 403/فارغ)، لذا نتعامل معها بمرونة ولا نعتمد عليها.

قيود مهمّة يجب توضيحها للمستخدم:
- لا يوجد endpoint عام يُظهر "ساعات التشغيل المتبقية (750/شهر)" أو دقائق البناء؛
  هذه تُرى في لوحة Render (Billing) فقط. لذا نعرضها كـ "غير متاح عبر API".
- suspended == "suspended" تعني تعليقاً صارماً (نفاد الساعات/تعليق يدوي)،
  وليس التوقّف بالخمول (15 دقيقة). لا يوجد حدث رسمي للتوقّف بالخمول.
- الخطة المجانية توقف الخدمة بعد ~15 دقيقة خمول، وتستيقظ خلال ~دقيقة (cold start).
  نكتشف ذلك اختيارياً بقياس زمن الاستجابة لرابط الخدمة (probe).
"""

import os
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

RENDER_API_BASE = "https://api.render.com/v1"

# كاش 60 ثانية (حالة الخدمة نادرة التغيّر؛ حدود Render على GET سخية لكن نتحفّظ)
CACHE_TTL_SECONDS = 60
_cache: Dict[str, Any] = {"data": None, "fetched_at": 0.0}
_lock = asyncio.Lock()


def _env() -> Dict[str, Optional[str]]:
    return {
        "api_key": os.getenv("RENDER_API_KEY"),
        "service_id": os.getenv("RENDER_SERVICE_ID"),
        # رابط الخدمة العام (يُستخدم لفحص cold-start الاختياري)
        "external_url": os.getenv("RENDER_EXTERNAL_URL"),
        # هل نفعّل فحص الاستيقاظ؟ (يوقظ الخدمة، لذا افتراضياً معطّل)
        "probe": os.getenv("RENDER_ENABLE_PROBE", "false").lower() == "true",
    }


def _fmt_dt(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso


async def _get_json(session, url, headers) -> Any:
    async with session.get(
        url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)
    ) as resp:
        status = resp.status
        try:
            body = await resp.json()
        except Exception:
            body = None
        return status, body


async def _fetch_service(session: aiohttp.ClientSession) -> Dict[str, Any]:
    env = _env()
    if not env["api_key"] or not env["service_id"]:
        return {
            "ok": False,
            "configured": False,
            "error": "RENDER_API_KEY و/أو RENDER_SERVICE_ID غير مضبوطة",
        }

    headers = {
        "Authorization": f"Bearer {env['api_key']}",
        "Accept": "application/json",
    }
    sid = env["service_id"]

    # 1) حالة الخدمة
    status, body = await _get_json(session, f"{RENDER_API_BASE}/services/{sid}", headers)
    if status == 401:
        return {"ok": False, "configured": True, "status": 401,
                "error": "401: مفتاح Render غير صالح."}
    if status == 404:
        return {"ok": False, "configured": True, "status": 404,
                "error": "404: RENDER_SERVICE_ID غير موجود أو لا يخصّ هذا الحساب."}
    if status >= 400 or not isinstance(body, dict):
        return {"ok": False, "configured": True, "status": status,
                "error": f"تعذّر جلب الخدمة (HTTP {status})."}

    details = body.get("serviceDetails", {}) or {}
    suspended_raw = body.get("suspended", "")
    is_suspended = suspended_raw == "suspended"

    result: Dict[str, Any] = {
        "ok": True,
        "configured": True,
        "service_id": body.get("id"),
        "name": body.get("name"),
        "type": body.get("type"),
        "plan": details.get("plan"),
        "region": details.get("region"),
        "url": details.get("url") or env.get("external_url"),
        "suspended": is_suspended,
        "suspended_raw": suspended_raw,
        "dashboard_url": body.get("dashboardUrl"),
        "updated_at": _fmt_dt(body.get("updatedAt")),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    # 2) آخر عملية نشر (اختياري — لا نُفشل البطاقة إن غابت)
    try:
        st, deploys = await _get_json(
            session, f"{RENDER_API_BASE}/services/{sid}/deploys?limit=1", headers
        )
        if st == 200 and isinstance(deploys, list) and deploys:
            d = deploys[0].get("deploy", deploys[0]) or {}
            result["last_deploy"] = {
                "status": d.get("status"),
                "created_at": _fmt_dt(d.get("createdAt")),
                "finished_at": _fmt_dt(d.get("finishedAt")),
            }
    except Exception as e:
        logger.warning("Render deploys fetch failed: %s", e)

    # 3) النطاق المستهلك (قد لا يتوفّر على المجاني — نتعامل بمرونة)
    try:
        # آخر 24 ساعة، دقة ساعة
        params = f"?resource={sid}&resolutionSeconds=3600"
        st, bw = await _get_json(
            session, f"{RENDER_API_BASE}/metrics/bandwidth{params}", headers
        )
        if st == 200 and isinstance(bw, list) and bw:
            total_bytes = 0.0
            for series in bw:
                for point in series.get("values", []):
                    total_bytes += float(point.get("value") or 0)
            result["bandwidth_24h_mb"] = round(total_bytes / 1_000_000, 2)
        elif st == 403:
            result["bandwidth_note"] = "المقاييس التفصيلية تتطلب خطة مدفوعة"
    except Exception as e:
        logger.warning("Render bandwidth fetch failed: %s", e)

    # 4) فحص cold-start اختياري (يوقظ الخدمة — معطّل افتراضياً)
    if env["probe"] and result.get("url"):
        try:
            t0 = time.perf_counter()
            async with session.get(
                result["url"], timeout=aiohttp.ClientTimeout(total=90)
            ) as r:
                await r.read()
                latency_ms = round((time.perf_counter() - t0) * 1000)
                result["probe"] = {
                    "http_status": r.status,
                    "latency_ms": latency_ms,
                    # cold start يستغرق ~دقيقة؛ >5 ثوانٍ = استيقاظ محتمل
                    "state": "cold_start" if latency_ms > 5000 else "warm",
                }
        except Exception as e:
            result["probe"] = {"state": "unreachable", "error": str(e)[:120]}

    # ملاحظة ثابتة عن القيود
    result["quota_note"] = "ساعات التشغيل المتبقية (750/شهر) غير متاحة عبر API — راجع لوحة Render."
    return result


async def get_render_metrics(session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """الواجهة الرئيسية: حالة Render مع كاش 60 ثانية و stale-while-error."""
    now = time.time()
    if _cache["data"] and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
        return {**_cache["data"], "cache": {"hit": True, "stale": False}}

    async with _lock:
        now = time.time()
        if _cache["data"] and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
            return {**_cache["data"], "cache": {"hit": True, "stale": False}}

        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            data = await _fetch_service(session)
            if data.get("ok"):
                _cache["data"] = data
                _cache["fetched_at"] = time.time()
                return {**data, "cache": {"hit": False, "stale": False}}

            if _cache["data"]:
                logger.warning("Render refresh failed, serving stale: %s", data.get("error"))
                return {**_cache["data"], "cache": {"hit": True, "stale": True},
                        "refresh_error": data.get("error")}
            return {**data, "cache": {"hit": False, "stale": False}}

        except Exception as e:
            logger.error("Render fetch exception: %s", e)
            if _cache["data"]:
                return {**_cache["data"], "cache": {"hit": True, "stale": True},
                        "refresh_error": str(e)[:300]}
            return {"ok": False, "configured": True, "error": str(e)[:300],
                    "cache": {"hit": False, "stale": False}}
        finally:
            if owns_session and session:
                await session.close()


# اختبار سريع محلي
if __name__ == "__main__":
    async def _test():
        print(await get_render_metrics())
    asyncio.run(_test())
