from pathlib import Path

from alembic import command
from alembic.config import Config

from ledger.db import engine


def migrate():
    command.upgrade(Config("alembic.ini"), "head")
    grants = Path(__file__).resolve().parent.parent / "deploy" / "database-grants.sql"
    with engine.begin() as connection:
        connection.exec_driver_sql(grants.read_text())


if __name__ == "__main__":
    migrate()
