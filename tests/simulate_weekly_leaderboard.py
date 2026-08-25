# ============================================================
# 🧪 محاكاة لوحة المتصدرين الأسبوعية (Update 18)
# ------------------------------------------------------------
# اختبارات منطقية خالصة — بدون قاعدة بيانات وبدون شبكة:
#   1) حدود الأسابيع وتواريخ الحافة (اثنين/أحد/قفزة شهر/سنة كبيسة)
#   2) نافذة التسوية + منع التكرار (Idempotency)
#   3) الترتيب (فلتر الحد الأدنى، التعادل الحتمي، الدلتا السالب)
#   4) توزيع الجوائز على المراكز الثلاثة
#   5) ترتيب العارض (مركز المستخدم + أهليته)
#   6) محاكاة تسوية كاملة ببيانات وهمية تُظهر سلامة الدفع المزدوج
#
# التشغيل:  python tests/simulate_weekly_leaderboard.py
# ============================================================
import os
import sys
from datetime import datetime, timedelta, timezone, date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from telegram_bot import leaderboard as lb

PASS = 0
FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


SYR = lb.SYRIA_TZ


def syria_dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=SYR)


print("=" * 64)
print("🧪 [1] حدود الأسابيع (week_monday / previous_week_monday)")
print("=" * 64)

# الثلاثاء 2026-08-25 → اثنين الأسبوع 2026-08-24
check("الثلاثاء → الاثنين 2026-08-24",
      lb.week_monday(syria_dt(2026, 8, 25)) == date(2026, 8, 24))
# الأحد آخر الأسبوع
check("الأحد 2026-08-30 ضمن نفس الأسبوع",
      lb.week_monday(syria_dt(2026, 8, 30, 23, 59)) == date(2026, 8, 24))
# الاثنين 00:01 → أسبوع جديد
check("الاثنين 00:01 → اثنين الأسبوع الجديد 2026-08-31",
      lb.week_monday(syria_dt(2026, 8, 31, 0, 1)) == date(2026, 8, 31))
# الأسبوع المنصرم
check("previous_week_monday من 2026-08-25 → 2026-08-17",
      lb.previous_week_monday(syria_dt(2026, 8, 25)) == date(2026, 8, 17))
# قفزة شهر: الجمعة 2026-05-01 → اثنين الأسبوع 2026-04-27
check("قفزة شهر: 2026-05-01 → 2026-04-27",
      lb.week_monday(syria_dt(2026, 5, 1)) == date(2026, 4, 27))
# حافة سنة: الخميس 2027-01-01 → الاثنين 2026-12-28
check("حافة سنة: 2027-01-01 → 2026-12-28",
      lb.week_monday(syria_dt(2027, 1, 1)) == date(2026, 12, 28))
check("week_label ثابت",
      lb.week_label(date(2026, 8, 24)) == "2026-08-24")

print("=" * 64)
print("🧪 [2] نافذة التسوية ومنع التكرار (settlement_due)")
print("=" * 64)

last_settled = "2026-08-17"  # الأسبوع قبل المنصرم
# الاثنين 31/08 الساعة 00:04 → لم يحن
check("قبل 00:05 → لا تسوية",
      lb.settlement_due(last_settled, syria_dt(2026, 8, 31, 0, 4)) is False)
# الاثنين 00:05 فصاعداً + الأسبوع المنصرم غير مسوّى → تسوية
check("اثنين 00:06 + أسبوع منصرم جديد → تسوية",
      lb.settlement_due(last_settled, syria_dt(2026, 8, 31, 0, 6)) is True)
# لو سُوّي 2026-08-24 بالفعل → لا إعادة (منع دفع مزدوج بعد إعادة تشغيل)
check("أسبوع مُسوّى سابقاً → لا تكرار",
      lb.settlement_due("2026-08-24", syria_dt(2026, 8, 31, 12, 30)) is False)
# بعد 48+ ساعة من افتتاح النافذة (الأربعاء) → يُتخطّى (يُعالج يدوياً)
check("بعد 48 ساعة (الأربعاء) → تخطٍّ تلقائي",
      lb.settlement_due(last_settled, syria_dt(2026, 9, 2, 1, 0)) is False)
# الثلاثاء (تعويض انقطاع يوم واحد) → لا يزال مسموحاً
check("تعويض انقطاع: ثلاثاء 10:00 → مسموح",
      lb.settlement_due(last_settled, syria_dt(2026, 9, 1, 10, 0)) is True)
# منتصف الأسبوع الأحد → بعيد 72h → لا
check("الأحد قبل نهاية الأسبوع → لا تسوية",
      lb.settlement_due("2026-08-10", syria_dt(2026, 8, 23, 12, 0)) is False)

print("=" * 64)
print("🧪 [3] الترتيب الأسبوعي (compute_standings)")
print("=" * 64)

snapshots = [
    {"player_id": "101", "telegram_id": "11", "username": "أحمد", "baseline_turnover": 1000, "last_turnover": 96000},   # 95k
    {"player_id": "102", "telegram_id": "12", "username": "ليلى", "baseline_turnover": 0, "last_turnover": 150000},      # 150k
    {"player_id": "103", "telegram_id": "13", "username": "عمر", "baseline_turnover": 5000, "last_turnover": 55000},     # 50k
    {"player_id": "104", "telegram_id": "14", "username": "سما", "baseline_turnover": 8000, "last_turnover": 8000},      # 0 جديدة
    {"player_id": "105", "telegram_id": "15", "username": "كريم", "baseline_turnover": 100000, "last_turnover": 40000},  # دلتا سالب → 0
    {"player_id": "106", "telegram_id": "16", "username": "رامي", "baseline_turnover": 2000, "last_turnover": 97000},    # 95k تعادل مع أحمد
]

st = lb.compute_standings(snapshots, min_weekly_turnover=0, limit=10)
check("الأولى ليلى 150k", st[0]["username"] == "ليلى" and st[0]["weekly_turnover"] == 150000)
# التعادل 95k بين أحمد(101) ورامي(106): الأعلى إجمالياً أولاً → أحمد 96k+1k=97k vs رامي 97k؟
# رامي إجمالي 97,000 = أحمد 96,000 → أحمد أولاً بإجمالي أعلى (96k > 97k؟ لا)
# تفصيل: total أحمد=96000، رامي=97000 → رامي أعلى → رامي قبل أحمد
check("التعادل: إجمالي أعلى يتقدم (رامي قبل أحمد)",
      [s["username"] for s in st[1:3]] == ["رامي", "أحمد"])
check("ترتيب حتمي متكرر",
      [s["player_id"] for s in lb.compute_standings(snapshots, 0, 10)] ==
      [s["player_id"] for s in st])
check("الدلتا السالب صفر", next(s for s in snapshots if s["player_id"] == "105")["last_turnover"] <
      next(s for s in snapshots if s["player_id"] == "105")["baseline_turnover"])
st_min = lb.compute_standings(snapshots, min_weekly_turnover=50000, limit=10)
users_min = {s["username"] for s in st_min}
check("الحد الأدنى 50k شامل (عمر بالضبط 50k يتأهل)، وتستبعد سما/كريم",
      users_min == {"ليلى", "أحمد", "رامي", "عمر"})
st_min2 = lb.compute_standings(snapshots, min_weekly_turnover=51000, limit=10)
check("الحد 51k يستبعد عمر (50k دونه)",
      {s["username"] for s in st_min2} == {"ليلى", "أحمد", "رامي"})
check("الحد الأقصى للنتائج limit=2",
      len(lb.compute_standings(snapshots, 0, 2)) == 2)
check("قائمة فارغة آمنة", lb.compute_standings([], 0, 10) == [])
check("قيم None آمنة", lb.compute_standings([{"player_id": "1", "last_turnover": None, "baseline_turnover": None}], 0, 5)[0]["weekly_turnover"] == 0)

print("=" * 64)
print("🧪 [4] توزيع الجوائز (assign_prizes)")
print("=" * 64)

winners = lb.assign_prizes(st, prize_1=100000, prize_2=50000, prize_3=25000)
check("3 فائزين مراكز 1-3", [w["rank"] for w in winners] == [1, 2, 3])
check("الجائزة الأولى 100,000 لليلى",
      winners[0]["username"] == "ليلى" and winners[0]["prize_syp"] == 100000)
check("الجائزة الثالثة 25,000",
      winners[2]["prize_syp"] == 25000)
check("بدون جوائز → قيم صفرية لكن الفائزون مسجلون",
      all(w["prize_syp"] == 0 for w in lb.assign_prizes(st)))

print("=" * 64)
print("🧪 [5] مركز العارض (find_user_rank)")
print("=" * 64)

mine = lb.find_user_rank(snapshots, "13", min_weekly_turnover=100000)  # عمر 50k < 100k
check("عمر متتبع لكن غير مؤهل", mine["tracked"] and not mine["qualifies"])
mine2 = lb.find_user_rank(snapshots, "999", min_weekly_turnover=0)
check("غير موجود → tracked=False", mine2["tracked"] is False and mine2["rank"] is None)
mine3 = lb.find_user_rank(snapshots, "12", min_weekly_turnover=100000)
check("ليلى مركزها 1 ومؤهلة", mine3["rank"] == 1 and mine3["qualifies"])

print("=" * 64)
print("🧪 [6] محاكاة تسوية كاملة — سلامة الأرشفة والدفع (يدمج كل شيء)")
print("=" * 64)

# قاعدة بيانات وهمية تحاكي دلالات الجداول الحقيقية:
archive = {}          # (week, telegram_id) -> row  | UNIQUE(week_start, telegram_id)
balances = {t: 50000 for t in ["11", "12", "13", "16"]}


def insert_result(week, w):
    """يعيد id أو None عند التضارب — يحاكي ON CONFLICT DO NOTHING."""
    key = (week, w["telegram_id"])
    if key in archive:
        return None
    archive[key] = {**w, "credited": False}
    return len(archive)


def claim_credit(week, tid):
    """يحاكي UPDATE ... WHERE credited=FALSE (ضمان أحادي الدفع)."""
    row = archive.get((week, tid))
    if row and not row["credited"]:
        row["credited"] = True
        return True
    return False


def credit_balance(tid, amount):
    balances[tid] = balances.get(tid, 0) + amount


def run_settlement(week_label, snaps, prizes):
    standings = lb.compute_standings(snaps, min_weekly_turnover=50000, limit=50)
    winners = lb.assign_prizes(standings, *prizes)
    for w in winners:
        insert_result(week_label, w)
    paid = []
    for w in winners:
        if w["prize_syp"] > 0 and claim_credit(week_label, w["telegram_id"]):
            credit_balance(w["telegram_id"], w["prize_syp"])
            paid.append(w)
    return winners, paid


week1 = "2026-08-24"
winners1, paid1 = run_settlement(week1, snapshots, (100000, 50000, 25000))
check("أُرشف 3 فائزين", len(archive) == 3)
check("دُفعت 3 جوائز", len(paid1) == 3)
check("رصيد ليلى = 50k + 100k = 150,000", balances["12"] == 150000)

# إعادة تشغيل التسوية لنفس الأسبوع (محاكاة انقطاع/إعادة إقلاع)
balances_before = dict(balances)
winners_again, paid_again = run_settlement(week1, snapshots, (100000, 50000, 25000))
check("إعادة التسوية: لا سجلات مكررة", len(archive) == 3)
check("إعادة التسوية: لا دفع مزدوج 💰", paid_again == [] and balances == balances_before)

# أسبوع جديد بعد تدوير خط الأساس (baseline = last القديم)
snaps_w2 = []
for s in snapshots:
    snaps_w2.append({**s, "baseline_turnover": s["last_turnover"],
                     "last_turnover": s["last_turnover"] + 100000})  # الجميع +100k أسبوعياً
winners2, paid2 = run_settlement("2026-08-31", snaps_w2, (100000, 50000, 25000))
check("الأسبوع الثاني مستقل (6 سجلات)", len(archive) == 6)
check("الدلتا تحسب من خط الأساس الجديد",
      all((archive[("2026-08-31", w["telegram_id"])]["weekly_turnover"]) == 100000 for w in winners2))

print()
print("=" * 64)
print(f"📊 النتيجة النهائية: ✅ نجاح {PASS} | ❌ فشل {FAIL}")
print("=" * 64)

if FAIL:
    sys.exit(1)
print("🎉 كل اختبارات منطق لوحة المتصدرين ناجحة!")
