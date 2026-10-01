# Цели, которые зовут CI (.github/workflows/ci.yml) и проверки исполнителя
# (.agents/runner.yaml). Соседи по плоской раскладке: ../platform-auth-sdk и
# ../platform-llm — path-зависимости экстр http, mcp и llm.
.PHONY: install lint fmt typecheck test test-e2e check

# Ядро рядом — только для сквозного теста исполнителя (test-e2e).
CONTROL_PLANE_DIR ?= ../control-plane

install:
	uv sync --frozen --all-extras

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check . --fix
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run --all-extras pytest -q

# Скилл SDK сквозь настоящий исполнитель ядра по local, http и mcp. Идёт в
# окружении ядра рядом: в окружении самого SDK исполнителя нет, и тест пропускается.
test-e2e:
	cd $(CONTROL_PLANE_DIR) && PYTHONPATH=$(CURDIR)/src:$(CURDIR) uv run pytest -q \
		-p no:cacheprovider -c $(CURDIR)/pyproject.toml $(CURDIR)/tests/test_executor_e2e.py

check: lint typecheck test
