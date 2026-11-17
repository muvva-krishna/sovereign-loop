"""Shared pytest fixtures isolating database state into a temporary directory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from contracts.config import get_settings
from dataset.seed import seed_database
from services.mcp_erp.db import configure_database


@pytest.fixture(scope="session", autouse=True)
def isolated_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point DATABASE_URL and Traces to a temporary directory for the test session."""
    db_dir = tmp_path_factory.mktemp("sovereign_db")
    db_file = db_dir / "test_sovereign.db"
    traces_dir = db_dir / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    prev_db = os.environ.get("DATABASE_URL")
    prev_traces = os.environ.get("TRACES_DIR")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    os.environ["TRACES_DIR"] = str(traces_dir)
    get_settings.cache_clear()

    configure_database(os.environ["DATABASE_URL"])
    seed_database(reset=True)

    yield db_file

    if prev_db is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = prev_db
    if prev_traces is None:
        os.environ.pop("TRACES_DIR", None)
    else:
        os.environ["TRACES_DIR"] = prev_traces
    get_settings.cache_clear()
    configure_database()


@pytest.fixture(scope="module")
def seeded_db(isolated_database: Path) -> Path:
    """Reset and re-seed the isolated database cleanly for a test module."""
    configure_database(f"sqlite:///{isolated_database}")
    seed_database(reset=True)
    return isolated_database


@pytest.fixture(scope="module")
def client(seeded_db: Path) -> Iterator[TestClient]:
    """Provide a FastAPI TestClient backed by the isolated seeded database."""
    from services.gateway.app import app

    with TestClient(app) as tc:
        yield tc
