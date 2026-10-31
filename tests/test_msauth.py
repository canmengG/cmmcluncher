"""Offline tests for the Microsoft/Xbox authentication chain."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from mc_launcher.core import secure_store, settings
from mc_launcher.core.launch import build_launch_plan
from mc_launcher.core.models import LaunchOptions, MicrosoftAccount
from mc_launcher.core.msauth import (
    AUTH_HOSTS,
    AuthError,
    DeviceCode,
    HttpAuthTransport,
    authorize_xsts,
    ensure_account_fresh,
    fetch_profile,
    login_device_code,
    login_with_xbox,
    poll_device_code,
    refresh_account,
    request_device_code,
    validate_client_id,
)
from mc_launcher.core.net import DownloadError, HttpClient
from mc_launcher.core.paths import normalize_game_root
from mc_launcher.core.version import list_versions

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
PROFILE_ID = "0123456789abcdef0123456789abcdef"
DEVICE = {
    "device_code": "device-code",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://microsoft.com/devicelogin",
    "expires_in": 900,
    "interval": 1,
    "message": "test",
}
MS_TOKEN = {
    "access_token": "msa-access",
    "refresh_token": "msa-refresh",
    "expires_in": 3600,
    "scope": "XboxLive.signin offline_access",
}
XBL = {
    "Token": "xbl-token",
    "DisplayClaims": {"xui": [{"uhs": "user-hash", "xid": "12345"}]},
}
XSTS = {
    "Token": "xsts-token",
    "DisplayClaims": {"xui": [{"uhs": "user-hash", "xid": "12345"}]},
}
MC = {"access_token": "mc-access", "expires_in": 86400}
PROFILE = {"id": PROFILE_ID, "name": "Steve"}


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _take(self, kind: str, url: str, payload: dict) -> tuple[int, dict]:
        self.calls.append((kind, url, payload))
        if not self.responses:
            raise AssertionError(f"unexpected {kind} request: {url}")
        return self.responses.pop(0)

    def post_form(self, url: str, fields: dict[str, str]) -> tuple[int, dict]:
        return self._take("form", url, dict(fields))

    def post_json(self, url: str, payload: dict) -> tuple[int, dict]:
        return self._take("json", url, payload)

    def get_json(self, url: str, *, bearer: str) -> tuple[int, dict]:
        return self._take("get", url, {"bearer": bearer})


def _full_responses() -> list[tuple[int, dict]]:
    return [
        (200, DEVICE),
        (200, MS_TOKEN),
        (200, XBL),
        (200, XSTS),
        (200, MC),
        (200, PROFILE),
    ]


class MsaAuthTests(unittest.TestCase):
    def test_validate_client_id(self) -> None:
        self.assertEqual(validate_client_id(CLIENT_ID), CLIENT_ID)
        for value in ("", "not-a-guid", "11111111111111111111111111111111"):
            with self.assertRaises(AuthError):
                validate_client_id(value)

    def test_full_device_code_login(self) -> None:
        transport = FakeTransport(_full_responses())
        codes: list[DeviceCode] = []
        account = login_device_code(
            CLIENT_ID,
            transport,
            on_code=codes.append,
            sleep=lambda _seconds: None,
            now=lambda: 1000.0,
        )
        self.assertEqual(account.name, "Steve")
        self.assertEqual(account.uuid, "01234567-89ab-cdef-0123-456789abcdef")
        self.assertEqual(account.access_token, "mc-access")
        self.assertEqual(account.refresh_token, "msa-refresh")
        self.assertEqual(account.xuid, "12345")
        self.assertEqual(account.client_id, CLIENT_ID)
        self.assertEqual(account.expires_at, 1000.0 + 86400)
        self.assertEqual(codes[0].user_code, "ABCD-EFGH")
        self.assertEqual(
            [call[0] for call in transport.calls],
            ["form", "form", "json", "json", "json", "get"],
        )
        self.assertIn("devicecode", transport.calls[0][1])
        self.assertIn("oauth2/v2.0/token", transport.calls[1][1])
        self.assertEqual(
            transport.calls[1][2]["grant_type"],
            "urn:ietf:params:oauth:grant-type:device_code",
        )
        self.assertEqual(
            transport.calls[2][2]["Properties"]["RpsTicket"], "d=msa-access"
        )
        self.assertEqual(
            transport.calls[3][2]["Properties"]["UserTokens"], ["xbl-token"]
        )
        self.assertEqual(
            transport.calls[4][2]["identityToken"],
            "XBL3.0 x=user-hash;xsts-token",
        )
        self.assertEqual(transport.calls[5][2]["bearer"], "mc-access")

    def test_device_code_error_mentions_public_client(self) -> None:
        transport = FakeTransport(
            [
                (
                    400,
                    {
                        "error": "unauthorized_client",
                        "error_description": "unauthorized_client: public client required",
                    },
                )
            ]
        )
        with self.assertRaises(AuthError) as raised:
            request_device_code(CLIENT_ID, transport)
        self.assertIn("公共客户端", str(raised.exception))
        self.assertIn("unauthorized_client", str(raised.exception))

    def test_poll_authorization_pending_then_success(self) -> None:
        transport = FakeTransport(
            [
                (200, DEVICE),
                (400, {"error": "authorization_pending"}),
                (200, MS_TOKEN),
                (200, XBL),
                (200, XSTS),
                (200, MC),
                (200, PROFILE),
            ]
        )
        account = login_device_code(
            CLIENT_ID, transport, sleep=lambda _seconds: None, now=lambda: 1.0
        )
        self.assertEqual(account.name, "Steve")

    def test_poll_slow_down(self) -> None:
        device = DeviceCode(
            device_code="d",
            user_code="u",
            verification_uri="https://example.com",
            expires_in=900,
            interval=1,
        )
        transport = FakeTransport(
            [
                (400, {"error": "slow_down"}),
                (200, MS_TOKEN),
            ]
        )
        token = poll_device_code(device, CLIENT_ID, transport, sleep=lambda _s: None)
        self.assertEqual(token.access_token, "msa-access")
        self.assertEqual(len(transport.calls), 2)

    def test_poll_expired(self) -> None:
        device = DeviceCode(
            device_code="d",
            user_code="u",
            verification_uri="https://example.com",
            expires_in=900,
            interval=1,
        )
        transport = FakeTransport([(400, {"error": "expired_token"})])
        with self.assertRaises(AuthError) as raised:
            poll_device_code(device, CLIENT_ID, transport, sleep=lambda _s: None)
        self.assertIn("过期", str(raised.exception))

    def test_xsts_no_xbox_account(self) -> None:
        transport = FakeTransport(
            [
                (200, DEVICE),
                (200, MS_TOKEN),
                (200, XBL),
                (401, {"XErr": 2148916233}),
            ]
        )
        with self.assertRaises(AuthError) as raised:
            login_device_code(
                CLIENT_ID, transport, sleep=lambda _s: None, now=lambda: 1.0
            )
        self.assertIn("没有 Xbox 账户", str(raised.exception))

    def test_invalid_app_registration_is_explained(self) -> None:
        transport = FakeTransport(
            [
                (200, DEVICE),
                (200, MS_TOKEN),
                (200, XBL),
                (200, XSTS),
                (
                    403,
                    {
                        "errorMessage": "Invalid app registration",
                        "path": "/authentication/login_with_xbox",
                    },
                ),
            ]
        )
        with self.assertRaises(AuthError) as raised:
            login_device_code(
                CLIENT_ID, transport, sleep=lambda _s: None, now=lambda: 1.0
            )
        message = str(raised.exception)
        self.assertIn("Invalid app registration", message)
        self.assertIn("allowlist", message)
        self.assertIn("16254801392141", message)
        self.assertIn("不是账号没有 Java 版", message)

    def test_profile_404_means_no_ownership(self) -> None:
        responses = _full_responses()
        responses[-1] = (404, {})
        transport = FakeTransport(responses)
        with self.assertRaises(AuthError) as raised:
            login_device_code(
                CLIENT_ID, transport, sleep=lambda _s: None, now=lambda: 1.0
            )
        self.assertIn("没有 Minecraft Java 版", str(raised.exception))

    def test_refresh_account(self) -> None:
        old = MicrosoftAccount(
            name="Old",
            uuid="01234567-89ab-cdef-0123-456789abcdef",
            access_token="old-access",
            refresh_token="old-refresh",
            expires_at=1.0,
            client_id=CLIENT_ID,
        )
        responses = [(200, MS_TOKEN), (200, XBL), (200, XSTS), (200, MC), (200, PROFILE)]
        transport = FakeTransport(responses)
        account = refresh_account(old, transport, now=lambda: 2000.0)
        self.assertEqual(account.access_token, "mc-access")
        self.assertEqual(account.expires_at, 2000.0 + 86400)
        self.assertEqual(transport.calls[0][2]["grant_type"], "refresh_token")
        self.assertEqual(transport.calls[0][2]["refresh_token"], "old-refresh")

    def test_ensure_account_fresh_skips_network(self) -> None:
        account = MicrosoftAccount(
            name="Steve",
            uuid="01234567-89ab-cdef-0123-456789abcdef",
            access_token="token",
            refresh_token="refresh",
            expires_at=10_000.0,
            client_id=CLIENT_ID,
        )
        transport = FakeTransport([])
        result = ensure_account_fresh(account, transport, now=1000.0)
        self.assertIs(result, account)
        self.assertEqual(transport.calls, [])

    def test_ensure_account_fresh_refreshes_expired(self) -> None:
        account = MicrosoftAccount(
            name="Steve",
            uuid="01234567-89ab-cdef-0123-456789abcdef",
            access_token="token",
            refresh_token="refresh",
            expires_at=1000.0,
            client_id=CLIENT_ID,
        )
        transport = FakeTransport(
            [(200, MS_TOKEN), (200, XBL), (200, XSTS), (200, MC), (200, PROFILE)]
        )
        result = ensure_account_fresh(account, transport, now=2000.0)
        self.assertEqual(result.access_token, "mc-access")
        self.assertEqual(len(transport.calls), 5)

    def test_account_repr_does_not_leak_tokens(self) -> None:
        account = MicrosoftAccount(
            name="Steve",
            uuid="u",
            access_token="super-secret-access",
            refresh_token="super-secret-refresh",
            expires_at=1.0,
        )
        rendered = repr(account)
        self.assertNotIn("super-secret", rendered)

    def test_auth_hosts_are_not_download_hosts(self) -> None:
        from mc_launcher.core.net import PROVIDER_HOSTS

        self.assertFalse(AUTH_HOSTS & PROVIDER_HOSTS)
        client = HttpClient({"example.com"}, check_dns=False, allow_private=True)
        with self.assertRaises(DownloadError):
            client.post_form(
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode",
                {"client_id": CLIENT_ID},
            )


class SecureStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-secure-"))
        self._old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(self.work)

    def tearDown(self) -> None:
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata
        shutil.rmtree(self.work, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_roundtrip_and_no_plaintext(self) -> None:
        payload = {
            "name": "Steve",
            "access_token": "plain-access-token",
            "refresh_token": "plain-refresh-token",
        }
        secure_store.save_account(payload)
        raw = secure_store.account_path().read_bytes()
        self.assertNotIn(b"plain-access-token", raw)
        self.assertNotIn(b"plain-refresh-token", raw)
        self.assertEqual(secure_store.load_account(), payload)
        secure_store.delete_account()
        self.assertIsNone(secure_store.load_account())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
    def test_corrupt_file_raises(self) -> None:
        path = secure_store.account_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not dpapi data")
        with self.assertRaises(secure_store.SecureStoreError):
            secure_store.load_account()


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-settings-"))
        self._old_appdata = os.environ.get("APPDATA")
        self._old_env = os.environ.pop(settings.ENV_CLIENT_ID, None)
        os.environ["APPDATA"] = str(self.work)

    def tearDown(self) -> None:
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata
        if self._old_env is not None:
            os.environ[settings.ENV_CLIENT_ID] = self._old_env
        else:
            os.environ.pop(settings.ENV_CLIENT_ID, None)
        shutil.rmtree(self.work, ignore_errors=True)

    def test_client_id_precedence(self) -> None:
        settings.save_client_id(CLIENT_ID)
        self.assertEqual(settings.resolve_client_id(), CLIENT_ID)
        self.assertEqual(settings.resolve_client_id("explicit"), "explicit")
        os.environ[settings.ENV_CLIENT_ID] = "from-env"
        self.assertEqual(settings.resolve_client_id(), "from-env")
        self.assertEqual(settings.resolve_client_id("explicit"), "explicit")


class LaunchAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-account-launch-"))
        root = self.work / "game"
        version_dir = root / "versions" / "1.20.1"
        version_dir.mkdir(parents=True)
        (root / "libraries").mkdir()
        (root / "assets" / "indexes").mkdir(parents=True)
        (root / "assets" / "indexes" / "5.json").write_text(
            '{"objects": {}}', encoding="utf-8"
        )
        (version_dir / "1.20.1.jar").write_bytes(b"jar")
        (version_dir / "1.20.1.json").write_text(
            json.dumps(
                {
                    "id": "1.20.1",
                    "type": "release",
                    "mainClass": "net.minecraft.client.main.Main",
                    "assets": "5",
                    "assetIndex": {"id": "5"},
                    "arguments": {
                        "game": [
                            "--username",
                            "${auth_player_name}",
                            "--uuid",
                            "${auth_uuid}",
                            "--accessToken",
                            "${auth_access_token}",
                            "--userType",
                            "${user_type}",
                            "--xuid",
                            "${auth_xuid}",
                            "--clientId",
                            "${clientid}",
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        self.root = normalize_game_root(root)
        self.summary = list_versions(self.root)[0]

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def _options(self, account: MicrosoftAccount | None) -> LaunchOptions:
        return LaunchOptions(
            username="OfflineName",
            java_path=Path(sys.executable),
            isolated=True,
            natives_dir=self.work / "natives",
            check_writable=False,
            account=account,
        )

    def test_microsoft_account_is_used_in_command(self) -> None:
        account = MicrosoftAccount(
            name="Steve",
            uuid="01234567-89ab-cdef-0123-456789abcdef",
            access_token="mc-access-token",
            refresh_token="refresh",
            expires_at=time.time() + 3600,
            xuid="12345",
            client_id=CLIENT_ID,
        )
        plan = build_launch_plan(self.root, self.summary, self._options(account))
        joined = " ".join(plan.command)
        self.assertIn("--username", joined)
        self.assertIn("Steve", joined)
        self.assertIn("mc-access-token", joined)
        self.assertIn("01234567-89ab-cdef-0123-456789abcdef", joined)
        self.assertIn("--userType msa", joined)
        self.assertIn("--xuid 12345", joined)
        self.assertIn(f"--clientId {CLIENT_ID}", joined)
        self.assertNotIn("OfflineName", joined)

    def test_offline_account_still_uses_zero_token(self) -> None:
        plan = build_launch_plan(self.root, self.summary, self._options(None))
        joined = " ".join(plan.command)
        self.assertIn("OfflineName", joined)
        self.assertIn("--accessToken 0", joined)
        self.assertIn("--userType msa", joined)


class HttpClientPostTests(unittest.TestCase):
    def test_post_form_and_json(self) -> None:
        requests = []

        class Response:
            status = 200
            headers = {}

            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self, _size: int = -1) -> bytes:
                return self._body

            def geturl(self) -> str:
                return "https://login.microsoftonline.com/token"

            def getcode(self) -> int:
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

        class Opener:
            def open(self, request, timeout=None):
                requests.append(request)
                return Response(b'{"ok": true}')

        client = HttpClient(
            {"login.microsoftonline.com"},
            check_dns=False,
            allow_private=True,
            retries=0,
        )
        client._build_opener = lambda: Opener()  # type: ignore[method-assign]
        status, body = client.post_form(
            "https://login.microsoftonline.com/token", {"a": "b"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"ok": true}')
        self.assertEqual(requests[0].data, b"a=b")
        self.assertEqual(
            requests[0].get_header("Content-type"),
            "application/x-www-form-urlencoded",
        )
        status, _ = client.post_json(
            "https://login.microsoftonline.com/token", {"x": 1}
        )
        self.assertEqual(status, 200)
        self.assertEqual(requests[1].data, b'{"x":1}')

    def test_http_error_body_is_returned(self) -> None:
        class Opener:
            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(
                    request.full_url,
                    400,
                    "Bad Request",
                    {},
                    io.BytesIO(b'{"error":"authorization_pending"}'),
                )

        client = HttpClient(
            {"login.microsoftonline.com"},
            check_dns=False,
            allow_private=True,
            retries=0,
        )
        client._build_opener = lambda: Opener()  # type: ignore[method-assign]
        status, body = client.post_form(
            "https://login.microsoftonline.com/token", {"a": "b"}
        )
        self.assertEqual(status, 400)
        self.assertIn(b"authorization_pending", body)

    def test_default_transport_uses_auth_allowlist(self) -> None:
        transport = HttpAuthTransport()
        self.assertEqual(transport.client.allowed_hosts, AUTH_HOSTS)


if __name__ == "__main__":
    unittest.main()
