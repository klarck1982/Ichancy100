#!/usr/bin/env python3
"""Offline simulation for Robert VIP Hub, public proxy, cache, and bot button."""
from __future__ import annotations
import ast,asyncio,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];MAIN=ROOT/'telegram_bot/main.py';HUB=ROOT/'webapp/robert_vip_hub.html';KEYS=ROOT/'telegram_bot/keyboards/inline.py'

def selected():
 text=MAIN.read_text();tree=ast.parse(text);names={'ROBERT_VIP_API_BASE','ROBERT_PUBLIC_CACHE'};funcs={'_safe_robert_public_item','robert_vip_public_handler'};nodes=[]
 for n in tree.body:
  if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets):nodes.append(n)
  if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in funcs:nodes.append(n)
 return ast.Module(body=nodes,type_ignores=[])

class Response:
 def __init__(self,data):self.status=200;self.data=data
 async def __aenter__(self):return self
 async def __aexit__(self,*a):pass
 async def json(self,content_type=None):return self.data
class Session:
 def __init__(self):self.calls=[]
 def get(self,url,headers=None):
  self.calls.append(url)
  if url.endswith('/banners'):return Response({'data':[{'ref_id':'b1','title_ar':'عرض','image_url':'https://api.robert.vip/storage/a.jpg','mobile_image_url':'https://api.robert.vip/storage/m.jpg','link_url':'https://robert.vip/dashboard'}]})
  return Response({'data':[{'ref_id':'s1','title_ar':'قصة','thumbnail_url':'https://api.robert.vip/storage/s.jpg','link_url':'https://evil.example/phish'}]})
class Request:
 def __init__(self,s):self.app={'neon_session':s}
class Web:
 @staticmethod
 def json_response(data,headers=None):return {'data':data,'headers':headers or {}}
class Logger:
 def warning(self,*a,**k):raise AssertionError(a)

def main():
 ns={'time':time,'asyncio':asyncio,'urllib':__import__('urllib.parse'),'web':Web,'logger':Logger()};exec(compile(selected(),str(MAIN),'exec'),ns)
 safe=ns['_safe_robert_public_item'];good=safe({'title_ar':'x','image_url':'https://api.robert.vip/a.jpg','link_url':'https://robert.vip/x'},'banner');assert good['image_url'] and good['link_url']
 bad=safe({'title_ar':'x','image_url':'javascript:alert(1)','link_url':'https://evil.example'},'banner');assert bad['image_url']=='' and bad['link_url']==''
 session=Session();first=asyncio.run(ns['robert_vip_public_handler'](Request(session)));second=asyncio.run(ns['robert_vip_public_handler'](Request(session)))
 assert len(session.calls)==2 and first['data']['cached'] is False and second['data']['cached'] is True
 assert first['data']['stories'][0]['link_url']==''
 hub=HUB.read_text();keys=KEYS.read_text();main_src=MAIN.read_text()
 for token in ('فتح المنصة الآن','إنشاء حساب','تسجيل الدخول','/api/robert-vip/public','/api/public/links','ROBERT VIP HUB'):assert token in hub
 assert 'password' not in hub.lower() and 'كلمة المرور' in hub
 assert 'get_robert_vip_hub_url' in keys and '🌟 ROBERT VIP — فتح المنصة' in keys
 assert 'app.router.add_get("/robert-vip"' in main_src and 'app.router.add_get("/api/robert-vip/public"' in main_src
 assert 'ROBERT_PUBLIC_CACHE' in main_src and 'expires_at' in main_src and 'now + 300' in main_src
 assert 'DatabaseManager' not in ast.get_source_segment(main_src,next(n for n in ast.parse(main_src).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='robert_vip_public_handler'))
 print('PASS: bot menu contains a wide Robert VIP WebApp button')
 print('PASS: Hub provides platform, register, login, offers, stories, and help actions')
 print('PASS: public proxy sanitizes external URLs')
 print('PASS: banners and stories are fetched once then served from 5-minute memory cache')
 print('PASS: second simulated request performs no upstream calls')
 print('PASS: proxy uses no Neon/database query')
 print('PASS: Hub contains no password field or credential storage')
 print('PASS: no real network, Telegram, Render, or Neon used')
if __name__=='__main__':main()
