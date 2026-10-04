"""Интеграционный тест клиента подключений против настоящего OpenBao (I016, SC-005).

Контейнер тест не поднимает и без переменной окружения пропускается. Запуск::

    docker run --rm -p 8200:8200 -e BAO_DEV_ROOT_TOKEN_ID=root openbao/openbao server -dev
    SKILL_SDK_TEST_OPENBAO_URL=http://127.0.0.1:8200 SKILL_SDK_TEST_OPENBAO_TOKEN=root \\
        uv run --with 'pyjwt[crypto]' pytest tests/test_connections_openbao.py

Корневой токен нужен только подготовке: движок ``kv`` (kv-v2, ``max_versions=1``),
метод ``jwt`` с ключом проверки теста, политика и роль агента в той форме, в какой их
пишет воркер ядра ``connections-policy-sync`` (CP-ADR-0079 п.9). Дальше клиент SDK
ходит так же, как на стенде: вход ``auth/jwt/login`` токеном audience ``openbao`` и
чтение ``kv/data/tenants/<t>/connections/<key>``. Сведения ядра подменены подделкой:
ядро в этом тесте не участвует. Путь ``oauth2/creds`` требует плагина и здесь не
проверяется.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import pytest

from skill_sdk.connections import ConnectionClient, ConnectionInfo, OpenBaoStore
from skill_sdk.errors import SkillError
from skill_sdk.testing import FakeConnectionDirectory

URL = os.environ.get("SKILL_SDK_TEST_OPENBAO_URL", "")
ROOT = os.environ.get("SKILL_SDK_TEST_OPENBAO_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not (URL and ROOT), reason="SKILL_SDK_TEST_OPENBAO_URL/_TOKEN не заданы: нужен OpenBao"
)

ISSUER = "https://iam.test"
TENANT = "t-" + uuid.uuid4().hex[:8]
KEY = "crm"
PRINCIPAL = str(uuid.uuid4())  # principal агента в ядре — имя роли
IAM_SUBJECT = str(uuid.uuid4())  # principal агента в IAM — субъект токена
IAM_TENANT = str(uuid.uuid4())
REF = f"kv/data/tenants/{TENANT}/connections/{KEY}"
POLICY = f"cp-agent-{PRINCIPAL}"
ROLE = f"agent-{PRINCIPAL}"


def _admin(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    response = httpx.request(
        method, f"{URL}/v1/{path}", headers={"X-Vault-Token": ROOT}, json=body, timeout=10
    )
    assert response.status_code < 300 or response.status_code == 400, response.text
    return response


@pytest.fixture(scope="module")
def signing_key() -> Any:
    pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    _admin("POST", "sys/mounts/kv", {"type": "kv", "options": {"version": "2"}})
    _admin("POST", "kv/config", {"max_versions": 1})
    _admin("POST", "sys/auth/jwt", {"type": "jwt"})
    _admin(
        "POST",
        "auth/jwt/config",
        {"jwt_validation_pubkeys": [public.decode()], "bound_issuer": ISSUER},
    )
    return key


def _grant() -> None:
    _admin(
        "PUT",
        f"sys/policies/acl/{POLICY}",
        {"policy": f'path "{REF}" {{ capabilities = ["read"] }}'},
    )
    _admin(
        "POST",
        f"auth/jwt/role/{ROLE}",
        {
            "role_type": "jwt",
            "user_claim": "sub",
            "bound_subject": IAM_SUBJECT,
            "bound_claims_type": "string",
            "bound_claims": {"tenant_id": IAM_TENANT, "principal_type": "agent"},
            "bound_audiences": ["openbao"],
            "token_policies": [POLICY],
            "token_ttl": 300,
            "token_max_ttl": 300,
        },
    )


def _client(signing_key: Any, directory: FakeConnectionDirectory) -> ConnectionClient:
    import jwt

    async def iam_token() -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": IAM_SUBJECT,
            "aud": "openbao",
            "tenant_id": IAM_TENANT,
            "principal_type": "agent",
            "iat": now,
            "exp": now + 300,
        }
        return str(jwt.encode(claims, signing_key, algorithm="RS256"))

    store = OpenBaoStore(URL, jwt=iam_token, role=ROLE)
    return ConnectionClient(directory, store, cache_seconds=0)


def _info(status: str, secret_ref: str | None) -> ConnectionInfo:
    return ConnectionInfo(key=KEY, type="crm-x", status=status, auth="token", secret_ref=secret_ref)


@pytest.mark.asyncio
async def test_read_then_revoke_refuses_the_next_request(signing_key: Any) -> None:
    _grant()
    _admin("POST", REF, {"data": {"access_token": "at-live", "expires_at": None}})
    directory = FakeConnectionDirectory()
    directory.infos[KEY] = _info("active", REF)
    client = _client(signing_key, directory)
    try:
        assert await client.access_token(KEY) == "at-live"

        # отзыв ядром (CP-ADR-0079 п.10): материал со всеми версиями, затем политика
        _admin("DELETE", f"kv/metadata/tenants/{TENANT}/connections/{KEY}")
        _admin("DELETE", f"sys/policies/acl/{POLICY}")
        # учёт ядра ещё active — хранилище уже отказывает, SDK сверяется с ядром
        with pytest.raises(SkillError) as lag:
            await client.access_token(KEY)
        assert lag.value.code == "secret_store_unavailable"

        directory.infos[KEY] = _info("revoked", None)
        with pytest.raises(SkillError) as revoked:
            await client.access_token(KEY)
        assert revoked.value.code == "connection_revoked"
    finally:
        await client.aclose()
        _admin("DELETE", f"auth/jwt/role/{ROLE}")
        _admin("DELETE", f"sys/policies/acl/{POLICY}")


@pytest.mark.asyncio
async def test_foreign_path_is_refused_by_policy(signing_key: Any) -> None:
    _grant()
    other = f"kv/data/tenants/{TENANT}/connections/other"
    _admin("POST", other, {"data": {"access_token": "at-other"}})
    directory = FakeConnectionDirectory()
    directory.infos["other"] = ConnectionInfo(
        key="other", type="crm-x", status="active", auth="token", secret_ref=other
    )
    client = _client(signing_key, directory)
    try:
        with pytest.raises(SkillError) as caught:
            await client.access_token("other")
        assert caught.value.code == "secret_store_unavailable"
        assert caught.value.details == {"connection": "other", "reason": "access_not_granted"}
    finally:
        await client.aclose()
        _admin("DELETE", f"kv/metadata/tenants/{TENANT}/connections/other")
        _admin("DELETE", f"auth/jwt/role/{ROLE}")
        _admin("DELETE", f"sys/policies/acl/{POLICY}")
