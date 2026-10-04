"""``ctx.settings``: настройки пакета скилла из контекста вызова (CP-ADR-0081 В3)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from skill_sdk import SkillContext, skill
from skill_sdk.context import package_settings
from skill_sdk.http import create_app
from skill_sdk.mcp import IDEMPOTENCY_META, SETTINGS_META, create_server
from skill_sdk.testing import invoke

SETTINGS = {
    "package": "invoice-payment",
    "version": 3,
    "schemaRevision": 2,
    "values": {"approvalThreshold": 1000, "reviewers": {"role": "finance"}},
}


@skill(
    "pkg.settings",
    version="1",
    description="Настройки пакета, которые видит скилл",
    side_effects="none",
    risk="low",
    inputs_schema={"type": "object"},
    outputs_schema={"type": "object", "required": ["settings"]},
)
def show(inputs: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
    return {"settings": dict(ctx.settings)}


# --- разбор контекста вызова --------------------------------------------------


def test_values_of_the_package_are_the_settings():
    assert package_settings(SETTINGS) == SETTINGS["values"]


@pytest.mark.parametrize(
    "raw",
    [
        None,  # скилл не из пакета или пакет без настроек
        {},
        "settings",
        ["values"],
        {"package": "p", "version": 0},  # без values
        {"values": None},
        {"values": [1, 2]},
        {"values": "x"},
        {"values": {}},
    ],
)
def test_no_settings_is_an_empty_mapping(raw):
    assert package_settings(raw) == {}


def test_settings_are_a_read_only_copy_per_invocation():
    raw = {"values": {"limit": 1, "nested": {"a": [1]}}}
    first, second = package_settings(raw), package_settings(raw)
    with pytest.raises(TypeError):
        first["limit"] = 2  # type: ignore[index]
    first["nested"]["a"].append(2)  # вложенное — копия: ни ядру, ни другому вызову не видно
    assert raw["values"]["nested"] == {"a": [1]}
    assert second["nested"] == {"a": [1]}


# --- testing.invoke и прямой вызов --------------------------------------------


def test_invoke_passes_settings_and_without_them_they_are_empty():
    values = SETTINGS["values"]
    assert invoke(show, {}, settings=values).outputs == {"settings": values}
    assert invoke(show, {}).outputs == {"settings": {}}
    assert invoke(show, {}, settings={}).outputs == {"settings": {}}


async def test_direct_execute_has_no_settings():
    outputs, _cost = await show.execute({})
    assert outputs == {"settings": {}}


# --- local: meta вызова __skill_invoke__ ---------------------------------------


def test_local_settings_come_from_the_meta():
    answer = show.__skill_invoke__({}, {"invocationId": "i", "settings": SETTINGS})
    assert answer["outputs"] == {"settings": SETTINGS["values"]}


@pytest.mark.parametrize("meta", [{}, {"settings": None}, {"settings": "broken"}])
def test_local_without_settings_they_are_empty(meta):
    assert show.__skill_invoke__({}, meta)["outputs"] == {"settings": {}}
    assert show({}) == {"settings": {}}


# --- http: член settings тела --------------------------------------------------


def _client() -> httpx.AsyncClient:
    app = create_app([show], allow_anonymous=True)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://skills")


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"settings": SETTINGS}, SETTINGS["values"]),
        ({"settings": None}, {}),
        ({}, {}),  # ядро старше CP-ADR-0081
        ({"settings": 42}, {}),
    ],
)
async def test_http_settings_come_from_the_body(extra, expected):
    body = {"invocationId": "inv-1", "idempotencyKey": "idem-1", "inputs": {}, **extra}
    async with _client() as http:
        response = await http.post("/skills/pkg.settings@1", json=body)
    assert response.status_code == 200
    assert response.json() == {"settings": expected}


# --- mcp: _meta["skill/settings"] ----------------------------------------------


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({SETTINGS_META: SETTINGS, IDEMPOTENCY_META: "idem-1"}, SETTINGS["values"]),
        ({IDEMPOTENCY_META: "idem-1"}, {}),  # клиент MCP не шлёт null: нет ключа — нет настроек
        (None, {}),
    ],
)
async def test_mcp_settings_come_from_the_request_meta(meta, expected):
    from mcp.client import Client

    async with Client(create_server([show])) as mcp:
        result = await mcp.call_tool("pkg.settings", {}, meta=meta)
    assert not result.is_error
    assert result.structured_content == {"settings": expected}
