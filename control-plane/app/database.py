import time

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

_PG_COLUMNS = [
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS repo_url varchar(255)",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS git_branch varchar(100)",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS addons_subdir varchar(255)",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS modules varchar(255)",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS git_ref varchar(64)",
    "ALTER TABLE environments ADD COLUMN IF NOT EXISTS repo_id integer",
]


def init_db(retries: int = 10, delay: float = 2.0) -> None:
    from . import models  # noqa: F401

    last_err = None
    for _ in range(retries):
        try:
            if settings.database_url.startswith("postgresql"):
                from .db import PgAdmin

                PgAdmin().ensure_database(settings.control_db)
            Base.metadata.create_all(engine)
            if settings.database_url.startswith("postgresql"):
                with engine.begin() as conn:
                    for stmt in _PG_COLUMNS:
                        conn.execute(text(stmt))
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(delay)
    raise RuntimeError(f"could not initialise control database: {last_err}")
