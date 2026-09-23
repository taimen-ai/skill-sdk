"""HTTP- и MCP-хостинг: протокол исполнителя, проверка IAM-токена, ошибки и cost."""

from __future__ import annotations

import json

import httpx
import pytest
from platform_auth import StaticKeySet, TokenVerifier, VerifierConfig
from platform_auth.testing import SigningKey

from skill_sdk.http import create_app
from skill_sdk.mcp import COST_META, create_server, http_app
from tests import sample_skills as s

ISSUER, AUDIENCE = "https://iam.test", "acme-skills"
SKILLS = [s.add, s.write, s.classify]


@pytest.fixture(scope="module")
def key() -> SigningKey:
    return SigningKey.generate("k1")


@pytest.fixture
def verifier(key: SigningKey) -> TokenVerifier:
    keys = StaticKeySet(key.public_pem, key_id=key.key_id)
    return TokenVerifier(keys, VerifierConfig(issuer=ISSUER, audience=AUDIENCE))


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://skills")


def body(inputs, key="idem-1"):
    return {"invocationId": "inv-1", "idempotencyKey": key, "inputs": inputs}


# --- http ---------------------------------------------------------------------


def test_http_refuses_to_start_without_token_check():
    with pytest.raises(RuntimeError, match="без проверки токена"):
        create_app(SKILLS)


async def test_http_invocation_with_a_valid_token(key, verifier):
    token = key.issue(issuer=ISSUER, audience=AUDIENCE, principal_type="agent")
    async with client(create_app(SKILLS, verifier=verifier)) as http:
        response = await http.post(
            "/skills/math.add@1",
            json=body({"a": 2, "b": 3}),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json() == {"sum": 5, "note": "idem-1"}  # тело — outputs без конверта
    assert json.loads(response.headers["X-Skill-Cost"]) == {"units": {"ops": 1.0}}


@pytest.mark.parametrize("audience", ["control-plane", AUDIENCE])
async def test_http_rejects_a_foreign_or_missing_token(key, verifier, audience):
    headers = (
        {}
        if audience == AUDIENCE
        else {"Authorization": f"Bearer {key.issue(issuer=ISSUER, audience=audience)}"}
    )
    async with client(create_app(SKILLS, verifier=verifier)) as http:
        response = await http.post(
            "/skills/math.add@1", json=body({"a": 1, "b": 1}), headers=headers
        )
    assert response.status_code == 401
    assert response.json()["error"]["retryable"] is False


@pytest.mark.parametrize(
    ("text", "status", "code", "retryable"),
    [
        ("busy", 503, "upstream_busy", True),
        ("denied", 422, "denied", False),
        ("crash", 500, "skill_error", False),
        ("bad-output", 422, "output_contract_violation", False),
    ],
)
async def test_http_errors_carry_code_and_retryability(text, status, code, retryable):
    async with client(create_app(SKILLS, allow_anonymous=True)) as http:
        response = await http.post("/skills/ext.write@1", json=body({"text": text}))
    assert response.status_code == status
    error = response.json()["error"]
    assert (error["code"], error["retryable"]) == (code, retryable)


async def test_http_input_violation_unknown_skill_and_bad_body():
    async with client(create_app(SKILLS, allow_anonymous=True)) as http:
        invalid = await http.post("/skills/math.add@1", json=body({"a": "x"}))
        unknown = await http.post("/skills/math.add@9", json=body({}))
        broken = await http.post("/skills/math.add@1", content=b"not json")
        listing = await http.get("/skills")
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (
        422,
        "input_contract_violation",
    )
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "skill_not_hosted")
    assert broken.status_code == 400
    assert {item["ref"] for item in listing.json()["skills"]} == {
        s.add.ref,
        s.write.ref,
        s.classify.ref,
    }


# --- mcp ----------------------------------------------------------------------


async def test_mcp_tools_list_and_call():
    from mcp.client import Client

    async with Client(create_server(SKILLS)) as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
        result = await mcp.call_tool("math.add", {"a": 2, "b": 2})
    assert set(tools) == {"math.add", "ext.write", "doc.classify"}
    assert tools["math.add"].input_schema["required"] == ["a", "b"]
    assert result.structured_content == {"sum": 4, "note": None}
    assert result.meta[COST_META] == {"units": {"ops": 1.0}}


async def test_mcp_error_is_an_envelope():
    from mcp.client import Client

    async with Client(create_server(SKILLS)) as mcp:
        result = await mcp.call_tool("ext.write", {"text": "busy"})
    assert result.is_error
    [block] = result.content
    assert json.loads(block.text)["error"] == {
        "code": "upstream_busy",
        "message": "позже",
        "retryable": True,
        "details": None,
    }


def test_mcp_refuses_two_versions_of_one_tool():
    with pytest.raises(ValueError, match="две версии"):
        create_server(
            [
                s.add,
                s.add.__class__(
                    s.add.function,
                    name="math.add",
                    version="2",
                    side_effects="none",
                    risk="low",
                    idempotency="none",
                    timeout=30,
                    retry=(1, 0),
                    description=None,
                    permissions=(),
                    preconditions=(),
                    postconditions=(),
                    cost_model=None,
                    implementation=None,
                    inputs_schema=None,
                    outputs_schema=None,
                ),
            ]
        )


async def test_mcp_http_checks_the_token_before_the_session(key, verifier):
    app = http_app(SKILLS, verifier=verifier)
    async with client(app) as http:
        response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"missing_authorization", "invalid_token"}
