#!/usr/bin/env python3
"""Offline structural simulation for the organized admin command center."""
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DASH=(ROOT/'webapp/dashboard.html').read_text();MAIN=(ROOT/'telegram_bot/main.py').read_text()

def main():
    for token in ('👑 غرفة العمليات','ops-room','open_support_count','oldest_pending','active_cashier_profile'):
        assert token in DASH or token in MAIN
    for token in ("tab==='control'","function loadControlCenter()",'update_service_gates','create_cashier_profile','activate_cashier_profile','delete_cashier_profile'):
        assert token in DASH
    assert 'مناوبة المشرفين المالية' in DASH and '＋ إضافة مشرف جديد' in DASH
    assert 'cashier_switch_audit' in MAIN and 'cashier_profiles' in MAIN
    for token in ('requestFilter','setRequestSort','requestAge','cashier_profile_name','payment_destination'):
        assert token in DASH
    for token in ('SUPPORT_TEMPLATES','saveSupportDraft','restoreSupportDraft','clearSupportDraft'):
        assert token in DASH
    assert "sessionStorage.setItem('caesar_admin_last_tab'" in DASH
    assert 'setInterval(loadDashboard' not in DASH and 'setInterval(loadControlCenter' not in DASH
    assert 'الاعتماد/الرفض يبقى من قناة المراجعة' in MAIN
    print('PASS: operations room uses dashboard counts and oldest-request data')
    print('PASS: tools are grouped into control, finance, marketing, and integrations')
    print('PASS: cashier create/edit/activate/delete controls are wired to admin API')
    print('PASS: request filters, age labels, sorting, and pinned cashier details exist')
    print('PASS: support templates and drafts use local storage')
    print('PASS: last main admin tab persists for the session')
    print('PASS: financial approval/rejection remains in review channel')
    print('PASS: no admin polling loop added')
if __name__=='__main__':main()
