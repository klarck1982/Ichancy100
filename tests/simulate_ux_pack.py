#!/usr/bin/env python3
"""Offline simulation for the production UX pack.

Checks royal SVG buttons, top Flash Bonus placement, local privacy state,
one-time logo hint, pending summaries, safe areas, and removal of debug UI.
No database, network, Telegram token, or Neon connection is used.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def extract_between(text: str, begin: str, end: str) -> str:
    assert begin in text and end in text
    return text[text.index(begin): text.index(end) + len(end)]


def run_node(source: str, label: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as fh:
        fh.write(source)
        path = Path(fh.name)
    try:
        result = subprocess.run(["node", str(path)], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(f"{label} failed:\n{result.stderr}")
    finally:
        path.unlink(missing_ok=True)


def simulate_file(path: Path) -> None:
    html = path.read_text(encoding="utf-8")

    # Visual production order: flash, balance, royal actions, pending status, VIP.
    home = html.index('<div class="page active" id="pageHome">')
    flash = html.index('<div id="flashBanner"></div>', home)
    balance = html.index('<div id="balanceCard">', home)
    actions = html.index('<section class="royal-actions"', home)
    pending = html.index('<div id="pendingStatus"', home)
    vip = html.index('<div id="vipCard"></div>', home)
    assert home < flash < balance < actions < pending < vip

    assert "PINGO_WHEEL_BUILD" not in html
    assert "pingo-wheel-v4-20260708" not in html
    assert html.count('class="icon royal-icon"') == 4
    assert html.count('<svg viewBox="0 0 48 48">') == 4
    assert "appearance:none;-webkit-appearance:none" in html
    assert "royalButtonIn" in html
    assert "body.balance-hidden" in html
    assert "safe-area-inset-bottom" in html
    assert "tg-content-safe-area-inset-bottom" in html
    assert "renderPendingStatus(d.recent_transactions||[])" in html
    assert "loadAll().finally(()=>setTimeout(showLogoHintOnce,700))" in html
    assert 'class="balance-privacy"' in html

    # Flash countdown is local: it may tick every second, but must not fetch.
    flash_source = html[html.index("function renderFlash(d)"): html.index("// ===== لوحة الصدارة", html.index("function renderFlash(d)"))]
    assert "setInterval(update,1000)" in flash_source
    assert "fetch(" not in flash_source and "apiGet(" not in flash_source and "/api/" not in flash_source

    ux = extract_between(html, "// UX_PACK_BEGIN", "// UX_PACK_END")
    assert "fetch(" not in ux and "/api/" not in ux

    harness = f"""
const store=new Map();
const noteCalls=[];
const haptics=[];
const classes=new Set();
const classList={{
  contains:(name)=>classes.has(name),
  toggle:(name,force)=>{{if(force)classes.add(name);else classes.delete(name);return force;}}
}};
const privacyButton={{attrs:{{}},setAttribute:(key,val)=>privacyButton.attrs[key]=val}};
const pendingBox={{innerHTML:''}};
const document={{
  body:{{classList}},
  querySelector:(selector)=>selector==='.balance-privacy'?privacyButton:null,
  getElementById:(id)=>id==='pendingStatus'?pendingBox:null
}};
const localStorage={{
  getItem:(key)=>store.has(key)?store.get(key):null,
  setItem:(key,val)=>store.set(key,String(val))
}};
const tg={{HapticFeedback:{{selectionChanged:()=>haptics.push('selection')}}}};
function showBrandRefreshNote(message,type){{noteCalls.push([message,type]);}}
{ux}
if(classes.has('balance-hidden'))throw new Error('privacy should start visible');
toggleBalancePrivacy();
if(!classes.has('balance-hidden'))throw new Error('privacy did not hide');
if(store.get(BALANCE_PRIVACY_KEY)!=='1')throw new Error('privacy not persisted');
if(privacyButton.attrs['aria-pressed']!=='true')throw new Error('aria privacy state missing');
toggleBalancePrivacy();
if(classes.has('balance-hidden'))throw new Error('privacy did not restore');
if(haptics.length!==2)throw new Error('privacy haptics mismatch');
if(showLogoHintOnce()!==true)throw new Error('first hint not shown');
if(showLogoHintOnce()!==false)throw new Error('hint repeated');
if(noteCalls.length!==1)throw new Error('hint call mismatch');
const summary=getPendingTransactions([
  {{id:11,type:'deposit_bot',status:'pending'}},
  {{id:12,type:'withdraw_bot',status:'approved'}},
  {{id:13,type:'withdraw_bot',status:'PENDING'}},
  {{id:14,type:'gift',status:'pending'}}
]);
if(summary.length!==2)throw new Error('pending limit/filter failed');
if(summary[0].type!=='إيداع رصيد'||summary[1].type!=='سحب رصيد')throw new Error('pending labels failed');
renderPendingStatus([{{id:11,type:'deposit_bot',status:'pending'}}]);
if(!pendingBox.innerHTML.includes('إيداع رصيد #11'))throw new Error('pending rendering failed');
renderPendingStatus([]);
if(pendingBox.innerHTML!=='')throw new Error('pending clear failed');
"""
    run_node(harness, f"UX simulation ({path.name})")


def main() -> None:
    for filename in ("user_app.html", "user_app_pingo.html"):
        simulate_file(ROOT / "webapp" / filename)
    print("PASS: Royal SVG buttons exist and appear under the balance card")
    print("PASS: Flash Bonus placeholder is above balance and timer uses no API/Neon")
    print("PASS: Balance privacy toggle persists locally and restores correctly")
    print("PASS: Logo refresh hint appears once only")
    print("PASS: Pending transactions are derived from already-loaded data")
    print("PASS: Debug build marker removed")
    print("PASS: Bottom safe-area support enabled")
    print("PASS: UX simulation passed in both Mini App files")


if __name__ == "__main__":
    main()
