import logging
import re
from http.cookies import SimpleCookie

from aiohttp import ClientSession
from .client_types import (
    TrackerDevice,
    One2TrackConfig,
    AuthenticationError
)

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.one2trackgps.com"
LOGIN_URL = f"{BASE_URL}/auth/users/sign_in"
DEVICE_URL = f"{BASE_URL}/users/{{account_id}}/devices"
SESSION_COOKIE = "_iadmin"


class GpsClient():
    config: One2TrackConfig
    cookie: str = ""
    csrf: str = ""
    account_id: str

    def __init__(self, config: One2TrackConfig, session: ClientSession):
        self.config = config
        self.account_id = config.id or ""
        self.session = session

    def set_account_id(self, account_id):
        self.account_id = account_id

    async def get_csrf(self):
        login_page = await self.call_api(LOGIN_URL)
        if login_page.status == 200:
            html = await login_page.text()
            self.csrf = self._parse_csrf(html)
            _LOGGER.debug("[pre-login] Found CSRF token")
            cookie = self._extract_session_cookie(login_page)
            if cookie:
                self.cookie = cookie
                _LOGGER.debug("[pre-login] Found session cookie")
        else:
            _LOGGER.warning("[pre-login] Failed pre-login, response code: %s", login_page.status)
            raise AuthenticationError("Login page unavailable")

    async def call_api(self, url: str, data=None, allow_redirects=True, use_json=False, extra_headers=None):
        headers = {}
        cookies = {'accepted_cookies': 'true'}

        if data is not None:
            headers["content-type"] = "application/x-www-form-urlencoded"

        if use_json:
            headers["content-type"] = "application/json"
            headers["Accept"] = "application/json"

        if extra_headers:
            headers.update(extra_headers)

        if self.cookie:
            cookies[SESSION_COOKIE] = self.cookie

        _LOGGER.debug('[http] %s', url)

        if data is not None:
            return await self.session.post(url,
                                           data=data,
                                           headers=headers,
                                           allow_redirects=allow_redirects,
                                           cookies=cookies
                                           )
        else:
            return await self.session.get(url, headers=headers, allow_redirects=allow_redirects, cookies=cookies)

    def _extract_session_cookie(self, response) -> str:
        """Extract _iadmin cookie value from response headers.

        Uses proper Set-Cookie parsing instead of string splitting to handle
        varying header formats across different server configurations.
        """
        for header_value in response.headers.getall('Set-Cookie', []):
            sc = SimpleCookie()
            sc.load(header_value)
            if SESSION_COOKIE in sc:
                value = sc[SESSION_COOKIE].value
                if value:
                    return value

        _LOGGER.debug("No %s cookie in response", SESSION_COOKIE)
        return ""

    def _parse_csrf(self, html) -> str:
        m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
        if not m:
            raise AuthenticationError("CSRF token not found on page")
        return m.group(1)

    async def login(self):
        login_data = {
            "authenticity_token": self.csrf,
            "user[login]": self.config.username,
            "user[password]": self.config.password,
            "gdpr": "1",
            "user[remember_me]": "1",
        }
        response = await self.call_api(LOGIN_URL, data=login_data, allow_redirects=False)

        _LOGGER.debug("[login] Status: %s", response.status)

        if response.status == 302:
            new_cookie = self._extract_session_cookie(response)
            if new_cookie:
                self.cookie = new_cookie
                _LOGGER.debug("[login] Login success")
                return

        _LOGGER.warning("[login] Failed to login, response code: %s", response.status)
        raise AuthenticationError("Invalid username or password")

    async def get_user_id(self):
        response = await self.call_api(f"{BASE_URL}/", allow_redirects=False)
        if response.status != 302 or 'Location' not in response.headers:
            raise AuthenticationError("Could not determine account ID")
        url = response.headers['Location']
        account_id = url.split('/')[4]
        _LOGGER.debug("[install] Extracted account id from redirect")
        self.set_account_id(account_id)
        return account_id

    async def install(self):
        await self.get_csrf()
        await self.login()
        return await self.get_user_id()

    async def _ensure_authenticated(self):
        """Ensure we have a valid session."""
        if not self.cookie:
            _LOGGER.debug("No session, logging in")
            await self.get_csrf()
            await self.login()
            await self.get_user_id()

    async def _get_csrf_token(self) -> str:
        """Get a fresh CSRF token for action endpoints."""
        response = await self.call_api(LOGIN_URL)
        if response.status == 200:
            html = await response.text()
            new_cookie = self._extract_session_cookie(response)
            if new_cookie:
                self.cookie = new_cookie
            return self._parse_csrf(html)
        raise AuthenticationError("Could not get CSRF token")

    async def _send_function(self, device_uuid: str, code: str, name: str) -> bool:
        """Send a function command to a device."""
        await self._ensure_authenticated()
        csrf = await self._get_csrf_token()

        url = f"{BASE_URL}/api/devices/{device_uuid}/functions"

        headers = {
            "x-csrf-token": csrf,
            "x-requested-with": "XMLHttpRequest",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        data = {
            "utf8": "\u2713",
            "function[code]": code,
            "function[name]": name,
        }

        response = await self.call_api(url, data=data, extra_headers=headers)
        return response.status == 200

    async def send_message(self, device_uuid: str, message: str) -> bool:
        """Send a text message to a One2Track device."""
        await self._ensure_authenticated()
        csrf = await self._get_csrf_token()

        url = f"{BASE_URL}/devices/{device_uuid}/messages"

        headers = {
            "x-csrf-token": csrf,
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "accept": "text/vnd.turbo-stream.html, text/html, application/xhtml+xml",
        }

        data = {
            "utf8": "\u2713",
            "authenticity_token": csrf,
            "device_message[message]": message,
        }

        response = await self.call_api(url, data=data, extra_headers=headers)
        return response.status == 200

    async def force_update(self, device_uuid: str) -> bool:
        """Activate positioning mode on the device for ~2 minutes."""
        return await self._send_function(device_uuid, "0039", "Actieve positioneringmodus")

    async def power_off(self, device_uuid: str) -> bool:
        """Shut down the device remotely."""
        return await self._send_function(device_uuid, "0048", "Shutdown")

    async def update(self) -> list[TrackerDevice]:
        await self._ensure_authenticated()
        return await self.get_device_data()

    async def get_device_data(self) -> list[TrackerDevice]:
        url = DEVICE_URL.format(account_id=self.account_id)
        response = await self.call_api(url, use_json=True)

        if response.status != 200:
            _LOGGER.error("[one2track] Cannot get devices, code: %s", response.status)
            self.cookie = ""
            self.csrf = ""
            raise AuthenticationError(f"API returned status {response.status}")

        data = await response.json(content_type=None)
        _LOGGER.debug("[devices] Got %s devices", len(data))
        return [item['device'] for item in data]
