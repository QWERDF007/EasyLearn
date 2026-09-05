import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg import sql
from sqlalchemy.engine import make_url

from easylearn.config import Settings
from easylearn.main import create_app


@pytest.fixture
def pdf_bytes():
    return Path("3rdparty/MinerU/tests/unittest/pdfs/test.pdf").read_bytes()


@pytest.fixture
def database_url():
    admin_url = os.environ.get(
        "EASYLEARN_TEST_DATABASE_URL", "postgresql://easylearn@127.0.0.1:55432/postgres"
    )
    database = "easylearn_test_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    test_url = make_url(admin_url).set(database=database, drivername="postgresql+psycopg")
    try:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", test_url.render_as_string(hide_password=False))
        command.upgrade(config, "head")
        yield test_url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    finally:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database))
            )


@pytest_asyncio.fixture
async def client(database_url, tmp_path):
    app = create_app(Settings(database_url=database_url, storage_root=tmp_path))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client
