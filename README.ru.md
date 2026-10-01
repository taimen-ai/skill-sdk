*Русская версия. English: [README.md](README.md)*

# skill-sdk

SDK скиллов платформы Taimen. Скилл пишется один раз в коде, а SDK даёт всё
остальное:

- контракт v1 (CP-ADR-0056);
- контекст вызова;
- хостинг по всем трём протоколам исполнителя — `local`, `http`, `mcp`;
- YAML для пакета каталога (TAI-ADR-0044).

Решение — TAI-ADR-0045 суперпроекта.

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
    """Влить опубликованную ветку в целевую."""
    ...
    if conflict:
        return MergeOut(merged=False, reason="conflict")  # исход контракта — это выход
    raise SkillError("git_unavailable", "remote недоступен", retryable=True)  # сбой — ошибка
```

## Контракт — из кода

- **Вход и выход** берутся из моделей pydantic в аннотациях: первый аргумент и
  возвращаемое значение. Если модель не подходит (например, у уже опубликованной
  версии своя JSON Schema), схему можно задать явно через `inputs_schema=` и
  `outputs_schema=`.
- **Политика** — из аргументов декоратора: `side_effects` (`none`,
  `external_read` или `external_write`), `risk`, `idempotency`, `timeout`,
  `retry=(maxAttempts, backoffSeconds)`, `permissions`, `preconditions`,
  `postconditions`, `cost_model`.
- **Описание** — первый абзац docstring.
- **Реализация по умолчанию** — `local` с entrypoint `модуль:имя` самой функции.
  Для `http` и `mcp` её задают при экспорте: как хостить — решение инсталляции.

SDK отвергает то, что отвергло бы ядро, ещё при импорте. Например,
`external_write` без идемпотентности с повторами.

## Вызов

Функция принимает `(inputs)` или `(inputs, ctx)`, может быть синхронной или
`async`. SDK проверяет вход по схеме контракта, передаёт модель, проверяет выход
и сериализует его. Нарушение контракта даёт `input_contract_violation` или
`output_contract_violation`.

`SkillContext`:

| | |
|---|---|
| `ctx.invocation_id`, `ctx.idempotency_key` | какой это вызов; повтор с тем же ключом не должен дать второй внешний эффект |
| `ctx.remaining()`, `ctx.check_deadline()` | сколько осталось до таймаута контракта |
| `ctx.log` | журнал с id вызова |
| `ctx.config(name)`, `ctx.secret(name)` | параметры и секреты хостинга; секрет ищется в окружении, затем в файле секрета узла (см. ниже); нет секрета — повторяемый `config_missing` |
| `ctx.llm` | LLM-клиент по конфигурации инсталляции (`platform-llm` или Claude по подписке); токены учитываются сами; персональные данные физических лиц в промпте (ФИО, СНИЛС, паспорт, телефон, e-mail) заменяются маркером `[ПДн:вид]` до вызова модели, в журнал — только счёт (`skill_sdk.pii`) |
| `ctx.add_cost(unit, amount)` | своё потребление; уходит в `cost` вызова вместе с токенами LLM |
| `ctx.caller` | проверенный контекст вызывающего (http) |
| `ctx.artifacts.read(id)` | содержимое артефакта через ядро учётной записью исполнителя скиллов (`ArtifactContent`: `data`, `media_type`, `text()`) |
| `ctx.knowledge` | база знаний через ядро: `preview(snapshot, workspace_id=…)` — план без записи и `stateToken`; `apply(snapshot, workspace_id=…, expected_state=…)` — применить, только если состояние не менялось, иначе `SnapshotStale`; `document(…)` — документ с фрагментами и связями; `recall(**query)` — типизированный обход с `where`; `query(workspace_id=…, kinds=[…], where=…, as_of=…)` — сущности заданных видов по условиям: страницы ядра обходит сам и возвращает все; длиннее `max_items` — ошибка `knowledge_query_too_large`, а не обрезка |

Секреты: `ctx.secret(name)` сначала читает переменную окружения `name`, затем
файл `$SKILL_SDK_SECRETS_DIR/<name>` (по умолчанию `/run/secrets`). Ровно так узел
fleet передаёт `placement.secrets` описания агента: по файлу на секрет, имя файла —
имя секрета как есть, без перевода регистра и `_`/`-`. Файл ищется только для
имён, подходящих под шаблон имён секретов узла `[a-z0-9][a-z0-9-]{0,62}`: вызов
`ctx.secret("ext-token")` найдёт и переменную `ext-token`, и файл, а
`ctx.secret("EXT_TOKEN")` — только переменную. Файл из одних пробельных символов
(пустой после `strip()`) — секрета нет, и `config_missing` говорит, что файл пуст. В
остальных случаях обрезаются только хвостовые `\r` и `\n`: внутренние и краевые
пробелы непустого значения — его часть.

Это правило чтения — канон для SDK платформы, и он выставлен публичными функциями
`skill_sdk.secrets`: `read_secret(name, *, environ, secrets_dir=None)` — окружение,
затем файл, как у `ctx.secret`; `read_secret_file(directory, name)` — только файл
(`None` — файла нет, `""` — файл пуст или из пробелов); `check_secret_name(name)` —
проверка имени файла секрета узла. Остальные читатели файлов секретов узла зовут их
или повторяют правило вместе с общими тестами-примерами.

Путь проходится по компонентам от дескриптора каталога секретов: каждый компонент
открывается `openat` с `O_NOFOLLOW`, символические ссылки разрешаются вручную и только
внутри каталога (раскладка Kubernetes `token -> ..data/token` работает), ссылка или
`..` наружу — отказ. Компонент, подменённый ссылкой между проверкой и открытием, или
каталог секретов, подменённый на время открытия, читается заново, до трёх попыток, —
штатная ротация `..data` в Kubernetes проходит со второй; подмена на каждой попытке —
окончательный отказ `symlink_swapped`.

Это защита в глубину, а не гарантия. Каталог секретов — тот, на который указывает его
путь в момент чтения: кто может подменять сам этот путь или писать файлы внутри
каталога, управляет секретами. Обход не даёт ссылке или подменённому промежуточному
каталогу увести чтение за пределы каталога, но не делает безопасным каталог, открытый
на запись чужому процессу.

| Исход | Код |
|---|---|
| нет ни переменной, ни файла; в сообщении оба места поиска, значений нет | `config_missing`, повторяемый |
| имя с `/`, `..` или абсолютный путь; зарезервированное `agent-pat` (узел fleet всегда кладёт туда PAT самого агента, это не секрет скилла) | `secret_name_invalid` |
| ссылка за пределы каталога секретов, путь, подменённый во время чтения на каждой из трёх попыток (`symlink_swapped`), не обычный файл (каталог, FIFO), больше 64 КиБ, не UTF-8 | `secret_file_rejected` (`details.reason`) |
| файл есть, но процессу не хватает прав на чтение | `secret_unreadable`, повторяемый |

Клиента Control Plane в контексте нет намеренно: скилл не заводит и не двигает
задачи. Это делают исходы approval и правила ядра (TAI-ADR-0041). `ctx.artifacts`
и `ctx.knowledge` — узкий доступ к файлам и базе знаний (TAI-ADR-0056): адрес
ядра — `CONTROL_PLANE_URL` или `CONTROL_PLANE_SERVER` исполнителя, credential —
его же, через `control_plane_client` (нужен у хостинга; без него — повторяемый
`core_unavailable`). Память — только через ядро.

LLM: провайдер выбирает `SKILL_LLM_PROVIDER`. Произвольный провайдер задаётся
кодом через `skill_sdk.configure_llm(factory)`.

| `SKILL_LLM_PROVIDER` | Что это | Настройка |
|---|---|---|
| `openai` (по умолчанию) | `platform_llm.OpenAICompatibleClient` — любой OpenAI-совместимый `/chat/completions` | `SKILL_LLM_BASE_URL`, `SKILL_LLM_API_KEY`, `SKILL_LLM_MODELS` (через запятую) |
| `claude-code` | Claude по подписке через Claude Code CLI (`claude -p`) — как у кодовых агентов | `CLAUDE_CODE_OAUTH_TOKEN` в окружении, `SKILL_LLM_MODELS` (алиасы Claude: `sonnet`, `claude-sonnet-5`…; пусто — модель CLI по умолчанию), `SKILL_LLM_TIMEOUT_SECONDS` (300), `SKILL_LLM_CLAUDE_BINARY` (`claude`) |

Как устроен `claude-code` (`skill_sdk.claude_code.ClaudeCodeLlm`):

- чистое завершение текста: `--tools ""`, MCP-серверов нет, сессия не
  сохраняется, рабочий каталог — пустой временный;
- system prompt скилла, сообщения и JSON-схема ответа (из pydantic-модели)
  сводятся в один промпт и идут через **stdin** — в argv только постоянный
  нейтральный system prompt;
- ответ — `--output-format json`; JSON берётся из текста модели (как есть,
  из блока ```` ```json ```` или по фигурным скобкам) и проверяется моделью ответа;
  не прошёл — одна повторная попытка с текстом ошибки, затем следующая модель
  списка, затем повторяемый `llm_invalid_response`;
- токен подписки CLI наследует из окружения процесса; SDK его не читает, не
  логирует и вычищает из текста ошибок. `ANTHROPIC_API_KEY` и
  `ANTHROPIC_AUTH_TOKEN` дочернему процессу не передаются — иначе вызов ушёл бы
  по ключу API, а не по подписке;
- лимит окна подписки или 429 — повторяемый `llm_rate_limited` сразу (лимит
  общий на все модели подписки); нет CLI — `llm_unavailable`, таймаут —
  `llm_timeout`, прочий сбой CLI — `llm_failed`, все повторяемые;
- в `cost` уходят токены; условная цена CLI под подпиской расходом не считается.

## Хостинг

| Протокол | Как | Что видит исполнитель |
|---|---|---|
| `local` | пакет установлен рядом с демоном, `CONTROL_PLANE_SKILLS_LOCAL_PACKAGES=<пакет>` | исполнитель находит скиллы SDK сам, вызывает `__skill_invoke__` и получает `{outputs, cost}` |
| `http` | `skill-sdk serve http my_skills` или `skill_sdk.http.create_app(...)` в своём ASGI | `POST /skills/{name}@{version}`; 200 — outputs, cost — в `X-Skill-Cost`; ошибка — `{"error": {code, retryable, …}}` |
| `mcp` | `skill-sdk serve mcp-stdio` или `mcp-http` | инструмент с именем скилла; cost — в `_meta["skill/cost"]`; ошибка — `isError` с тем же `{"error": …}`; id вызова и ключ идемпотентности — `_meta["skill/invocationId"]` и `_meta["skill/idempotencyKey"]` запроса |

`http` и `mcp-http` проверяют IAM-токен audience скилла через `platform-auth-sdk`
(`SKILL_SDK_IAM_ISSUER`, `SKILL_SDK_AUDIENCE`, `SKILL_SDK_JWKS_URL`). Без проверки
хостинг не стартует — только с явным `--allow-anonymous` для разработки.

## Пакет каталога

```bash
skill-sdk export --package ../packages/selfdev taimen_selfdev           # записать skills/*.yaml
skill-sdk export --package ../packages/selfdev --check taimen_selfdev   # CI: код == YAML
skill-sdk export --package ../packages/acme --protocol http \
    --endpoint '${ACME_SKILLS_URL}' --audience acme-skills acme_skills  # хостинг по http
```

YAML с пометкой «сгенерировано» руками не правится. Контракт версии в ядре
неизменяем: изменение контракта означает новую `version` в декораторе.

## Тесты скилла

```python
from skill_sdk.testing import check_contract, invoke


def test_merge_contract():
    check_contract(merge)  # валидаторы ядра, если control-plane рядом


def test_conflict_is_an_outcome():
    result = invoke(
        merge, {"repository": "…", "branch": "b", "commit": "abc1234", "target": "main"}
    )
    assert result.outputs["reason"] == "conflict"
```

Ядро в тестах — `FakeCore`: артефакты в памяти процесса и сверка снимка по
ключам со `stateToken`.

```python
from skill_sdk import configure_core
from skill_sdk.testing import FakeCore

core = FakeCore()
core.artifacts.put("a1", "key,title\nSKU-1,Разработка\n", "text/csv")
configure_core(lambda ctx: core)
```

LLM в тестах — `FakeLlm`: ответ один на все вызовы, список по порядку или функция
от вызова (`LlmCall`: промпт, схема, `temperature`, `max_tokens`) — ответ «по
шаблону промпта» пишется такой функцией, декларативной таблицы «шаблон → ответ»
нет; ответ `chat_json` проверяется `response_model`, вызовы (уже после стража ПДн)
копятся в `llm.calls` и учитываются в cost.

```python
from skill_sdk.testing import FakeLlm, invoke

llm = FakeLlm([{"summary": "кратко"}, {"summary": "ещё короче"}])
result = invoke(summarize, {"text": "…"}, llm=llm)
assert result.outputs == {"summary": "кратко"}
assert llm.calls[0].prompt == "…"
```

## Установка

```bash
uv add skill-sdk                    # контракт, local, тесты, экспорт
uv add "skill-sdk[http]"            # + ASGI-хостинг и проверка токена
uv add "skill-sdk[mcp]"             # + MCP-сервер
uv add "skill-sdk[llm]"             # + ctx.llm
```

`platform-auth-sdk` и `platform-llm` подключаются соседними папками (плоская
раскладка суперпроекта). Тесты: `uv run pytest -q`.

## Лицензия

Apache License 2.0 — см. [LICENSE](LICENSE) и [NOTICE](NOTICE). Сторонние
компоненты перечислены в [THIRD_PARTY.md](THIRD_PARTY.md) (`sbom.json`, CycloneDX).
