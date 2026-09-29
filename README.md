*English. Russian version: [README.ru.md](README.ru.md)*

# skill-sdk

The skill SDK of the Taimen platform. A skill is written once, in code, and the
SDK provides everything else:

- the v1 contract (CP-ADR-0056);
- the invocation context;
- hosting over all three executor protocols — `local`, `http`, `mcp`;
- the YAML for a catalog package (TAI-ADR-0044).

The decision is TAI-ADR-0045 of the superproject.

```python
from typing import Literal

from pydantic import BaseModel
from skill_sdk import SkillContext, SkillError, skill


class MergeIn(BaseModel):
    repository: str
    branch: str
    commit: str
    target: str


class MergeOut(BaseModel):
    merged: bool
    sha: str | None = None
    reason: Literal["conflict", "branch_moved", "already_merged"] | None = None


@skill(
    "git.merge",
    version="2",
    side_effects="external_write",
    risk="medium",
    idempotency="natural",
    timeout=300,
    retry=(3, 30),
)
def merge(inputs: MergeIn, ctx: SkillContext) -> MergeOut:
    """Merge a published branch into the target branch."""
    ...
    if conflict:
        return MergeOut(merged=False, reason="conflict")  # a contract outcome is an output
    raise SkillError("git_unavailable", "remote unavailable", retryable=True)  # a failure is an error
```

## The contract comes from code

- **Inputs and outputs** come from the pydantic models in the annotations: the
  first argument and the return value. When a model does not fit (for example, an
  already published version has its own JSON Schema), pass the schema explicitly
  with `inputs_schema=` and `outputs_schema=`.
- **Policy** comes from the decorator arguments: `side_effects` (`none`,
  `external_read` or `external_write`), `risk`, `idempotency`, `timeout`,
  `retry=(maxAttempts, backoffSeconds)`, `permissions`, `preconditions`,
  `postconditions`, `cost_model`.
- **Description** is the first paragraph of the docstring.
- **Default implementation** is `local` with the entrypoint `module:name` of the
  function itself. `http` and `mcp` are set at export: how to host is a decision of
  the installation.

The SDK rejects at import time what the core would reject, for example
`external_write` with retries but without idempotency.

## Invocation

The function takes `(inputs)` or `(inputs, ctx)` and may be synchronous or
`async`. The SDK validates the input against the contract schema, passes the
model, validates the output and serializes it. A contract violation is
`input_contract_violation` or `output_contract_violation`.

`SkillContext`:

| | |
|---|---|
| `ctx.invocation_id`, `ctx.idempotency_key` | which invocation this is; a retry with the same key must not cause a second external effect |
| `ctx.remaining()`, `ctx.check_deadline()` | time left until the contract timeout |
| `ctx.log` | a logger carrying the invocation id |
| `ctx.config(name)`, `ctx.secret(name)` | hosting parameters and secrets; a missing secret is a retryable `config_missing` |
| `ctx.llm` | an LLM client configured by the installation (`platform-llm` or Claude on a subscription); tokens are counted automatically; personal data of individuals in the prompt (full names, SNILS, passport, phone, e-mail) is replaced with a marker `[<PD>:kind]` before the model is called (the tag is the Russian abbreviation for personal data, see `skill_sdk.pii.marker`), only counts are logged (`skill_sdk.pii`) |
| `ctx.add_cost(unit, amount)` | the skill's own consumption; goes into the invocation `cost` together with LLM tokens |
| `ctx.caller` | the verified caller context (http) |
| `ctx.artifacts.read(id)` | artifact content through the core, with the skills executor's credential (`ArtifactContent`: `data`, `media_type`, `text()`) |
| `ctx.knowledge` | the knowledge base through the core: `preview(snapshot, workspace_id=…)` — a plan without writing plus `stateToken`; `apply(snapshot, workspace_id=…, expected_state=…)` — apply only if the state has not changed, otherwise `SnapshotStale`; `document(…)` — a document with chunks and links; `recall(**query)` — typed traversal with `where` |

There is deliberately no Control Plane client in the context: a skill does not
create or move tasks. Approval outcomes and core rules do that (TAI-ADR-0041).

LLM: the provider is chosen by `SKILL_LLM_PROVIDER`. An arbitrary provider is set
in code with `skill_sdk.configure_llm(factory)`.

| `SKILL_LLM_PROVIDER` | What it is | Configuration |
|---|---|---|
| `openai` (default) | `platform_llm.OpenAICompatibleClient` — any OpenAI-compatible `/chat/completions` | `SKILL_LLM_BASE_URL`, `SKILL_LLM_API_KEY`, `SKILL_LLM_MODELS` (comma-separated) |
| `claude-code` | Claude on a subscription through the Claude Code CLI (`claude -p`), as coding agents use it | `CLAUDE_CODE_OAUTH_TOKEN` in the environment, `SKILL_LLM_MODELS` (Claude aliases: `sonnet`, `claude-sonnet-5`…; empty — the CLI default), `SKILL_LLM_TIMEOUT_SECONDS` (300), `SKILL_LLM_CLAUDE_BINARY` (`claude`) |

How `claude-code` works (`skill_sdk.claude_code.ClaudeCodeLlm`):

- plain text completion: `--tools ""`, no MCP servers, no session persistence,
  an empty temporary working directory;
- the skill's system prompt, the messages and the response JSON schema (from the
  pydantic model) are rendered into one prompt sent over **stdin**; argv carries
  only a constant neutral system prompt;
- the reply is `--output-format json`; JSON is taken from the model's text (as is,
  from a ```` ```json ```` block or by braces) and validated against the response
  model; on failure — one retry quoting the error, then the next model in the list,
  then a retryable `llm_invalid_response`;
- the CLI inherits the subscription token from the process environment; the SDK
  never reads or logs it and redacts it from error text. `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` are not passed to the child, otherwise the call would go
  through an API key rather than the subscription;
- a subscription window limit or 429 is a retryable `llm_rate_limited` at once (the
  limit is shared by all models); no CLI — `llm_unavailable`, timeout —
  `llm_timeout`, any other CLI failure — `llm_failed`, all retryable;
- `cost` gets the tokens; the CLI's notional price under a subscription is not spend.

## Hosting

| Protocol | How | What the executor sees |
|---|---|---|
| `local` | the package is installed next to the daemon, `CONTROL_PLANE_SKILLS_LOCAL_PACKAGES=<package>` | the executor finds SDK skills itself, calls `__skill_invoke__` and gets `{outputs, cost}` |
| `http` | `skill-sdk serve http my_skills` or `skill_sdk.http.create_app(...)` in your own ASGI app | `POST /skills/{name}@{version}`; 200 — outputs, cost in `X-Skill-Cost`; an error is `{"error": {code, retryable, …}}` |
| `mcp` | `skill-sdk serve mcp-stdio` or `mcp-http` | a tool named after the skill; cost in `_meta["skill/cost"]`; an error is `isError` with the same `{"error": …}` |

`http` and `mcp-http` verify an IAM token of the skill's audience through
`platform-auth-sdk` (`SKILL_SDK_IAM_ISSUER`, `SKILL_SDK_AUDIENCE`,
`SKILL_SDK_JWKS_URL`). Without verification hosting does not start, except with an
explicit `--allow-anonymous` for development.

## Catalog package

```bash
skill-sdk export --package ../packages/selfdev taimen_selfdev           # write skills/*.yaml
skill-sdk export --package ../packages/selfdev --check taimen_selfdev   # CI: code == YAML
skill-sdk export --package ../packages/acme --protocol http \
    --endpoint '${ACME_SKILLS_URL}' --audience acme-skills acme_skills  # hosted over http
```

YAML marked as generated is never edited by hand. A version contract is immutable
in the core: changing the contract means a new `version` in the decorator.

## Testing a skill

```python
from skill_sdk.testing import check_contract, invoke


def test_merge_contract():
    check_contract(merge)  # core validators, when control-plane is next to it


def test_conflict_is_an_outcome():
    result = invoke(
        merge, {"repository": "…", "branch": "b", "commit": "abc1234", "target": "main"}
    )
    assert result.outputs["reason"] == "conflict"
```

## Installation

```bash
uv add skill-sdk                    # contract, local, testing, export
uv add "skill-sdk[http]"            # + ASGI hosting and token verification
uv add "skill-sdk[mcp]"             # + MCP server
uv add "skill-sdk[llm]"             # + ctx.llm
```

`platform-auth-sdk` and `platform-llm` are sibling folders (the flat layout of the
superproject). Tests: `uv run pytest -q`.

## Licence

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party
components are listed in [THIRD_PARTY.md](THIRD_PARTY.md) (`sbom.json`, CycloneDX).
