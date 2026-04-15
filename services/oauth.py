"""OAuth provider registry and helpers."""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

import jwt
import requests
from jwt import PyJWKClient
from jwt.exceptions import ImmatureSignatureError

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None

if load_dotenv:
    load_dotenv()

oauth_logger = logging.getLogger("oauth-app")

APPLE_AUTH_URL = "https://appleid.apple.com/auth/authorize"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
APPLE_IDENTITY_ISSUER = "https://appleid.apple.com"
DEFAULT_ID_TOKEN_LEEWAY_SECONDS = 300  # Apple 時鐘常有秒級誤差

_jwk_client: PyJWKClient = PyJWKClient(APPLE_KEYS_URL)


def _load_apple_private_key() -> str:
    pk = os.environ.get("APPLE_PRIVATE_KEY")
    if pk:
        return pk.replace("\\n", "\n")
    path = os.environ.get("APPLE_PRIVATE_KEY_PATH")
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as file:
            return file.read()
    raise RuntimeError("APPLE_PRIVATE_KEY or APPLE_PRIVATE_KEY_PATH must be set")


def build_apple_client_secret() -> str:
    team_id = os.environ.get("APPLE_TEAM_ID", "")
    key_id = os.environ.get("APPLE_KEY_ID", "")
    client_id = os.environ.get("APPLE_CLIENT_ID", "")
    if not all([team_id, key_id, client_id]):
        raise RuntimeError("Missing APPLE_TEAM_ID / APPLE_KEY_ID / APPLE_CLIENT_ID")

    private_key = _load_apple_private_key()
    now = int(time.time())
    payload = {
        "iss": team_id,
        "iat": now,
        "exp": now + 60 * 60 * 24 * 180,
        "aud": "https://appleid.apple.com",
        "sub": client_id,
    }
    headers = {"kid": key_id, "alg": "ES256"}
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


def _resolve_leeway() -> int:
    leeway_raw = os.environ.get("APPLE_ID_TOKEN_LEEWAY", "")
    if not leeway_raw:
        return DEFAULT_ID_TOKEN_LEEWAY_SECONDS
    try:
        return max(0, int(leeway_raw))
    except ValueError:
        return DEFAULT_ID_TOKEN_LEEWAY_SECONDS


def _decode_id_token(id_token: str, audience: str) -> Dict[str, object]:
    if not id_token:
        raise RuntimeError("Apple token response missing id_token")
    if not audience:
        raise RuntimeError("APPLE_CLIENT_ID not configured")
    signing_key = _jwk_client.get_signing_key_from_jwt(id_token)
    try:
        return jwt.decode(
            id_token,
            signing_key.key,
            audience=audience,
            algorithms=["RS256"],
            issuer=APPLE_IDENTITY_ISSUER,
            leeway=_resolve_leeway(),
        )
    except ImmatureSignatureError:
        payload_preview = jwt.decode(id_token, options={"verify_signature": False})
        iat = payload_preview.get("iat")
        skew = None
        if isinstance(iat, (int, float)):
            skew = int(iat) - int(time.time())
        oauth_logger.warning(
            "Apple id_token iat ahead of local clock, skipping iat validation",
            extra={"skew_seconds": skew},
        )
        return jwt.decode(
            id_token,
            signing_key.key,
            audience=audience,
            algorithms=["RS256"],
            issuer=APPLE_IDENTITY_ISSUER,
            options={"verify_iat": False},
        )


def apple_userinfo_from_tokens(tokens: Dict[str, str]) -> Dict[str, object]:
    client_id = os.environ.get("APPLE_CLIENT_ID", "")
    userinfo = _decode_id_token(tokens.get("id_token", ""), client_id)

    raw_user = tokens.get("_apple_raw_user")
    if raw_user:
        try:
            parsed_user = json.loads(raw_user)
        except json.JSONDecodeError:
            parsed_user = {"raw_user": raw_user}
        if isinstance(parsed_user, dict):
            userinfo = {**parsed_user, "id_token_payload": userinfo}
        else:
            userinfo = {"user": parsed_user, "id_token_payload": userinfo}
    return userinfo


@dataclass
class AppleProviderConfig:
    name: str = "apple"
    client_id: Optional[str] = os.environ.get("APPLE_CLIENT_ID")
    scope: str = "name email"
    authorize_url: str = APPLE_AUTH_URL
    token_url: str = APPLE_TOKEN_URL
    required_env: Tuple[str, ...] = ("APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_CLIENT_ID")
    client_secret_factory: Callable[[], str] = staticmethod(build_apple_client_secret)
    userinfo_fetcher: Callable[[Dict[str, str]], Dict[str, object]] = staticmethod(
        apple_userinfo_from_tokens
    )


@dataclass
class OAuthProvider:
    name: str
    client_id: Optional[str]
    client_secret: Optional[str]
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str
    extra_authorize_params: Dict[str, str] = field(default_factory=dict)
    extra_token_params: Dict[str, str] = field(default_factory=dict)
    required_env: Tuple[str, ...] = ()
    client_secret_factory: Optional[Callable[[], str]] = None
    userinfo_fetcher: Optional[Callable[[Dict[str, str]], Dict[str, object]]] = None

    def is_configured(self) -> bool:
        return all(os.environ.get(name) for name in self.required_env)

    def missing_env(self) -> list[str]:
        return [name for name in self.required_env if not os.environ.get(name)]

    def build_authorize_url(self, state: str, redirect_uri: str) -> str:
        query = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "state": state,
        }
        query.update(self.extra_authorize_params)
        return f"{self.authorize_url}?{urlencode(query)}"

    def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> Dict[str, str]:
        client_secret = self.client_secret
        if not client_secret and self.client_secret_factory:
            client_secret = self.client_secret_factory()

        data: Dict[str, str] = {
            "code": code,
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if client_secret:
            data["client_secret"] = client_secret
        data.update(self.extra_token_params)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        response = requests.post(self.token_url, data=data, headers=headers, timeout=10)
        response.raise_for_status()
        tokens = response.json()
        if "access_token" not in tokens and not self.userinfo_fetcher:
            raise RuntimeError("Provider response missing access_token")
        return tokens

    def fetch_userinfo(self, tokens: Dict[str, str]) -> Dict[str, object]:
        if self.userinfo_fetcher:
            return self.userinfo_fetcher(tokens)

        access_token = tokens.get("access_token")
        if not access_token:
            raise RuntimeError("Provider response missing access_token")
        response = requests.get(
            self.userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()


class OAuthRegistry:
    def __init__(self) -> None:
        self._providers: Dict[str, OAuthProvider] = {}

    def register(self, provider: OAuthProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> Optional[OAuthProvider]:
        return self._providers.get(name)

    def configured_providers(self) -> Dict[str, bool]:
        return {
            name: provider.is_configured() for name, provider in self._providers.items()
        }


registry = OAuthRegistry()

facebook_id_env = (
    "FACEBOOK_CLIENT_ID" if os.environ.get("FACEBOOK_CLIENT_ID") else "FACEBOOK_APP_ID"
)
facebook_secret_env = (
    "FACEBOOK_CLIENT_SECRET"
    if os.environ.get("FACEBOOK_CLIENT_SECRET")
    else "FACEBOOK_APP_SECRET"
)
facebook_client_id = os.environ.get(facebook_id_env)
facebook_client_secret = os.environ.get(facebook_secret_env)

registry.register(
    OAuthProvider(
        name="google",
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
        scope="openid email profile",
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        required_env=("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
    ),
)
registry.register(
    OAuthProvider(
        name="apple",
        client_id=os.environ.get("APPLE_CLIENT_ID"),
        client_secret=None,  # 不用固定密鑰，token 交換時動態產生 JWT
        authorize_url=AppleProviderConfig.authorize_url,
        token_url=AppleProviderConfig.token_url,
        userinfo_url="",  # Apple 無 userinfo endpoint
        scope=AppleProviderConfig.scope,
        # Apple 要求 name/email scope 時使用 response_mode=form_post
        extra_authorize_params={"response_mode": "form_post"},
        required_env=AppleProviderConfig.required_env,
        client_secret_factory=AppleProviderConfig.client_secret_factory,
        userinfo_fetcher=AppleProviderConfig.userinfo_fetcher,
    )
)
registry.register(
    OAuthProvider(
        name="facebook",
        client_id=facebook_client_id,
        client_secret=facebook_client_secret,
        authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
        token_url="https://graph.facebook.com/v19.0/oauth/access_token",
        userinfo_url="https://graph.facebook.com/v19.0/me?fields=id,name,email,picture",
        scope="public_profile,email",
        extra_authorize_params={"response_type": "code"},
        required_env=(facebook_id_env, facebook_secret_env),
    )
)


__all__ = [
    "AppleProviderConfig",
    "OAuthProvider",
    "OAuthRegistry",
    "registry",
    "oauth_logger",
    "build_apple_client_secret",
    "apple_userinfo_from_tokens",
]
