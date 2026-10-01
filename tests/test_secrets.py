"""ctx.secret и skill_sdk.secrets: окружение, затем файл секрета узла fleet
(TASK-001163, TASK-001170)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from skill_sdk import Invocation, SkillContext, SkillError
from skill_sdk import secrets as secrets_module
from skill_sdk.context import ENV_SECRETS_DIR, MAX_SECRET_FILE_BYTES
from skill_sdk.secrets import SWAP_ATTEMPTS, read_secret, read_secret_file


def _ctx(secrets_dir: Path, **env: str) -> SkillContext:
    return SkillContext(
        Invocation(skill="t", protocol="local"),
        env={ENV_SECRETS_DIR: str(secrets_dir), **env},
    )


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    return directory


def test_env_without_file(secrets_dir: Path) -> None:
    assert _ctx(secrets_dir, **{"ext-token": "from-env"}).secret("ext-token") == "from-env"
    assert _ctx(secrets_dir, EXT_TOKEN="upper").secret("EXT_TOKEN") == "upper"


def test_file_without_env_trims_trailing_newline(secrets_dir: Path) -> None:
    (secrets_dir / "ext-token").write_text("from-file\n", encoding="utf-8")
    (secrets_dir / "crlf").write_text("v\r\n", encoding="utf-8")
    assert _ctx(secrets_dir).secret("ext-token") == "from-file"
    assert _ctx(secrets_dir).secret("crlf") == "v"


def test_default_dir_is_run_secrets() -> None:
    ctx = SkillContext(Invocation(skill="t", protocol="local"), env={})
    with pytest.raises(SkillError) as error:
        ctx.secret("surely-absent-secret-1163")
    assert "/run/secrets/surely-absent-secret-1163" in error.value.message


def test_env_wins_over_file(secrets_dir: Path) -> None:
    (secrets_dir / "ext-token").write_text("from-file", encoding="utf-8")
    assert _ctx(secrets_dir, **{"ext-token": "from-env"}).secret("ext-token") == "from-env"


def test_missing_names_both_places_without_values(secrets_dir: Path) -> None:
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir, OTHER="hidden-value").secret("absent")
    assert (error.value.code, error.value.retryable) == ("config_missing", True)
    assert "переменной окружения absent" in error.value.message
    assert str(secrets_dir / "absent") in error.value.message
    assert error.value.details == {"secret": "absent", "secretsDir": str(secrets_dir)}
    assert "hidden-value" not in str(error.value.as_error())


def test_empty_file_says_so(secrets_dir: Path) -> None:
    (secrets_dir / "empty").write_text("\r\n\n", encoding="utf-8")
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("empty")
    assert error.value.code == "config_missing"
    assert f"файл {secrets_dir / 'empty'} пуст" in error.value.message


def test_only_trailing_line_breaks_are_trimmed(secrets_dir: Path) -> None:
    (secrets_dir / "padded").write_text(" v \r\n", encoding="utf-8")
    (secrets_dir / "inner").write_text("a b\n", encoding="utf-8")
    assert _ctx(secrets_dir).secret("padded") == " v "
    assert _ctx(secrets_dir).secret("inner") == "a b"


@pytest.mark.parametrize("blank", ["  \n", " ", "\t \r\n", "\n \n"])
def test_whitespace_only_file_is_absent(secrets_dir: Path, blank: str) -> None:
    (secrets_dir / "blank").write_text(blank, encoding="utf-8")
    assert read_secret_file(secrets_dir, "blank") == ""
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("blank")
    assert error.value.code == "config_missing"
    assert f"файл {secrets_dir / 'blank'} пуст" in error.value.message


def test_agent_pat_is_reserved(secrets_dir: Path) -> None:
    (secrets_dir / "agent-pat").write_text("pat-value", encoding="utf-8")
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir, **{"agent-pat": "pat-value"}).secret("agent-pat")
    assert (error.value.code, error.value.retryable) == ("secret_name_invalid", False)
    assert "pat-value" not in str(error.value.as_error())


def test_env_style_name_does_not_look_for_a_file(secrets_dir: Path) -> None:
    (secrets_dir / "EXT_TOKEN").write_text("never-read", encoding="utf-8")
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("EXT_TOKEN")
    assert error.value.code == "config_missing"
    assert "не искали" in error.value.message
    assert "never-read" not in str(error.value.as_error())


@pytest.mark.parametrize("name", ["../outside", "/etc/passwd", "a/b", "..", "a\\b", ""])
def test_path_traversal_is_rejected(secrets_dir: Path, name: str) -> None:
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir, **{name: "v"} if name else {}).secret(name)
    assert (error.value.code, error.value.retryable) == ("secret_name_invalid", False)


def test_symlink_outside_is_rejected(secrets_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("stolen", encoding="utf-8")
    (secrets_dir / "leak").symlink_to(outside)
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("leak")
    assert error.value.code == "secret_file_rejected"
    assert error.value.details == {"secret": "leak", "reason": "outside_secrets_dir"}
    assert "stolen" not in str(error.value.as_error())


def test_symlink_inside_is_followed(secrets_dir: Path) -> None:
    (secrets_dir / "real").write_text("inside", encoding="utf-8")
    (secrets_dir / "alias").symlink_to(secrets_dir / "real")
    assert _ctx(secrets_dir).secret("alias") == "inside"


def test_too_large_file_is_rejected(secrets_dir: Path) -> None:
    (secrets_dir / "big").write_bytes(b"x" * (MAX_SECRET_FILE_BYTES + 1))
    (secrets_dir / "edge").write_bytes(b"y" * MAX_SECRET_FILE_BYTES)
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("big")
    assert error.value.details == {"secret": "big", "reason": "too_large"}
    assert len(_ctx(secrets_dir).secret("edge")) == MAX_SECRET_FILE_BYTES


def test_directory_is_rejected(secrets_dir: Path) -> None:
    (secrets_dir / "nested").mkdir()
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("nested")
    assert error.value.code == "secret_file_rejected"


@pytest.mark.skipif(os.geteuid() == 0, reason="root читает файл без прав")
def test_unreadable_file_is_a_clear_error(secrets_dir: Path) -> None:
    path = secrets_dir / "locked"
    path.write_text("v", encoding="utf-8")
    path.chmod(0)
    try:
        with pytest.raises(SkillError) as error:
            _ctx(secrets_dir).secret("locked")
    finally:
        path.chmod(0o600)
    assert (error.value.code, error.value.retryable) == ("secret_unreadable", True)
    assert "нет прав" in error.value.message
    assert str(path) in error.value.message


def test_fifo_is_rejected_without_hanging(secrets_dir: Path) -> None:
    os.mkfifo(secrets_dir / "pipe")
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("pipe")
    assert error.value.details == {"secret": "pipe", "reason": "not_regular_file"}


class _Race:
    """Подменить путь во время чтения — окно TOCTOU по требованию.

    ``swap`` зовётся перед ``open`` (только для компонента ``on``, если задан; первые
    ``times`` раз, если задано), ``restore`` — сразу после: «подменил и вернул на
    место». ``roots`` — сколько раз открывали сам каталог секретов (по попытке)."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        swap: Callable[[], None],
        restore: Callable[[], None] | None = None,
        times: int | None = None,
        on: str | None = None,
    ) -> None:
        self.roots = 0
        self.swaps = 0
        real_open = os.open

        def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if "dir_fd" not in kwargs:
                self.roots += 1
            if (on is not None and os.fspath(path) != on) or (
                times is not None and self.swaps >= times
            ):
                return real_open(path, flags, *args, **kwargs)
            self.swaps += 1
            swap()
            try:
                return real_open(path, flags, *args, **kwargs)
            finally:
                if restore is not None:
                    restore()

        monkeypatch.setattr(secrets_module.os, "open", racing_open)


def test_secrets_dir_swapped_and_put_back_is_caught_by_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Каталог секретов подменён на время ``open`` и возвращён — путь до и после тот
    же, подмену видит только сверка inode дескриптора с путём."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "token").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "token").write_text("stolen", encoding="utf-8")
    parked = tmp_path / "secrets-parked"

    def swap() -> None:
        secrets.rename(parked)
        secrets.symlink_to(outside)

    def restore() -> None:
        secrets.unlink()
        parked.rename(secrets)

    race = _Race(monkeypatch, swap, restore, on=str(secrets))
    with pytest.raises(SkillError) as error:
        _ctx(secrets).secret("token")
    assert (error.value.code, error.value.retryable) == ("secret_file_rejected", False)
    assert error.value.details == {"secret": "token", "reason": "symlink_swapped"}
    assert "stolen" not in str(error.value.as_error())
    assert race.roots == SWAP_ATTEMPTS
    assert (secrets / "token").read_text(encoding="utf-8") == "inside"  # вернул на место


def test_file_swapped_for_symlink_on_every_attempt_is_rejected(
    secrets_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = secrets_dir / "token"
    token.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.write_text("stolen", encoding="utf-8")

    def swap() -> None:
        token.unlink()
        token.symlink_to(outside)

    def restore() -> None:
        token.unlink()
        token.write_text("inside", encoding="utf-8")

    race = _Race(monkeypatch, swap, restore, on="token")
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("token")
    assert error.value.details == {"secret": "token", "reason": "symlink_swapped"}
    assert not error.value.retryable
    assert "stolen" not in str(error.value.as_error())
    assert race.roots == SWAP_ATTEMPTS


def test_intermediate_directory_triple_race_does_not_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пробник ревью: промежуточный каталог подменён перед ``open``, возвращён, снова
    подменён перед ``stat`` и возвращён. Сверка «путь и inode после open» такую гонку
    пропускала; обход от дескриптора каталога — нет."""
    secrets = tmp_path / "secrets"
    (secrets / "sub").mkdir(parents=True)
    (secrets / "sub" / "token").write_text("inside", encoding="utf-8")
    (secrets / "token").symlink_to("sub/token")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "token").write_text("stolen", encoding="utf-8")
    parked = tmp_path / "parked"

    def swap() -> None:
        (secrets / "sub").rename(parked)
        (secrets / "sub").symlink_to(outside)

    def restore() -> None:
        (secrets / "sub").unlink()
        parked.rename(secrets / "sub")

    real_open, real_stat = os.open, os.stat
    armed = False

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal armed
        swap()
        try:
            return real_open(path, flags, *args, **kwargs)
        finally:
            restore()
            armed = True

    def racing_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal armed
        if not armed:
            return real_stat(path, *args, **kwargs)
        armed = False
        swap()
        try:
            return real_stat(path, *args, **kwargs)
        finally:
            restore()

    monkeypatch.setattr(secrets_module.os, "open", racing_open)
    monkeypatch.setattr(secrets_module.os, "stat", racing_stat)
    with pytest.raises(SkillError) as error:
        read_secret_file(secrets, "token")
    assert error.value.details == {"secret": "token", "reason": "symlink_swapped"}
    assert "stolen" not in str(error.value.as_error())


def _kubernetes_volume(secrets: Path, value: str) -> None:
    """Раскладка секрета Kubernetes: ``token -> ..data/token``, ``..data -> ..<версия>``."""
    (secrets / "..v1").mkdir()
    (secrets / "..v1" / "token").write_text(value, encoding="utf-8")
    (secrets / "..data").symlink_to("..v1")
    (secrets / "token").symlink_to("..data/token")


def _rotate(secrets: Path, value: str) -> None:
    """Ротация Kubernetes: новая версия, атомарная замена ``..data``, старая удаляется."""
    (secrets / "..v2").mkdir()
    (secrets / "..v2" / "token").write_text(value, encoding="utf-8")
    (secrets / "..data_tmp").symlink_to("..v2")
    (secrets / "..data_tmp").rename(secrets / "..data")
    (secrets / "..v1" / "token").unlink()
    (secrets / "..v1").rmdir()


def test_kubernetes_rotation_between_readlink_and_open_is_retried(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``..data`` уже прочитан как ``..v1``, а открыть ``..v1`` не успели — его удалили."""
    _kubernetes_volume(secrets_dir, "old")
    race = _Race(monkeypatch, lambda: _rotate(secrets_dir, "new"), times=1, on="..v1")
    assert _ctx(secrets_dir).secret("token") == "new"
    assert race.roots == 2


def test_kubernetes_rotation_after_opening_the_old_version_is_retried(
    secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Старый каталог уже открыт, и ротация удаляет из него файл до чтения."""
    _kubernetes_volume(secrets_dir, "old")
    real_open = os.open
    rotated = False

    def open_then_rotate(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal rotated
        fd = real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == "..v1" and not rotated:
            rotated = True
            _rotate(secrets_dir, "new")
        return fd

    monkeypatch.setattr(secrets_module.os, "open", open_then_rotate)
    assert _ctx(secrets_dir).secret("token") == "new"
    assert rotated


def test_dangling_symlink_inside_is_absent(secrets_dir: Path) -> None:
    (secrets_dir / "token").symlink_to(secrets_dir / "gone")
    with pytest.raises(SkillError) as error:
        _ctx(secrets_dir).secret("token")
    assert error.value.code == "config_missing"


def test_public_rule_reads_the_file_without_a_context(secrets_dir: Path) -> None:
    (secrets_dir / "ext-token").write_text("v\n", encoding="utf-8")
    assert read_secret_file(secrets_dir, "ext-token") == "v"
    assert read_secret_file(secrets_dir, "absent") is None
    assert read_secret("ext-token", environ={}, secrets_dir=secrets_dir) == "v"
    assert read_secret("ext-token", environ={"ext-token": "env"}, secrets_dir=secrets_dir) == "env"
    assert read_secret("ext-token", environ={ENV_SECRETS_DIR: str(secrets_dir)}) == "v"
    for bad in ("EXT_TOKEN", "agent-pat", "../x"):
        with pytest.raises(SkillError) as error:
            read_secret_file(secrets_dir, bad)
        assert error.value.code == "secret_name_invalid"
