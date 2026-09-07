import psycopg
from psycopg import sql

from .config import settings


class PgAdmin:
    def _connect(self):
        return psycopg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_admin_user,
            password=settings.postgres_password,
            dbname="postgres",
            autocommit=True,
        )

    def ensure_database(self, name: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if not cur.fetchone():
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    def ensure_role_and_db(self, db: str, role: str, password: str) -> None:
        role_id = sql.Identifier(role)
        db_id = sql.Identifier(db)
        pw = sql.Literal(password)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone():
                cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN CREATEDB PASSWORD {}").format(role_id, pw))
            else:
                cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN CREATEDB PASSWORD {}").format(role_id, pw))
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
            if not cur.fetchone():
                cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(db_id, role_id))

    def recreate_database(self, db: str, owner: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db)))
            cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(db), sql.Identifier(owner)))

    def drop_db(self, db: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db)))

    def drop_role(self, role: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
