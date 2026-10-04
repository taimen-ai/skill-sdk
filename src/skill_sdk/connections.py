"""Подключения к внешним системам: ``ctx.connection(key)`` и ``ConnectionClient``
(TAI-ADR-0061, CP-ADR-0079 п.1 и п.8).

Подключение — учётка внешней системы в организации. Что это за подключение
(тип, учётка, несекретные настройки, статус, путь материала в хранилище), агент
узнаёт у ядра: ``GET /api/v1/agents/me/connections/{key}``. Ядро отвечает только
про подключения из ``spec.connections`` текущей ревизии агента, материала в
ответе нет. Сам материал (``access_token``) агент читает из хранилища секретов
своей identity: обмен PAT на токен IAM audience ``openbao`` (канон
``control_plane_client.iam``), вход ``POST /v1/auth/jwt/login`` с ролью
``agent-<principalId>``, чтение ``GET /v1/<secretRef>`` —
``oauth2/creds/tenants/<t>/connections/<key>`` или
``kv/data/tenants/<t>/connections/<key>``. Через ядро материал не ходит.

Кэш материала — не дольше 60 с и не дольше срока самого токена; токен, которому
осталось меньше 5 с, не отдаётся — он перечитывается. Один ключ читается одним
походом в хранилище за раз. Отзыв
подключения видит следующий вызов ``access_token()`` после истечения кэша, а
``access_token(fresh=True)`` — сразу (например, после ``401`` провайдера).

Ошибки — ``SkillError`` с машинным кодом:

- ``connection_revoked`` — подключение отозвано (не повторяемо);
- ``connection_expired`` — доступ потерян: срок ключа вышел или хранилище не
  может выдать токен (не повторяемо, нужно переподключение);
- ``secret_store_unavailable`` — хранилище не настроено, недоступно,
  запечатано, не пускает агента (роль ещё не сведена) или политика агента ещё
  не сведена (повторяемо; отказ входа сначала сверяется со статусом в ядре);
- ``connection_not_found`` — ключа нет в описании агента или подключения нет;
- ``connection_pending`` — подключение заведено, но ещё не подключено.

Значение токена SDK не пишет ни в журнал, ни в текст ошибок, ни в ``repr``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from skill_sdk.errors import SkillError

if TYPE_CHECKING:
    from skill_sdk.context import SkillContext

logger = logging.getLogger("skill_sdk.connections")

ENV_SECRET_STORE_URL = "SKILL_SDK_SECRET_STORE_URL"
ENV_SECRET_STORE_AUDIENCE = "SKILL_SDK_SECRET_STORE_AUDIENCE"
ENV_SECRET_STORE_SCOPES = "SKILL_SDK_SECRET_STORE_SCOPES"
ENV_SECRET_STORE_ROLE = "SKILL_SDK_SECRET_STORE_ROLE"

DEFAULT_AUDIENCE = "openbao"
DEFAULT_SCOPES = ("secrets:read",)
# Верхняя граница кэша материала (plan фичи integrations-connections §4).
MAX_CACHE_SECONDS = 60.0
# Токен, который истекает в пути, провайдер отклонит: отдаём с запасом.
EXPIRY_MARGIN_SECONDS = 5.0
ROLE_PREFIX = "agent-"

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# secretRef приходит из ядра, но в адрес запроса попадает только путь материала
# подключения (CP-ADR-0079 п.1), и ключ в нём — ключ запрошенного подключения.
SECRET_REF_RE = re.compile(
    r"^(oauth2/creds|kv/data)/tenants/[A-Za-z0-9_-]{1,100}/connections/"
    r"(?P<key>[a-z0-9][a-z0-9-]{0,62})$"
)

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"


# --- ошибки ---


class ConnectionRevoked(SkillError):
    def __init__(self, key: str) -> None:
        super().__init__(
            "connection_revoked",
            f"подключение {key} отозвано — нужно подключить заново",
            retryable=False,
            details={"connection": key},
        )


class ConnectionExpired(SkillError):
    def __init__(self, key: str, reason: str | None = None) -> None:
        details: dict[str, Any] = {"connection": key}
        if reason:
            details["reason"] = reason
        super().__init__(
            "connection_expired",
            f"доступ по подключению {key} потерян — нужно переподключение",
            retryable=False,
            details=details,
        )


class SecretStoreUnavailable(SkillError):
    def __init__(self, message: str = "", *, reason: str, key: str | None = None) -> None:
        details: dict[str, Any] = {"reason": reason}
        if key is not None:
            details["connection"] = key
        super().__init__(
            "secret_store_unavailable",
            message or "хранилище секретов недоступно",
            retryable=True,
            details=details,
        )


class ConnectionNotFound(SkillError):
    def __init__(self, key: str) -> None:
        super().__init__(
            "connection_not_found",
            f"подключения {key} нет в описании агента",
            retryable=False,
            details={"connection": key},
        )


class ConnectionPending(SkillError):
    def __init__(self, key: str) -> None:
        super().__init__(
            "connection_pending",
            f"подключение {key} ещё не подключено",
            retryable=False,
            details={"connection": key},
        )


def _invalid_ref() -> SkillError:
    return SkillError(
        "secret_ref_invalid", "путь материала вне хранилища подключений", retryable=False
    )


def check_secret_ref(secret_ref: str, key: str | None = None) -> None:
    """Путь материала — ``oauth2/creds|kv/data/tenants/<t>/connections/<key>``;
    с ``key`` — ещё и того самого подключения."""
    match = SECRET_REF_RE.match(secret_ref)
    if match is None or (key is not None and match.group("key") != key):
        raise _invalid_ref()


class MaterialRefused(Exception):
    """Хранилище отказало в материале (``400``/``403``/``404``): политика снята,
    материал удалён или его нельзя выдать. Что это значит, решает клиент, сверившись
    со статусом подключения в ядре. Не ``SkillError``: наружу не выходит."""

    def __init__(self, status: int) -> None:
        super().__init__(f"store refused: {status}")
        self.status = status


# --- сведения и материал ---


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ConnectionInfo:
    """``AgentConnectionOut`` ядра (CP-ADR-0079 п.8): сведения без материала."""

    key: str
    type: str
    status: str
    type_version: int | None = None
    account: str | None = None
    auth: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None
    expires_at: datetime | None = None

    @classmethod
    def from_api(cls, body: Mapping[str, Any]) -> ConnectionInfo:
        settings = body.get("settings")
        version = body.get("typeVersion")
        return cls(
            key=str(body["key"]),
            type=str(body["type"]),
            status=str(body["status"]),
            type_version=version if isinstance(version, int) else None,
            account=body.get("account"),
            auth=body.get("auth"),
            settings=dict(settings) if isinstance(settings, Mapping) else {},
            secret_ref=body.get("secretRef"),
            expires_at=_parse_time(body.get("expiresAt")),
        )


@dataclass(frozen=True)
class Material:
    """Материал доступа из хранилища. Значение не попадает в ``repr``."""

    access_token: str = field(repr=False)
    expires_at: datetime | None = None


def material_from_document(document: Mapping[str, Any]) -> Material:
    """Документ ``oauth2/creds`` или ``kv`` → материал: поле ``access_token`` у обоих,
    срок — ``expires_at`` (документ ``kv``) или ``expire_time`` (ответ плагина)."""
    token = document.get("access_token")
    if not isinstance(token, str) or not token:
        raise SkillError(
            "secret_material_malformed",
            "в документе хранилища нет access_token",
            retryable=False,
        )
    expires = _parse_time(document.get("expires_at")) or _parse_time(document.get("expire_time"))
    return Material(access_token=token, expires_at=expires)


# --- протоколы: сведения из ядра и материал из хранилища ---


class ConnectionDirectory(Protocol):
    """Откуда клиент берёт сведения о подключениях агента (ядро)."""

    async def connection(self, key: str) -> ConnectionInfo: ...

    async def aclose(self) -> None: ...


class SecretStore(Protocol):
    """Откуда клиент читает материал: ``read(secret_ref)`` → ``Material``.

    Отказ по политике или отсутствию материала — ``MaterialRefused``; недоступное,
    запечатанное или не настроенное хранилище — ``SecretStoreUnavailable``."""

    async def read(self, secret_ref: str) -> Material: ...

    async def aclose(self) -> None: ...


# --- подключение, как его видит скилл ---


class Connection:
    """``ctx.connection(key)``: тип, учётка, настройки и ``access_token()``."""

    __slots__ = ("_client", "info")

    def __init__(self, info: ConnectionInfo, client: ConnectionClient) -> None:
        self.info = info
        self._client = client

    @property
    def key(self) -> str:
        return self.info.key

    @property
    def type(self) -> str:
        return self.info.type

    @property
    def type_version(self) -> int | None:
        return self.info.type_version

    @property
    def account(self) -> str | None:
        return self.info.account

    @property
    def auth(self) -> str | None:
        return self.info.auth

    @property
    def status(self) -> str:
        return self.info.status

    @property
    def settings(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self.info.settings))

    async def access_token(self, *, fresh: bool = False) -> str:
        """Токен доступа к внешней системе. ``fresh=True`` — мимо кэша."""
        return await self._client.access_token(self.key, fresh=fresh)

    def __repr__(self) -> str:
        return (
            f"Connection(key={self.key!r}, type={self.type!r}, "
            f"account={self.account!r}, status={self.status!r})"
        )


@dataclass
class _Cached:
    value: Any = field(repr=False)
    until: float


class ConnectionClient:
    """Клиент подключений агента: сведения из ядра, материал из хранилища, кэш ≤ 60 с.

    Для скилла его создаёт контекст (``ctx.connection``); коннектор-наблюдатель без
    ``ctx`` держит свой: ``ConnectionClient.from_environment()`` и ``await
    client.access_token("crm")`` на каждый запрос к провайдеру."""

    def __init__(
        self,
        directory: ConnectionDirectory,
        store: SecretStore,
        *,
        cache_seconds: float = MAX_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 <= cache_seconds <= MAX_CACHE_SECONDS:
            raise ValueError(f"cache_seconds должен быть в [0, {MAX_CACHE_SECONDS:g}]")
        self._directory = directory
        self._store = store
        self._cache_seconds = float(cache_seconds)
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._infos: dict[str, _Cached] = {}
        self._tokens: dict[str, _Cached] = {}
        # Один поход в хранилище на ключ; поколение не даёт положить в кэш токен,
        # прочитанный до invalidate().
        self._locks: dict[str, asyncio.Lock] = {}
        self._generations: dict[str, int] = {}

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        cache_seconds: float = MAX_CACHE_SECONDS,
    ) -> ConnectionClient:
        """Ядро — ``CONTROL_PLANE_URL`` (или ``CONTROL_PLANE_SERVER``) с credential
        исполнителя; хранилище — ``SKILL_SDK_SECRET_STORE_URL``, вход обменом PAT
        (``CONTROL_PLANE_IAM_*``, ``IAM_*``) на токен audience
        ``SKILL_SDK_SECRET_STORE_AUDIENCE`` (``openbao``)."""
        import os

        values = os.environ if env is None else env
        directory = ControlPlaneDirectory.from_environment(values)
        store: SecretStore
        if (values.get(ENV_SECRET_STORE_URL) or "").strip():
            store = OpenBaoStore.from_environment(values, principal=directory.principal_id)
        else:
            # Сведения о подключении нужны и без хранилища; материал — нет.
            store = UnconfiguredStore()
        return cls(directory, store, cache_seconds=cache_seconds)

    async def connection(self, key: str, *, fresh: bool = False) -> Connection:
        """Сведения о подключении (без материала). Отозванное или истёкшее
        подключение сведения отдаёт: статус видно в ``Connection.status``."""
        return Connection(await self._info(key, fresh=fresh), self)

    async def access_token(self, key: str, *, fresh: bool = False) -> str:
        """Токен доступа: из кэша (≤ 60 с) или из хранилища. ``fresh=True`` — мимо кэша."""
        cached = None if fresh else self._cached_token(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Пока ждали, токен мог прочитать соседний вызов.
            cached = None if fresh else self._cached_token(key)
            if cached is not None:
                return cached
            return await self._read_token(key, fresh=fresh)

    def _cached_token(self, key: str) -> str | None:
        cached = self._tokens.get(key)
        if cached is not None and self._clock() < cached.until:
            return str(cached.value)
        return None

    async def _read_token(self, key: str, *, fresh: bool) -> str:
        self._tokens.pop(key, None)
        generation = self._generations.get(key, 0)
        info = await self._info(key, fresh=fresh)
        self._require_usable(info)
        assert info.secret_ref is not None  # _require_usable проверил
        check_secret_ref(info.secret_ref, key)

        material = await self._read_material(info)
        wall = self._now()
        left = (
            None
            if material.expires_at is None
            else (material.expires_at - wall).total_seconds() - EXPIRY_MARGIN_SECONDS
        )
        if left is not None and left <= 0:
            # Токен на излёте: хранилище (плагин OAuth) могло уже обновить — читаем
            # ещё раз; тот же токен на излёте — доступа нет.
            material = await self._read_material(info)
            wall = self._now()
            left = (
                None
                if material.expires_at is None
                else (material.expires_at - wall).total_seconds() - EXPIRY_MARGIN_SECONDS
            )
            if left is not None and left <= 0:
                self._infos.pop(key, None)
                raise ConnectionExpired(key, "token_expired")
        now = self._clock()
        until = now + self._cache_seconds
        if left is not None:
            until = min(until, now + left)
        if until > now and self._generations.get(key, 0) == generation:
            self._tokens[key] = _Cached(material.access_token, until)
        logger.debug("connection %s: material read from store", key)
        return material.access_token

    async def _read_material(self, info: ConnectionInfo) -> Material:
        key = info.key
        assert info.secret_ref is not None
        try:
            return await self._store.read(info.secret_ref)
        except MaterialRefused as refused:
            # Хранилище не даёт материал: учёт ядра скажет, отозвано ли подключение.
            await self._recheck(key)
            if refused.status == 400:
                # Материал есть, а выдать его хранилище не может (обновление не прошло).
                logger.info("connection %s: store cannot issue a token", key)
                raise ConnectionExpired(key, "token_unavailable") from None
            logger.info("connection %s: store access not granted yet", key)
            raise SecretStoreUnavailable(
                f"хранилище ещё не выдаёт материал подключения {key}",
                reason="access_not_granted",
                key=key,
            ) from None
        except SecretStoreUnavailable as error:
            if (error.details or {}).get("reason") == "login_refused":
                # Роли агента нет или токен ей не подходит: так бывает и после
                # отзыва последнего подключения — сначала спросить ядро.
                await self._recheck(key)
                logger.info("connection %s: store login refused", key)
                raise SecretStoreUnavailable(
                    f"хранилище не пускает агента к подключению {key}",
                    reason="login_refused",
                    key=key,
                ) from None
            raise

    async def _recheck(self, key: str) -> None:
        self._infos.pop(key, None)
        self._require_usable(await self._info(key, fresh=True))

    def invalidate(self, key: str | None = None) -> None:
        """Забыть кэш подключения (или всех)."""
        keys = set(self._infos) | set(self._tokens) if key is None else {key}
        for name in keys:
            self._generations[name] = self._generations.get(name, 0) + 1
            self._infos.pop(name, None)
            self._tokens.pop(name, None)

    async def aclose(self) -> None:
        self.invalidate()
        try:
            await self._store.aclose()
        finally:
            await self._directory.aclose()

    async def __aenter__(self) -> ConnectionClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- внутреннее --

    async def _info(self, key: str, *, fresh: bool) -> ConnectionInfo:
        if not KEY_RE.match(key):
            raise ValueError(f"ключ подключения {key!r} не соответствует {KEY_RE.pattern}")
        now = self._clock()
        cached = None if fresh else self._infos.get(key)
        if cached is not None and now < cached.until:
            info: ConnectionInfo = cached.value
            return info
        info = await self._directory.connection(key)
        if info.status == STATUS_ACTIVE and self._cache_seconds > 0:
            # Кэшируется только активное: переподключение видно сразу.
            self._infos[key] = _Cached(info, now + self._cache_seconds)
        else:
            self._infos.pop(key, None)
        return info

    def _require_usable(self, info: ConnectionInfo) -> None:
        if info.status == STATUS_REVOKED:
            self._tokens.pop(info.key, None)
            raise ConnectionRevoked(info.key)
        if info.status == STATUS_EXPIRED:
            self._tokens.pop(info.key, None)
            raise ConnectionExpired(info.key)
        if info.status != STATUS_ACTIVE or not info.secret_ref:
            raise ConnectionPending(info.key)


# --- фабрика для ctx.connection ---

ConnectionsFactory = Callable[["SkillContext"], ConnectionClient]
_connections_factory: ConnectionsFactory | None = None


def configure_connections(factory: ConnectionsFactory | None) -> None:
    """Задать, откуда контекст берёт клиент подключений.

    Тесты подставляют ``FakeConnections.client()`` из ``skill_sdk.testing``; хостинг
    по умолчанию собирает ``ConnectionClient.from_environment`` из окружения вызова."""
    global _connections_factory
    _connections_factory = factory


def connections_for(ctx: SkillContext, env: Mapping[str, str]) -> ConnectionClient:
    if _connections_factory is not None:
        return _connections_factory(ctx)
    return ConnectionClient.from_environment(env)


# --- ядро: сведения о подключениях агента ---


class ControlPlaneDirectory:
    """``GET /agents/me/connections/{key}`` и ``GET /agents/me`` через клиент ядра
    (канон ``control_plane_client``, credential исполнителя)."""

    def __init__(self, url: str) -> None:
        from skill_sdk.core import _Session

        self._session = _Session(url.rstrip("/"))
        self._principal: str | None = None

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> ControlPlaneDirectory:
        from skill_sdk.core import ENV_CONTROL_PLANE_SERVER, ENV_CONTROL_PLANE_URL

        url = env.get(ENV_CONTROL_PLANE_URL) or env.get(ENV_CONTROL_PLANE_SERVER)
        if not url:
            raise SkillError(
                "config_missing",
                f"не задан ни {ENV_CONTROL_PLANE_URL}, ни {ENV_CONTROL_PLANE_SERVER}",
                retryable=True,
            )
        return cls(url)

    async def _get(self, path: str) -> Mapping[str, Any]:
        cp = await self._session.client()
        try:
            answer: Mapping[str, Any] = await cp._request("GET", path)
        except Exception as error:
            raise _core_error(error) from error
        return answer

    async def connection(self, key: str) -> ConnectionInfo:
        try:
            body = await self._get(f"/agents/me/connections/{key}")
        except SkillError as error:
            if error.code == "not_found":
                raise ConnectionNotFound(key) from None
            raise
        return ConnectionInfo.from_api(body)

    async def principal_id(self) -> str:
        """Principal агента в ядре — из него имя роли хранилища ``agent-<id>``."""
        if self._principal is None:
            me = await self._get("/agents/me")
            principal = me.get("principalId")
            if not principal:
                raise SkillError(
                    "agent_identity_missing",
                    "у агента нет principal в ядре — доступа к хранилищу нет",
                    retryable=False,
                )
            self._principal = str(principal)
        return self._principal

    async def aclose(self) -> None:
        await self._session.aclose()


def _core_error(error: Exception) -> SkillError:
    if isinstance(error, SkillError):
        return error
    code = getattr(error, "code", None)
    status = getattr(error, "status", 0) or 0
    if code == "transport_error" or status >= 500 or status == 429:
        return SkillError("core_unavailable", "ядро недоступно", retryable=True)
    if status == 404:
        return SkillError("not_found", "ядро ответило 404", retryable=False)
    safe = code if isinstance(code, str) and re.match(r"^[a-z0-9_.-]{1,100}$", code) else None
    return SkillError(safe or "core_error", f"ядро ответило {status}", retryable=False)


# --- хранилище: вход по jwt и чтение материала ---


class UnconfiguredStore:
    """Хранилище не настроено у хостинга: материал не выдаётся, повтор уместен
    (другой исполнитель может быть настроен)."""

    async def read(self, secret_ref: str) -> Material:
        raise SecretStoreUnavailable(
            f"у хостинга не задан {ENV_SECRET_STORE_URL}", reason="not_configured"
        )

    async def aclose(self) -> None:
        return None


TokenSource = Callable[[], Awaitable[str]]


class OpenBaoStore:
    """Хранилище секретов по HTTP API: ``POST /v1/auth/jwt/login`` токеном IAM
    audience ``openbao`` и ``GET /v1/<secretRef>`` токеном хранилища.

    Токен хранилища живёт в памяти до конца lease (роль агента — 5 мин); отказ
    ``403`` на чтении один раз повторяется со свежим входом — токен мог истечь."""

    def __init__(
        self,
        url: str,
        *,
        jwt: TokenSource,
        role: str | TokenSource,
        transport: Any = None,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url.rstrip("/")
        self._jwt = jwt
        self._role = role
        self._transport = transport
        self._timeout = timeout
        self._clock = clock
        self._http: Any = None
        self._token: str | None = None
        self._token_until = 0.0

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str], *, principal: TokenSource | None = None
    ) -> OpenBaoStore:
        url = (env.get(ENV_SECRET_STORE_URL) or "").strip()
        if not url:
            raise SecretStoreUnavailable(
                f"у хостинга не задан {ENV_SECRET_STORE_URL}", reason="not_configured"
            )
        role: str | TokenSource
        explicit_role = (env.get(ENV_SECRET_STORE_ROLE) or "").strip()
        if explicit_role:
            role = explicit_role
        elif principal is not None:
            principal_source = principal

            async def role_of_agent() -> str:
                return ROLE_PREFIX + await principal_source()

            role = role_of_agent
        else:
            raise SecretStoreUnavailable(
                f"не задан {ENV_SECRET_STORE_ROLE}", reason="not_configured"
            )
        return cls(url, jwt=_iam_token_source(env), role=role)

    async def read(self, secret_ref: str) -> Material:
        check_secret_ref(secret_ref)
        for attempt in (0, 1):
            token = await self._login()
            response = await self._send(
                "GET", f"/v1/{secret_ref}", headers={"X-Vault-Token": token}
            )
            status = response.status_code
            if status == 200:
                return material_from_document(_document(secret_ref, _json(response)))
            if status == 403 and attempt == 0:
                self._token = None  # токен хранилища мог истечь: войти заново
                continue
            if status in (400, 403, 404):
                raise MaterialRefused(status)
            raise _unavailable(status)
        raise MaterialRefused(403)  # pragma: no cover - цикл всегда выходит раньше

    async def aclose(self) -> None:
        self._token = None
        if self._http is not None:
            http, self._http = self._http, None
            await http.aclose()

    # -- внутреннее --

    async def _client(self) -> Any:
        if self._http is None:
            try:
                import httpx
            except ImportError as error:
                raise SecretStoreUnavailable(
                    "httpx не установлен у хостинга (skill-sdk[connections])",
                    reason="client_missing",
                ) from error
            self._http = httpx.AsyncClient(
                base_url=self._url, transport=self._transport, timeout=self._timeout
            )
        return self._http

    async def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        http = await self._client()
        try:
            return await http.request(method, path, **kwargs)
        except Exception as error:
            # Адрес, заголовки и тело в текст ошибки не попадают.
            raise SecretStoreUnavailable(
                f"хранилище недоступно: {type(error).__name__}", reason="unreachable"
            ) from None

    async def _login(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._token_until:
            return self._token
        self._token = None
        role = self._role if isinstance(self._role, str) else await self._role()
        jwt = await self._jwt()
        response = await self._send("POST", "/v1/auth/jwt/login", json={"role": role, "jwt": jwt})
        status = response.status_code
        if status in (400, 403):
            # OpenBao отвечает 400 на любой отказ входа: роли ещё нет (политику не
            # свели), sub, audience или principal_type не те. Это не потеря доступа
            # к подключению — клиент сверится с ядром, а повтор уместен.
            raise SecretStoreUnavailable(
                "хранилище отказало во входе агенту", reason="login_refused"
            )
        if status != 200:
            raise _unavailable(status)
        auth = _json(response).get("auth") or {}
        token = auth.get("client_token")
        if not isinstance(token, str) or not token:
            raise SecretStoreUnavailable("хранилище не выдало токен", reason="login_malformed")
        lease = auth.get("lease_duration")
        lease_seconds = float(lease) if isinstance(lease, int | float) and lease > 0 else 60.0
        self._token = token
        self._token_until = now + lease_seconds - min(30.0, lease_seconds / 2)
        return token


def _json(response: Any) -> Mapping[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, Mapping) else {}


def _document(secret_ref: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    data = body.get("data")
    if not isinstance(data, Mapping):
        return {}
    if secret_ref.startswith("kv/data/"):
        inner = data.get("data")  # kv-v2: {"data": {"data": {...}, "metadata": {...}}}
        return inner if isinstance(inner, Mapping) else {}
    return data


def _unavailable(status: int) -> SecretStoreUnavailable:
    if status == 503:
        return SecretStoreUnavailable(
            "хранилище секретов запечатано или не готово", reason="sealed_or_standby"
        )
    return SecretStoreUnavailable(f"хранилище ответило {status}", reason=f"status_{status}")


def _iam_status_retryable(error: Exception) -> bool:
    """``iam_exchange_failed`` канона не несёт статус полем, только в тексте
    («IAM answered 503 …»): 5xx и 429 — сбой среды, прочее — отказ."""
    status = getattr(error, "status", 0) or 0
    if not status:
        found = re.search(r"\banswered (\d{3})\b", str(error))
        status = int(found.group(1)) if found else 0
    return status >= 500 or status == 429 or status == 0


def _iam_token_source(env: Mapping[str, str]) -> TokenSource:
    """Обмен PAT исполнителя на токен audience хранилища — каноном
    ``control_plane_client.iam`` (TAI-ADR-0030), своего клиента IAM здесь нет."""
    audience = (env.get(ENV_SECRET_STORE_AUDIENCE) or "").strip() or DEFAULT_AUDIENCE
    scopes = (env.get(ENV_SECRET_STORE_SCOPES) or "").strip() or " ".join(DEFAULT_SCOPES)
    credential: Any = None

    async def token() -> str:
        nonlocal credential
        if credential is None:
            try:
                from control_plane_client.iam import (
                    ENV_IAM_AUDIENCE,
                    ENV_IAM_SCOPES,
                    iam_credential_from_environment,
                )
            except ImportError as error:
                raise SecretStoreUnavailable(
                    "control-plane-client не установлен у хостинга", reason="client_missing"
                ) from error
            credential = iam_credential_from_environment(
                {**env, ENV_IAM_AUDIENCE: audience, ENV_IAM_SCOPES: scopes}
            )
            if credential is None:
                raise SkillError(
                    "config_missing",
                    "у хостинга не настроена identity IAM (CONTROL_PLANE_IAM_URL)",
                    retryable=True,
                )
        try:
            return str(await credential.token())
        except Exception as error:
            code = getattr(error, "code", None)
            if code == "iam_unreachable" or (
                code == "iam_exchange_failed" and _iam_status_retryable(error)
            ):
                raise SecretStoreUnavailable("IAM недоступен", reason="iam_unreachable") from None
            if isinstance(code, str) and code.startswith("iam_"):
                raise SkillError(code, f"обмен PAT на токен {audience}: {code}") from None
            raise

    return token
