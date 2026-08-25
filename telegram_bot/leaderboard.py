# ============================================================
# 🏆 محرك لوحة المتصدرين الأسبوعية (Turnover Leaderboard Engine)
# ------------------------------------------------------------
# منطق خالص (Pure Logic) بدون قاعدة بيانات أو شبكة، ليكون:
#   1) قابلاً للاختبار مباشرة في tests/simulate_weekly_leaderboard.py
#   2) مستخدماً من المجدول (main.py) وطبقة البيانات (repository.py)
#
# الفكرة الأساسية:
#   iChancy تعيد "إجمالي المراهنات" التراكمي (totalBet) لكل لاعب.
#   لا يمكننا الوثوق بوجود فلتر زمني في API كازينو طرف ثالث،
#   لذا نعتمد الفرق: دوران الأسبوع = الإجمالي الحالي - قيمة الخط الأساس
#   المحفوظة محلياً في turnover_leaderboard_snapshots.
# ============================================================
from datetime import datetime, timedelta, timezone, date

# 🕒 توقيت سوريا الثابت (UTC+3) — يتطابق مع repository.get_syria_now
SYRIA_TZ = timezone(timedelta(hours=3), name="Asia/Damascus")

# نافذة التسوية: أول 5 ثوانٍ من يوم الاثنين فصاعداً وحتى 48 ساعة
SETTLE_DELAY_MINUTES = 5          # التسوية تبدأ بعد دخول الاثنين بخمس دقائق
SETTLE_MAX_AGE_HOURS = 48         # تسوية متأخرة (تعويض انقطاع) حتى يومين كحد أقصى


def syria_now(reference=None):
    """الوقت الحالي بتوقيت سوريا (أو تحويل مرجع معطى)."""
    if reference is None:
        return datetime.now(SYRIA_TZ)
    if isinstance(reference, datetime):
        return reference.astimezone(SYRIA_TZ) if reference.tzinfo else reference.replace(tzinfo=SYRIA_TZ)
    raise TypeError("reference must be datetime or None")


def week_monday(reference=None):
    """تاريخ (date) يوم الاثنين لبداية الأسبوع الذي يقع فيه المرجع (بتوقيت سوريا)."""
    now = syria_now(reference)
    monday = now.date() - timedelta(days=now.weekday())
    return monday


def previous_week_monday(reference=None):
    """تاريخ يوم الاثنين لبداية الأسبوع الـمنصرم (الذي يجب تسويته الآن)."""
    return week_monday(reference) - timedelta(days=7)


def week_label(week_start):
    """معرّف نصي ثابت للأسبوع: '2026-08-24' — يُستخدم لضمان عدم التكرار (Idempotency)."""
    if isinstance(week_start, datetime):
        week_start = week_start.date()
    return week_start.isoformat()


def settlement_due(last_settled_label, reference=None):
    """هل حان موعد تسوية الأسبوع الـمنصرم؟

    الشروط:
      1) مرّ على بداية أسبوع التتبع الحالي 5 دقائق على الأقل (حانة الحسابات)،
      2) لم نتجاوز 48 ساعة (أسبوع فائت أقدم من ذلك يُتخطّى عمداً — زر 'تسوية يدوية' يعالجه)،
      3) الأسبوع الـمنصرم لم يُسوَّ سابقاً.
    """
    now = syria_now(reference)
    current_monday = week_monday(now)
    prev_monday = current_monday - timedelta(days=7)
    settle_from = datetime.combine(current_monday, datetime.min.time(), SYRIA_TZ) + timedelta(minutes=SETTLE_DELAY_MINUTES)
    if now < settle_from:
        return False
    if (now - settle_from) > timedelta(hours=SETTLE_MAX_AGE_HOURS):
        return False
    return str(last_settled_label or "") != week_label(prev_monday)


def next_settlement_datetime(reference=None):
    """الموعد القادم للتسوية (اثنين 00:05 بتوقيت سوريا)."""
    now = syria_now(reference)
    current_monday = week_monday(now)
    settle_at = datetime.combine(current_monday, datetime.min.time(), SYRIA_TZ) + timedelta(minutes=SETTLE_DELAY_MINUTES)
    while settle_at <= now:
        settle_at += timedelta(days=7)
    return settle_at


def compute_standings(snapshots, min_weekly_turnover=0, limit=10):
    """حساب الترتيب الأسبوعي من صفوف لقطات الحالة.

    snapshots: قائمة dict بالمفاتيح:
        player_id, telegram_id, username, baseline_turnover, last_turnover
    تعيد قائمة مرتبة تنازلياً بالدوران الأسبوعي (الفارق عن الخط الأساس).
    - الفرق السالب (تصفير عداد iChancy مثلاً) يُعتبر صفراً.
    - من لا يتجاوز min_weekly_turnover يُستبعد من الترتيب التنافسي.
    - التعادل: الأعلى إجمالي تراكمي، ثم أصغر player_id لضمان ترتيب حتمي ثابت.
    """
    entries = []
    for row in snapshots or []:
        try:
            last = int(row.get("last_turnover") or 0)
            baseline = int(row.get("baseline_turnover") or 0)
        except (TypeError, ValueError):
            continue
        weekly = max(0, last - baseline)
        entries.append({
            "player_id": str(row.get("player_id") or ""),
            "telegram_id": str(row.get("telegram_id") or ""),
            "username": row.get("username") or "لاعب",
            "weekly_turnover": weekly,
            "total_turnover": last,
            "baseline_turnover": baseline,
        })

    entries.sort(key=lambda e: (-e["weekly_turnover"], -e["total_turnover"], e["player_id"]))

    ranked = []
    rank = 0
    for e in entries:
        if e["weekly_turnover"] < int(min_weekly_turnover or 0):
            continue
        rank += 1
        item = dict(e)
        item["rank"] = rank
        ranked.append(item)
        if limit and rank >= int(limit):
            break
    return ranked


def find_user_rank(snapshots, telegram_id, min_weekly_turnover=0):
    """ترتيب مستخدم معيّن ودورانه الأسبوعي (None إن لم يكن متتبَّعاً أو غير مؤهل)."""
    ranked_all = compute_standings(snapshots, min_weekly_turnover=0, limit=0)
    tid = str(telegram_id)
    for e in ranked_all:
        if e["telegram_id"] == tid:
            qualifies = e["weekly_turnover"] >= int(min_weekly_turnover or 0)
            return {
                "rank": e["rank"],
                "weekly_turnover": e["weekly_turnover"],
                "qualifies": qualifies,
                "tracked": True,
            }
    return {"rank": None, "weekly_turnover": 0, "qualifies": False, "tracked": False}


def assign_prizes(standings, prize_1=0, prize_2=0, prize_3=0):
    """توزيع جوائز المراكز الثلاثة الأولى على الترتيب النهائي.

    تعيد قائمة الفائزين فقط (rank 1..3 مع prize_syp)،
    حتى لو كانت جائزة مركز ما صفراً (يُسجّل للسجل لكن لا يُضاف رصيد).
    """
    prizes = {1: int(prize_1 or 0), 2: int(prize_2 or 0), 3: int(prize_3 or 0)}
    winners = []
    for entry in (standings or [])[:3]:
        winners.append({
            **entry,
            "prize_syp": prizes.get(entry["rank"], 0),
        })
    return winners


def format_turnover(value):
    """تنسيق رقم الدوران للعرض: 1,234,567"""
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)
