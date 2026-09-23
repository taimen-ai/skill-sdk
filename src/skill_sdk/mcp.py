"""MCP-хостинг скиллов: каждый скилл — инструмент (протокол ``mcp`` CP-ADR-0056 §5).

Имя инструмента — имя скилла (``implementation.entrypoint`` контракта). Успех —
``structuredContent`` = outputs и тот же JSON текстовым блоком для клиентов без
structured content; cost — в ``_meta["ai.taimen/cost"]``. ``SkillError`` —
``isError`` с единственным текстовым блоком ``{"error": {code, message, retryable,
details}}``: по нему исполнитель узнаёт код и повторяемость.

Транспорты: stdio (исполнитель запускает сервер из своей конфигурации,
``stdio:<имя>``) и streamable HTTP (Bearer-токен IAM, как у ``http``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from skill_sdk.auth import Unauthorized, authenticate, require_verifier, verifier_from_env
from skill_sdk.context import Invocation
from skill_sdk.errors import SkillError, from_exception
from skill_sdk.skill import Skill

logger = logging.getLogger("skill_sdk.mcp")
COST_META = "ai.taimen/cost"


def _tools(skills: Iterable[Skill]) -> dict[str, Skill]:
    tools: dict[str, Skill] = {}
    for item in skills:
        if item.name in tools:
            raise ValueError(f"MCP-инструмент {item.name}: две версии скилла на одном сервере")
        tools[item.name] = item
    return tools


def create_server(skills: Iterable[Skill], *, name: str = "skills") -> Any:
    """Низкоуровневый ``mcp.server.lowlevel.Server`` со скиллами как инструментами."""
    from mcp import types
    from mcp.server.lowlevel import Server

    tools = _tools(skills)

    def failure_result(failure: SkillError) -> Any:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    text=json.dumps({"error": failure.as_error()}, ensure_ascii=False)
                )
            ],
            is_error=True,
        )

    async def list_tools(_ctx: Any, _params: Any) -> Any:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool,
                    description=item.description,
                    input_schema=item.inputs_schema,
                    output_schema=item.outputs_schema,
                )
                for tool, item in tools.items()
            ]
        )

    async def call_tool(_ctx: Any, params: Any) -> Any:
        target = tools.get(params.name)
        if target is None:
            return failure_result(
                SkillError("skill_not_hosted", f"{params.name} здесь не хостится")
            )
        invocation = Invocation(skill=target.ref, protocol="mcp", timeout_seconds=target.timeout)
        try:
            outputs, cost = await target.execute(params.arguments or {}, invocation)
        except SkillError as failure:
            return failure_result(failure)
        except Exception as exc:
            logger.exception("skill %s failed", target.ref)
            return failure_result(from_exception(exc))
        return types.CallToolResult(
            content=[types.TextContent(text=json.dumps(outputs, ensure_ascii=False))],
            structured_content=outputs,
            meta={COST_META: cost} if cost else None,  # type: ignore[call-arg]  # alias _meta
        )

    return Server(name, on_list_tools=list_tools, on_call_tool=call_tool)


def http_app(
    skills: Iterable[Skill],
    *,
    verifier: Any = None,
    allow_anonymous: bool = False,
    path: str = "/mcp",
    host: str = "0.0.0.0",
) -> Any:
    """Streamable HTTP с проверкой Bearer-токена до MCP-сессии."""
    from starlette.responses import JSONResponse

    require_verifier(verifier, allow_anonymous)
    inner = create_server(skills).streamable_http_app(
        streamable_http_path=path, stateless_http=True, json_response=True, host=host
    )

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            try:
                await authenticate(verifier, headers.get("authorization"))
            except Unauthorized as denied:
                failure = SkillError(denied.code, retryable=denied.status >= 500)
                await JSONResponse({"error": failure.as_error()}, status_code=denied.status)(
                    scope, receive, send
                )
                return
        await inner(scope, receive, send)

    return app


async def serve_stdio(skills: Iterable[Skill], *, name: str = "skills") -> None:
    from mcp.server.stdio import stdio_server

    server = create_server(skills, name=name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def serve_http(
    skills: Iterable[Skill],
    *,
    host: str = "0.0.0.0",
    port: int = 8081,
    allow_anonymous: bool = False,
) -> None:
    import uvicorn

    uvicorn.run(
        http_app(skills, verifier=verifier_from_env(), allow_anonymous=allow_anonymous, host=host),
        host=host,
        port=port,
    )
