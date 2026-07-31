from pathlib import Path

import pytest

from relay.db import connect, init_db
from relay.tools import build_registry

KB_DIR = Path(__file__).parent.parent / "kb"


@pytest.fixture()
def conn():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def registry(conn):
    return build_registry(conn, KB_DIR)
