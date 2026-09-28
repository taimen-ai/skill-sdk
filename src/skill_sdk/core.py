"""Узкий доступ скилла к ядру: содержимое артефактов и база знаний (TAI-ADR-0056 Р5).

Скилл по-прежнему не заводит и не двигает задачи (TAI-ADR-0041). Но загрузчику
таблицы нужен файл из артефакта, а загрузчику документа — предпросмотр и
применение снимка. Раньше каждый пакет писал для этого свой клиент; теперь в
контексте два узких протокола:

- ``ctx.artifacts`` — ``read(artifact_id)``: содержимое артефакта через
  ``GET /api/v1/artifacts/{id}/content`` учётной записью исполнителя скиллов
  (право ``artifacts.read`` на workspace задачи, CP-ADR-0072 амендмент);
- ``ctx.knowledge`` — ``preview``, ``apply``, ``document``, ``recall``, ``query``:
  маршруты ``/api/v1/knowledge/*`` и ``/api/v1/context/recall`` (CP-ADR-0060
  амендмент); ``query`` — перечень сущностей видов с фильтром ``where`` через
  ``POST /knowledge/entities:query`` по всем страницам (K032).
  Память — только через ядро.

По умолчанию протоколы реализует ``ControlPlaneCore`` поверх канона клиента ядра
``control_plane_client`` (TAI-ADR-0030): адрес — ``CONTROL_PLANE_URL`` или адрес
демона-исполнителя ``CONTROL_PLANE_SERVER``, credential — ``resolve_credential``.
Клиент импортируется лениво: скиллу без ядра он не нужен. Тесты подставляют
``skill_sdk.testing.FakeCore`` через ``configure_core``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from skill_sdk.errors import SkillError

if TYPE_CHECKING:
    from skill_sdk.context import SkillContext

ENV_CONTROL_PLANE_URL = "CONTROL_PLANE_URL"
ENV_CONTROL_PLANE_SERVER = "CONTROL_PLANE_SERVER"

Json = dict[str, Any]


@dataclass(frozen=True)
class ArtifactContent:
    """Содержимое артефакта: байты, тип и контрольная сумма, как их отдало ядро."""

    data: bytes
    media_type: str | None = None
    sha256: str | None = None

    def text(self, encoding: str = "utf-8") -> str:
        return self.data.decode(encoding)


class SnapshotStale(SkillError):
    """Состояние источника изменилось после предпросмотра (``409 snapshot_stale``).

    Не повторяемо тем же вызовом: план нужно построить заново."""

    def __init__(self, message: str = "", *, state_token: str | None = None) -> None:
        super().__init__(
            "snapshot_stale",
            message or "состояние источника изменилось после предпросмотра — постройте план заново",
            retryable=False,
            details={"stateToken": state_token} if state_token else None,
        )
        self.state_token = state_token


QUERY_PAGE = 500
QUERY_MAX_ITEMS = 10_000


def _too_many(max_items: int) -> SkillError:
    return SkillError(
        "knowledge_query_too_large",
        f"перечень больше {max_items} сущностей — сузьте виды или условия where",
        retryable=False,
    )


class Artifacts(Protocol):
    async def read(self, artifact_id: str, *, for_task: str | None = None) -> ArtifactContent: ...


class Knowledge(Protocol):
    async def preview(self, snapshot: Json, *, workspace_id: str) -> Json: ...

    async def apply(
        self, snapshot: Json, *, workspace_id: str, expected_state: str | None = None
    ) -> Json: ...

    async def document(
        self,
        *,
        workspace_id: str,
        natural_key: str,
        title: str,
        chunks: list[Json],
        type: str = "document",
        links: list[Json] | None = None,
        meta: Json | None = None,
    ) -> Json: ...

    async def recall(self, **query: Any) -> Json: ...

    async def query(
        self,
        *,
        workspace_id: str,
        kinds: list[str],
        where: list[Json] | None = None,
        as_of: str | None = None,
        limit: int = QUERY_PAGE,
        max_items: int = QUERY_MAX_ITEMS,
    ) -> list[Json]: ...


class Core(Protocol):
    artifacts: Artifacts
    knowledge: Knowledge

    async def aclose(self) -> None: ...


CoreFactory = Callable[["SkillContext"], Core]
_core_factory: CoreFactory | None = None


def configure_core(factory: CoreFactory | None) -> None:
    """Задать, откуда контекст берёт доступ к ядру.

    Тесты подставляют ``FakeCore``, хостинг берёт ``ControlPlaneCore`` по умолчанию."""
    global _core_factory
    _core_factory = factory


def core_for(ctx: SkillContext) -> Core:
    if _core_factory is not None:
        return _core_factory(ctx)
    url = ctx.config(ENV_CONTROL_PLANE_URL) or ctx.config(ENV_CONTROL_PLANE_SERVER)
    if not url:
        raise SkillError(
            "config_missing",
            f"у хостинга не задан ни {ENV_CONTROL_PLANE_URL}, ни {ENV_CONTROL_PLANE_SERVER}",
            retryable=True,
        )
    return ControlPlaneCore(url.rstrip("/"))


def _stale_or_raise(error: Exception) -> None:
    """``409 snapshot_stale`` ядра → ``SnapshotStale``; остальное — как есть."""
    code = getattr(error, "code", None)
    if code == "snapshot_stale":
        details = getattr(error, "details", None) or {}
        token = details.get("stateToken") if isinstance(details, Mapping) else None
        raise SnapshotStale(str(error), state_token=token) from error
    raise error


class _Session:
    """Один клиент ядра на вызов скилла; открывается при первом обращении."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._client: Any = None

    async def client(self) -> Any:
        if self._client is None:
            try:
                from control_plane_client.client import ControlPlaneClient
                from control_plane_client.credentials import resolve_credential
            except ImportError as error:
                raise SkillError(
                    "core_unavailable",
                    "control-plane-client не установлен у хостинга скиллов",
                    retryable=True,
                ) from error
            self._client = ControlPlaneClient(self.url, resolve_credential(self.url))
            await self._client.__aenter__()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.__aexit__(None, None, None)


class _ControlPlaneArtifacts:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def read(self, artifact_id: str, *, for_task: str | None = None) -> ArtifactContent:
        cp = await self._session.client()
        with tempfile.TemporaryDirectory(prefix="skill-artifact-") as folder:
            target = Path(folder) / "content"
            meta = await cp.download_artifact_content(artifact_id, target, for_task=for_task)
            return ArtifactContent(
                data=target.read_bytes(),
                media_type=meta.get("mediaType"),
                sha256=meta.get("sha256"),
            )


class _ControlPlaneKnowledge:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def _post(self, path: str, body: Json, *, idempotent: bool = False) -> Json:
        cp = await self._session.client()
        try:
            answer: Json = await cp._request("POST", path, json_body=body, idempotent=idempotent)
        except Exception as error:
            _stale_or_raise(error)
            raise
        return answer

    async def preview(self, snapshot: Json, *, workspace_id: str) -> Json:
        return await self._post(
            "/knowledge/snapshots:preview", {**snapshot, "workspaceId": workspace_id}
        )

    async def apply(
        self, snapshot: Json, *, workspace_id: str, expected_state: str | None = None
    ) -> Json:
        body: Json = {**snapshot, "workspaceId": workspace_id}
        if expected_state is not None:
            body["expectedState"] = expected_state
        # повтор того же snapshotId память узнаёт — вызов идемпотентен
        return await self._post("/knowledge/snapshots", body, idempotent=True)

    async def document(
        self,
        *,
        workspace_id: str,
        natural_key: str,
        title: str,
        chunks: list[Json],
        type: str = "document",
        links: list[Json] | None = None,
        meta: Json | None = None,
    ) -> Json:
        body: Json = {
            "workspaceId": workspace_id,
            "naturalKey": natural_key,
            "title": title,
            "type": type,
            "chunks": chunks,
            "links": links or [],
        }
        if meta is not None:
            body["meta"] = meta
        return await self._post("/knowledge/documents", body, idempotent=True)

    async def recall(self, **query: Any) -> Json:
        """Типизированный обход (CP-ADR-0064) — тело ``POST /context/recall`` в camelCase:
        ``anchor``, ``kinds``, ``relations``, ``depth``, ``where``, ``workspaceId``…"""
        return await self._post("/context/recall", dict(query))

    async def query(
        self,
        *,
        workspace_id: str,
        kinds: list[str],
        where: list[Json] | None = None,
        as_of: str | None = None,
        limit: int = QUERY_PAGE,
        max_items: int = QUERY_MAX_ITEMS,
    ) -> list[Json]:
        """Все сущности ``kinds``, действующие на ``as_of`` (сейчас, если не задано), чьи
        атрибуты выполняют каждое условие ``where`` (``{attr, op, value}``, литералы):
        страницы ``POST /knowledge/entities:query`` до ``nextCursor: null``. Перечень
        длиннее ``max_items`` — ошибка ``knowledge_query_too_large``, а не обрезка."""
        body: Json = {"workspaceId": workspace_id, "kinds": list(kinds), "limit": limit}
        if where:
            body["where"] = list(where)
        if as_of is not None:
            body["asOf"] = as_of
        items: list[Json] = []
        cursor: str | None = None
        while True:
            page = await self._post(
                "/knowledge/entities:query", {**body, **({"cursor": cursor} if cursor else {})}
            )
            items.extend(page.get("items") or [])
            if len(items) > max_items:
                raise _too_many(max_items)
            cursor = page.get("nextCursor")
            if not cursor:
                return items


class ControlPlaneCore:
    """Доступ скилла к ядру через ``control_plane_client`` с credential исполнителя."""

    def __init__(self, url: str) -> None:
        self._session = _Session(url)
        self.artifacts: Artifacts = _ControlPlaneArtifacts(self._session)
        self.knowledge: Knowledge = _ControlPlaneKnowledge(self._session)

    async def aclose(self) -> None:
        await self._session.aclose()
