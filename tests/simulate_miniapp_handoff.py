#!/usr/bin/env python3
"""Offline regression simulation for Mini App -> bot handoff and auto-close."""
from pathlib import Path
import subprocess,tempfile
ROOT=Path(__file__).resolve().parents[1]

def run(js,label):
    with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f:f.write(js);p=Path(f.name)
    try:
        r=subprocess.run(['node',str(p)],capture_output=True,text=True)
        if r.returncode:raise AssertionError(label+'\n'+r.stderr)
    finally:p.unlink(missing_ok=True)

def simulate(path):
    html=path.read_text();begin='// MINIAPP_SHORTCUTS_BEGIN';end='// MINIAPP_SHORTCUTS_END';code=html[html.index(begin):html.index(end)+len(end)]
    assert 'id="botFlowOverlay"' in html and 'سيتم إغلاق الداشبورد تلقائيًا' in html
    assert 'openTelegramLink(url);scheduleMiniAppClose()' in code and 'tg?.close?.()' in code
    js=f"""
const queue=[];let closeCount=0;const calls=[];
const overlay={{shown:false,hidden:'true',classList:{{add:()=>overlay.shown=true,remove:()=>overlay.shown=false}},setAttribute:(k,v)=>overlay.hidden=v}};
const title={{textContent:''}};const document={{getElementById:(id)=>id==='botFlowOverlay'?overlay:id==='botFlowTitle'?title:null}};
const window={{location:{{href:''}}}};const setTimeout=(fn,delay)=>{{queue.push([delay,fn]);return queue.length}};
const tg={{openTelegramLink:(u)=>calls.push(u),close:()=>closeCount++,HapticFeedback:{{impactOccurred:()=>{{}}}}}};
{code}
if(!openBotFlow('deposit'))throw new Error('deposit rejected');
if(!overlay.shown||!title.textContent.includes('شحن'))throw new Error('transition overlay missing');
if(openBotFlow('withdraw')!==false)throw new Error('double click not blocked');
if(calls.length!==1)throw new Error('duplicate Telegram links');
queue.find(x=>x[0]===420)[1]();if(closeCount!==1)throw new Error('Mini App did not close');
queue.find(x=>x[0]===1800)[1]();if(overlay.shown)throw new Error('fallback overlay did not clear');
if(!openBotFlow('withdraw'))throw new Error('handoff did not unlock');
"""
    run(js,'handoff '+path.name)

def main():
    for n in ('user_app.html','user_app_pingo.html'):simulate(ROOT/'webapp'/n)
    print('PASS: transition overlay appears immediately with the selected service')
    print('PASS: Telegram deep link opens before Mini App close')
    print('PASS: Mini App close is scheduled after a safe 420ms handoff delay')
    print('PASS: double clicks cannot open duplicate bot routes')
    print('PASS: overlay fallback clears if the client does not close')
    print('PASS: both Mini App files passed without network or Telegram')
if __name__=='__main__':main()
