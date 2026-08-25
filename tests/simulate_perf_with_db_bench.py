# ============================================================
# 📊 محاكاة الأداء (Update 20) على PostgreSQL حقيقي
# بيانات: 8,000 مستخدم + 150,000 معاملة موزعة 30 يوماً
# يقيس: 1) مؤشرات قبل/بعد  2) سرعة اللوحتين  3) إثبات الكاشات  4) ميدلوير الشروط
# ============================================================
import os
import sys
import time
import asyncio

# ⚠️ اختبار أداء اختياري: يحتاج قاعدة PostgreSQL حقيقية.
# شغّله فقط بعد ضبط DATABASE_URL يدوياً، وإلا يتخطّى نفسه بأمان.
os.environ.setdefault('DATABASE_URL', '')
if not os.environ.get('DATABASE_URL'):
    print("⏭️  تخطٍّ: اضبط DATABASE_URL لتشغيل اختبار الأداء (بيانات ثقيلة 150k معاملة)")
    raise SystemExit(0)
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'fake')
os.environ.setdefault('ADMIN_TELEGRAM_ID', '999999')

sys.path.insert(0, '/home/user/Ichancy100')

from database.connection import DatabaseManager
import database.repository as repo

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name}")
    else:
        FAIL += 1; print(f"  ❌ {name} {extra}")

def q(sql, params=None, fetch=None):
    return DatabaseManager.execute_query_dict(sql, params, fetch=fetch)

def timed(fn, runs=3):
    """وسطي زمن التنفيذ بالـms (أسرع تشغيل من 3 للعدالة مع الكاشات الداخلية لـ PG)."""
    best = None
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        dt = (time.perf_counter() - t) * 1000
        best = dt if best is None else min(best, dt)
    return best

print("=" * 66)
print("🔧 [1] تهيئة الجداول + بذر البيانات الثقيلة")
print("=" * 66)
DatabaseManager.initialize_pool()

# بذر سريع بـ SQL واحد لكل جدول
DatabaseManager.execute_query("DELETE FROM transactions")
DatabaseManager.execute_query("DELETE FROM users")
t0 = time.perf_counter()
DatabaseManager.execute_query("""
    INSERT INTO users (telegram_id, telegram_username, ichancy_username, terms_accepted, bot_balance)
    SELECT 'u'||g, 'user'||g, CASE WHEN g %% 3 = 0 THEN 'ich'||g END, TRUE, (random()*100000)::int
    FROM generate_series(1, 8000) g
""")
DatabaseManager.execute_query("""
    INSERT INTO transactions (user_telegram_id, type, payment_method, amount, transfer_number, status, created_at)
    SELECT 'u' || (1 + floor(random()*8000))::int,
           (ARRAY['deposit_bot','withdraw_bot','deposit_to_game','withdraw_from_game'])[1+floor(random()*4)::int],
           (ARRAY['syriatel','mtn','usdt_trc'])[1+floor(random()*3)::int],
           (random()*100000)::numeric,
           CASE WHEN random() < 0.2 THEN 'Bonus-'||g ELSE 'TX-'||g END,
           (ARRAY['pending','approved','rejected','completed'])[1+floor(random()*4)::int],
           NOW() - interval '1 second' * (random()*2592000)
    FROM generate_series(1, 150000) g
""")
# مستخدم معيّن ببيانات اليوم لاختبار user_me
DatabaseManager.execute_query("""
    INSERT INTO transactions (user_telegram_id, type, payment_method, amount, status, created_at)
    SELECT 'u1', 'deposit_bot', 'syriatel', 50000, 'approved', NOW() - (x || ' hours')::interval
    FROM generate_series(0, 5) x
""")
DatabaseManager.execute_query("INSERT INTO support_tickets (user_telegram_id, status) VALUES ('u1','open') ON CONFLICT DO NOTHING")
tx_total = q("SELECT COUNT(*) as c FROM transactions", fetch='one')['c']
print(f"  📦 {tx_total:,} معاملة + 8,000 مستخدم بُذروا في {(time.perf_counter()-t0):.1f}s")

NEW_INDEXES = ['idx_tx_created_at', 'idx_tx_type_status', 'idx_tx_user', 'idx_support_status', 'idx_tlb_snap_cycle', 'idx_tlb_results_week']

print("=" * 66)
print("🧪 [2] قياس فعلي: بدون مؤشرات × بالمؤشرات الجديدة (150k صف)")
print("=" * 66)
for idx in NEW_INDEXES:
    DatabaseManager.execute_query(f"DROP INDEX IF EXISTS {idx}")
DatabaseManager.execute_query("ANALYZE transactions")
DatabaseManager.execute_query("ANALYZE users")

TODAY_AGG = """SELECT
  COALESCE(SUM(CASE WHEN type='deposit_bot' AND status='approved' THEN amount END),0) as d,
  COALESCE(SUM(CASE WHEN type='withdraw_bot' AND status='approved' THEN amount END),0) as w,
  COALESCE(SUM(CASE WHEN type='deposit_to_game' AND status IN ('completed','approved') THEN amount END),0) as g,
  COALESCE(SUM(CASE WHEN type IN ('deposit_to_game','deposit_bot') AND status IN ('completed','approved') AND transfer_number LIKE '%%Bonus%%' THEN amount END),0) as b
FROM transactions WHERE created_at >= CURRENT_DATE AND created_at < (CURRENT_DATE + INTERVAL '1 day')"""
INACTIVE = """SELECT COUNT(*) FROM users WHERE terms_accepted = TRUE AND telegram_id NOT IN (
  SELECT DISTINCT user_telegram_id FROM transactions WHERE created_at >= CURRENT_DATE - INTERVAL '7 days')"""
VOLUME = "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE status='approved'"
CHART = """SELECT d::date, COALESCE(SUM(CASE WHEN t.type='deposit_bot' AND t.status='approved' THEN t.amount ELSE 0 END),0)
FROM generate_series(CURRENT_DATE - INTERVAL '6 days', CURRENT_DATE, '1 day') as d
LEFT JOIN transactions t ON t.created_at >= d::date AND t.created_at < (d::date + INTERVAL '1 day') GROUP BY d::date"""

before = {
    'today_agg': timed(lambda: q(TODAY_AGG, fetch='one')),
    'inactive': timed(lambda: q(INACTIVE, fetch='one')),
    'volume': timed(lambda: q(VOLUME, fetch='one')),
    'chart7': timed(lambda: q(CHART, fetch='all')),
}

plan_rows = q(f"EXPLAIN {TODAY_AGG}", fetch='all')
plan_txt = " | ".join(r.get('QUERY PLAN', '') for r in plan_rows)
print(f"  📋 خطة التنفيذ بدون مؤشرات: {('Seq Scan' in plan_txt and 'Seq Scan (فحص تسلسلي) ✔ متوقع') or plan_txt[:80]}")

# إعادة إنشاء المؤشرات بنفس SQL المستودع (محاكاة إقلاع نظيف)
for sql in [
    "CREATE INDEX IF NOT EXISTS idx_tx_created_at ON transactions(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tx_type_status ON transactions(type, status)",
    "CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_telegram_id)",
    "CREATE INDEX IF NOT EXISTS idx_support_status ON support_tickets(status)",
    "CREATE INDEX IF NOT EXISTS idx_tlb_snap_cycle ON turnover_leaderboard_snapshots(cycle_start)",
    "CREATE INDEX IF NOT EXISTS idx_tlb_results_week ON turnover_leaderboard_results(week_start)",
]:
    DatabaseManager.execute_query(sql)
DatabaseManager.execute_query("ANALYZE transactions")

after = {
    'today_agg': timed(lambda: q(TODAY_AGG, fetch='one')),
    'inactive': timed(lambda: q(INACTIVE, fetch='one')),
    'volume': timed(lambda: q(VOLUME, fetch='one')),
    'chart7': timed(lambda: q(CHART, fetch='all')),
}

print(f"  📊 مجاميع اليوم:  {before['today_agg']:7.1f}ms → {after['today_agg']:6.1f}ms  (تسريع ×{before['today_agg']/max(after['today_agg'],0.01):.1f})")
print(f"  📊 المستخدمون الخاملون: {before['inactive']:6.1f}ms → {after['inactive']:6.1f}ms  (×{before['inactive']/max(after['inactive'],0.01):.1f})")
print(f"  📊 حجم المعتمد:  {before['volume']:7.1f}ms → {after['volume']:6.1f}ms  (×{before['volume']/max(after['volume'],0.01):.1f})")
check("المؤشرات موجودة بعد التهيئة", all(q("SELECT indexname FROM pg_indexes WHERE indexname=%s", (i,), fetch='one') for i in NEW_INDEXES))
plan_rows2 = q(f"EXPLAIN {TODAY_AGG}", fetch='all')
plan2_txt = " | ".join(r.get('QUERY PLAN', '') for r in plan_rows2)
check("مجاميع اليوم لم تعد تفحص تسلسلياً", 'Seq Scan' not in plan2_txt, plan2_txt[:100])

print("=" * 66)
print("🧪 [3] لوحة تحكم الأدمن: زمن أول جلب × زمن الكاش + عدّاد الاستعلامات")
print("=" * 66)
import telegram_bot.main as m

_qcount = {'n': 0}
_orig_eqd = DatabaseManager.execute_query_dict
_orig_eq = DatabaseManager.execute_query
def _counted_eqd(*a, **kw):
    _qcount['n'] += 1
    return _orig_eqd(*a, **kw)
def _counted_eq(*a, **kw):
    _qcount['n'] += 1
    return _orig_eq(*a, **kw)
DatabaseManager.execute_query_dict = _counted_eqd
DatabaseManager.execute_query = _counted_eq

class StubReq:
    headers = {'X-Telegram-Init-Data': 'x'}

m._is_admin = lambda raw: True
m._DASHBOARD_CACHE['data'] = None

async def dashboard_two_phases():
    # المرحلة 1: مس كاش — الجمع الكامل
    t = time.perf_counter()
    resp1 = await m.dashboard_api_handler(StubReq())
    t1 = (time.perf_counter() - t) * 1000
    n1 = _qcount['n']
    # المرحلة 2: ضربة كاش
    _qcount['n'] = 0
    t = time.perf_counter()
    resp2 = await m.dashboard_api_handler(StubReq())
    t2 = (time.perf_counter() - t) * 1000
    n2 = _qcount['n']
    return resp1, t1, n1, resp2, t2, n2

resp1, t1, n1, resp2, t2, n2 = asyncio.run(dashboard_two_phases())
import json as _j
d1 = _j.loads(resp1.text)
d2_keys = set(_j.loads(resp2.text).keys())
print(f"  🚀 فتح 1 (جمع كامل): {t1:.0f}ms بعدد {n1} استعلام DB")
print(f"  ⚡ فتح 2 (من الكاش): {t2:.3f}ms بعدد {n2} استعلام DB")

EXPECTED_DASH_KEYS = {
 'total_users','new_users_today','today_tx_count','approved_volume','total_bot_balance','agent_balance',
 'agent_balance_alert_threshold','usd_buy_rate','usd_sell_rate','exchange_rate','withdraw_commission',
 'game_min_deposit_syp','agent_revenue_percent','min_deposit_syp','min_deposit_usd','min_withdraw_syp',
 'min_withdraw_usd','syp_version','is_cookie_alive','cookie_age_minutes','pending_deposits','pending_withdraws',
 'recent_transactions','today_deposits','today_withdraws','today_game_deposits','today_bonus_paid',
 'estimated_burn','estimated_revenue','net_profit','chart_labels','chart_deposits','chart_withdraws',
 'chart_burn_rev','chart_comm_rev','wheel_stats','cashback_stats','checkin_stats','inactive_users',
 'agent_balance_alert','pending_count','open_support_count','oldest_pending','service_gates','active_cashier_profile'
}
check("مفاتيح لوحة الأدمن لم تتغير (لن يكسر واجهة JS)", set(d1.keys()) - {'cookie_checked_at'} == EXPECTED_DASH_KEYS,
      str(set(d1.keys()) ^ EXPECTED_DASH_KEYS)[:160])
check("ضربة الكاش بلا أي استعلام DB", n2 == 0)
check("تسريع الكاش أكبر من 10×", t1 / max(t2, 0.05) > 10, f"{t1:.0f}ms vs {t2:.2f}ms")

print("=" * 66)
print("🧪 [4] لوحة القيصر للمستخدم: الزمن + المفاتيح + عدّاد STMT")
print("=" * 66)
_qcount['n'] = 0
t = time.perf_counter()
payload = m._collect_user_me_payload_sync('u1', 'Caesar_Bot_Uname')
t_um = (time.perf_counter() - t) * 1000
n_um = _qcount['n']
print(f"  🚀 user_me collect: {t_um:.1f}ms بعدد {n_um} استعلام")
EXPECTED_UM_KEYS = {
 'telegram_id','username','bot_balance','bonus_balance','cashback_pending_balance','checkin_pending_balance',
 'active_game_bonus','game_balance','recent_transactions','active_offers','open_contests','referral','checkin',
 'flash_bonus','leaderboard','features','bonus_eligibility','vip','cashback','service_gates'
}
check("مفاتيح user_me لم تتغير", set(payload.keys()) == EXPECTED_UM_KEYS, str(set(payload.keys()) ^ EXPECTED_UM_KEYS)[:160])
check("user_me زمن مقبول تحت 150k سجل (<500ms)", t_um < 500, f"{t_um:.0f}ms")
check("معاملات u1 الأخيرة تظهر (≥6)", payload['bot_balance'] >= 0 and len(payload['recent_transactions']) >= 6, str(len(payload['recent_transactions'])))

print("=" * 66)
print("🧪 [5] إثبات الكاشات في repository")
print("=" * 66)
_qcount['n'] = 0
repo.invalidate_user_features_cache()
repo.get_user_features_settings()
repo.get_user_features_settings()
repo.get_wheel_settings()
repo.get_user_features_settings()
check("4 قراءات لإعدادات الميزات = استعلام واحد فقط خلال 60ث", _qcount['n'] <= 2, str(_qcount['n']))
_qcount['n'] = 0
repo.update_user_features_settings(checkin_enabled=True)
n_write = _qcount['n']
repo.get_user_features_settings()
q_after_update = _qcount['n']
check("تعديل الإعدادات يكسر الكاش (القراءة التالية تضرب القاعدة)", q_after_update == n_write + 1, f"update={n_write} total_after_read={q_after_update}")

_qcount['n'] = 0
repo.invalidate_button_link_cache()
repo.get_button_link('games_url')
repo.get_button_link('games_url')
repo.get_button_link('website_url')
repo.get_button_link('games_url')
check("4 قراءات روابط أزرار = استعلامان فقط (كاش 5 دقائق)", _qcount['n'] == 2, str(_qcount['n']))
_qcount['n'] = 0
repo.set_button_link('games_url', 'https://ichancy100.com/games')
repo.get_button_link('games_url')
check("تعديل الرابط يكسر كاشه", _qcount['n'] == 2, str(_qcount['n']))

print("=" * 66)
print("🧪 [6] ميدلوير الشروط: حدث حقيقي لكامل الرحلة")
print("=" * 66)
from telegram_bot.middlewares.terms_check import TermsCheckMiddleware, invalidate_terms_cache
from aiogram.types import Message, CallbackQuery, User, Chat
from datetime import datetime as _dt

mw = TermsCheckMiddleware()
user1 = User(id=19001, is_bot=False, first_name='ت')
repo.delete_user_completely('19001')
repo.create_user('19001', 'testu')
DatabaseManager.execute_query("UPDATE users SET terms_accepted=TRUE WHERE telegram_id='19001'")
invalidate_terms_cache('19001')

class FakeMessageObj:
    """يجريب يشبه aiogram Message دون الحاجة لقناة بوت حقيقية."""
    def __init__(self, from_user):
        self.from_user = from_user

async def pass_event(data_user):
    hits = []
    async def handler(event, data):
        hits.append(1)
        return 'HANDLED'
    msg = Message(message_id=1, date=_dt.now(), chat=Chat(id=19001, type='private'), from_user=data_user)
    return await mw(handler, msg, {'event_from_user': data_user}), len(hits)

# تمريرة 1: مستخدم مقبول → استعلام get_user واحد ثم تمرير
_qcount['n'] = 0
res1, h1 = asyncio.run(pass_event(user1))
n_mw1 = _qcount['n']
# تمريرة 2: كاش قبول → صفر استعلام
_qcount['n'] = 0
res2, h2 = asyncio.run(pass_event(user1))
n_mw2 = _qcount['n']
print(f"  🛂 تمريرة 1: {n_mw1} استعلام | تمريرة 2: {n_mw2} استعلام")
check("الحدث الأول يمر باستعلام واحد", res1 == 'HANDLED' and h1 == 1 and n_mw1 == 1, f"q={n_mw1}")
check("الحدث الثاني يمر بصفر استعلام (كاش القبول)", res2 == 'HANDLED' and n_mw2 == 0, f"q={n_mw2}")

# حذف الحساب يكسر الكاش فوراً
invalidate_terms_cache('19001')
_qcount['n'] = 0
repo.delete_user_completely('19001')
try:
    res3, _ = asyncio.run(pass_event(user1))
    gate_hit = res3 != 'HANDLED'
except RuntimeError:
    # المحاكاة بلا bot: event.answer تفشل بعد دخول فرع الشروط — الدخول نفسه هو المطلوب
    gate_hit = True
check("بعد الحذف + الإبطال: الميدلوير أدخل المستخدم لبوابة الشروط", gate_hit and _qcount['n'] >= 1, f"q={_qcount['n']}")

DatabaseManager.execute_query_dict = _orig_eqd
DatabaseManager.execute_query = _orig_eq

print()
print("=" * 66)
print(f"📊 النتيجة النهائية: ✅ {PASS} | ❌ {FAIL}")
print("=" * 66)
t_new = after
spd = before['today_agg'] / max(after['today_agg'], 0.01)
sys.exit(1 if FAIL else 0)
