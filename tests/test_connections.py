"""ctx.connection(key) и ConnectionClient (TAI-ADR-0061, CP-ADR-0079 п.8): кэш не дольше
60 с, ошибки connection_revoked / connection_expired / secret_store_unavailable,
запечатанное хранилище, адаптеры к ядру и хранилищу, значение токена вне журналов."""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from skill_sdk import SkillContext, SkillError, configure_connections, skill
from skill_sdk.connections import (
    ConnectionClient,
    ConnectionInfo,
    ControlPlaneDirectory,
    MaterialRefused,
    OpenBaoStore,
    _iam_token_source,
)
from skill_sdk.context import Invocation
from skill_sdk.testing import FakeConnectionDirectory, FakeConnections, invoke

TOKEN = "access-value-marker"
STORE_TOKEN = "s.store-token-SECRET"
IAM_JWT = "eyJ.iam-jwt.SECRET"


class DealIn(BaseModel):
    connection: str


class DealOut(BaseModel):
    type: str
    account: str | None
    pipeline: str | None
    authorized: bool


@skill("test.crm_probe", version="1", side_effects="none", risk="low")
async def crm_probe(inputs: DealIn, ctx: SkillContext) -> DealOut:
    """Прочитать сведения подключения и взять токен."""
    connection = await ctx.connection(inputs.connection)
    token = await connection.access_token()
    return DealOut(
        type=connection.type,
        account=connection.account,
        pipeline=connection.settings.get("pipeline"),
        authorized=token == TOKEN,
    )


@pytest.fixture
def fake():
    connections = FakeConnections()
    connections.add(
        "crm",
        type="crm-x",
        account="example.test",
        settings={"pipeline": "sales"},
        token=TOKEN,
    )
    configure_connections(lambda ctx: connections.client())
    yield connections
    configure_connections(None)


# --- ctx.connection ---


def test_skill_sees_type_account_settings_and_token(fake):
    out = invoke(crm_probe, {"connection": "crm"}).outputs
    assert out == {
        "type": "crm-x",
        "account": "example.test",
        "pipeline": "sales",
        "authorized": True,
    }
    assert fake.directory.lookups == ["crm"]
    assert len(fake.store.reads) == 1


@pytest.mark.asyncio
async def test_settings_are_read_only_and_repr_has_no_token(fake):
    client = fake.client()
    connection = await client.connection("crm")
    with pytest.raises(TypeError):
        connection.settings["pipeline"] = "other"  # type: ignore[index]
    await connection.access_token()
    assert TOKEN not in repr(connection) and TOKEN not in repr(client._tokens)


@pytest.mark.asyncio
async def test_context_closes_its_client(fake):
    ctx = SkillContext(Invocation(skill="s@1", protocol="test"))
    await ctx.connection("crm")
    await ctx.aclose()
    assert fake.store.closed == 1 and fake.directory.closed == 1


# --- кэш ---


@pytest.mark.asyncio
async def test_token_is_cached_no_longer_than_60_seconds(fake):
    client = fake.client()
    assert await client.access_token("crm") == TOKEN
    fake.advance(59)
    assert await client.access_token("crm") == TOKEN
    assert len(fake.store.reads) == 1  # из кэша

    fake.rotate("crm", "at-rotated")
    fake.advance(2)  # 61 с с чтения
    assert await client.access_token("crm") == "at-rotated"
    assert len(fake.store.reads) == 2


@pytest.mark.asyncio
async def test_cache_never_outlives_the_token(fake):
    fake.rotate("crm", TOKEN, expires_at=fake.now + timedelta(seconds=20))
    client = fake.client()
    await client.access_token("crm")
    fake.advance(14)  # 20 - 5 с запаса = 15
    await client.access_token("crm")
    assert len(fake.store.reads) == 1
    fake.rotate("crm", "at-next", expires_at=fake.now + timedelta(minutes=10))
    fake.advance(2)  # у прежнего токена меньше 5 с — кэш его не держит
    assert await client.access_token("crm") == "at-next"
    assert len(fake.store.reads) == 2


@pytest.mark.asyncio
async def test_fresh_and_zero_cache_read_the_store_each_time(fake):
    client = fake.client()
    await client.access_token("crm")
    await client.access_token("crm", fresh=True)
    assert len(fake.store.reads) == 2

    uncached = fake.client(cache_seconds=0)
    await uncached.access_token("crm")
    await uncached.access_token("crm")
    assert len(fake.store.reads) == 4


def test_cache_longer_than_60_seconds_is_refused():
    with pytest.raises(ValueError):
        FakeConnections().client(cache_seconds=61)


# --- ошибки ---


@pytest.mark.asyncio
async def test_revoked_is_reported_from_the_core_without_a_store_read(fake):
    client = fake.client()
    fake.revoke("crm")
    with pytest.raises(SkillError) as caught:
        await client.access_token("crm")
    assert caught.value.code == "connection_revoked" and caught.value.retryable is False
    assert caught.value.details == {"connection": "crm"}
    assert fake.store.reads == []  # статус из ядра, в хранилище не ходили
    # сведения отозванного подключения доступны: статус виден скиллу
    assert (await client.connection("crm")).status == "revoked"


@pytest.mark.asyncio
async def test_revoke_after_cache_expiry_refuses_the_next_request(fake):
    """SC-005 на уровне SDK: сведения ещё в кэше как active, а хранилище уже отказывает
    — клиент сверяется с ядром и отвечает connection_revoked."""
    client = fake.client()
    await client.access_token("crm")
    fake.advance(30)
    await client.connection("crm")  # сведения active обновлены в кэше
    fake.revoke("crm")
    fake.advance(31)  # токен вышел из кэша, сведения — ещё нет
    with pytest.raises(SkillError) as caught:
        await client.access_token("crm")
    assert caught.value.code == "connection_revoked"


@pytest.mark.asyncio
async def test_fresh_sees_revocation_at_once(fake):
    client = fake.client()
    await client.access_token("crm")
    fake.revoke("crm")
    assert await client.access_token("crm") == TOKEN  # кэш ≤ 60 с
    with pytest.raises(SkillError) as caught:
        await client.access_token("crm", fresh=True)
    assert caught.value.code == "connection_revoked"


@pytest.mark.asyncio
async def test_expired_status_expired_token_and_store_refusal(fake):
    client = fake.client()
    fake.expire("crm")
    with pytest.raises(SkillError) as by_status:
        await client.access_token("crm")
    assert by_status.value.code == "connection_expired" and by_status.value.retryable is False

    fake.add("key", type="crm-x", token=TOKEN, expires_at=fake.now - timedelta(seconds=1))
    with pytest.raises(SkillError) as by_token:
        await client.access_token("key")
    assert by_token.value.code == "connection_expired"
    assert by_token.value.details == {"connection": "key", "reason": "token_expired"}

    info = fake.add("oauth", type="crm-x", auth="oauth2", token=TOKEN)
    assert info.secret_ref is not None
    fake.store.refusals[info.secret_ref] = 400  # хранилище не смогло обновить токен
    with pytest.raises(SkillError) as by_store:
        await client.access_token("oauth")
    assert by_store.value.code == "connection_expired"
    assert by_store.value.details == {"connection": "oauth", "reason": "token_unavailable"}


@pytest.mark.asyncio
async def test_sealed_store_is_retryable_and_does_not_break_the_client(fake):
    client = fake.client()
    fake.seal()
    for _ in range(3):
        with pytest.raises(SkillError) as caught:
            await client.access_token("crm")
        assert caught.value.code == "secret_store_unavailable"
        assert caught.value.retryable is True
    fake.unseal()
    assert await client.access_token("crm") == TOKEN


def test_sealed_store_is_a_retryable_skill_error(fake):
    fake.seal()
    with pytest.raises(SkillError) as caught:
        invoke(crm_probe, {"connection": "crm"})
    assert caught.value.code == "secret_store_unavailable" and caught.value.retryable is True


@pytest.mark.asyncio
async def test_policy_not_synced_yet_is_retryable(fake):
    ref = fake.directory.infos["crm"].secret_ref
    fake.store.granted.discard(ref)  # учёт active, воркер ещё не свёл политику
    with pytest.raises(SkillError) as caught:
        await fake.client().access_token("crm")
    assert caught.value.code == "secret_store_unavailable" and caught.value.retryable is True
    assert caught.value.details == {"connection": "crm", "reason": "access_not_granted"}
    assert fake.directory.lookups == ["crm", "crm"]  # сверка с ядром после отказа


@pytest.mark.asyncio
async def test_unknown_and_pending_connections(fake):
    client = fake.client()
    with pytest.raises(SkillError) as unknown:
        await client.connection("other")
    assert unknown.value.code == "connection_not_found" and unknown.value.retryable is False

    fake.pending("fresh", type="crm-x")
    with pytest.raises(SkillError) as pending:
        await client.access_token("fresh")
    assert pending.value.code == "connection_pending"

    with pytest.raises(ValueError):
        await client.connection("Not A Key")


@pytest.mark.asyncio
async def test_unconfigured_store_still_gives_connection_info(monkeypatch, client_module):
    configure_connections(None)
    ctx = SkillContext(
        Invocation(skill="s@1", protocol="test"), env={"CONTROL_PLANE_URL": "https://cp.example"}
    )
    client_module.responses["/agents/me/connections/crm"] = _agent_connection()
    connection = await ctx.connection("crm")
    assert connection.type == "crm-x"
    with pytest.raises(SkillError) as caught:
        await connection.access_token()
    assert caught.value.code == "secret_store_unavailable" and caught.value.retryable is True
    assert caught.value.details == {"reason": "not_configured"}
    await ctx.aclose()


# --- адаптер ядра ---


class _FakeCpError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class _FakeClient:
    instances: list[_FakeClient] = []  # noqa: RUF012
    responses: dict[str, Any] = {}  # noqa: RUF012

    def __init__(self, url: str, credential: Any) -> None:
        self.url = url
        self.calls: list[tuple[str, str]] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def _request(self, method: str, path: str, **_: Any) -> Any:
        self.calls.append((method, path))
        answer = _FakeClient.responses.get(path)
        if answer is None:
            raise _FakeCpError("not_found", 404)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _agent_connection(**overrides: Any) -> dict[str, Any]:
    body = {
        "key": "crm",
        "type": "crm-x",
        "typeVersion": 2,
        "account": "example.test",
        "auth": "oauth2",
        "status": "active",
        "settings": {"pipeline": "sales"},
        "secretRef": "oauth2/creds/tenants/t1/connections/crm",
        "expiresAt": None,
    }
    return {**body, **overrides}


@pytest.fixture
def client_module(monkeypatch):
    _FakeClient.instances.clear()
    _FakeClient.responses = {}
    package = types.ModuleType("control_plane_client")
    client = types.ModuleType("control_plane_client.client")
    client.ControlPlaneClient = _FakeClient  # type: ignore[attr-defined]
    credentials = types.ModuleType("control_plane_client.credentials")
    credentials.resolve_credential = lambda url: f"cred-for:{url}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "control_plane_client", package)
    monkeypatch.setitem(sys.modules, "control_plane_client.client", client)
    monkeypatch.setitem(sys.modules, "control_plane_client.credentials", credentials)
    return _FakeClient


@pytest.mark.asyncio
async def test_directory_speaks_the_agent_routes(client_module):
    client_module.responses["/agents/me/connections/crm"] = _agent_connection(
        expiresAt="2026-10-01T00:00:00Z"
    )
    client_module.responses["/agents/me"] = {"principalId": "p-1"}
    directory = ControlPlaneDirectory("https://cp.example/")
    info = await directory.connection("crm")
    assert info == ConnectionInfo(
        key="crm",
        type="crm-x",
        status="active",
        type_version=2,
        account="example.test",
        auth="oauth2",
        settings={"pipeline": "sales"},
        secret_ref="oauth2/creds/tenants/t1/connections/crm",
        expires_at=info.expires_at,
    )
    assert info.expires_at is not None and info.expires_at.year == 2026
    assert await directory.principal_id() == "p-1"
    assert await directory.principal_id() == "p-1"  # один запрос
    await directory.aclose()
    [client] = client_module.instances
    assert client.url == "https://cp.example"
    assert client.calls == [("GET", "/agents/me/connections/crm"), ("GET", "/agents/me")]


@pytest.mark.asyncio
async def test_directory_maps_core_errors(client_module):
    directory = ControlPlaneDirectory("https://cp.example")
    with pytest.raises(SkillError) as missing:
        await directory.connection("crm")
    assert missing.value.code == "connection_not_found"

    client_module.responses["/agents/me/connections/crm"] = _FakeCpError("transport_error", 0)
    with pytest.raises(SkillError) as down:
        await directory.connection("crm")
    assert down.value.code == "core_unavailable" and down.value.retryable is True

    client_module.responses["/agents/me"] = {"principalId": None}
    with pytest.raises(SkillError) as anonymous:
        await directory.principal_id()
    assert anonymous.value.code == "agent_identity_missing"


# --- адаптер хранилища ---


class Bao:
    """Хранилище по HTTP: вход jwt и чтение по политике, как отвечает настоящее."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.granted: set[str] = set()
        self.sealed = False
        self.roles = {"agent-p-1"}
        self.logins: list[dict[str, Any]] = []
        self.reads: list[tuple[str, str | None]] = []
        self.expire_tokens = False
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.sealed:
            return httpx.Response(503, json={"errors": ["Vault is sealed"]})
        # хранилище за периметром: префикс /secrets сохраняется в адресе
        assert request.url.path.startswith("/secrets/v1/")
        path = request.url.path.removeprefix("/secrets")
        if path == "/v1/auth/jwt/login":
            body = __import__("json").loads(request.content)
            self.logins.append(body)
            if body["role"] not in self.roles:
                return httpx.Response(400, json={"errors": ['role "x" could not be found']})
            return httpx.Response(
                200, json={"auth": {"client_token": STORE_TOKEN, "lease_duration": 300}}
            )
        ref = path.removeprefix("/v1/")
        token = request.headers.get("X-Vault-Token")
        self.reads.append((ref, token))
        if token != STORE_TOKEN or self.expire_tokens:
            self.expire_tokens = False
            return httpx.Response(403, json={"errors": ["permission denied"]})
        if ref not in self.granted:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        if ref not in self.documents:
            return httpx.Response(404, json={"errors": []})
        return httpx.Response(200, json={"data": self.documents[ref]})


def _store(bao: Bao, *, role: str = "agent-p-1") -> OpenBaoStore:
    async def jwt() -> str:
        return IAM_JWT

    return OpenBaoStore(
        "https://cp.example/secrets/",
        jwt=jwt,
        role=role,
        transport=httpx.MockTransport(bao.handler),
    )


KV = "kv/data/tenants/t1/connections/key"
OAUTH = "oauth2/creds/tenants/t1/connections/crm"


@pytest.mark.asyncio
async def test_store_reads_kv_and_oauth2_documents():
    bao = Bao()
    bao.documents[KV] = {
        "data": {"access_token": TOKEN, "expires_at": "2026-12-31T00:00:00Z"},
        "metadata": {"version": 1},
    }
    bao.documents[OAUTH] = {
        "access_token": "at-oauth",
        "expire_time": "2026-10-01T10:00:00Z",
        "type": "Bearer",
    }
    bao.granted |= {KV, OAUTH}
    store = _store(bao)
    kv = await store.read(KV)
    oauth = await store.read(OAUTH)
    await store.aclose()
    assert kv.access_token == TOKEN and kv.expires_at is not None
    assert oauth.access_token == "at-oauth" and oauth.expires_at is not None
    assert oauth.expires_at.hour == 10
    assert bao.logins == [{"role": "agent-p-1", "jwt": IAM_JWT}]  # вход один
    assert [ref for ref, _ in bao.reads] == [KV, OAUTH]


@pytest.mark.asyncio
async def test_store_relogs_in_once_on_403_then_refuses():
    bao = Bao()
    bao.documents[KV] = {"data": {"access_token": TOKEN}}
    bao.granted.add(KV)
    store = _store(bao)
    await store.read(KV)
    bao.expire_tokens = True  # токен хранилища истёк
    assert (await store.read(KV)).access_token == TOKEN
    assert len(bao.logins) == 2

    bao.granted.clear()  # политика снята
    with pytest.raises(MaterialRefused) as refused:
        await store.read(KV)
    assert refused.value.status == 403

    bao.granted.add(KV)
    del bao.documents[KV]
    with pytest.raises(MaterialRefused) as missing:
        await store.read(KV)
    assert missing.value.status == 404


@pytest.mark.asyncio
async def test_store_login_refusal_is_retryable():
    """OpenBao отвечает 400 на любой отказ входа (роли ещё нет — политику не свели,
    чужой sub, не тот audience): это повторяемый secret_store_unavailable."""
    bao = Bao()
    with pytest.raises(SkillError) as refused:
        await _store(bao, role="agent-gone").read(KV)
    assert refused.value.code == "secret_store_unavailable" and refused.value.retryable is True
    assert refused.value.details == {"reason": "login_refused"}


def _bao_client(bao: Bao, status: str = "active", *, role: str = "agent-p-1") -> Any:
    directory = FakeConnectionDirectory()
    directory.infos["crm"] = ConnectionInfo.from_api(_agent_connection(status=status))
    return directory, ConnectionClient(directory, _store(bao, role=role))


@pytest.mark.asyncio
async def test_client_login_refused_while_active_is_retryable():
    bao = Bao()
    directory, client = _bao_client(bao, role="agent-not-synced")
    with pytest.raises(SkillError) as caught:
        await client.access_token("crm")
    assert caught.value.code == "secret_store_unavailable" and caught.value.retryable is True
    assert caught.value.details == {"connection": "crm", "reason": "login_refused"}
    assert directory.lookups == ["crm", "crm"]  # сверка с ядром после отказа входа


@pytest.mark.asyncio
async def test_client_login_refused_after_revoke_is_revoked():
    """Отзыв последнего подключения снимает роль агента: вход отклонён, ядро говорит
    revoked — ответ connection_revoked, а не повтор."""
    bao = Bao()
    directory, client = _bao_client(bao, role="agent-removed")
    await client.connection("crm")  # сведения active в кэше
    directory.infos["crm"] = ConnectionInfo.from_api(_agent_connection(status="revoked"))
    with pytest.raises(SkillError) as caught:
        await client.access_token("crm")
    assert caught.value.code == "connection_revoked"


@pytest.mark.asyncio
async def test_sealed_or_unreachable_store_is_retryable():
    bao = Bao()
    bao.sealed = True
    store = _store(bao)
    with pytest.raises(SkillError) as sealed:
        await store.read(KV)
    assert sealed.value.code == "secret_store_unavailable" and sealed.value.retryable is True
    assert sealed.value.details == {"reason": "sealed_or_standby"}

    bao.sealed, bao.down = False, True
    with pytest.raises(SkillError) as down:
        await store.read(KV)
    assert down.value.code == "secret_store_unavailable"
    assert down.value.details == {"reason": "unreachable"}

    bao.down = False  # процесс жив, следующий вызов проходит
    bao.documents[KV] = {"data": {"access_token": TOKEN}}
    bao.granted.add(KV)
    assert (await store.read(KV)).access_token == TOKEN


@pytest.mark.asyncio
async def test_store_rejects_paths_outside_connection_material():
    store = _store(Bao())
    for ref in ("sys/policies/acl/x", "kv/data/../sys/seal", "kv/metadata/tenants/t1/x", ""):
        with pytest.raises(SkillError) as caught:
            await store.read(ref)
        assert caught.value.code == "secret_ref_invalid"


# --- обмен PAT на токен хранилища: канон клиента ядра ---


@pytest.fixture
def iam_module(monkeypatch):
    seen: list[dict[str, str]] = []

    class Credential:
        def __init__(self, environ: dict[str, str]) -> None:
            self.environ = environ
            self.error: Exception | None = None

        async def token(self) -> str:
            if self.error is not None:
                raise self.error
            return IAM_JWT

    made: list[Credential] = []

    def from_env(environ: dict[str, str]) -> Credential | None:
        seen.append(dict(environ))
        if not environ.get("CONTROL_PLANE_IAM_URL"):
            return None
        made.append(Credential(environ))
        return made[-1]

    module = types.ModuleType("control_plane_client.iam")
    module.ENV_IAM_AUDIENCE = "CONTROL_PLANE_IAM_AUDIENCE"  # type: ignore[attr-defined]
    module.ENV_IAM_SCOPES = "CONTROL_PLANE_IAM_SCOPES"  # type: ignore[attr-defined]
    module.iam_credential_from_environment = from_env  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "control_plane_client", types.ModuleType("cpc"))
    monkeypatch.setitem(sys.modules, "control_plane_client.iam", module)
    return seen, made


class _IamError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@pytest.mark.asyncio
async def test_store_login_exchanges_the_pat_for_the_store_audience(iam_module):
    seen, made = iam_module
    env = {"CONTROL_PLANE_IAM_URL": "https://iam.example", "CONTROL_PLANE_IAM_AUDIENCE": "cp"}
    source = _iam_token_source(env)
    assert await source() == IAM_JWT
    assert await source() == IAM_JWT
    [environ] = seen  # credential один на процесс, кэш токена — у канона
    assert environ["CONTROL_PLANE_IAM_AUDIENCE"] == "openbao"
    assert environ["CONTROL_PLANE_IAM_SCOPES"] == "secrets:read"
    assert env["CONTROL_PLANE_IAM_AUDIENCE"] == "cp"  # окружение не тронуто

    made[0].error = _IamError("iam_unreachable")
    with pytest.raises(SkillError) as down:
        await source()
    assert down.value.code == "secret_store_unavailable" and down.value.retryable is True

    made[0].error = _IamError("iam_audience_not_allowed")
    with pytest.raises(SkillError) as denied:
        await source()
    assert denied.value.code == "iam_audience_not_allowed" and denied.value.retryable is False


@pytest.mark.asyncio
async def test_store_login_without_iam_identity_is_config_missing(iam_module):
    with pytest.raises(SkillError) as caught:
        await _iam_token_source({})()
    assert caught.value.code == "config_missing"


# --- значение токена не попадает в журналы SDK ---


@pytest.mark.asyncio
async def test_token_values_never_reach_sdk_logs(caplog):
    caplog.set_level(logging.DEBUG)
    bao = Bao()
    bao.documents[OAUTH] = {"access_token": TOKEN, "expire_time": "2099-01-01T00:00:00Z"}
    bao.granted.add(OAUTH)
    directory = FakeConnectionDirectory()
    directory.infos["crm"] = ConnectionInfo.from_api(_agent_connection())
    client = ConnectionClient(directory, _store(bao))
    errors: list[SkillError] = []

    assert await client.access_token("crm") == TOKEN
    assert await client.access_token("crm", fresh=True) == TOKEN
    bao.granted.clear()  # политика ещё не сведена
    for step in ("refused", "sealed", "down"):
        if step == "sealed":
            bao.sealed = True
        if step == "down":
            bao.sealed, bao.down = False, True
        try:
            await client.access_token("crm", fresh=True)
        except SkillError as error:
            errors.append(error)
    directory.infos["crm"] = ConnectionInfo.from_api(_agent_connection(status="revoked"))
    try:
        await client.access_token("crm", fresh=True)
    except SkillError as error:
        errors.append(error)
    await client.aclose()

    assert [e.code for e in errors] == ["secret_store_unavailable"] * 3 + ["connection_revoked"]
    assert caplog.records, "SDK должен писать журнал без значений"
    text = caplog.text + "".join(
        f"{r.getMessage()}{r.args}{r.exc_text or ''}" for r in caplog.records
    )
    rendered = "".join(f"{e}{e.as_error()}{e.__cause__}{e.__context__}" for e in errors)
    for secret in (TOKEN, STORE_TOKEN, IAM_JWT):
        assert secret not in text
        assert secret not in rendered


# --- ревью: путь материала, single-flight, IAM 5xx, токен на излёте ---


@pytest.mark.asyncio
async def test_secret_ref_must_name_the_requested_connection(fake):
    info = fake.directory.infos["crm"]
    foreign = "kv/data/tenants/tenant-1/connections/other"
    fake.directory.infos["crm"] = ConnectionInfo(
        key="crm", type=info.type, status="active", auth="token", secret_ref=foreign
    )
    fake.store.granted.add(foreign)
    with pytest.raises(SkillError) as caught:
        await fake.client().access_token("crm")
    assert caught.value.code == "secret_ref_invalid"
    assert fake.store.reads == []


class _SlowStore:
    def __init__(self) -> None:
        self.reads = 0
        self.release = asyncio.Event()

    async def read(self, secret_ref: str) -> Any:
        from skill_sdk.connections import Material

        self.reads += 1
        await self.release.wait()
        return Material(access_token=TOKEN)

    async def aclose(self) -> None:
        return None


def _slow_client() -> tuple[_SlowStore, ConnectionClient]:
    directory = FakeConnectionDirectory()
    directory.infos["crm"] = ConnectionInfo(
        key="crm",
        type="crm-x",
        status="active",
        secret_ref="kv/data/tenants/t1/connections/crm",
    )
    store = _SlowStore()
    return store, ConnectionClient(directory, store)


@pytest.mark.asyncio
async def test_concurrent_calls_read_the_store_once():
    store, client = _slow_client()
    calls = [asyncio.create_task(client.access_token("crm")) for _ in range(5)]
    await asyncio.sleep(0)
    store.release.set()
    assert await asyncio.gather(*calls) == [TOKEN] * 5
    assert store.reads == 1


@pytest.mark.asyncio
async def test_token_read_before_invalidate_is_not_cached():
    store, client = _slow_client()
    call = asyncio.create_task(client.access_token("crm"))
    await asyncio.sleep(0)
    client.invalidate("crm")  # отзыв увиден, пока чтение в пути
    store.release.set()
    await call
    await client.access_token("crm")
    assert store.reads == 2


@pytest.mark.asyncio
async def test_token_on_its_last_seconds_is_reread(fake):
    fake.rotate("crm", "at-old", expires_at=fake.now + timedelta(seconds=3))
    client = fake.client()
    ref = fake.directory.infos["crm"].secret_ref
    original = fake.store.read

    async def refreshing_read(secret_ref: str) -> Any:
        material = await original(secret_ref)
        fake.rotate("crm", "at-new", expires_at=fake.now + timedelta(minutes=10))
        return material

    fake.store.read = refreshing_read  # type: ignore[method-assign]
    assert await client.access_token("crm") == "at-new"
    assert fake.store.reads == [ref, ref]

    fake.store.read = original  # type: ignore[method-assign]
    fake.rotate("crm", "at-dying", expires_at=fake.now + timedelta(seconds=3))
    with pytest.raises(SkillError) as caught:
        await client.access_token("crm", fresh=True)
    assert caught.value.code == "connection_expired"


@pytest.mark.asyncio
async def test_iam_exchange_5xx_is_retryable_and_4xx_is_not(iam_module):
    _, made = iam_module
    source = _iam_token_source({"CONTROL_PLANE_IAM_URL": "https://iam.example"})
    await source()
    made[0].error = _IamError("iam_exchange_failed", "IAM answered 503 to the exchange")
    with pytest.raises(SkillError) as down:
        await source()
    assert down.value.code == "secret_store_unavailable" and down.value.retryable is True
    made[0].error = _IamError("iam_exchange_failed", "IAM answered 400 to the exchange")
    with pytest.raises(SkillError) as bad:
        await source()
    assert bad.value.code == "iam_exchange_failed" and bad.value.retryable is False
