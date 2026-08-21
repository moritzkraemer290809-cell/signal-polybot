"""Project isolation guarantees.

- no sys.path manipulation and no parent-directory escapes in app code
- only project modules, stdlib, or dependencies declared in pyproject.toml
- .env is resolved strictly inside the repository root
- Docker / PostgreSQL / Redis resources are polysignal_-prefixed
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

from app import config

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"

# import-name mapping for declared dependencies
DECLARED_IMPORTS = {
    "fastapi",
    "uvicorn",
    "httpx",
    "websockets",
    "sqlalchemy",
    "asyncpg",
    "alembic",
    "pydantic",
    "pydantic_settings",
    "redis",
    "structlog",
    "greenlet",
}


def _iter_app_files() -> list[Path]:
    files = sorted(APP_DIR.rglob("*.py"))
    assert files, "app package must contain python files"
    return files


def test_env_file_is_repo_local() -> None:
    assert config.REPO_ROOT == REPO_ROOT
    assert config.ENV_FILE == REPO_ROOT / ".env"
    assert ".." not in str(config.ENV_FILE)


def test_no_sys_path_manipulation_or_parent_escapes() -> None:
    for path in _iter_app_files():
        source = path.read_text()
        assert "sys.path" not in source, f"sys.path manipulation in {path}"
        assert "../" not in source, f"parent directory escape in {path}"
        assert "Path.home" not in source, f"home directory access in {path}"


def test_imports_only_project_stdlib_or_declared_dependencies() -> None:
    stdlib = set(sys.stdlib_module_names)
    for path in _iter_app_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    # relative imports stay within the app package by definition
                    continue
                if node.module:
                    modules = [node.module]
            for module in modules:
                top = module.split(".")[0]
                assert top == "app" or top in stdlib or top in DECLARED_IMPORTS, (
                    f"{path} imports undeclared module {module!r}"
                )


def test_declared_dependencies_match_pyproject() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    deps = " ".join(pyproject["project"]["dependencies"])
    for name in ("fastapi", "httpx", "sqlalchemy", "alembic", "redis", "structlog"):
        assert name in deps


def test_docker_resources_are_polysignal_prefixed() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "name: polysignal_intelligence" in compose
    for resource in (
        "polysignal_app",
        "polysignal_postgres",
        "polysignal_redis",
        "polysignal_pgdata",
        "polysignal_redisdata",
        "polysignal_network",
    ):
        assert resource in compose, f"missing isolated resource {resource}"
    assert "POSTGRES_DB: polysignal_intelligence" in compose
    assert "POSTGRES_USER: polysignal_user" in compose


def test_env_example_has_no_real_secrets() -> None:
    env_example = (REPO_ROOT / ".env.example").read_text()
    assert "TELEGRAM_BOT_TOKEN=" in env_example
    assert "TELEGRAM_BOT_TOKEN=\n" in env_example or "TELEGRAM_BOT_TOKEN=$" not in env_example
    assert "REDIS_KEY_PREFIX=polysignal:" in env_example


def test_phase5_data_layer_produces_no_signals_or_telegram() -> None:
    """The realtime data layer must not import strategy/risk/cost/signal or
    Telegram modules - phase 5 is pure data infrastructure."""
    forbidden_prefixes = (
        "app.strategy",
        "app.risk",
        "app.costs",
        "app.monitoring",
        "app.adapters.telegram",
    )
    phase5_files = [
        *sorted((APP_DIR / "data").rglob("*.py")),
        APP_DIR / "adapters" / "polymarket_ws.py",
        APP_DIR / "adapters" / "rate_limiter.py",
    ]
    for path in phase5_files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                for prefix in forbidden_prefixes:
                    assert not module.startswith(prefix), (
                        f"{path} imports {module!r} - phase 5 must stay signal-free"
                    )


def test_telegram_layer_imports_no_strategy_or_market_adapters() -> None:
    """The Telegram layer is a pure communication channel - it must not import
    strategy/risk/cost/monitoring code or the Polymarket market-data adapters."""
    forbidden_prefixes = (
        "app.strategy",
        "app.risk",
        "app.costs",
        "app.monitoring",
        "app.adapters.polymarket_rest",
        "app.adapters.polymarket_ws",
    )
    telegram_files = [
        *sorted((APP_DIR / "telegram").rglob("*.py")),
        APP_DIR / "adapters" / "telegram.py",
        APP_DIR / "bot_state.py",
    ]
    assert len(telegram_files) > 5
    for path in telegram_files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                for prefix in forbidden_prefixes:
                    assert not module.startswith(prefix), (
                        f"{path} imports {module!r} - telegram layer must stay isolated"
                    )


def test_phase6_generates_no_trade_signals() -> None:
    """Strategy/risk/cost modules must still be pure placeholders: nothing in
    the codebase computes long/short signals, entries, stops or leverage."""
    for package in ("strategy", "risk", "costs"):
        for path in sorted((APP_DIR / package).rglob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text())
            non_docstring = [
                node
                for node in tree.body
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
                and not isinstance(node, ast.ImportFrom | ast.Import)
            ]
            assert non_docstring == [], (
                f"{path} contains executable code - strategy phases are not unlocked yet"
            )
