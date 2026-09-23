"""Проверка токена исполнителя для http и mcp (CP-ADR-0056 §5, амендмент D).

Исполнитель приходит с токеном IAM audience скилла (``implementation.auth.audience``).
Проверку делает канонический ``platform-auth-sdk`` (TAI-ADR-0030) — своя здесь не пишется.
"""

from __future__ import annotations

import os
from typing import Any

ENV_ISSUER = "SKILL_SDK_IAM_ISSUER"
ENV_AUDIENCE = "SKILL_SDK_AUDIENCE"
ENV_JWKS_URL = "SKILL_SDK_JWKS_URL"


class Unauthorized(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def verifier_from_env() -> Any:
    """``TokenVerifier`` из ``SKILL_SDK_IAM_ISSUER``, ``SKILL_SDK_AUDIENCE`` и
    ``SKILL_SDK_JWKS_URL``; ``None`` — переменные не заданы."""
    issuer, audience = os.environ.get(ENV_ISSUER), os.environ.get(ENV_AUDIENCE)
    jwks_url = os.environ.get(ENV_JWKS_URL)
    if not (issuer and audience and jwks_url):
        return None
    from platform_auth import JwksCache, TokenVerifier, VerifierConfig

    return TokenVerifier(JwksCache(jwks_url), VerifierConfig(issuer=issuer, audience=audience))


async def authenticate(verifier: Any, authorization: str | None) -> Any:
    """Проверенный контекст вызывающего; ``None`` — хостинг без проверки (только явно)."""
    if verifier is None:
        return None
    from platform_auth import EnforcementError, VerificationUnavailable, parse_bearer

    try:
        return await verifier.verify(parse_bearer(authorization))
    except VerificationUnavailable as error:
        raise Unauthorized(error.code, 503) from error
    except EnforcementError as error:
        raise Unauthorized(error.code, 401) from error


def require_verifier(verifier: Any, allow_anonymous: bool) -> None:
    if verifier is None and not allow_anonymous:
        raise RuntimeError(
            f"хостинг скиллов без проверки токена: задайте {ENV_ISSUER}, {ENV_AUDIENCE}, "
            f"{ENV_JWKS_URL} или явно allow_anonymous=True (только для локальной разработки)"
        )
