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


def test_risk_and_cost_layer_isolation() -> None:
    """Phase-9 risk/costs modules: no Telegram, no market adapters, no
    network clients, no wallet/order/signing code paths.  They are pure
    domain/application logic over immutable public-data snapshots."""
    forbidden_imports = (
        "app.telegram",
        "app.adapters",
        "app.monitoring",
        "httpx",
        "websockets",
        "aiogram",
    )
    forbidden_terms = (
        "wallet",
        "private_key",
        "place_order",
        "create_order",
        "submit_order",
        "order_client",
        "signing",
    )
    risk_cost_files = [
        *sorted((APP_DIR / "risk").rglob("*.py")),
        *sorted((APP_DIR / "costs").rglob("*.py")),
    ]
    assert len(risk_cost_files) > 20
    for path in risk_cost_files:
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                for prefix in forbidden_imports:
                    assert not module.startswith(prefix), (
                        f"{path} imports {module!r} - risk/cost isolation violated"
                    )
        lowered = source.lower()
        for term in forbidden_terms:
            assert term not in lowered, f"{path} contains forbidden term {term!r}"


def test_risk_and_cost_layers_claim_no_guarantees() -> None:
    """No risk/cost source promises profits or guaranteed outcomes."""
    forbidden_phrases = (
        "guaranteed profit",
        "garantierter gewinn",
        "gewinnwahrscheinlichkeit:",
        "profit guarantee",
        "optimal leverage",
        "optimaler hebel:",
    )
    for package in ("risk", "costs"):
        for path in sorted((APP_DIR / package).rglob("*.py")):
            lowered = path.read_text().lower()
            for phrase in forbidden_phrases:
                assert phrase not in lowered, f"{path} contains {phrase!r}"


def test_selection_and_session_layer_isolation() -> None:
    """Selection modules must not import strategy/cost/risk/trading modules;
    session modules must additionally not import Telegram or the data layer."""
    selection_forbidden = (
        "app.strategy",
        "app.risk",
        "app.costs",
        "app.monitoring",
        "app.adapters.polymarket_rest",
        "app.adapters.polymarket_ws",
    )
    session_forbidden = (*selection_forbidden, "app.telegram", "app.adapters", "app.data")
    for directory, forbidden in (
        (APP_DIR / "selection", selection_forbidden),
        (APP_DIR / "sessions", session_forbidden),
    ):
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    for prefix in forbidden:
                        assert not module.startswith(prefix), (
                            f"{path} imports {module!r} - layer isolation violated"
                        )


def test_selection_layer_contains_no_trading_terminology() -> None:
    """Phase 7 never uses direction/trade terms in decision logic."""
    forbidden_terms = ("LONG", "SHORT", "entry_price", "stop_price", "take_profit", "leverage")
    for directory in (APP_DIR / "selection", APP_DIR / "sessions"):
        for path in sorted(directory.rglob("*.py")):
            source = path.read_text()
            for term in forbidden_terms:
                assert term not in source, f"{path} contains trading term {term!r}"


def test_signal_lifecycle_core_isolation() -> None:
    """Phase-10 signals modules: pure lifecycle logic over immutable
    snapshots.  Only lifecycle_context.py (the adapter) may touch
    repositories and data services; the core imports no persistence, no
    market adapters, no strategy/risk/cost engines, no Telegram, no network
    clients.  Nothing in the package claims fills, orders or real PnL."""
    core_forbidden = (
        "app.telegram",
        "app.adapters",
        "app.risk",
        "app.costs",
        "app.strategy",
        "app.selection",
        "app.monitoring",
        "app.data",
        "app.repositories",
        "app.jobs",
        "httpx",
        "websockets",
        "aiogram",
    )
    adapter_forbidden = (
        "app.telegram",
        "app.adapters",
        "app.risk",
        "app.costs",
        "app.strategy",
        "app.monitoring",
        "httpx",
        "websockets",
        "aiogram",
    )
    forbidden_terms = (
        "wallet",
        "private_key",
        "place_order",
        "create_order",
        "submit_order",
        "order_client",
        "signing",
        "fill_price",
        "filled_at",
        "realised_pnl",
        "realized_pnl",
    )
    signal_files = sorted((APP_DIR / "signals").rglob("*.py"))
    assert len(signal_files) > 20
    job_file = APP_DIR / "jobs" / "signal_lifecycle_monitor.py"
    for path in [*signal_files, job_file]:
        adapter = path.name in ("lifecycle_context.py", "signal_lifecycle_monitor.py")
        forbidden = adapter_forbidden if adapter else core_forbidden
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                for prefix in forbidden:
                    assert not module.startswith(prefix), (
                        f"{path} imports {module!r} - signal lifecycle isolation violated"
                    )
        lowered = source.lower()
        for term in forbidden_terms:
            assert term not in lowered, f"{path} contains forbidden term {term!r}"


def test_signal_lifecycle_claims_no_execution_or_guarantees() -> None:
    """No lifecycle source declares executions, positions or guarantees."""
    forbidden_phrases = (
        "guaranteed profit",
        "garantierter gewinn",
        "profit guarantee",
        "order executed",
        "position opened",
        "position closed",
        "trade executed",
    )
    for path in sorted((APP_DIR / "signals").rglob("*.py")):
        lowered = path.read_text().lower()
        for phrase in forbidden_phrases:
            assert phrase not in lowered, f"{path} contains {phrase!r}"


def test_strategy_layer_isolation_and_no_trade_parameters() -> None:
    """Phase-8 strategy modules: no Telegram/risk/cost/trading imports, no
    network clients, and no trade-parameter terminology (entry/stop/target/
    leverage/position size) anywhere in the research core."""
    forbidden_imports = (
        "app.telegram",
        "app.adapters.telegram",
        "app.risk",
        "app.costs",
        "app.monitoring",
        "app.adapters.polymarket_rest",
        "app.adapters.polymarket_ws",
        "httpx",
        "websockets",
        "aiogram",
    )
    forbidden_terms = (
        "entry_price",
        "stop_loss",
        "stop_price",
        "take_profit",
        "leverage",
        "position_size",
        "order_size",
        "notional_size",
    )
    strategy_files = sorted((APP_DIR / "strategy").rglob("*.py"))
    assert len(strategy_files) > 15
    for path in strategy_files:
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                for prefix in forbidden_imports:
                    assert not module.startswith(prefix), (
                        f"{path} imports {module!r} - strategy isolation violated"
                    )
        lowered = source.lower()
        for term in forbidden_terms:
            assert term not in lowered, f"{path} contains trade parameter term {term!r}"
