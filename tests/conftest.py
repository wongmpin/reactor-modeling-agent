from __future__ import annotations

import pytest

from reactor_agent.storage.db import Database


@pytest.fixture
def database(tmp_path):
    return Database(tmp_path / "test.sqlite3")
