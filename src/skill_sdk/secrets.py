"""Секреты узла fleet — канон чтения для SDK платформы (TAI-ADR-0052).

Узел монтирует ``placement.secrets`` описания агента в ``/run/secrets``: один файл на
секрет, имя файла равно имени секрета, без перевода регистра и ``_``/``-``. Правило
чтения такого файла здесь одно на все SDK; остальные читатели файлов секретов узла
зовут :func:`read_secret_file` или повторяют правило вместе с общими тестами-примерами.

- Имя файла — по шаблону ``[a-z0-9][a-z0-9-]{0,62}`` (тот же, что у
  ``placement.secrets`` в ядре и у узла fleet); ``agent-pat`` зарезервировано — это
  PAT самого агента, узел кладёт его рядом.
- Файл из одних пробельных символов (пустой после ``strip()``) — секрета нет. У
  непустого значения обрезаются только хвостовые ``\r`` и ``\n``; внутренние и
  краевые пробелы — часть значения.
- Не больше 64 КиБ, UTF-8, только обычный файл: каталог, FIFO (открывается с
  ``O_NONBLOCK`` и не вешает вызов) — отказ.
- Путь проходится по компонентам от дескриптора каталога секретов: каждый компонент
  открывается ``openat`` с ``O_NOFOLLOW``, символические ссылки разрешаются вручную и
  только внутри каталога (так раскладывают секреты Kubernetes); ссылка наружу или
  ``..`` выше каталога — отказ. Компонент, подменённый ссылкой между проверкой и
  открытием, или каталог секретов, подменённый во время открытия, — повтор, до трёх
  попыток: штатная ротация ``..data`` в Kubernetes проходит со второй; подмена на
  каждой попытке — окончательный отказ ``symlink_swapped``.

Это защита в глубину, а не гарантия. Каталог секретов — тот, на который указывает
путь в момент чтения: кто может подменять сам этот путь или файлы внутри каталога,
управляет секретами. Защита не даёт прочитать файл вне каталога через ссылку или
подмену промежуточного каталога, но не делает каталог, доступный на запись чужому
процессу, безопасным.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from collections import deque
from collections.abc import Mapping
from pathlib import Path

from skill_sdk.errors import SkillError

ENV_SECRETS_DIR = "SKILL_SDK_SECRETS_DIR"
DEFAULT_SECRETS_DIR = "/run/secrets"
#: Шаблон имени секрета узла — тот же, что у ``placement.secrets`` в ядре и у узла fleet.
SECRET_FILE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
#: Узел fleet всегда кладёт в ``agent-pat`` PAT самого агента: это учётка исполнителя,
#: а не секрет инсталляции.
RESERVED_SECRET_NAMES = frozenset({"agent-pat"})
MAX_SECRET_FILE_BYTES = 64 * 1024
#: Сколько раз читать файл, путь к которому подменяется во время чтения.
SWAP_ATTEMPTS = 3
#: Сколько символических ссылок разрешается по пути к одному файлу.
MAX_SYMLINKS = 40

__all__ = [
    "DEFAULT_SECRETS_DIR",
    "ENV_SECRETS_DIR",
    "MAX_SECRET_FILE_BYTES",
    "RESERVED_SECRET_NAMES",
    "SECRET_FILE_NAME",
    "SWAP_ATTEMPTS",
    "check_secret_name",
    "read_secret",
    "read_secret_file",
]


def check_secret_name(name: str) -> None:
    """Имя файла секрета узла: не путь (без ``/``, ``\\``, ``..``, NUL), не пустое,
    не зарезервированное и по шаблону :data:`SECRET_FILE_NAME` — иначе
    ``secret_name_invalid``."""
    _check_not_path_or_reserved(name)
    if not SECRET_FILE_NAME.fullmatch(name):
        raise SkillError(
            "secret_name_invalid",
            f"имя секрета не подходит под шаблон имён секретов узла {SECRET_FILE_NAME.pattern}",
        )


def _check_not_path_or_reserved(name: str) -> None:
    if not name or "/" in name or "\\" in name or "\0" in name or ".." in name:
        raise SkillError(
            "secret_name_invalid",
            "имя секрета не может быть путём: без '/', '..' и абсолютных путей",
        )
    if name in RESERVED_SECRET_NAMES:
        raise SkillError(
            "secret_name_invalid",
            f"имя {name} зарезервировано: это учётка агента на узле, а не секрет",
        )


def read_secret(
    name: str,
    *,
    environ: Mapping[str, str],
    secrets_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Секрет по имени: сначала окружение ``environ[name]``, затем файл секрета узла.

    Каталог — ``secrets_dir``, иначе ``environ[SKILL_SDK_SECRETS_DIR]``, иначе
    ``/run/secrets``. Файл ищется, только если имя подходит под
    :data:`SECRET_FILE_NAME`. Нет ни там, ни там — повторяемый ``config_missing``
    (другой хост может его иметь), в сообщении оба места поиска, значений нет.
    Имя-путь и зарезервированное — ``secret_name_invalid``; отказы файла — как у
    :func:`read_secret_file`."""
    _check_not_path_or_reserved(name)
    value = environ.get(name)
    if value:
        return value
    directory = Path(secrets_dir or environ.get(ENV_SECRETS_DIR) or DEFAULT_SECRETS_DIR)
    if SECRET_FILE_NAME.fullmatch(name):
        value = read_secret_file(directory, name)
        if value:
            return value
        if value is None:
            where = f"нет ни переменной окружения {name}, ни файла {directory / name}"
        else:
            where = f"нет переменной окружения {name}, а файл {directory / name} пуст"
    else:
        where = (
            f"нет переменной окружения {name}; файл в {directory} не искали — имя не "
            f"подходит под шаблон имён секретов узла {SECRET_FILE_NAME.pattern}"
        )
    raise SkillError(
        "config_missing",
        f"у хостинга не задан секрет {name}: {where}",
        retryable=True,
        details={"secret": name, "secretsDir": str(directory)},
    )


def read_secret_file(directory: str | os.PathLike[str], name: str) -> str | None:
    """Значение файла секрета узла ``<directory>/<name>``: ``None`` — файла нет,
    ``""`` — файл пуст или из одних пробельных символов.

    Отказы (:class:`SkillError`): имя не по :func:`check_secret_name` —
    ``secret_name_invalid``; файл без прав на чтение — повторяемый
    ``secret_unreadable``; остальное — ``secret_file_rejected`` с
    ``details.reason``: ``outside_secrets_dir``, ``symlink_swapped``,
    ``not_regular_file``, ``too_large``, ``not_utf8``, ``unreadable``."""
    check_secret_name(name)
    directory = Path(directory)
    for attempt in range(SWAP_ATTEMPTS):
        try:
            return _read_once(directory, name)
        except _Swapped:
            continue
        except _Vanished:
            if attempt == SWAP_ATTEMPTS - 1:
                return None  # висячая ссылка внутри каталога — файла нет
    raise _rejected(name, "путь к файлу подменён во время чтения", "symlink_swapped")


class _Swapped(Exception):
    """Путь подменён во время чтения — попытку стоит повторить."""


class _Vanished(Exception):
    """Компонент, до которого дошли по ссылке, пропал: висячая ссылка или ротация."""


def _read_once(directory: Path, name: str) -> str | None:
    try:
        root = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        raise _unreadable(name, directory / name) from None
    try:
        # каталог секретов, подменённый как раз на время open, — не тот каталог
        opened, named = os.fstat(root), _stat_or_none(directory)
        if named is None or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise _Swapped
        return _walk(root, directory, name)
    finally:
        os.close(root)


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path)
    except OSError:
        return None


def _walk(root: int, directory: Path, name: str) -> str | None:
    """Разрешить ``name`` от дескриптора ``root``: ``openat`` с ``O_NOFOLLOW`` на каждом
    компоненте, ссылки — вручную и только внутри каталога, ``..`` — по своему стеку
    дескрипторов, а не по файловой системе."""
    stack = [root]
    parts = deque([name])
    links = 0
    followed = False
    prefixes = {os.path.abspath(directory), os.path.realpath(directory)}
    try:
        while parts:
            part = parts.popleft()
            if part in ("", "."):
                continue
            if part == "..":
                if len(stack) == 1:
                    raise _outside(name)
                os.close(stack.pop())
                continue
            here = stack[-1]
            try:
                info = os.stat(part, dir_fd=here, follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                if followed:
                    raise _Vanished from None
                return None
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > MAX_SYMLINKS:
                    raise _rejected(name, "слишком много символических ссылок", "unreadable")
                try:
                    target = os.readlink(part, dir_fd=here)
                except OSError:
                    raise _Swapped from None
                followed = True
                if os.path.isabs(target):
                    inside = _inside(target, prefixes)
                    if inside is None:
                        raise _outside(name)
                    while len(stack) > 1:
                        os.close(stack.pop())
                    target = inside
                parts.extendleft(reversed(target.split("/")))
                continue
            if any(rest not in ("", ".") for rest in parts):
                if not stat.S_ISDIR(info.st_mode):
                    raise _Vanished  # путь по ссылке идёт через файл — как висячая ссылка
                stack.append(_open_dir(part, here, directory / name, name))
                continue
            return _read_file(_open_file(part, here, directory / name, name), name)
        raise _rejected(name, "это не обычный файл", "not_regular_file")
    finally:
        for fd in stack[1:]:
            os.close(fd)


def _inside(target: str, prefixes: set[str]) -> str | None:
    """Абсолютная цель ссылки → путь от каталога секретов; вне каталога — ``None``."""
    for prefix in prefixes:
        if target == prefix:
            return "."
        if target.startswith(prefix.rstrip("/") + "/"):
            return target[len(prefix.rstrip("/")) + 1 :]
    return None


def _open_dir(part: str, here: int, path: Path, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(part, flags, dir_fd=here)
    except FileNotFoundError:
        raise _Vanished from None
    except NotADirectoryError:
        raise _Swapped from None  # был каталогом при проверке — стал ссылкой или файлом
    except PermissionError:
        raise _unreadable(name, path) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _Swapped from None
        raise _rejected(name, f"каталог не открывается ({exc.strerror})", "unreadable") from None


def _open_file(part: str, here: int, path: Path, name: str) -> int:
    # O_NONBLOCK: open FIFO без писателя иначе висит; для обычного файла флаг ничего
    # не меняет, а всё, что не обычный файл, отвергается по fstat.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(part, flags, dir_fd=here)
    except FileNotFoundError:
        raise _Vanished from None
    except PermissionError:
        raise _unreadable(name, path) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _Swapped from None  # был файлом при проверке — стал ссылкой
        raise _rejected(name, f"файл не открывается ({exc.strerror})", "unreadable") from None


def _read_file(fd: int, name: str) -> str:
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _rejected(name, "это не обычный файл", "not_regular_file")
        data = b""
        while len(data) <= MAX_SECRET_FILE_BYTES:
            chunk = os.read(fd, MAX_SECRET_FILE_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_SECRET_FILE_BYTES:
        raise _rejected(name, f"файл больше {MAX_SECRET_FILE_BYTES // 1024} КиБ", "too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _rejected(name, "файл не в UTF-8", "not_utf8") from None
    if not text.strip():
        return ""  # файл из одних пробельных символов — секрета нет
    return text.rstrip("\r\n")


def _unreadable(name: str, path: Path) -> SkillError:
    return SkillError(
        "secret_unreadable",
        f"файл секрета {path} не читается: нет прав у пользователя процесса (uid {os.getuid()})",
        retryable=True,
        details={"secret": name, "path": str(path)},
    )


def _outside(name: str) -> SkillError:
    return _rejected(name, "ссылка ведёт за пределы каталога секретов", "outside_secrets_dir")


def _rejected(name: str, why: str, reason: str) -> SkillError:
    return SkillError(
        "secret_file_rejected",
        f"файл секрета {name} отвергнут: {why}",
        details={"secret": name, "reason": reason},
    )
