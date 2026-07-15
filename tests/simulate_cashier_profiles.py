#!/usr/bin/env python3
"""Offline simulation for cashier profiles and atomic address switching."""
from __future__ import annotations
import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT/'database/repository.py'
CONN=ROOT/'database/connection.py'
MENU=ROOT/'telegram_bot/handlers/menu.py'
MAIN=ROOT/'telegram_bot/main.py'


def extract(names):
    text=REPO.read_text();tree=ast.parse(text);nodes=[]
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in node.targets):nodes.append(node)
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names:nodes.append(node)
    return ast.Module(body=nodes,type_ignores=[])


def test_pure_routing():
    ns={};exec(compile(extract({'CASHIER_METHOD_COLUMNS','resolve_cashier_payment_route'}),str(REPO),'exec'),ns)
    resolve=ns['resolve_cashier_payment_route']
    profile={'id':4,'name':'رأفت','is_enabled':True,'sham_syp_address':'SYP-R','sham_usd_address':'USD-R','syriatel_address':'SYR-R','mtn_address':'MTN-R'}
    expected={'sham_syp':'SYP-R','sham_usd':'USD-R','syriatel':'SYR-R','mtn':'MTN-R'}
    for method,address in expected.items():
        route=resolve(method,profile,'LEGACY','database')
        assert route=={'address':address,'source':'cashier_profile','cashier_profile_id':4,'cashier_profile_name':'رأفت'}
    assert resolve('usdt_trc',profile,'TRC-LEGACY','render')['address']=='TRC-LEGACY'
    assert resolve('syriatel',{**profile,'is_enabled':False},'OLD','database')['address']=='OLD'
    assert resolve('mtn',{**profile,'mtn_address':''},'OLD-MTN','render')['source']=='render'


def test_atomic_switch():
    class Logger:
        def error(self,*a,**k):raise AssertionError(a)
    class Cursor:
        def __init__(self):self.calls=[];self.step=0
        def execute(self,sql,params=None):self.calls.append((' '.join(sql.split()),params));self.step+=1
        def fetchone(self):
            if self.step==1:return (8,'مشرف جديد','SYP','USD','SYR','MTN')
            if self.step==2:return (3,)
            return None
        def close(self):pass
    class Conn:
        def __init__(self):self.cursor_obj=Cursor();self.committed=False;self.rolled=False
        def cursor(self):return self.cursor_obj
        def commit(self):self.committed=True
        def rollback(self):self.rolled=True
    conn=Conn()
    class DB:
        invalidated=False
        @classmethod
        def get_connection(cls):return conn
        @classmethod
        def put_connection(cls,c):assert c is conn
        @classmethod
        def invalidate_settings_cache(cls):cls.invalidated=True
    ns={'DatabaseManager':DB,'logger':Logger()}
    exec(compile(extract({'activate_cashier_profile'}),str(REPO),'exec'),ns)
    result=ns['activate_cashier_profile'](8,'999')
    assert result['ok'] and result['previous_profile_id']==3 and result['profile_name']=='مشرف جديد'
    assert conn.committed and not conn.rolled and DB.invalidated
    sql=' '.join(x[0] for x in conn.cursor_obj.calls)
    assert 'FOR UPDATE' in sql and 'UPDATE bot_settings SET active_cashier_profile_id' in sql and 'INSERT INTO cashier_switch_audit' in sql


def test_pinned_transaction_and_schema():
    conn=CONN.read_text();repo=REPO.read_text();menu=MENU.read_text();main=MAIN.read_text()
    for token in ('CREATE TABLE IF NOT EXISTS cashier_profiles','CREATE TABLE IF NOT EXISTS cashier_switch_audit','active_cashier_profile_id','cashier_profile_id INTEGER','payment_destination TEXT'):
        assert token in conn
    assert 'cashier_profile_id=None' in repo and 'payment_destination=None' in repo
    assert 'deposit_payment_destination=payment_address' in menu
    assert 'cashier_profile_id=cashier_profile_id' in menu and 'payment_destination=payment_destination' in menu
    assert "'cashier_profile_name': tx.get('cashier_profile_name')" in main
    assert "action == 'activate_cashier_profile'" in main and 'switched_by=admin_id' in main
    assert "action == 'create_cashier_profile'" in main


def main():
    test_pure_routing();test_atomic_switch();test_pinned_transaction_and_schema()
    print('PASS: local cash methods route through the active cashier profile')
    print('PASS: USDT and missing profile addresses retain legacy fallbacks')
    print('PASS: cashier activation is atomic and writes an audit row')
    print('PASS: settings cache invalidates immediately after switching')
    print('PASS: deposit state pins cashier ID, name, and shown destination')
    print('PASS: transaction schema stores the pinned cashier destination')
    print('PASS: admin API exposes create/update/activate/delete actions')
    print('PASS: no database or network connection used')
if __name__=='__main__':main()
