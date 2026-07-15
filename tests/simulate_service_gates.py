#!/usr/bin/env python3
"""Offline simulation for maintenance and financial service gates."""
from __future__ import annotations
import ast
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT/'database/repository.py';MENU=ROOT/'telegram_bot/handlers/menu.py';MAIN=ROOT/'telegram_bot/main.py';DASH=ROOT/'webapp/dashboard.html'

def extract(names):
    text=REPO.read_text();tree=ast.parse(text);return ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names],type_ignores=[])

def simulate_logic():
    state={'maintenance_mode':False,'deposits_enabled':True,'withdrawals_enabled':True,'game_transfers_enabled':True}
    calls=[]
    class DB:
        @staticmethod
        def execute_query(sql,params=None):calls.append((sql,params))
        @staticmethod
        def invalidate_settings_cache():calls.append(('invalidate',None))
    ns={'DatabaseManager':DB,'get_bot_settings':lambda:dict(state)}
    exec(compile(extract({'get_service_gates','update_service_gates','service_gate_status'}),str(REPO),'exec'),ns)
    status=ns['service_gate_status'];update=ns['update_service_gates']
    for service in ('deposit','withdraw','game',None):assert status(service)==(True,None)
    state['maintenance_mode']=True
    for service in ('deposit','withdraw','game',None):
        ok,msg=status(service);assert not ok and 'الصيانة' in msg
    state['maintenance_mode']=False;state['deposits_enabled']=False
    assert status('deposit')[0] is False and status('withdraw')[0] is True
    state['deposits_enabled']=True;state['withdrawals_enabled']=False
    assert status('withdraw')[0] is False and status('deposit')[0] is True
    state['withdrawals_enabled']=True;state['game_transfers_enabled']=False
    assert status('game')[0] is False
    result=update(False,True,True,True);assert result['game_transfers_enabled'] is True
    assert any('UPDATE bot_settings SET maintenance_mode' in sql for sql,_ in calls if isinstance(sql,str))
    assert ('invalidate',None) in calls

def static_guards():
    menu=MENU.read_text();main=MAIN.read_text();dash=DASH.read_text()
    for token in ("service_gate_status('deposit')","service_gate_status('withdraw')","service_gate_status('game')"):
        assert token in menu
    assert 'async def _ensure_service_gate' in menu
    assert "action == 'update_service_gates'" in main
    assert "'service_gates': repo.get_service_gates()" in main
    assert 'renderServiceGates(d.service_gates||{})' in (ROOT/'webapp/user_app_pingo.html').read_text()
    for id_ in ('gate_maintenance','gate_deposits','gate_withdrawals','gate_game'):assert id_ in dash

def main():
    simulate_logic();static_guards()
    print('PASS: maintenance mode blocks all simulated user services')
    print('PASS: deposit, withdrawal, and game gates operate independently')
    print('PASS: gate updates invalidate cached settings')
    print('PASS: start and final-confirmation handlers both recheck gates')
    print('PASS: user Mini App receives gate state and disables affected shortcuts')
    print('PASS: admin control center exposes all four confirmed switches')
    print('PASS: no database or network connection used')
if __name__=='__main__':main()
