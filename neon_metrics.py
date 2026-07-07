"""
Neon Consumption Metrics Client (Free Plan)
===========================================
يجلب بيانات استخدام Neon للخطة المجانية عبر endpoint المشروع:
    GET https://console.neon.tech/api/v2/projects/{project_id}

لماذا هذا الـ endpoint وليس consumption_history/v2؟
- consumption_history/v2/projects متاح فقط للخطط المدفوعة (يُرجع 403 على Free).
- endpoint المشروع يُرجع حقولاً مسطّحة (flat) تمثّل استهلاك دورة الفوترة الحالية،
  وهي الأنسب لعرض "نسبة الاستخدام من الحد المجاني".

الحقول المستخدمة من كائن project:
- compute_time_seconds        : ثوانٍ حوسبة (تُقسم على 3600 → CU-hours)
- data_transfer_bytes         : بايتات نقل الشبكة خلال الدورة
- consumption_period_start    : بداية دورة الاستهلاك (متى تُصفَّر العدادات)
- consumption_period_end      : نهاية الدورة

قياس التخزين (مهم):
- الحد المجاني للتخزين (0.5 GB) يُقاس على الحجم اللحظي الفعلي، وليس byte-hours.
- لذلك لا نستخدم data_storage_bytes_hour لحساب حصة التخزين، بل نجمع logical_size
  من كل الفروع عبر endpoint منفصل: GET /projects/{id}/branches.
- ملاحظة: logical_size قد يغيب للفروع الخاملة (بلا compute نشط)، لذا نتجاهل الغائب.

ملاحظات وحدات مهمّة (وفق توثيق Neon):
- Neon يستخدم جيجابايت عشري: 1 GB = 1,000,000,000 بايت (وليس GiB / 1024^3).
- شهر الفوترة ثابت = 744 ساعة (31 × 24).
- القيم تُحدَّث بتأخير (~15 دقيقة، وقد تصل لساعة)، وتُصفَّر عند بداية كل دورة.
"""

import os
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

NEON_API_BASE = "https://console.neon.tech/api/v2"

# ثابت التحويل الصحيح: جيجابايت عشري + شهر فوترة ثابت 744 ساعة
GB = 1_000_000_000            # 1 GB = 10^9 بايت (وليس 1024^3)
BILLING_HOURS = 744          # 31 يوم × 24 ساعة

# حدود الخطة المجانية (لكل مشروع / لكل شهر) — وفق توثيق Neon
FREE_TIER_LIMITS = {
    "compute_cu_hours": 100.0,   # 100 CU-hours / project / month
    "storage_gb": 0.5,           # 0.5 GB
    "transfer_gb": 5.0,          # 5 GB / month
}

# ===== كاش داخلي للحد من طلبات Neon (تُحدَّث كل ~15 دقيقة) =====
CACHE_TTL_SECONDS = 15 * 60
_cache: Dict[str, Any] = {"data": None, "fetched_at": 0.0}
_lock = asyncio.Lock()


def _env() -> Dict[str, Optional[str]]:
    return {
        "api_key": os.getenv("NEON_API_KEY"),
        "project_id": os.getenv("NEON_PROJECT_ID"),
    }


def _hours_elapsed_in_period(period_start_iso: Optional[str]) -> float:
    """عدد الساعات المنقضية منذ بداية دورة الاستهلاك حتى الآن."""
    if not period_start_iso:
        return 0.0
    try:
        start = datetime.fromisoformat(period_start_iso.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta_hours = (now - start).total_seconds() / 3600.0
        return max(delta_hours, 0.0)
    except Exception:
        return 0.0


def _shape_project_metrics(
    project: Dict[str, Any],
    storage_bytes: Optional[float] = None,
    branches_count: int = 0,
) -> Dict[str, Any]:
    """يحوّل حقول project الخام إلى قيم جاهزة للعرض + نسب الاستخدام.

    storage_bytes: الحجم اللحظي الفعلي (مجموع logical_size من الفروع) بالبايت.
                   إن كان None نلجأ لتقدير من data_storage_bytes_hour (احتياطي فقط).
    """
    compute_seconds = float(project.get("compute_time_seconds") or 0)
    transfer_bytes = float(project.get("data_transfer_bytes") or 0)
    storage_byte_hours = float(project.get("data_storage_bytes_hour") or 0)
    period_start = project.get("consumption_period_start")
    period_end = project.get("consumption_period_end")

    hours_elapsed = _hours_elapsed_in_period(period_start)

    # Compute → ساعات (كمية تراكمية، القسمة على 3600 صحيحة)
    compute_hours = compute_seconds / 3600.0

    # Storage: الحجم اللحظي الفعلي من الفروع (هو ما يُقاس عليه الحد المجاني)
    if storage_bytes is not None:
        storage_gb = storage_bytes / GB
        storage_source = "branches_logical_size"
    else:
        # احتياطي: تقدير متوسط GB من byte-hours إن تعذّر جلب الفروع
        storage_gb = (storage_byte_hours / hours_elapsed / GB) if hours_elapsed > 0 else 0.0
        storage_source = "byte_hours_estimate"

    # Transfer: بايتات → GB (جمع مباشر صحيح)
    transfer_gb = transfer_bytes / GB

    # نسب الاستخدام من الحد المجاني
    def pct(value: float, limit: float) -> float:
        if limit <= 0:
            return 0.0
        return round(min(value / limit * 100.0, 100.0), 1)

    usage_percent = {
        "compute": pct(compute_hours, FREE_TIER_LIMITS["compute_cu_hours"]),
        "storage": pct(storage_gb, FREE_TIER_LIMITS["storage_gb"]),
        "transfer": pct(transfer_gb, FREE_TIER_LIMITS["transfer_gb"]),
    }
    usage_percent["overall"] = max(usage_percent.values()) if usage_percent else 0.0

    return {
        "project_id": project.get("id"),
        "project_name": project.get("name"),
        "compute_hours": round(compute_hours, 2),
        "storage_gb": round(storage_gb, 4),
        "storage_source": storage_source,
        "branches_count": branches_count,
        "transfer_gb": round(transfer_gb, 4),
        "period_start": period_start,
        "period_end": period_end,
        "hours_elapsed": round(hours_elapsed, 1),
        "usage_percent": usage_percent,
        "limits": FREE_TIER_LIMITS,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


async def _fetch_branches_storage(
    session: aiohttp.ClientSession, project_id: str, headers: Dict[str, str]
) -> Optional[Dict[str, Any]]:
    """يجلب الحجم اللحظي الفعلي: مجموع logical_size من كل الفروع.

    يُرجع {'bytes': int, 'count': int} أو None عند الفشل (فنلجأ للاحتياطي).
    ملاحظة: logical_size قد يغيب للفروع الخاملة، فنتجاهل الغائب.
    """
    url = f"{NEON_API_BASE}/projects/{project_id}/branches"
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:
        logger.warning("Neon branches fetch failed: %s", e)
        return None

    branches = data.get("branches", []) if isinstance(data, dict) else []
    total = 0
    for br in branches:
        size = br.get("logical_size")
        if isinstance(size, (int, float)):
            total += size
    return {"bytes": total, "count": len(branches)}


async def _fetch_project(session: aiohttp.ClientSession) -> Dict[str, Any]:
    """يجلب كائن المشروع + حجم التخزين الفعلي من الفروع (بدون كاش)."""
    env = _env()
    if not env["api_key"] or not env["project_id"]:
        return {
            "ok": False,
            "configured": False,
            "error": "NEON_API_KEY و/أو NEON_PROJECT_ID غير مضبوطة",
        }

    project_id = env["project_id"]
    headers = {
        "Authorization": f"Bearer {env['api_key']}",
        "Accept": "application/json",
    }
    url = f"{NEON_API_BASE}/projects/{project_id}"

    async with session.get(
        url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
    ) as resp:
        text = await resp.text()
        if resp.status == 403:
            return {
                "ok": False, "configured": True, "status": 403,
                "error": "403: تحقق من صلاحية المفتاح أو أن المشروع تابع لهذا الحساب.",
            }
        if resp.status >= 400:
            return {
                "ok": False, "configured": True, "status": resp.status,
                "error": (text or "")[:400],
            }
        data = await resp.json()

    project = data.get("project") if isinstance(data, dict) else None
    if not project:
        return {"ok": False, "configured": True, "error": "لا يوجد كائن project في الرد"}

    # الحجم اللحظي الفعلي من الفروع (الأصح لقياس الحد المجاني)
    storage = await _fetch_branches_storage(session, project_id, headers)
    storage_bytes = storage["bytes"] if storage else None
    branches_count = storage["count"] if storage else 0

    shaped = _shape_project_metrics(project, storage_bytes=storage_bytes, branches_count=branches_count)
    return {"ok": True, "configured": True, "source": "neon-project", **shaped}


async def get_neon_metrics(session: Optional[aiohttp.ClientSession] = None) -> Dict[str, Any]:
    """
    الواجهة الرئيسية: تُرجع مقاييس Neon مع كاش 15 دقيقة، قفل single-flight،
    ومنطق stale-while-error (إرجاع بيانات قديمة عند فشل التحديث).
    """
    now = time.time()
    if _cache["data"] and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
        return {**_cache["data"], "cache": {"hit": True, "stale": False}}

    async with _lock:
        # فحص مزدوج داخل القفل (double-checked)
        now = time.time()
        if _cache["data"] and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
            return {**_cache["data"], "cache": {"hit": True, "stale": False}}

        owns_session = session is None
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            data = await _fetch_project(session)
            if data.get("ok"):
                _cache["data"] = data
                _cache["fetched_at"] = time.time()
                return {**data, "cache": {"hit": False, "stale": False}}

            # فشل التحديث → أعد آخر بيانات صالحة إن وُجدت (stale)
            if _cache["data"]:
                logger.warning("Neon refresh failed, serving stale cache: %s", data.get("error"))
                return {
                    **_cache["data"],
                    "cache": {"hit": True, "stale": True},
                    "refresh_error": data.get("error"),
                }
            return {**data, "cache": {"hit": False, "stale": False}}

        except Exception as e:
            logger.error("Neon fetch exception: %s", e)
            if _cache["data"]:
                return {
                    **_cache["data"],
                    "cache": {"hit": True, "stale": True},
                    "refresh_error": str(e)[:300],
                }
            return {"ok": False, "configured": True, "error": str(e)[:300],
                    "cache": {"hit": False, "stale": False}}
        finally:
            if owns_session and session:
                await session.close()


def get_neon_status_text(data: Optional[Dict]) -> str:
    """نص حالة Neon جاهز للعرض في تيليجرام (للوحة الأدمن النصية)."""
    if not data or not data.get("ok"):
        reason = (data or {}).get("error", "غير متاح")
        return f"🔴 تعذر جلب بيانات Neon\n{reason}"

    pct = data.get("usage_percent", {})
    overall = pct.get("overall", 0)
    if overall >= 90:
        status = "🔴 قريب من الحد الأقصى"
    elif overall >= 70:
        status = "🟡 يقترب من الحد"
    else:
        status = "🟢 ضمن الحدود الآمنة"

    stale = " (بيانات مخزّنة)" if data.get("cache", {}).get("stale") else ""
    return (
        f"{status}{stale}\n\n"
        f"🧮 Compute: {data.get('compute_hours', 0):.1f} / {FREE_TIER_LIMITS['compute_cu_hours']:.0f} ساعة"
        f" ({pct.get('compute', 0)}%)\n"
        f"💾 Storage: {data.get('storage_gb', 0):.3f} / {FREE_TIER_LIMITS['storage_gb']} GB"
        f" ({pct.get('storage', 0)}%)\n"
        f"🌐 Transfer: {data.get('transfer_gb', 0):.3f} / {FREE_TIER_LIMITS['transfer_gb']} GB"
        f" ({pct.get('transfer', 0)}%)"
    )


# للاختبار السريع محلياً
if __name__ == "__main__":
    async def _test():
        result = await get_neon_metrics()
        print(result)
        print("---")
        print(get_neon_status_text(result))
    asyncio.run(_test())
