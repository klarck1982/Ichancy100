#!/usr/bin/env python3
"""Offline simulation for marketing gift-code campaigns."""
from __future__ import annotations
import ast
import subprocess
import tempfile
from datetime import datetime,timezone,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];REPO=ROOT/'database/repository.py';CONN=ROOT/'database/connection.py';MAIN=ROOT/'telegram_bot/main.py';DASH=ROOT/'webapp/dashboard.html'

def funcs(names):
 text=REPO.read_text();tree=ast.parse(text);return ast.Module(body=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names],type_ignores=[])

def distribution_test():
 ns={};exec(compile(funcs({'calculate_campaign_distribution'}),str(REPO),'exec'),ns);calc=ns['calculate_campaign_distribution']
 assert calc('per_code',10000,5)=={'ok':True,'reward_amount':10000,'total_budget':50000,'remainder':0}
 assert calc('total_budget',50000,5)=={'ok':True,'reward_amount':10000,'total_budget':50000,'remainder':0}
 assert calc('total_budget',50003,5)=={'ok':True,'reward_amount':10000,'total_budget':50000,'remainder':3}
 assert not calc('per_code',0,5)['ok']

def redemption_test():
 class Logger:
  def error(self,*a,**k):raise AssertionError(a)
 class Cursor:
  def __init__(self,already=False):self.already=already;self.last='';self.calls=[];self.rowcount=1
  def execute(self,sql,params=None):self.last=' '.join(sql.split());self.calls.append((self.last,params))
  def fetchone(self):
   if 'FROM gift_campaign_codes' in self.last:return (5,2,1,0,True,'حملة تجريبية','bonus',10000,5,True,'active',datetime.now(timezone.utc)-timedelta(hours=1),datetime.now(timezone.utc)+timedelta(hours=2))
   if 'FROM users WHERE' in self.last:return (0,'PLAYER1')
   if 'FROM gift_campaign_redemptions WHERE campaign_id' in self.last:return (77,) if self.already else None
   if 'SELECT COUNT(*)' in self.last:return (0,)
   return None
  def close(self):pass
 class Conn:
  def __init__(self,c):self.c=c;self.committed=False;self.rolled=False
  def cursor(self):return self.c
  def commit(self):self.committed=True
  def rollback(self):self.rolled=True
 class DB:
  conn=None
  @classmethod
  def get_connection(cls):return cls.conn
  @classmethod
  def put_connection(cls,c):pass
 def run(already):
  cur=Cursor(already);DB.conn=Conn(cur);ns={'DatabaseManager':DB,'logger':Logger(),'datetime':datetime,'timezone':timezone};exec(compile(funcs({'redeem_campaign_code'}),str(REPO),'exec'),ns);return ns['redeem_campaign_code']('CODE','123'),DB.conn,cur
 result,conn,cur=run(False);assert result['ok'] and result['amount']==10000 and conn.committed
 sql=' '.join(x[0] for x in cur.calls);assert 'FOR UPDATE OF cc, c' in sql and 'users WHERE telegram_id=%s FOR UPDATE' in sql and 'INSERT INTO gift_campaign_redemptions' in sql and 'bonus_balance' in sql
 result2,conn2,_=run(True);assert not result2['ok'] and result2['reason']=='user_limit' and conn2.rolled

def ui_budget_test():
 dash=DASH.read_text();start=dash.index('function campaignBudgetPreview(){');end=dash.index('function renderGiftCampaigns()',start);code=dash[start:end]
 harness=f"""
let values={{campaign_count:'5',campaign_input_mode:'per_code',campaign_value:'10000'}};const box={{innerHTML:''}};
function val(id){{return values[id]||''}}function fmt(n){{return String(n)}}const document={{getElementById:(id)=>id==='campaign_budget_preview'?box:null}};
{code}
let r=campaignBudgetPreview();if(r.per!==10000||r.total!==50000||r.remainder!==0)throw new Error('per-code UI calculation failed');
values.campaign_input_mode='total_budget';values.campaign_value='50003';r=campaignBudgetPreview();if(r.per!==10000||r.total!==50000||r.remainder!==3)throw new Error('total-budget UI calculation failed');
if(!box.innerHTML.includes('50000')||!box.innerHTML.includes('3'))throw new Error('budget summary not rendered');
"""
 with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:f.write(harness);path=Path(f.name)
 try:
  result=subprocess.run(['node',str(path)],capture_output=True,text=True);assert result.returncode==0,result.stderr
 finally:path.unlink(missing_ok=True)

def static_test():
 conn=CONN.read_text();main=MAIN.read_text();dash=DASH.read_text();repo=REPO.read_text()
 for token in ('gift_campaigns_table','gift_campaign_codes_table','gift_campaign_redemptions_table','UNIQUE(campaign_id, user_telegram_id)'):assert token in conn
 assert 'campaign_result = redeem_campaign_code' in repo
 assert "INTERVAL '24 hours'" in repo and "CAESAR-BONUS-%%" in repo
 assert 'admin_gift_campaigns_handler' in main and '/api/admin/gift-campaigns' in main
 for token in ("tab==='campaigns'",'function loadGiftCampaigns','قيمة كل كود','إجمالي الميزانية','نسخ جميع الأكواد'):assert token in dash
 assert 'setInterval(loadGiftCampaigns' not in dash

def main():
 distribution_test();redemption_test();ui_budget_test();static_test()
 print('PASS: per-code value and total-budget modes calculate correctly')
 print('PASS: indivisible total budgets report the unused remainder')
 print('PASS: campaign redemption locks campaign/code and user rows')
 print('PASS: one redemption per user/campaign is enforced')
 print('PASS: successful redemption credits the configured reward atomically')
 print('PASS: campaign tables include a database uniqueness constraint')
 print('PASS: admin API and separate campaign tab are connected')
 print('PASS: browser budget calculator matches backend distribution')
 print('PASS: batch copy UI exists without polling')
 print('PASS: no real database, Telegram, Neon, or network used')
if __name__=='__main__':main()
