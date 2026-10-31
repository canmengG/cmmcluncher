"""Microsoft / Xbox Live device-code authentication for Minecraft Java.

The flow is deliberately implemented with the standard library:

1. request a device code from the Microsoft identity platform;
2. poll the token endpoint until the user finishes signing in;
3. exchange the Microsoft token for Xbox Live and XSTS tokens;
4. exchange the XSTS token for a Minecraft access token;
5. read the player profile (name + UUID).

Only public-client device code flow is used: there is no client secret and no
redirect URI.  Tokens are never logged and are persisted only through
:mod:`mc_launcher.core.secure_store` (Windows DPAPI).

Minecraft Services additionally requires the Azure application's client ID to
be approved/allowlisted by Mojang.  Without that approval,
``login_with_xbox`` returns HTTP 403 ``Invalid app registration`` even when the
Microsoft account owns Minecraft: Java Edition.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from .errors import LauncherError
from .models import MicrosoftAccount
from .net import HttpClient
from . import secure_store

SCOPE = "XboxLive.signin offline_access"
AUTH_HOSTS = frozenset(
    {
        "login.microsoftonline.com",
        "user.auth.xboxlive.com",
        "xsts.auth.xboxlive.com",
        "api.minecraftservices.com",
    }
)
DEVICE_CODE_URL = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
)
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

CLIENT_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_TENANT_RE = re.compile(r"^[A-Za-z0-9.-]{1,128}$")


class AuthError(LauncherError):
    """A user-facing Microsoft/Xbox authentication failure."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class AuthCancelled(AuthError):
    """The user cancelled the device-code login."""


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    message: str = ""


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str = ""


@dataclass(frozen=True)
class XboxToken:
    token: str
    user_hash: str
    xuid: str = ""


@dataclass(frozen=True)
class MinecraftToken:
    access_token: str
    expires_in: int


class AuthTransport(Protocol):
    def post_form(self, url: str, fields: dict[str, str]) -> tuple[int, dict]: ...

    def post_json(self, url: str, payload: dict) -> tuple[int, dict]: ...

    def get_json(self, url: str, *, bearer: str) -> tuple[int, dict]: ...


def _json_object(body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class HttpAuthTransport:
    """Thin standard-library transport used by the launcher."""

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.client = HttpClient(
            AUTH_HOSTS,
            strict_redirects=True,
            timeout=timeout,
            retries=0,
        )

    def post_form(self, url: str, fields: dict[str, str]) -> tuple[int, dict]:
        status, body = self.client.post_form(url, fields)
        return status, _json_object(body)

    def post_json(self, url: str, payload: dict) -> tuple[int, dict]:
        status, body = self.client.post_json(url, payload)
        return status, _json_object(body)

    def get_json(self, url: str, *, bearer: str) -> tuple[int, dict]:
        status, body = self.client.get_json(url, bearer=bearer)
        return status, _json_object(body)


def validate_client_id(client_id: str) -> str:
    value = (client_id or "").strip()
    if not CLIENT_ID_RE.fullmatch(value):
        raise AuthError(
            "Azure client ID 格式无效。请填写 Entra ID 应用注册里的 "
            "“Application (client) ID”（形如 00000000-0000-0000-0000-000000000000）。"
        )
    return value


def _tenant(tenant: str) -> str:
    value = (tenant or "consumers").strip()
    if not _TENANT_RE.fullmatch(value):
        raise AuthError("Microsoft 登录租户参数无效。")
    return value


def _string(data: dict, key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _integer(data: dict, key: str, default: int = 0) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _http_error(prefix: str, status: int, data: dict) -> AuthError:
    description = _string(data, "error_description") or _string(data, "error")
    description = description.replace("\r", " ").replace("\n", " ").strip()
    if len(description) > 320:
        description = description[:320] + "…"
    message = f"{prefix}（HTTP {status}）"
    if description:
        message += f"：{description}"
    if "AADSTS700016" in description:
        message += (
            "\n请确认 client ID 属于当前登录的租户，并且应用支持个人 Microsoft 账户。"
        )
    elif "AADSTS65001" in description or "AADSTS50020" in description:
        message += "\n请在浏览器中同意该应用的登录请求，或改用正确的 Microsoft 账户。"
    elif "unauthorized_client" in description or "invalid_client" in description:
        message += "\n请确认已在 Entra ID 应用注册中开启“允许公共客户端流”。"
    return AuthError(message, code=_string(data, "error"))


def _device_url(tenant: str) -> str:
    return DEVICE_CODE_URL.format(tenant=urllib.parse.quote(_tenant(tenant)))


def _token_url(tenant: str) -> str:
    return TOKEN_URL.format(tenant=urllib.parse.quote(_tenant(tenant)))


def request_device_code(
    client_id: str,
    transport: AuthTransport,
    *,
    tenant: str = "consumers",
) -> DeviceCode:
    client_id = validate_client_id(client_id)
    status, data = transport.post_form(
        _device_url(tenant),
        {"client_id": client_id, "scope": SCOPE},
    )
    if status != 200:
        raise _http_error("获取微软设备代码失败", status, data)
    device_code = _string(data, "device_code")
    user_code = _string(data, "user_code")
    verification_uri = _string(data, "verification_uri") or _string(
        data, "verification_url"
    )
    if not device_code or not user_code or not verification_uri:
        raise AuthError("微软登录服务返回的设备代码不完整。")
    return DeviceCode(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        expires_in=max(1, _integer(data, "expires_in", 900)),
        interval=max(1, _integer(data, "interval", 5)),
        message=_string(data, "message"),
    )


def poll_device_code(
    device: DeviceCode,
    client_id: str,
    transport: AuthTransport,
    *,
    tenant: str = "consumers",
    cancel=None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> TokenResponse:
    client_id = validate_client_id(client_id)
    deadline = clock() + max(1, device.expires_in)
    interval = max(1, device.interval)
    while clock() < deadline:
        if cancel is not None and cancel.is_set():
            raise AuthCancelled("已取消微软登录。")
        status, data = transport.post_form(
            _token_url(tenant),
            {
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device.device_code,
            },
        )
        if status == 200:
            return _token_response(data, previous_refresh="")
        error = _string(data, "error")
        if error == "authorization_pending":
            sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            sleep(interval)
            continue
        if error in ("expired_token", "code_expired"):
            raise AuthError("微软设备代码已过期，请重新发起登录。", code=error)
        if error == "authorization_declined":
            raise AuthError("你已拒绝微软登录请求。", code=error)
        if status in (429, 500, 502, 503, 504):
            sleep(interval)
            continue
        raise _http_error("微软登录失败", status, data)
    raise AuthError("微软登录超时，请重新发起登录。", code="expired_token")


def _token_response(data: dict, *, previous_refresh: str) -> TokenResponse:
    access = _string(data, "access_token")
    refresh = _string(data, "refresh_token") or previous_refresh
    if not access:
        raise AuthError("微软登录没有返回访问令牌。")
    if not refresh:
        raise AuthError(
            "微软登录没有返回刷新令牌；请确认应用 scope 包含 offline_access。"
        )
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=max(1, _integer(data, "expires_in", 3600)),
        scope=_string(data, "scope"),
    )


def authenticate_xbox(ms_access_token: str, transport: AuthTransport) -> XboxToken:
    status, data = transport.post_json(
        XBL_URL,
        {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={ms_access_token}",
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        },
    )
    if status != 200:
        raise _http_error("Xbox Live 登录失败", status, data)
    token = _string(data, "Token")
    claims = data.get("DisplayClaims")
    xui = claims.get("xui") if isinstance(claims, dict) else None
    first = xui[0] if isinstance(xui, list) and xui and isinstance(xui[0], dict) else {}
    uhs = _string(first, "uhs")
    if not token or not uhs:
        raise AuthError("Xbox Live 没有返回有效的用户令牌。")
    return XboxToken(token=token, user_hash=uhs, xuid=_string(first, "xid"))


_XBOX_ERRORS = {
    "2148916233": "该微软账号没有 Xbox 账户。请先在 xbox.com 创建档案后重试。",
    "2148916235": "该账号所在地区不支持 Xbox Live。",
    "2148916238": "该账号是儿童账户，需要家长在 Microsoft 家庭中同意。",
}


def authorize_xsts(xbl: XboxToken, transport: AuthTransport) -> XboxToken:
    status, data = transport.post_json(
        XSTS_URL,
        {
            "Properties": {
                "SandboxId": "RETAIL",
                "UserTokens": [xbl.token],
            },
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        },
    )
    if status != 200:
        xerr = str(data.get("XErr", "") or "")
        if xerr in _XBOX_ERRORS:
            raise AuthError(_XBOX_ERRORS[xerr], code=xerr)
        raise _http_error("Xbox 安全令牌交换失败", status, data)
    token = _string(data, "Token")
    claims = data.get("DisplayClaims")
    xui = claims.get("xui") if isinstance(claims, dict) else None
    first = xui[0] if isinstance(xui, list) and xui and isinstance(xui[0], dict) else {}
    uhs = _string(first, "uhs")
    if not token or not uhs:
        raise AuthError("Xbox 安全令牌响应不完整。")
    return XboxToken(
        token=token,
        user_hash=uhs,
        xuid=_string(first, "xid") or xbl.xuid,
    )


def login_with_xbox(xsts: XboxToken, transport: AuthTransport) -> MinecraftToken:
    status, data = transport.post_json(
        MC_LOGIN_URL,
        {"identityToken": f"XBL3.0 x={xsts.user_hash};{xsts.token}"},
    )
    if status != 200:
        if status in (401, 403):
            detail = (
                _string(data, "errorMessage")
                or _string(data, "error")
                or _string(data, "message")
            )
            if "invalid app registration" in detail.lower():
                raise AuthError(
                    "Minecraft 服务拒绝了该 Azure 应用（Invalid app registration）。\n"
                    "这表示 client ID 还没有获得 Minecraft Services 的 allowlist 批准，"
                    "不是账号没有 Java 版。\n"
                    "请先在 Minecraft Java Edition Game Service API 审核中提交 client ID；"
                    "批准后同一个 client ID 即可登录。\n"
                    "https://help.minecraft.net/hc/en-us/articles/"
                    "16254801392141-Java-Edition-Game-Service-API-Review-or-Application-Process",
                    code="invalid_app_registration",
                )
            message = (
                "该微软账号没有 Minecraft Java 版，或登录已被拒绝"
                f"（HTTP {status}）。"
            )
            if detail:
                message += f"\n微软返回：{detail[:300]}"
            message += (
                "\n请确认使用的是已购买 Minecraft Java 版并完成迁移的微软账号；"
                "Xbox Game Pass 的 Bedrock 版不等于 Java 版。"
            )
            raise AuthError(message, code=str(status))
        raise _http_error("Minecraft 登录失败", status, data)
    access = _string(data, "access_token")
    if not access:
        raise AuthError("Minecraft 登录没有返回访问令牌。")
    return MinecraftToken(
        access_token=access,
        expires_in=max(1, _integer(data, "expires_in", 86400)),
    )


def fetch_profile(
    access_token: str, transport: AuthTransport
) -> tuple[str, str]:
    status, data = transport.get_json(PROFILE_URL, bearer=access_token)
    if status == 404:
        raise AuthError(
            "该微软账号没有 Minecraft Java 版档案。请确认已购买 Java 版，"
            "并已在官方启动器中完成迁移。",
            code="no_profile",
        )
    if status != 200:
        if status in (401, 403):
            raise AuthError("Minecraft 登录已过期或无效，请重新登录。", code=str(status))
        raise _http_error("读取 Minecraft 档案失败", status, data)
    profile_id = _string(data, "id")
    name = _string(data, "name")
    if not profile_id or not name:
        raise AuthError("Minecraft 档案响应不完整。")
    try:
        account_uuid = str(
            uuid.UUID(profile_id)
            if "-" in profile_id
            else uuid.UUID(hex=profile_id)
        )
    except ValueError as exc:
        raise AuthError("Minecraft 档案返回了无效的 UUID。") from exc
    return account_uuid, name


def _complete_login(
    token: TokenResponse,
    client_id: str,
    transport: AuthTransport,
    *,
    now: Callable[[], float] = time.time,
) -> MicrosoftAccount:
    xbl = authenticate_xbox(token.access_token, transport)
    xsts = authorize_xsts(xbl, transport)
    minecraft = login_with_xbox(xsts, transport)
    account_uuid, name = fetch_profile(minecraft.access_token, transport)
    return MicrosoftAccount(
        name=name,
        uuid=account_uuid,
        access_token=minecraft.access_token,
        refresh_token=token.refresh_token,
        expires_at=now() + minecraft.expires_in,
        xuid=xsts.xuid,
        client_id=client_id,
    )


def login_device_code(
    client_id: str,
    transport: AuthTransport,
    *,
    tenant: str = "consumers",
    on_code: Callable[[DeviceCode], None] | None = None,
    cancel=None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> MicrosoftAccount:
    """Run the full device-code login and return the Minecraft account."""
    client_id = validate_client_id(client_id)
    device = request_device_code(client_id, transport, tenant=tenant)
    if on_code is not None:
        on_code(device)
    token = poll_device_code(
        device,
        client_id,
        transport,
        tenant=tenant,
        cancel=cancel,
        sleep=sleep,
    )
    return _complete_login(token, client_id, transport, now=now)


def refresh_account(
    account: MicrosoftAccount,
    transport: AuthTransport,
    *,
    tenant: str = "consumers",
    now: Callable[[], float] = time.time,
) -> MicrosoftAccount:
    if not account.client_id:
        raise AuthError("保存的微软登录缺少 Azure client ID，请重新登录。")
    client_id = validate_client_id(account.client_id)
    status, data = transport.post_form(
        _token_url(tenant),
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": account.refresh_token,
            "scope": SCOPE,
        },
    )
    if status != 200:
        error = _string(data, "error")
        if error in ("invalid_grant", "interaction_required"):
            raise AuthError("微软登录已过期，请重新登录。", code=error)
        raise _http_error("刷新微软登录失败", status, data)
    token = _token_response(data, previous_refresh=account.refresh_token)
    return _complete_login(token, client_id, transport, now=now)


def ensure_account_fresh(
    account: MicrosoftAccount,
    transport: AuthTransport,
    *,
    skew: float = 300.0,
    now: float | None = None,
) -> MicrosoftAccount:
    current = time.time() if now is None else now
    if account.expires_at > current + skew:
        return account
    return refresh_account(account, transport)


def save_account(account: MicrosoftAccount) -> None:
    secure_store.save_account(account.to_dict())


def load_saved_account() -> MicrosoftAccount | None:
    data = secure_store.load_account()
    if not data:
        return None
    return MicrosoftAccount.from_dict(data)


def clear_saved_account() -> None:
    secure_store.delete_account()
