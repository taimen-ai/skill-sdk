"""``skill-sdk`` — контракт, экспорт в пакет, вызов и хостинг скиллов из командной строки.

    skill-sdk list     taimen_selfdev
    skill-sdk contract taimen_selfdev.git_merge:run
    skill-sdk export   --package packages/selfdev taimen_selfdev            # записать YAML
    skill-sdk export   --package packages/selfdev --check taimen_selfdev    # CI: код == YAML
    skill-sdk invoke   taimen_selfdev.adr_conformance:invoke --input '{"repository": "."}'
    skill-sdk serve http      --port 8080 my_skills
    skill-sdk serve mcp-http  --port 8081 my_skills
    skill-sdk serve mcp-stdio my_skills

TARGET — ``модуль:имя``, модуль или пакет (все скиллы в нём и в подмодулях).
Хостинг http и mcp-http проверяет токен IAM: ``SKILL_SDK_IAM_ISSUER``,
``SKILL_SDK_AUDIENCE``, ``SKILL_SDK_JWKS_URL`` (без них — только ``--allow-anonymous``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from skill_sdk import package
from skill_sdk.errors import SkillError
from skill_sdk.skill import Http, Implementation, Local, Mcp, Skill, discover
from skill_sdk.testing import invoke


def _implementation(args: argparse.Namespace, skill: Skill) -> Implementation | None:
    if args.protocol == "http":
        base = args.endpoint.rstrip("/")
        return Http(endpoint=f"{base}/skills/{skill.ref}", audience=args.audience)
    if args.protocol == "mcp":
        return Mcp(endpoint=args.endpoint, audience=args.audience)
    if args.protocol == "local":
        return Local()
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skill-sdk", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="скиллы в модулях")
    listing.add_argument("targets", nargs="+")

    contract = sub.add_parser("contract", help="контракт v1 одного скилла (JSON)")
    contract.add_argument("target")

    export = sub.add_parser("export", help="YAML kind: Skill в пакет каталога")
    export.add_argument("--package", type=Path, required=True)
    export.add_argument(
        "--protocol",
        choices=("local", "http", "mcp"),
        help="реализация в контракте; по умолчанию — объявленная в коде",
    )
    export.add_argument(
        "--endpoint", help="http: база сервиса (допускает ${VAR}); mcp: URL или stdio:<имя>"
    )
    export.add_argument("--audience", help="IAM audience, токен которой несёт исполнитель")
    export.add_argument("--check", action="store_true", help="не писать, а сверить с файлами")
    export.add_argument("targets", nargs="+")

    call = sub.add_parser("invoke", help="вызвать скилл локально с проверкой контракта")
    call.add_argument("target")
    call.add_argument("--input", required=True, help="JSON или @файл")

    serve = sub.add_parser("serve", help="хостинг скиллов")
    serve.add_argument("transport", choices=("http", "mcp-http", "mcp-stdio"))
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--allow-anonymous",
        action="store_true",
        help="без проверки токена — только локальная разработка",
    )
    serve.add_argument("targets", nargs="+")

    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path.cwd()))

    if args.command == "list":
        for item in discover(args.targets):
            print(f"{item.ref}\t{item.entrypoint}\t{item.side_effects}/{item.risk}")
        return 0

    if args.command == "contract":
        (item,) = discover([args.target])
        print(json.dumps(item.contract(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "export":
        if args.protocol in ("http", "mcp") and not args.endpoint:
            parser.error("--endpoint обязателен для http и mcp")
        skills = discover(args.targets)
        if args.check:
            problems = [
                p
                for s in skills
                for p in package.drift([s], args.package, _implementation(args, s))
            ]
            for problem in problems:
                print("расхождение:", problem)
            print(
                "ok" if not problems else f"расхождений: {len(problems)} — перегенерируйте export"
            )
            return 1 if problems else 0
        for item in skills:
            (path,) = package.export([item], args.package, _implementation(args, item))
            print("записан", path)
        return 0

    if args.command == "invoke":
        raw = Path(args.input[1:]).read_text() if args.input.startswith("@") else args.input
        (item,) = discover([args.target])
        try:
            result = invoke(item, json.loads(raw))
        except SkillError as failure:
            print(json.dumps({"error": failure.as_error()}, ensure_ascii=False, indent=2))
            return 1
        print(
            json.dumps(
                {"outputs": result.outputs, "cost": result.cost}, ensure_ascii=False, indent=2
            )
        )
        return 0

    skills = discover(args.targets)
    if args.transport == "http":
        from skill_sdk.http import serve as serve_http

        serve_http(skills, host=args.host, port=args.port, allow_anonymous=args.allow_anonymous)
    elif args.transport == "mcp-http":
        from skill_sdk.mcp import serve_http as serve_mcp_http

        serve_mcp_http(skills, host=args.host, port=args.port, allow_anonymous=args.allow_anonymous)
    else:
        from skill_sdk.mcp import serve_stdio

        asyncio.run(serve_stdio(skills))
    return 0


if __name__ == "__main__":
    sys.exit(main())
