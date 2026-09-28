"""ctx.artifacts и ctx.knowledge (TAI-ADR-0056 Р5): подделка ядра для тестов скиллов и
адаптер к клиенту ядра — формы запросов и 409 snapshot_stale."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from skill_sdk import SkillContext, SkillError, SnapshotStale, configure_core, skill
from skill_sdk.context import Invocation
from skill_sdk.core import ControlPlaneCore
from skill_sdk.testing import FakeCore, invoke


class ImportIn(BaseModel):
    fileArtifactId: str
    workspaceId: str
    expectedState: str | None = None


class ImportOut(BaseModel):
    rows: int
    opened: int
    stateToken: str | None = None


@skill(
    "test.import_rows",
    version="1",
    side_effects="external_write",
    risk="low",
    idempotency="natural",
)
async def import_rows(inputs: ImportIn, ctx: SkillContext) -> ImportOut:
    """Прочитать CSV артефакта и применить снимком."""
    content = await ctx.artifacts.read(inputs.fileArtifactId)
    rows = [line.split(",") for line in content.text().splitlines()[1:] if line]
    snapshot = {
        "pack": "company@1",
        "source": "template:offering",
        "scope": "default",
        "snapshotId": "s1",
        "entities": [{"kind": "offering", "key": key, "title": title} for key, title in rows],
    }
    plan = await ctx.knowledge.preview(snapshot, workspace_id=inputs.workspaceId)
    if inputs.expectedState is None:
        return ImportOut(rows=len(rows), opened=plan["opened"], stateToken=plan["stateToken"])
    result = await ctx.knowledge.apply(
        snapshot, workspace_id=inputs.workspaceId, expected_state=inputs.expectedState
    )
    return ImportOut(rows=len(rows), opened=result["opened"])


@pytest.fixture
def core():
    fake = FakeCore()
    configure_core(lambda ctx: fake)
    yield fake
    configure_core(None)


CSV = "key,title\nSKU-1,Разработка ПО\nSKU-2,Сопровождение\n"


def test_skill_reads_artifact_previews_and_applies_by_state(core):
    core.artifacts.put("a1", CSV, "text/csv")
    plan = invoke(import_rows, {"fileArtifactId": "a1", "workspaceId": "w1"}).outputs
    assert plan["rows"] == 2 and plan["opened"] == 2
    assert core.knowledge.applied == []  # предпросмотр ничего не пишет

    done = invoke(
        import_rows,
        {"fileArtifactId": "a1", "workspaceId": "w1", "expectedState": plan["stateToken"]},
    ).outputs
    assert done["opened"] == 2 and len(core.knowledge.applied) == 1

    again = invoke(import_rows, {"fileArtifactId": "a1", "workspaceId": "w1"}).outputs
    assert again["opened"] == 0  # повторная загрузка — пустой план
    assert core.closed == 3  # доступ к ядру закрывается в конце каждого вызова


def test_stale_state_is_a_typed_non_retryable_error(core):
    core.artifacts.put("a1", CSV)
    plan = invoke(import_rows, {"fileArtifactId": "a1", "workspaceId": "w1"}).outputs
    invoke(
        import_rows,
        {"fileArtifactId": "a1", "workspaceId": "w1", "expectedState": plan["stateToken"]},
    )
    with pytest.raises(SkillError) as caught:
        invoke(
            import_rows,
            {"fileArtifactId": "a1", "workspaceId": "w1", "expectedState": plan["stateToken"]},
        )
    assert isinstance(caught.value, SnapshotStale)
    assert caught.value.code == "snapshot_stale" and caught.value.retryable is False


def test_missing_artifact_is_an_error(core):
    with pytest.raises(SkillError) as caught:
        invoke(import_rows, {"fileArtifactId": "nope", "workspaceId": "w1"})
    assert caught.value.code == "artifact_not_found"


def test_without_core_address_access_is_a_retryable_config_error():
    ctx = SkillContext(Invocation(skill="x@1", protocol="test"), env={})
    with pytest.raises(SkillError) as caught:
        _ = ctx.knowledge
    assert caught.value.code == "config_missing" and caught.value.retryable is True


# --- адаптер к control_plane_client ---


class _ClientError(Exception):
    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def _write(destination: Any, data: bytes) -> None:
    Path(destination).write_bytes(data)


class _FakeClient:
    instances: ClassVar[list[_FakeClient]] = []

    def __init__(self, url: str, credential: Any) -> None:
        self.url, self.credential = url, credential
        self.calls: list[tuple[str, str, dict[str, Any], bool]] = []
        self.entered = self.exited = 0
        self.fail: _ClientError | None = None
        self.pages: list[dict[str, Any]] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self) -> _FakeClient:
        self.entered += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.exited += 1

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, idempotent: bool = False
    ) -> Any:
        self.calls.append((method, path, json_body, idempotent))
        if self.fail is not None:
            raise self.fail
        if path == "/knowledge/entities:query":
            return self.pages.pop(0)
        return {"stateToken": "st:1", "opened": 1}

    async def download_artifact_content(
        self, artifact_id: str, destination: Any, *, for_task: str | None = None
    ) -> Any:
        _write(destination, b"data:" + artifact_id.encode())
        return {"mediaType": "text/plain", "sha256": "abc", "path": str(destination)}


@pytest.fixture
def client_module(monkeypatch):
    _FakeClient.instances.clear()
    package = types.ModuleType("control_plane_client")
    client = types.ModuleType("control_plane_client.client")
    client.ControlPlaneClient = _FakeClient
    credentials = types.ModuleType("control_plane_client.credentials")
    credentials.resolve_credential = lambda url: f"cred-for:{url}"
    monkeypatch.setitem(sys.modules, "control_plane_client", package)
    monkeypatch.setitem(sys.modules, "control_plane_client.client", client)
    monkeypatch.setitem(sys.modules, "control_plane_client.credentials", credentials)
    return _FakeClient


@pytest.mark.asyncio
async def test_adapter_speaks_the_core_routes(client_module):
    core = ControlPlaneCore("https://cp.example")
    snapshot = {"pack": "company@1", "source": "template:offering", "entities": []}
    assert (await core.knowledge.preview(snapshot, workspace_id="w1"))["stateToken"] == "st:1"
    await core.knowledge.apply(snapshot, workspace_id="w1", expected_state="st:1")
    await core.knowledge.document(
        workspace_id="w1",
        natural_key="doc:1",
        title="Лицензия",
        chunks=[{"text": "…"}],
        links=[{"kind": "credential", "key": "lic:1", "rel": "evidenced_by"}],
    )
    await core.knowledge.recall(
        anchor="legal_entity:7700000000", where=[{"attr": "okpd2", "op": "prefix", "value": "62"}]
    )
    content = await core.artifacts.read("art-9")
    await core.aclose()

    [client] = client_module.instances  # один клиент на вызов
    assert client.credential == "cred-for:https://cp.example"
    assert [(m, p, idem) for m, p, _, idem in client.calls] == [
        ("POST", "/knowledge/snapshots:preview", False),
        ("POST", "/knowledge/snapshots", True),
        ("POST", "/knowledge/documents", True),
        ("POST", "/context/recall", False),
    ]
    assert client.calls[0][2]["workspaceId"] == "w1" and "expectedState" not in client.calls[0][2]
    assert client.calls[1][2]["expectedState"] == "st:1"
    assert client.calls[2][2]["naturalKey"] == "doc:1" and client.calls[2][2]["type"] == "document"
    assert client.calls[3][2]["where"][0]["op"] == "prefix"
    assert content.data == b"data:art-9" and content.media_type == "text/plain"
    assert client.entered == 1 and client.exited == 1


@pytest.mark.asyncio
async def test_adapter_maps_snapshot_stale(client_module):
    core = ControlPlaneCore("https://cp.example")
    await core.knowledge.preview({}, workspace_id="w1")
    client_module.instances[0].fail = _ClientError("snapshot_stale", {"stateToken": "st:2"})
    with pytest.raises(SnapshotStale) as caught:
        await core.knowledge.apply({}, workspace_id="w1", expected_state="st:1")
    assert caught.value.state_token == "st:2"
    client_module.instances[0].fail = _ClientError("forbidden")
    with pytest.raises(_ClientError):
        await core.knowledge.preview({}, workspace_id="w1")
    await core.aclose()


@pytest.mark.asyncio
async def test_adapter_without_client_package_is_retryable(monkeypatch):
    monkeypatch.setitem(sys.modules, "control_plane_client", None)
    core = ControlPlaneCore("https://cp.example")
    with pytest.raises(SkillError) as caught:
        await core.knowledge.preview({}, workspace_id="w1")
    assert caught.value.code == "core_unavailable" and caught.value.retryable is True


@pytest.mark.asyncio
async def test_adapter_query_reads_every_page(client_module):
    """K032: ``query`` проходит все страницы маршрута ядра с тем же телом и курсором."""
    core = ControlPlaneCore("https://cp.example")
    await core.knowledge.preview({}, workspace_id="w1")  # клиент создан
    [client] = client_module.instances
    client.pages = [
        {"items": [{"kind": "credential", "key": "c:1"}], "nextCursor": "k1"},
        {"items": [], "nextCursor": "k2"},  # страница может быть пустой до конца
        {"items": [{"kind": "credential", "key": "c:2"}], "nextCursor": None},
    ]
    where = [{"attr": "validUntil", "op": "lte", "value": "2026-11-01"}]
    items = await core.knowledge.query(
        workspace_id="w1", kinds=["credential"], where=where, as_of="2026-10-01T00:00:00Z"
    )
    assert [i["key"] for i in items] == ["c:1", "c:2"]
    bodies = [body for _, path, body, _ in client.calls if path == "/knowledge/entities:query"]
    assert [b.get("cursor") for b in bodies] == [None, "k1", "k2"]
    assert all(
        b["workspaceId"] == "w1"
        and b["kinds"] == ["credential"]
        and b["where"] == where
        and b["asOf"] == "2026-10-01T00:00:00Z"
        and b["limit"] == 500
        for b in bodies
    )
    client.pages = [
        {"items": [{"kind": "credential", "key": f"c:{i}"} for i in range(3)], "nextCursor": "k"}
    ]
    with pytest.raises(SkillError) as caught:
        await core.knowledge.query(workspace_id="w1", kinds=["credential"], max_items=2)
    assert caught.value.code == "knowledge_query_too_large" and caught.value.retryable is False
    await core.aclose()


@pytest.mark.asyncio
async def test_fake_query_filters_applied_snapshots_like_memory():
    """Подделка перечисляет применённые снимки workspace с семантикой ``where`` памяти."""
    fake = FakeCore().knowledge
    await fake.apply(
        {
            "pack": "company@1",
            "source": "template:credential",
            "scope": "t",
            "entities": [
                {
                    "kind": "credential",
                    "key": "lic:1",
                    "title": "Лицензия",
                    "attributes": {"validUntil": "2026-10-20", "okpd2": ["62.01.11"]},
                },
                {
                    "kind": "credential",
                    "key": "lic:2",
                    "attributes": {"validUntil": "2027-05-01", "okpd2": ["62.011"]},
                },
                {"kind": "credential", "key": "lic:3", "attributes": {}},
                {"kind": "offering", "key": "o:1", "attributes": {"validUntil": "2026-10-01"}},
            ],
        },
        workspace_id="w1",
    )
    await fake.apply(
        {"pack": "company@1", "source": "x", "entities": [{"kind": "credential", "key": "lic:9"}]},
        workspace_id="w2",
    )
    soon = await fake.query(
        workspace_id="w1",
        kinds=["credential"],
        where=[{"attr": "validUntil", "op": "lte", "value": "2026-11-01"}],
    )
    assert [(i["key"], i["title"], i["source"]) for i in soon] == [
        ("lic:1", "Лицензия", "template:credential")
    ]
    by_code = await fake.query(
        workspace_id="w1",
        kinds=["credential"],
        where=[{"attr": "okpd2", "op": "prefix", "value": "62.01"}],
    )
    assert [i["key"] for i in by_code] == ["lic:1"]  # 62.011 — не 62.01
    missing = await fake.query(
        workspace_id="w1",
        kinds=["credential"],
        where=[{"attr": "validUntil", "op": "exists", "value": False}],
    )
    assert [i["key"] for i in missing] == ["lic:3"]
    assert [i["key"] for i in await fake.query(workspace_id="w1", kinds=["credential"])] == [
        "lic:1",
        "lic:2",
        "lic:3",
    ]
    assert fake.queries[0]["where"][0]["op"] == "lte"
    with pytest.raises(SkillError):
        await fake.query(workspace_id="w1", kinds=["credential"], max_items=2)
