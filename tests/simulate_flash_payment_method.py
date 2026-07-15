#!/usr/bin/env python3
"""Offline regression simulation for Flash Bonus payment-method display and logic.

No database, Neon, Telegram, or network connection is used.
"""

from __future__ import annotations

import ast
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METHODS = {
    "all": "كل طرق الإيداع",
    "syriatel": "سيريتل كاش",
    "mtn": "MTN كاش",
    "sham_syp": "شام كاش (ليرة)",
    "sham_usd": "شام كاش (دولار)",
    "usdt_trc": "USDT (TRC20)",
    "usdt_bep": "USDT (BEP20)",
}


def selected_python_nodes(path: Path, assignments: set[str], functions: set[str]):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = set()
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
            if names & assignments:
                selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            selected.append(node)
    return ast.Module(body=selected, type_ignores=[])


def simulate_backend_payloads() -> None:
    path = ROOT / "telegram_bot/main.py"
    module = selected_python_nodes(
        path,
        {"FLASH_PAYMENT_METHOD_LABELS", "FLASH_PAYMENT_METHODS", "FLASH_PAYMENT_METHOD_ALIASES"},
        {"_normalize_flash_payment_method", "_flash_method_label", "_serialize_flash_bonus"},
    )
    namespace = {}
    exec(compile(module, str(path), "exec"), namespace)

    normalize = namespace["_normalize_flash_payment_method"]
    label = namespace["_flash_method_label"]
    serialize = namespace["_serialize_flash_bonus"]
    assert namespace["FLASH_PAYMENT_METHOD_LABELS"] == METHODS

    ends = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    for method, expected_label in METHODS.items():
        assert normalize(method) == method
        assert label(method) == expected_label
        payload = serialize({"id": 7, "percent": 8, "payment_method": method, "ends_at": ends})
        assert payload == {
            "id": 7,
            "percent": 8.0,
            "payment_method": method,
            "method_label": expected_label,
            "ends_at": ends.isoformat(),
        }

    aliases = {
        "syriatel_cash": "syriatel",
        "mtn_cash": "mtn",
        "sham_cash_syp": "sham_syp",
        "sham_cash_usd": "sham_usd",
        "usdt_trc20": "usdt_trc",
        "usdt_bep20": "usdt_bep",
    }
    for alias, canonical in aliases.items():
        assert normalize(alias) == canonical
        assert serialize({"payment_method": alias})["payment_method"] == canonical

    assert normalize("not-valid") is None
    invalid = serialize({"id": 9, "percent": 5, "payment_method": "not-valid", "ends_at": ends})
    assert invalid["payment_method"] == "unknown"
    assert invalid["method_label"] == "طريقة دفع غير معروفة"

    source = path.read_text(encoding="utf-8")
    assert "flash = _serialize_flash_bonus(fb)" in source
    assert "payment_method = _normalize_flash_payment_method(payload.get('payment_method'))" in source
    assert "طريقة الدفع غير صالحة لفلاش البونص" in source


def simulate_financial_matching() -> None:
    """Execute only calculate_best_deposit_bonus with fake in-memory rules."""
    path = ROOT / "database/repository.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "calculate_best_deposit_bonus"
    )
    module = ast.Module(body=[function], type_ignores=[])

    class Logger:
        def error(self, *args, **kwargs):
            raise AssertionError(f"Unexpected calculation error: {args}")

    state = {"flash": None}
    namespace = {
        "get_active_bonus_rules": lambda: [],
        "get_active_flash_bonus": lambda: state["flash"],
        "logger": Logger(),
    }
    exec(compile(module, str(path), "exec"), namespace)
    calculate = namespace["calculate_best_deposit_bonus"]

    for flash_method in METHODS:
        state["flash"] = {"id": 1, "percent": 10, "payment_method": flash_method}
        for deposit_method in METHODS:
            if deposit_method == "all":
                continue
            result = calculate(1000, deposit_method)
            should_apply = flash_method == "all" or flash_method == deposit_method
            assert result["bonus_amount"] == (100 if should_apply else 0), (flash_method, deposit_method, result)
            if should_apply:
                assert result["rule"]["payment_method"] == flash_method


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


def simulate_frontend(path: Path) -> None:
    html = path.read_text(encoding="utf-8")
    begin = "// FLASH_METHOD_DISPLAY_BEGIN"
    end = "// FLASH_METHOD_DISPLAY_END"
    assert begin in html and end in html
    js = html[html.index(begin): html.index(end) + len(end)]
    assert "fetch(" not in js and "/api/" not in js
    assert '<div class="fl-desc">${methodDescription}</div>' in html
    assert "بونص إضافي على كل إيداع" not in html

    expected = repr(METHODS).replace("'", '"')
    harness = f"""
{js}
const expected={expected};
for(const [method,label] of Object.entries(expected)){{
  const desc=getFlashBannerDescription({{payment_method:method}});
  if(method==='all'){{
    if(desc!=='على كل طرق الإيداع — لوقت محدود')throw new Error('all text mismatch: '+desc);
  }}else{{
    if(!desc.includes(label)||!desc.startsWith('حصريًا على إيداعات'))throw new Error(method+' mismatch: '+desc);
    if(desc.includes('كل طرق الإيداع'))throw new Error(method+' falsely says all');
  }}
}}
const unknown=getFlashBannerDescription({{payment_method:'invalid'}});
if(unknown.includes('كل طرق الإيداع'))throw new Error('invalid method falsely says all');
"""
    run_node(harness, f"Flash banner display ({path.name})")


def simulate_admin_dashboard() -> None:
    html = (ROOT / "webapp/dashboard.html").read_text(encoding="utf-8")
    for method, label in METHODS.items():
        assert f'<option value="{method}">' in html
        assert label in html
    assert "flashMethodLabel(af.payment_method)" in html
    assert "flashMethodLabel(f.payment_method)" in html


def main() -> None:
    simulate_backend_payloads()
    simulate_financial_matching()
    for filename in ("user_app.html", "user_app_pingo.html"):
        simulate_frontend(ROOT / "webapp" / filename)
    simulate_admin_dashboard()
    print("PASS: user API serializes payment_method and Arabic method_label")
    print("PASS: invalid Flash methods are rejected by admin API")
    print("PASS: financial calculation applies Flash only to the selected method")
    print("PASS: all-method Flash still applies to every supported deposit method")
    print("PASS: user banner text matches all 7 payment-method choices")
    print("PASS: specific-method banners never claim all deposit methods")
    print("PASS: admin active status and history display the selected method")
    print("PASS: no database, Neon, Telegram, or network connection used")


if __name__ == "__main__":
    main()
