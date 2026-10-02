import os
from pathlib import Path

import pytest

from nycynapse.semantic import gold
from nycynapse.semantic.load import load_catalog

SEMANTIC = Path(__file__).resolve().parents[1] / "semantic"


@pytest.fixture(scope="session")
def catalog():
    return load_catalog(SEMANTIC)


@pytest.fixture(scope="session")
def manifest():
    return gold.load(SEMANTIC / "gold_manifest.json")


@pytest.fixture
def app_dsn():
    dsn = os.environ.get("NYC_APP_TEST_PG_DSN")
    if not dsn:
        pytest.skip("NYC_APP_TEST_PG_DSN is not set")
    return dsn
