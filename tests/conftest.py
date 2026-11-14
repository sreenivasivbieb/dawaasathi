"""Shared fixtures: one dev-fixture SQLite DB per test session, the in-memory Store / MatchIndex,
test Settings (no keys, no network) and a TestClient factory with fake STT / TTS / OCR."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.gnani_stt import MockSTT  # noqa: E402
from app.adapters.gnani_tts import MockTTS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Store, connect  # noqa: E402
from app.match.index import MatchIndex  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import FakeOCR  # noqa: E402


# ---------------------------------------------------------------- database
@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "dev.db"
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.load_data", "--src", str(ROOT / "data" / "seed_dev"), "--db", str(path)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return path


@pytest.fixture(scope="session")
def store(db_path) -> Store:
    conn = connect(db_path)
    try:
        return Store.load(conn)
    finally:
        conn.close()


@pytest.fixture(scope="session")
def index(store) -> MatchIndex:
    return MatchIndex(store).build()


@pytest.fixture(scope="session")
def brand_ids(store) -> dict[str, int]:
    """Exact fixture brand name -> id."""
    return {b.name: b.id for b in store.brands.values()}


@pytest.fixture(scope="session")
def settings(db_path, tmp_path_factory):
    cache = tmp_path_factory.mktemp("tts")
    return dataclasses.replace(
        get_settings(), db_path=db_path, cache_dir=cache, gnani_api_key=None, groq_api_keys=(),
        rate_limit_per_min=10000,
    )


@pytest.fixture
def fake_ocr() -> FakeOCR:
    return FakeOCR()


# ---------------------------------------------------------------- API client
@pytest.fixture
def make_client(settings):
    """make_client(stt=MockSTT(...)) -> a started TestClient (lifespan run, state built)."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    opened = []

    def _make(stt=None, tts=None, ocr=None):
        app = create_app(settings, stt=stt or MockSTT(), tts=tts or MockTTS(), ocr=ocr or FakeOCR())
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return client

    yield _make
    for c in opened:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def pytest_terminal_summary(terminalreporter):
    import helpers

    if helpers.SUMMARY:
        terminalreporter.section("Dawaa Saathi accuracy summary")
        for line in helpers.SUMMARY:
            terminalreporter.write_line(line)
