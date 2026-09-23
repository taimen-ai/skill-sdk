"""Сквозь настоящий исполнитель control-plane: скилл SDK по local, http и mcp.

Запускается там, где установлен control-plane (его окружение и суперпроект):

    cd control-plane && PYTHONPATH=../skill-sdk/src:../skill-sdk \
        uv run pytest -q -c ../skill-sdk/pyproject.toml ../skill-sdk/tests/test_executor_e2e.py

В окружении самого skill-sdk исполнителя нет — тест пропускается.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

skills_module = pytest.importorskip("control_plane_agent.skills")

from skill_sdk.http import create_app  # noqa: E402
from skill_sdk.mcp import create_server  # noqa: E402
from tests import sample_skills as s  # noqa: E402


class FakeControlPlane:
    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    async def heartbeat_skill_invocation(self, invocation_id: str, **_: Any) -> dict:
        return {
            "id": invocation_id,
            "leaseExpiresAt": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        }

    async def complete_skill_invocation(self, invocation_id: str, **kwargs: Any) -> dict:
        self.completed.append(kwargs)
        return {"status": "succeeded"}

    async def fail_skill_invocation(self, invocation_id: str, **kwargs: Any) -> dict:
        self.failed.append(kwargs)
        return {"status": "failed"}


def claimed(skill: Any, implementation: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    contract = skill.contract()
    return {
        "invocation": {
            "id": "inv-1",
            "fencingToken": 1,
            "idempotencyKey": "idem-7",
            "inputs": inputs,
            "leaseExpiresAt": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        },
        "skill": {
            "name": skill.name,
            "version": skill.version,
            "contract": {**contract, "implementation": implementation},
        },
    }


async def run(handler_name: str, handler: Any, claim: dict[str, Any]) -> FakeControlPlane:
    fake = FakeControlPlane()
    executor = skills_module.SkillExecutor(fake, {handler_name: handler}, heartbeat_interval=0.05)
    await executor.execute_claimed(claim, "session-1")
    return fake


@pytest.mark.parametrize("isolation", ["process", "thread"])
async def test_local(isolation: str) -> None:
    [entrypoint] = skills_module.discover_local_entrypoints(["tests.sample_skills:add"])
    assert entrypoint == "tests.sample_skills:add"
    assert "tests.sample_skills:add" in skills_module.discover_local_entrypoints(
        ["tests.sample_skills"]
    )
    handler = skills_module.LocalProtocol([entrypoint], isolation=isolation)
    fake = await run(
        "local", handler, claimed(s.add, s.add.contract()["implementation"], {"a": 1, "b": 2})
    )
    assert fake.failed == []
    [done] = fake.completed
    assert done["output"] == {"sum": 3, "note": "idem-7"}
    assert done["cost"] == {"units": {"ops": 1.0}}


def http_policy() -> Any:
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    return skills_module.EndpointPolicy(
        origins=frozenset({"https://skills.test"}), audiences=frozenset(), resolve=resolve
    )


@pytest.mark.parametrize(
    ("text", "code", "retryable"), [("busy", "upstream_busy", True), ("denied", "denied", False)]
)
async def test_http(text: str, code: str, retryable: bool) -> None:
    app = create_app([s.add, s.write], allow_anonymous=True)
    handler = skills_module.HttpProtocol(
        policy=http_policy(), transport=httpx.ASGITransport(app=app)
    )
    ok = await run(
        "http",
        handler,
        claimed(
            s.add,
            {"protocol": "http", "endpoint": "https://skills.test/skills/math.add@1"},
            {"a": 2, "b": 2},
        ),
    )
    assert ok.completed[0]["output"] == {"sum": 4, "note": "idem-7"}
    assert ok.completed[0]["cost"] == {"units": {"ops": 1.0}}

    failed = await run(
        "http",
        handler,
        claimed(
            s.write,
            {"protocol": "http", "endpoint": "https://skills.test/skills/ext.write@1"},
            {"text": text},
        ),
    )
    [failure] = failed.failed
    assert (failure["code"], failure["retryable"]) == (code, retryable)


async def test_mcp() -> None:
    server = create_server([s.add, s.write])
    handler = skills_module.McpProtocol(connect=lambda implementation: server)
    mcp = {"protocol": "mcp", "endpoint": "stdio:skills"}
    ok = await run(
        "mcp", handler, claimed(s.add, {**mcp, "entrypoint": "math.add"}, {"a": 5, "b": 1})
    )
    assert ok.completed[0]["output"] == {
        "sum": 6,
        "note": None,
    }  # ключ идемпотентности MCP не несёт
    assert ok.completed[0]["cost"] == {"units": {"ops": 1.0}}
    busy = await run(
        "mcp", handler, claimed(s.write, {**mcp, "entrypoint": "ext.write"}, {"text": "busy"})
    )
    assert (busy.failed[0]["code"], busy.failed[0]["retryable"]) == ("upstream_busy", True)
