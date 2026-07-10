import logging
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.melchersman.com/syr-cash/v1"


def _normalize_ref(value: str) -> str:
    return str(value or '').strip().replace(' ', '').replace('-', '').upper()


def _parse_date(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    return None


async def get_incoming_history(query=None, status='success', page=1):
    token = getattr(settings, 'SYRIATEL_API_TOKEN', None)
    q = query or getattr(settings, 'SYRIATEL_API_QUERY', None)
    if not token or not q:
        return {'ok': False, 'reason': 'not_configured'}
    params = {'query': str(q), 'status': status, 'page': int(page or 1)}
    url = f"{BASE_URL}/IncomingHistory?{urlencode(params)}"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={'api-token': token}) as resp:
                data = await resp.json(content_type=None)
        if not data.get('success'):
            return {'ok': False, 'reason': data.get('code') or 'api_error', 'raw': data}
        transactions = (data.get('data') or {}).get('transactions') or []
        return {'ok': True, 'transactions': transactions, 'raw': data}
    except Exception as e:
        logger.error(f"Syriatel API IncomingHistory error: {e}", exc_info=True)
        return {'ok': False, 'reason': 'network_error', 'message': str(e)}


def find_matching_transaction(transactions, expected_amount, user_reference, created_at=None, tolerance_minutes=180):
    """Find matching Syriatel incoming transfer.

    user_reference may be transaction_no or sender phone. Amount must match exactly as integer SYP.
    If created_at is passed, ignore API transactions much older than the bot request.
    """
    expected = int(Decimal(str(expected_amount or 0)))
    ref = _normalize_ref(user_reference)
    is_phone = ref.startswith('09') and len(ref) == 10 and ref.isdigit()
    created_dt = created_at.replace(tzinfo=None) if hasattr(created_at, 'replace') else None
    if created_dt and getattr(created_at, 'tzinfo', None):
        created_dt = created_at.replace(tzinfo=None)

    for tx in transactions:
        try:
            if str(tx.get('status')) not in ('1', 'success', 'SUCCESS'):
                continue
            amount = int(Decimal(str(tx.get('amount') or tx.get('net') or 0)))
            if amount != expected:
                continue
            tx_no = _normalize_ref(tx.get('transaction_no'))
            from_gsm = _normalize_ref(tx.get('from_gsm'))
            if ref:
                if is_phone:
                    if from_gsm != ref:
                        continue
                elif tx_no != ref:
                    continue
            tx_date = _parse_date(tx.get('date'))
            if created_dt and tx_date:
                # Allow transfer a little before request, but reject very old matches
                if tx_date < created_dt - timedelta(minutes=10):
                    continue
                if tx_date > created_dt + timedelta(minutes=tolerance_minutes):
                    continue
            return {'ok': True, 'transaction': tx, 'external_ref': tx_no or ref}
        except Exception:
            continue
    return {'ok': False, 'reason': 'not_found'}


async def verify_incoming_deposit(expected_amount, user_reference, created_at=None):
    history = await get_incoming_history(status='success')
    if not history.get('ok'):
        return history
    return find_matching_transaction(history.get('transactions') or [], expected_amount, user_reference, created_at=created_at)
