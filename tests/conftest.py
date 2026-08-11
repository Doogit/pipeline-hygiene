"""Shared fixtures. Tests load tests/config_test.yaml ONLY, never config.yaml."""
from pathlib import Path

import pytest
import yaml

TEST_CONFIG_PATH = Path(__file__).parent / "config_test.yaml"


@pytest.fixture(scope="session")
def config():
    with open(TEST_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)
