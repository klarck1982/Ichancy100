#!/usr/bin/env python3
"""Runtime simulation of shortcut entry screens with aiogram states and fake DB.

Run after installing requirements. This script intentionally stubs repository,
connection, iChancy, and Syriatel modules before importing the handlers, so no
network or database connection can occur.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeRepository(types.ModuleType):
    def __init__(self):
        super().__init__("database.repository")
        self.pending = set()
        self.read_calls = []
        self.user = {
            "telegram_id": "123",
            "telegram_username": "tester",
            "player_id": "PLAYER-123",
            "bot_balance": 100_000,
        }

    def get_user(self, telegram_id):
        self.read_calls.append(("get_user", str(telegram_id)))
        return dict(self.user)

    def has_pending_transaction(self, telegram_id, tx_type=None):
        self.read_calls.append(("has_pending_transaction", str(telegram_id), tx_type))
        return tx_type in self.pending

    def get_user_transactions_history(self, telegram_id, limit=100):
        self.read_calls.append(("get_user_transactions_history", str(telegram_id), limit))
        return []

    def get_button_link(self, key):
        self.read_calls.append(("get_button_link", key))
        return "https://example.invalid/" + str(key)

    def service_gate_status(self, service):
        self.read_calls.append(("service_gate_status", service))
        return True, None


fake_repo = FakeRepository()
sys.modules["database.repository"] = fake_repo

fake_connection = types.ModuleType("database.connection")
fake_connection.DatabaseManager = type("DatabaseManager", (), {})
sys.modules["database.connection"] = fake_connection

fake_client_module = types.ModuleType("ichancy_api.client")
fake_client_module.ichancy_api_client = object()
sys.modules["ichancy_api.client"] = fake_client_module

fake_syriatel = types.ModuleType("integrations.syriatel_cash")

async def _unused_verify(*args, **kwargs):
    raise AssertionError("Syriatel API must not run while opening a shortcut")

fake_syriatel.verify_incoming_deposit = _unused_verify
sys.modules["integrations.syriatel_cash"] = fake_syriatel

from telegram_bot.handlers import menu, start  # noqa: E402


class FakeMessage:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.sent.append({
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        })

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edited.append({
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        })


class FakeState:
    def __init__(self):
        self.state = None
        self.data = {}

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)


async def simulate():
    # Deposit deep link: first screen only, no transaction creation.
    deposit_message, deposit_state = FakeMessage(), FakeState()
    ok = await menu.start_deposit_flow(deposit_message, 123, deposit_state, edit=False)
    assert ok is True
    assert deposit_state.state == menu.BotStates.selecting_deposit_currency
    assert len(deposit_message.sent) == 1
    deposit_buttons = deposit_message.sent[0]["reply_markup"].inline_keyboard
    assert deposit_buttons[0][0].callback_data == "dep_curr_syp"
    assert deposit_buttons[0][1].callback_data == "dep_curr_usd"

    # Withdraw deep link: fake user has no approved USD deposit, so SYP gateways appear.
    withdraw_message, withdraw_state = FakeMessage(), FakeState()
    ok = await menu.start_withdraw_flow(withdraw_message, 123, withdraw_state, edit=False)
    assert ok is True
    assert withdraw_state.state == menu.BotStates.selecting_withdraw_gateway
    assert withdraw_state.data == {"withdraw_currency": "syp"}
    withdraw_buttons = withdraw_message.sent[0]["reply_markup"].inline_keyboard
    assert withdraw_buttons[0][0].callback_data == "wit_gate_syriatel"

    # Gift deep link: asks for amount but does not deduct or create a gift.
    gift_message, gift_state = FakeMessage(), FakeState()
    ok = await menu.start_gift_flow(gift_message, 123, gift_state, edit=False)
    assert ok is True
    assert gift_state.state == menu.BotStates.entering_gift_amount
    assert "أرسل الآن المبلغ" in gift_message.sent[0]["text"]

    # Pending transaction guard still blocks a second deposit before state transition.
    fake_repo.pending.add("deposit_bot")
    pending_message, pending_state = FakeMessage(), FakeState()
    ok = await menu.start_deposit_flow(pending_message, 123, pending_state, edit=False)
    assert ok is False
    assert pending_state.state is None
    assert "طلب إيداع معلق" in pending_message.sent[0]["text"]

    # Registration guard still blocks deposit for a user without Player ID.
    fake_repo.pending.clear()
    old_player = fake_repo.user["player_id"]
    fake_repo.user["player_id"] = None
    unregistered_message, unregistered_state = FakeMessage(), FakeState()
    ok = await menu.start_deposit_flow(unregistered_message, 123, unregistered_state, edit=False)
    assert ok is False
    assert unregistered_state.state is None
    assert "تسجيل حساب iChancy" in unregistered_message.sent[0]["text"]
    fake_repo.user["player_id"] = old_player

    # Runtime dispatcher maps each whitelisted action to exactly one shared opener.
    routed = []

    def fake_opener(label):
        async def _open(message, user_id, state, edit=False):
            routed.append((label, user_id, edit))
            return True
        return _open

    menu.start_deposit_flow = fake_opener("deposit")
    menu.start_withdraw_flow = fake_opener("withdraw")
    menu.start_gift_flow = fake_opener("gift")
    for action in ("deposit", "withdraw", "gift"):
        ok = await start.open_miniapp_shortcut_flow(FakeMessage(), 123, FakeState(), action)
        assert ok is True
    assert await start.open_miniapp_shortcut_flow(FakeMessage(), 123, FakeState(), "invalid") is False
    assert routed == [
        ("deposit", 123, False),
        ("withdraw", 123, False),
        ("gift", 123, False),
    ]

    print("PASS: deposit shortcut reaches currency state")
    print("PASS: withdraw shortcut reaches SYP gateway state")
    print("PASS: gift shortcut reaches amount-entry state")
    print("PASS: runtime /start dispatcher routes all 3 whitelisted actions")
    print("PASS: invalid runtime action is rejected")
    print("PASS: pending-deposit guard blocks duplicate flow")
    print("PASS: iChancy registration guard remains active")
    print("PASS: fake repository exposed reads only; no financial write method exists")


if __name__ == "__main__":
    asyncio.run(simulate())
