"""HTTP-хостинг скиллов: ASGI-приложение по протоколу ``http`` CP-ADR-0056 §5.

    POST /skills/{name}@{version}   {invocationId, idempotencyKey, inputs} → outputs

- 200 — тело и есть ``outputs`` (без конверта), cost — в заголовке ``X-Skill-Cost``;
- ``SkillError`` — ``{"error": {code, message, retryable, details}}``: 503, если
  повторяемая, иначе 422 (исполнитель берёт ``code`` из тела и повторяемость — из статуса);
- 401 — токен не прошёл проверку, 404 — такого скилла здесь нет, 500 — сбой реализации.
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

logger = logging.getLogger("skill_sdk.http")
COST_HEADER = "X-Skill-Cost"


def endpoint_path(skill: Skill) -> str:
    return f"/skills/{skill.ref}"


def _index(skills: Iterable[Skill]) -> dict[str, Skill]:
    index: dict[str, Skill] = {}
    for item in skills:
        if item.ref in index:
            raise ValueError(f"{item.ref} зарегистрирован дважды")
        index[item.ref] = item
    return index


def create_app(
    skills: Iterable[Skill], *, verifier: Any = None, allow_anonymous: bool = False
) -> Any:
    """Starlette-приложение. ``verifier`` — ``platform_auth.TokenVerifier`` audience скилла."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    require_verifier(verifier, allow_anonymous)
    index = _index(skills)

    def error(status: int, failure: SkillError) -> JSONResponse:
        return JSONResponse({"error": failure.as_error()}, status_code=status)

    async def invoke(request: Request) -> JSONResponse:
        try:
            caller = await authenticate(verifier, request.headers.get("authorization"))
        except Unauthorized as denied:
            return error(denied.status, SkillError(denied.code, retryable=denied.status >= 500))
        target = index.get(request.path_params["ref"])
        if target is None:
            return error(
                404,
                SkillError("skill_not_hosted", f"{request.path_params['ref']} здесь не хостится"),
            )
        try:
            body = await request.json()
        except ValueError:
            return error(
                400, SkillError("bad_request", "тело — JSON {invocationId, idempotencyKey, inputs}")
            )
        if not isinstance(body, dict) or not isinstance(body.get("inputs"), dict):
            return error(
                400, SkillError("bad_request", "тело — JSON {invocationId, idempotencyKey, inputs}")
            )
        invocation = Invocation(
            skill=target.ref,
            protocol="http",
            invocation_id=body.get("invocationId"),
            idempotency_key=body.get("idempotencyKey"),
            timeout_seconds=target.timeout,
            caller=caller,
        )
        try:
            outputs, cost = await target.execute(body["inputs"], invocation)
        except SkillError as known:
            return error(503 if known.retryable else 422, known)
        except Exception as exc:
            logger.exception("skill %s failed", target.ref)
            crash = from_exception(exc)
            return error(503 if crash.retryable else 500, crash)
        headers = {COST_HEADER: json.dumps(cost, separators=(",", ":"))} if cost else None
        return JSONResponse(outputs, headers=headers)

    async def listing(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "skills": [
                    {"ref": s.ref, "name": s.name, "version": s.version, "path": endpoint_path(s)}
                    for s in index.values()
                ]
            }
        )

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/skills/{ref}", invoke, methods=["POST"]),
            Route("/skills", listing, methods=["GET"]),
            Route("/healthz", health, methods=["GET"]),
        ]
    )


def serve(
    skills: Iterable[Skill],
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    allow_anonymous: bool = False,
) -> None:
    import uvicorn

    uvicorn.run(
        create_app(skills, verifier=verifier_from_env(), allow_anonymous=allow_anonymous),
        host=host,
        port=port,
    )
