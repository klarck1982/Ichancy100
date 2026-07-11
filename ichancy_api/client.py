import requests
import logging
import asyncio
import time
from config import settings

logger = logging.getLogger(__name__)


class IChancyClient:
    BASE_URL = getattr(settings, 'ICHANCY_AGENT_BASE_URL', 'https://agents.ichancy100.com')

    HEADERS = {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://agents.ichancy100.com',
        'Referer': 'https://agents.ichancy100.com/'
    }

    def __init__(self):
        self.session = requests.Session()
        self.update_headers_and_cookies()
        self.load_cookie_from_db()

    def update_headers_and_cookies(self, new_cookie_string=None):
        self.session.headers.clear()
        self.session.headers.update(self.HEADERS)
        self.session.headers['User-Agent'] = settings.USER_AGENT

        cookie_to_use = new_cookie_string if new_cookie_string else getattr(settings, 'COOKIE_STRING', '')
        cookies_dict = self._parse_cookie_string(cookie_to_use)
        self.session.cookies.update(cookies_dict)
        logger.info(f"Loaded {len(cookies_dict)} cookies into Caesar_Bot iChancy session.")

    @staticmethod
    def _parse_cookie_string(cookie_string):
        cookies = {}
        if not cookie_string:
            return cookies
        for pair in cookie_string.split(';'):
            pair = pair.strip()
            if '=' in pair:
                name, value = pair.split('=', 1)
                cookies[name.strip()] = value.strip()
        return cookies

    @staticmethod
    def _is_invalid_session_result(result_data):
        if isinstance(result_data, str):
            return result_data.lower() in {"unauthorized", "expired", "session_expired", "ex", "not_authorized"}
        if isinstance(result_data, dict):
            msg = str(result_data.get('message') or result_data.get('error') or '').lower()
            return msg in {"unauthorized", "expired", "session_expired", "ex", "not_authorized"}
        return False

    def load_cookie_from_db(self):
        try:
            from database.connection import DatabaseManager
            res = DatabaseManager.execute_query_dict("SELECT ichancy_cookie FROM bot_settings WHERE id = 1", fetch='one')
            if res and res.get('ichancy_cookie'):
                cookie_str = res['ichancy_cookie']
                self.update_headers_and_cookies(cookie_str)
                logger.info("Successfully synchronized active session cookie from database.")
                return True
        except Exception as e:
            logger.error(f"Error loading cookie from database: {e}")
        return False

    def _login_agent(self):
        logger.info("[Caesar_Bot] [Auto-Login] Starting agent login...")
        login_page_url = f"{self.BASE_URL}/login"
        try:
            self.session.get(login_page_url, timeout=30)

            signin_url = f"{self.BASE_URL}/global/api/User/signIn"
            payload = {
                "username": settings.AGENT_USERNAME,
                "login": settings.AGENT_USERNAME,
                "password": settings.AGENT_PASSWORD
            }
            response_post = self.session.post(signin_url, json=payload, timeout=30)
            if response_post.status_code != 200:
                logger.error(f"Agent login failed: HTTP {response_post.status_code} - Response: {response_post.text}")
                return False

            response_json = response_post.json()
            result = response_json.get("result", {})
            is_success = False
            if isinstance(result, dict) and result.get("message") == "dashboard":
                is_success = True
            elif response_json.get("status") is True:
                is_success = True

            if not is_success:
                logger.error(f"Agent login failed: {response_json}")
                return False

            init_apis = [
                f"{self.BASE_URL}/global/api/core/getData",
                f"{self.BASE_URL}/global/api/Agent/getAgentWallet",
                f"{self.BASE_URL}/global/api/Message/getTotalUnreadMessagesCount",
                f"{self.BASE_URL}/global/api/UserNotification/getAllUserNotifications"
            ]
            for api_url in init_apis:
                try:
                    self.session.post(api_url, json={}, timeout=15)
                except Exception as init_err:
                    logger.warning(f"Init endpoint failed: {api_url} -> {init_err}")

            cookies_dict = self.session.cookies.get_dict()
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            try:
                from database.connection import DatabaseManager
                DatabaseManager.execute_query("UPDATE bot_settings SET ichancy_cookie = %s, last_cookie_update = CURRENT_TIMESTAMP WHERE id = 1", (cookie_str,))
            except Exception as db_err:
                logger.warning(f"Failed to persist cookie to DB: {db_err}")
            self.update_headers_and_cookies(cookie_str)
            logger.info("[Caesar_Bot] Agent Login: SUCCESSFUL")
            return True
        except Exception as e:
            logger.error(f"[Caesar_Bot] Agent Login exception: {e}")
            return False

    def _check_session_validity(self):
        url = f"{self.BASE_URL}/global/api/Agent/getAgentWalletByAgentId"
        payload = {
            'affiliateId': int(settings.PARENT_ID) if settings.PARENT_ID else None,
            'currencyCode': "NSP"
        }
        try:
            response = self.session.post(url, json=payload, timeout=10)
            if response.status_code != 200:
                return False
            data = response.json()
            result_data = data.get('result')
            if self._is_invalid_session_result(result_data):
                return False
            return bool(result_data)
        except Exception:
            return False

    def _fetch_player_statistics_page(self, payload):
        url = f"{self.BASE_URL}/global/api/Statistics/getPlayersStatisticsPro"
        response = self.session.post(url, json=payload, timeout=30)
        if response.status_code in [401, 403]:
            logger.warning("Session expired while fetching player statistics. Re-login...")
            if self._login_agent():
                response = self.session.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def _extract_player_id_from_records(self, records, target_username):
        target = str(target_username).strip().lower()
        for row in records or []:
            username = str(row.get('username', '')).strip().lower()
            if username == target:
                player_id = row.get('playerId') or row.get('playerID') or row.get('id')
                if player_id:
                    return str(player_id)
        return None

    def _get_player_id(self, target_username, max_attempts=5, delay_seconds=2):
        logger.info(f"[Caesar_Bot] Fetching Player ID for iChancy username: {target_username}")
        target_username = str(target_username).strip()
        if not target_username:
            return None

        base_payload = {
            "start": 0,
            "limit": 100,
            "filter": {
                "username": {
                    "action": "=",
                    "value": target_username
                }
            }
        }

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"[Caesar_Bot] Player ID fetch attempt {attempt}/{max_attempts} for {target_username}")
                data = self._fetch_player_statistics_page(base_payload)
                result_data = data.get('result')
                if isinstance(result_data, dict):
                    records = result_data.get('records', [])
                    player_id = self._extract_player_id_from_records(records, target_username)
                    if player_id:
                        logger.info(f"[Caesar_Bot] Player ID found by exact filter: {player_id}")
                        return player_id

                logger.warning("Player ID not found with exact filter, attempting fallback search...")
                fallback_payload = {
                    "start": 0,
                    "limit": 500,
                    "filter": {}
                }
                fallback_data = self._fetch_player_statistics_page(fallback_payload)
                fallback_result = fallback_data.get('result')
                if isinstance(fallback_result, dict):
                    fallback_records = fallback_result.get('records', [])
                    player_id = self._extract_player_id_from_records(fallback_records, target_username)
                    if player_id:
                        logger.info(f"[Caesar_Bot] Player ID found by fallback search: {player_id}")
                        return player_id

                if attempt < max_attempts:
                    logger.warning(f"[Caesar_Bot] Player ID not visible yet. Waiting {delay_seconds}s before retry...")
                    time.sleep(delay_seconds)
            except Exception as e:
                logger.error(f"Error fetching player ID on attempt {attempt}: {e}")
                if attempt < max_attempts:
                    time.sleep(delay_seconds)

        logger.error(f"[Caesar_Bot] Failed to fetch Player ID after {max_attempts} attempts for {target_username}")
        return None

    def _register_account(self, username, password, email, parent_id=None):
        logger.info(f"[Caesar_Bot] Submitting registration for username: {username} with email: {email}")
        try:
            try:
                logger.info("[Caesar_Bot] Pre-initializing session parameters via getData...")
                self.session.post(f"{self.BASE_URL}/global/api/core/getData", json={}, timeout=15)
            except Exception as e:
                logger.warning(f"[Caesar_Bot] Failed pre-initializing via getData: {e}")

            url = f"{self.BASE_URL}/global/api/Player/registerPlayer"
            payload = {
                "player": {
                    "login": username,
                    "email": email,
                    "password": password,
                    "parentId": int(parent_id or settings.PARENT_ID) if (parent_id or settings.PARENT_ID) else None
                }
            }
            response = self.session.post(url, json=payload, timeout=30)

            if response.status_code in [401, 403]:
                logger.warning("Session expired on registration. Re-login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)

            response_json = response.json()
            logger.info(f"[Caesar_Bot] Raw registration response JSON: {response_json}")
            result_data = response_json.get("result")

            is_invalid_session = False
            if self._is_invalid_session_result(result_data):
                if not self._check_session_validity():
                    is_invalid_session = True
            elif not result_data:
                if not self._check_session_validity():
                    is_invalid_session = True

            if is_invalid_session:
                logger.warning("[Caesar_Bot] Session expired on registration. Retrying login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
                    response_json = response.json()
                    logger.info(f"[Caesar_Bot] Raw registration response JSON after retry: {response_json}")
                    result_data = response_json.get("result")

            if not result_data or isinstance(result_data, str):
                error_content = "Registration failed"
                if result_data == "ex":
                    error_content = "اسم المستخدم أو البريد الإلكتروني مسجل مسبقاً في المنصة!"
                notifications = response_json.get("notification", [])
                if notifications:
                    error_content = notifications[0].get("content", error_content)
                return {'success': False, 'error': error_content}

            player_id = self._get_player_id(username, max_attempts=5, delay_seconds=2)
            return {
                'success': True,
                'username': username,
                'password': password,
                'email': email,
                'player_id': player_id,
                'response': response_json
            }
        except Exception as e:
            logger.error(f"HTTP exception during registration: {e}")
            return {'success': False, 'error': str(e)}

    def _get_admin_balance(self):
        url = f"{self.BASE_URL}/global/api/Agent/getAgentWalletByAgentId"
        payload = {
            'affiliateId': int(settings.PARENT_ID) if settings.PARENT_ID else None,
            'currencyCode': "NSP"
        }
        try:
            response = self.session.post(url, json=payload, timeout=30)
            if response.status_code in [401, 403]:
                logger.warning("Session expired! Triggering automatic self-healing login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            result_data = data.get('result')
            if self._is_invalid_session_result(result_data):
                logger.warning("Session invalid on admin balance! Retrying login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
                    data = response.json()
                    result_data = data.get('result')
            if result_data is not None:
                if isinstance(result_data, list) and len(result_data) > 0 and isinstance(result_data[0], dict):
                    if 'balance' in result_data[0]:
                        return int(result_data[0].get('balance') or 0)
                elif isinstance(result_data, dict) and 'balance' in result_data:
                    return int(result_data.get('balance') or 0)

            # مهم: لا نعيد 0 عند فشل/فراغ الرد، لأن ذلك يصفّر رصيد الكاشيرة المخزن ويطلق إنذاراً خاطئاً.
            logger.warning(f"Admin balance response did not contain a balance field: {data}")
            return None
        except Exception as e:
            logger.error(f"Error fetching admin balance: {e}")
            return None

    def _get_agent_transaction_list(self, from_date, to_date, limit=1000, start=0, is_to_me=False, affiliate_id=None):
        """جلب سجل حركات الكاشيرة/الوكيل من iChancy."""
        url = f"{self.BASE_URL}/global/api/Agent/getAgentTransactionList"
        agent_id = affiliate_id or getattr(settings, 'AGENT_ID', None) or getattr(settings, 'PARENT_ID', None)
        try:
            agent_id_int = int(agent_id) if agent_id else None
        except Exception:
            agent_id_int = None
        payload = {
            "start": int(start or 0),
            "limit": int(limit or 1000),
            "filter": {
                "currency": {
                    "action": "=",
                    "valueLabel": "NSP",
                    "value": "NSP"
                },
                "date": {
                    "action": "between",
                    "from": from_date,
                    "to": to_date,
                    "valueLabel": f"{from_date} - {to_date}"
                },
                "isToMe": {
                    "action": "=",
                    "value": bool(is_to_me),
                    "valueLabel": bool(is_to_me)
                }
            }
        }
        if agent_id_int is not None:
            payload["filter"]["affiliateId"] = {
                "action": "=",
                "value": agent_id_int,
                "valueLabel": agent_id_int
            }
        try:
            response = self.session.post(url, json=payload, timeout=45)
            if response.status_code in [401, 403]:
                logger.warning("Session expired while fetching agent transaction list. Re-login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=45)
            response.raise_for_status()
            data = response.json()
            result_data = data.get('result')
            if self._is_invalid_session_result(result_data):
                logger.warning("Session invalid on agent transaction list. Retrying login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=45)
                    response.raise_for_status()
                    data = response.json()
            return data
        except Exception as e:
            logger.error(f"Error fetching agent transaction list: {e}")
            return {'status': False, 'result': {'records': [], 'totalRecordsCount': 0}, 'error': str(e)}

    def _get_player_balance(self, player_id):
        url = f"{self.BASE_URL}/global/api/Player/getPlayerBalanceById"
        payload = {'playerId': player_id}
        try:
            response = self.session.post(url, json=payload, timeout=30)
            if response.status_code in [401, 403]:
                logger.warning("Session expired! Triggering automatic self-healing login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            result_data = data.get('result')
            if self._is_invalid_session_result(result_data):
                logger.warning("Session invalid on player balance! Retrying login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
                    data = response.json()
                    result_data = data.get('result')
            results = result_data if isinstance(result_data, list) else []
            if results:
                return int(results[0].get('balance', 0))
            return 0
        except Exception as e:
            logger.error(f"Error fetching player balance: {e}")
            return 0

    def _transfer_money(self, player_id, amount, comment=None):
        url = f"{self.BASE_URL}/global/api/Player/depositToPlayer"
        payload = {
            'amount': amount,
            'comment': comment,
            'playerId': player_id,
            'currencyCode': "NSP",
            'moneyStatus': 5
        }
        try:
            response = self.session.post(url, json=payload, timeout=30)
            if response.status_code in [401, 403]:
                logger.warning("Session expired! Triggering automatic self-healing login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            response_json = response.json()
            result_data = response_json.get("result")
            if self._is_invalid_session_result(result_data):
                logger.warning("Session invalid on money transfer! Retrying login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
                    response_json = response.json()
                    result_data = response_json.get("result")
            if self._is_invalid_session_result(result_data):
                logger.error(f"Transfer failed after re-login: invalid session result={result_data}")
                return False
            if result_data:
                logger.info(f"Successfully transferred +{amount} NSP to Player: {player_id}")
                return True
            logger.error(f"Transfer failed: empty/false result response={response_json}")
            return False
        except Exception as e:
            logger.error(f"Error transferring money: {e}")
            return False

    def _withdraw_money(self, player_id, amount, comment=None):
        url = f"{self.BASE_URL}/global/api/Player/withdrawFromPlayer"
        payload = {
            'amount': -amount,
            'comment': comment,
            'playerId': player_id,
            'currencyCode': "NSP",
            'moneyStatus': 5
        }
        try:
            response = self.session.post(url, json=payload, timeout=30)
            if response.status_code in [401, 403]:
                logger.warning("Session expired while withdrawing money. Re-login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            response_json = response.json()
            result_data = response_json.get("result")
            if self._is_invalid_session_result(result_data):
                logger.warning("Session invalid on money withdrawal! Retrying login...")
                if self._login_agent():
                    response = self.session.post(url, json=payload, timeout=30)
                    response_json = response.json()
                    result_data = response_json.get("result")
            if self._is_invalid_session_result(result_data):
                logger.error(f"Withdraw failed after re-login: invalid session result={result_data}")
                return False
            if result_data:
                logger.info(f"Successfully withdrew -{amount} NSP from Player: {player_id}")
                return True
            logger.error(f"Withdraw failed: empty/false result response={response_json}")
            return False
        except Exception as e:
            logger.error(f"Error withdrawing money: {e}")
            return False

    async def register_account(self, username, password, email):
        return await asyncio.to_thread(self._register_account, username, password, email)

    async def get_player_id(self, target_username):
        return await asyncio.to_thread(self._get_player_id, target_username)

    async def get_admin_balance(self):
        return await asyncio.to_thread(self._get_admin_balance)

    async def get_player_balance(self, player_id):
        return await asyncio.to_thread(self._get_player_balance, player_id)

    async def get_agent_transaction_list(self, from_date, to_date, limit=1000, start=0, is_to_me=False, affiliate_id=None):
        return await asyncio.to_thread(self._get_agent_transaction_list, from_date, to_date, limit, start, is_to_me, affiliate_id)

    async def transfer_money(self, player_id, amount, comment=None):
        return await asyncio.to_thread(self._transfer_money, player_id, amount, comment)

    async def withdraw_money(self, player_id, amount, comment=None):
        return await asyncio.to_thread(self._withdraw_money, player_id, amount, comment)

    async def get_player_turnover(self, player_id, field_name='totalBet'):
        """جلب إجمالي مبالغ المراهنات (Turnover) للاعب من إحصائيات iChancy."""
        try:
            payload = {
                "start": 0,
                "limit": 1,
                "filter": {
                    "playerId": {
                        "action": "=",
                        "value": player_id
                    }
                }
            }
            data = await asyncio.to_thread(self._fetch_player_statistics_page, payload)
            result = data.get('result', {})
            if isinstance(result, dict) and 'records' in result and result['records']:
                player_stats = result['records'][0]
                turnover = player_stats.get(field_name, 0)
                return int(turnover or 0)
            return 0
        except Exception as e:
            logger.error(f"Error fetching player turnover: {e}")
            return 0

    async def check_session_validity(self):
        return await asyncio.to_thread(self._check_session_validity)

    # ================================================================
    # 🆕 دوال الـ API القياسية (تعيد dict بـ success/message)
    # لكي تتوافق مع ما تتوقعه معالجات الإيداع/السحب التلقائي في اللعبة
    # ================================================================

    async def deposit_to_player(self, player_id, amount, comment=None):
        """إيداع مبلغ في حساب اللاعب — يعيد {'success': bool, 'message': str}."""
        try:
            ok = await asyncio.to_thread(self._transfer_money, player_id, amount, comment)
            if ok:
                return {'success': True, 'message': 'تم الإيداع في حساب اللاعب بنجاح.', 'player_id': player_id, 'amount': amount}
            return {'success': False, 'message': 'فشل الإيداع في حساب اللاعب (لم يؤكد الـ API العملية).'}
        except Exception as e:
            logger.error(f"deposit_to_player exception: {e}")
            return {'success': False, 'message': str(e)}

    async def withdraw_from_player(self, player_id, amount, comment=None):
        """سحب مبلغ من حساب اللاعب — يعيد {'success': bool, 'message': str}."""
        try:
            ok = await asyncio.to_thread(self._withdraw_money, player_id, amount, comment)
            if ok:
                return {'success': True, 'message': 'تم السحب من حساب اللاعب بنجاح.', 'player_id': player_id, 'amount': amount}
            return {'success': False, 'message': 'فشل السحب من حساب اللاعب (لم يؤكد الـ API العملية).'}
        except Exception as e:
            logger.error(f"withdraw_from_player exception: {e}")
            return {'success': False, 'message': str(e)}


ichancy_api_client = IChancyClient()
