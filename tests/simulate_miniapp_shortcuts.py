#!/usr/bin/env python3
"""Offline simulation for Mini App quick shortcuts.

No Telegram token, database URL, Render service, or Neon connection is used.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram_bot.miniapp_shortcuts import (  # noqa: E402
    ACTION_TO_PAYLOAD,
    build_miniapp_shortcut_url,
    resolve_miniapp_shortcut,
)

EXPECTED = {
    "deposit": "https://t.me/Caesar_Robert_bot?start=app_deposit",
    "withdraw": "https://t.me/Caesar_Robert_bot?start=app_withdraw",
    "gift": "https://t.me/Caesar_Robert_bot?start=app_gift",
}


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


def extract_shortcut_js(html: str) -> str:
    begin = "// MINIAPP_SHORTCUTS_BEGIN"
    end = "// MINIAPP_SHORTCUTS_END"
    assert begin in html and end in html
    return html[html.index(begin): html.index(end) + len(end)]


def function_source(path: Path, function_name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"Missing function: {function_name}")


def simulate_frontend(path: Path) -> None:
    html = path.read_text(encoding="utf-8")
    js = extract_shortcut_js(html)

    quick_start = html.index('<div class="quick-grid">')
    # Include enough markup to cover all four buttons.
    quick = html[quick_start: html.index('<!-- آخر المعاملات -->', quick_start)]
    assert 'onclick="closeApp()"' not in quick
    for action in EXPECTED:
        assert f'onclick="openBotFlow(\'{action}\')"' in quick
    assert 'onclick="showPage(\'referral\')"' in quick
    assert "fetch(" not in js and "/api/" not in js

    expected_json = str(list(EXPECTED.values())).replace("'", '"')
    native_harness = f"""
const calls=[];
const haptics=[];
const window={{location:{{href:''}}}};
const tg={{
  openTelegramLink:(url)=>calls.push(url),
  HapticFeedback:{{impactOccurred:(kind)=>haptics.push(kind)}}
}};
{js}
for(const action of ['deposit','withdraw','gift']){{
  if(openBotFlow(action)!==true)throw new Error('valid action rejected: '+action);
}}
if(openBotFlow('invalid')!==false)throw new Error('invalid action accepted');
const expected={expected_json};
if(JSON.stringify(calls)!==JSON.stringify(expected))throw new Error('URLs mismatch: '+JSON.stringify(calls));
if(haptics.length!==3)throw new Error('haptic mismatch');
if(window.location.href!=='')throw new Error('unexpected browser fallback');
"""
    run_node(native_harness, f"Telegram frontend simulation ({path.name})")

    fallback_harness = f"""
const window={{location:{{href:''}}}};
const tg={{HapticFeedback:{{impactOccurred:()=>{{}}}}}};
{js}
if(openBotFlow('deposit')!==true)throw new Error('fallback rejected');
if(window.location.href!=='{EXPECTED['deposit']}')throw new Error('fallback URL mismatch');
"""
    run_node(fallback_harness, f"Browser fallback simulation ({path.name})")


def simulate_backend() -> None:
    assert ACTION_TO_PAYLOAD == {
        "deposit": "app_deposit",
        "withdraw": "app_withdraw",
        "gift": "app_gift",
    }
    for action, expected_url in EXPECTED.items():
        payload = ACTION_TO_PAYLOAD[action]
        assert resolve_miniapp_shortcut(payload) == action
        assert resolve_miniapp_shortcut(payload.upper()) == action
        assert build_miniapp_shortcut_url(action) == expected_url
    for invalid in (None, "", "deposit", "app_unknown", "ref_123"):
        assert resolve_miniapp_shortcut(invalid) is None
    assert build_miniapp_shortcut_url("invalid") is None

    start_path = ROOT / "telegram_bot/handlers/start.py"
    menu_path = ROOT / "telegram_bot/handlers/menu.py"
    start_source = start_path.read_text(encoding="utf-8")
    assert start_source.index("terms_accepted") < start_source.index("if shortcut_action:")

    cmd_start = function_source(start_path, "cmd_start")
    assert "resolve_miniapp_shortcut" in cmd_start
    assert "open_miniapp_shortcut_flow" in cmd_start

    router = function_source(start_path, "open_miniapp_shortcut_flow")
    for name in ("start_deposit_flow", "start_withdraw_flow", "start_gift_flow"):
        assert name in router

    expected_states = {
        "start_deposit_flow": "BotStates.selecting_deposit_currency",
        "start_withdraw_flow": "BotStates.selecting_withdraw_",
        "start_gift_flow": "BotStates.entering_gift_amount",
    }
    forbidden_financial_writes = (
        "create_transaction",
        "create_withdraw_transaction_atomic",
        "reserve_game_deposit_atomic",
        "approve_deposit_atomic",
        "create_gift(",
    )
    for function_name, expected_state in expected_states.items():
        source = function_source(menu_path, function_name)
        assert expected_state in source
        assert all(token not in source for token in forbidden_financial_writes), function_name


def main() -> None:
    simulate_backend()
    for filename in ("user_app.html", "user_app_pingo.html"):
        simulate_frontend(ROOT / "webapp" / filename)
    print("PASS: 3 backend payload routes")
    print("PASS: Telegram openTelegramLink simulation in both Mini App files")
    print("PASS: browser fallback simulation in both Mini App files")
    print("PASS: invalid actions rejected")
    print("PASS: terms check precedes shortcut routing")
    print("PASS: shortcut entry functions contain no financial write operation")
    print("PASS: no API/Neon request in Mini App shortcut JavaScript")


if __name__ == "__main__":
    main()
