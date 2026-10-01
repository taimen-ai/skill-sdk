"""Декларация, контракт, вызов и экспорт в пакет."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from skill_sdk import Http, Mcp, SkillError, configure_llm, discover, skill
from skill_sdk.package import drift, export
from skill_sdk.testing import check_contract, invoke
from tests import sample_skills as s

CONTROL_PLANE = Path(__file__).resolve().parents[2] / "control-plane" / "src"


# --- контракт -----------------------------------------------------------------


def test_contract_comes_from_models_and_decorator():
    contract = s.add.contract()
    assert s.add.ref == "math.add@1"
    assert s.add.description == "Сложить два числа."
    assert contract["inputs"]["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert contract["inputs"]["required"] == ["a", "b"]
    assert contract["inputs"]["properties"]["b"]["minimum"] == 0
    assert contract["outputs"]["required"] == ["sum"]
    assert contract["timeoutSeconds"] == 30
    assert contract["retryPolicy"] == {"maxAttempts": 1, "backoffSeconds": 0}
    assert contract["implementation"] == {
        "protocol": "local",
        "entrypoint": "tests.sample_skills:add",
    }
    assert s.add.__skill_contract__ == contract


def test_implementation_is_chosen_at_export():
    http = s.add.contract(Http("${SVC}/skills/math.add@1", audience="acme-skills"))
    assert http["implementation"] == {
        "protocol": "http",
        "endpoint": "${SVC}/skills/math.add@1",
        "auth": {"audience": "acme-skills"},
    }
    mcp = s.add.contract(Mcp("stdio:acme"))
    assert mcp["implementation"] == {
        "protocol": "mcp",
        "endpoint": "stdio:acme",
        "entrypoint": "math.add",
    }


@pytest.mark.skipif(not CONTROL_PLANE.exists(), reason="control-plane не лежит рядом")
@pytest.mark.parametrize("target", [s.add, s.classify, s.echo, s.write, s.summarize])
def test_contracts_pass_core_validators(target):
    sys.path.insert(0, str(CONTROL_PLANE))
    check_contract(target)
    check_contract(target, Http("https://skills.example/x", audience="acme"))


class In(BaseModel):
    x: int


def test_declaration_errors_are_caught_at_import():
    with pytest.raises(ValueError, match="не повторяется"):
        skill("bad.write", version="1", side_effects="external_write", risk="low", retry=(2, 0))(
            lambda inputs: inputs
        )
    with pytest.raises(TypeError, match="выход"):

        @skill("bad.out", version="1", side_effects="none", risk="low")
        def no_output(inputs: In):
            return {}

    with pytest.raises(TypeError, match="вход"):

        @skill("bad.in", version="1", side_effects="none", risk="low")
        def no_input(inputs: dict) -> In:
            return In(x=1)

    with pytest.raises(ValueError, match="side_effects"):
        skill("bad", version="1", side_effects="sometimes", risk="low")(s.echo.function)


# --- вызов --------------------------------------------------------------------


def test_invoke_validates_and_passes_the_context():
    result = invoke(s.add, {"a": 2, "b": 3}, idempotency_key="k-1")
    assert result.outputs == {"sum": 5, "note": "k-1"}
    assert result.cost == {"units": {"ops": 1.0}}


def test_async_skill_without_context():
    assert invoke(s.classify, {"text": "hi"}).outputs == {"kind": "short"}


def test_explicit_schemas_are_validated_too():
    assert invoke(s.echo, {"x": "a"}).outputs == {"x": "a"}
    with pytest.raises(SkillError) as error:
        invoke(s.echo, {"y": 1})
    assert error.value.code == "input_contract_violation"


def test_input_violation_names_the_paths():
    with pytest.raises(SkillError) as error:
        invoke(s.add, {"a": "x", "b": -1})
    assert error.value.code == "input_contract_violation"
    assert error.value.retryable is False
    paths = {e["path"] for e in error.value.details["errors"]}
    assert {"/a", "/b"} <= paths


def test_output_violation_is_not_retryable():
    with pytest.raises(SkillError) as error:
        invoke(s.write, {"text": "bad-output"})
    assert (error.value.code, error.value.retryable) == ("output_contract_violation", False)


def test_errors_keep_code_and_retryability():
    with pytest.raises(SkillError) as busy:
        invoke(s.write, {"text": "busy"})
    assert (busy.value.code, busy.value.retryable) == ("upstream_busy", True)
    with pytest.raises(SkillError) as denied:
        invoke(s.write, {"text": "denied"})
    assert denied.value.as_error() == {
        "code": "denied",
        "message": "отказано",
        "retryable": False,
        "details": {"why": "policy"},
    }


def test_missing_secret_is_retryable_and_present_one_is_read():
    with pytest.raises(SkillError) as error:
        invoke(s.write, {"text": "ok"}, env={})
    assert (error.value.code, error.value.retryable) == ("config_missing", True)
    assert invoke(s.write, {"text": "ok"}, env={"EXT_TOKEN": "t"}).outputs == {"kind": "short"}


def test_legacy_call_and_wire_contract_of_the_local_executor():
    assert s.add({"a": 1, "b": 1}) == {"sum": 2, "note": None}
    answer = s.add.__skill_invoke__({"a": 1, "b": 1}, {"invocationId": "i", "idempotencyKey": "k"})
    assert answer == {"outputs": {"sum": 2, "note": "k"}, "cost": {"units": {"ops": 1.0}}}


# --- LLM ----------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class _Result:
    data: Any
    model: str
    usage: _Usage
    cost_usd: float | None


class FakeLlm:
    closed = False

    async def chat_json(self, *, response_model, **_kwargs):
        return _Result(response_model(summary="кратко"), "model-a", _Usage(10, 5, 15), 0.002)

    async def aclose(self):
        FakeLlm.closed = True


def test_llm_usage_becomes_the_cost():
    configure_llm(FakeLlm)
    try:
        result = invoke(s.summarize, {"text": "длинный текст"})
    finally:
        configure_llm(None)
    assert result.outputs == {"summary": "кратко"}
    assert result.cost == {
        "llm": {
            "calls": 1,
            "promptTokens": 10,
            "completionTokens": 5,
            "totalTokens": 15,
            "models": ["model-a"],
            "costUsd": 0.002,
        }
    }
    assert FakeLlm.closed


def test_llm_without_configuration_is_retryable(monkeypatch):
    for name in ("SKILL_LLM_BASE_URL", "SKILL_LLM_API_KEY", "SKILL_LLM_MODELS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SkillError) as error:
        invoke(s.summarize, {"text": "x"})
    assert error.value.code in {"llm_not_configured", "llm_unavailable"}
    assert error.value.retryable is True


# --- пакет --------------------------------------------------------------------


def test_discover_finds_every_skill_in_a_module():
    refs = [item.ref for item in discover(["tests.sample_skills"])]
    assert refs == [
        "doc.classify@2",
        "doc.summarize@1",
        "doc.summarize_later@1",
        "doc.summarize_sync@1",
        "ext.write@1",
        "legacy.echo@1",
        "math.add@1",
    ]
    assert discover(["tests.sample_skills:add"]) == [s.add]


def test_export_and_drift(tmp_path):
    [path] = export([s.add], tmp_path)
    # Шапка файла — по-английски, как всё, что пишется в пакет автора (TASK-001198).
    assert path.read_text().splitlines()[0] == (
        "# Generated by skill-sdk from the code (TAI-ADR-0045): edit the code and export again."
    )
    document = yaml.safe_load(path.read_text())
    assert document["kind"] == "Skill" and document["key"] == "math.add"
    assert document["spec"]["contract"] == s.add.contract()
    assert drift([s.add], tmp_path) == []

    document["spec"]["riskLevel"] = "high"
    path.write_text(yaml.safe_dump(document, allow_unicode=True))
    [problem] = drift([s.add], tmp_path)
    assert "riskLevel" in problem
    assert "нет файла" in drift([s.echo], tmp_path)[0]
