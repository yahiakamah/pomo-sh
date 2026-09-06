from sqlalchemy import select

from .database import SessionLocal
from .models import Environment
from .providers.docker_provider import DockerProvider
from .schemas import EnvironmentCreate

provider = DockerProvider()


def provision_environment(env_id: int, spec_data: dict) -> None:
    spec = EnvironmentCreate(**spec_data)
    db = SessionLocal()
    try:
        env = db.get(Environment, env_id)
        if env is None:
            return
        env.state = "provisioning"
        env.detail = "creating db + container"
        db.commit()
        try:
            info = provider.create_environment(spec)
            env.state = "running"
            env.url = info.url
            env.container_id = info.container_id
            env.detail = None
        except Exception as exc:  # noqa: BLE001
            env.state = "error"
            env.detail = str(exc)[:500]
        db.commit()
    finally:
        db.close()


def destroy_environment(slug: str, drop_data: bool) -> None:
    db = SessionLocal()
    try:
        provider.destroy_environment(slug, drop_data=drop_data)
        row = db.scalar(select(Environment).where(Environment.slug == slug))
        if row:
            db.delete(row)
            db.commit()
    except Exception as exc:  # noqa: BLE001
        row = db.scalar(select(Environment).where(Environment.slug == slug))
        if row:
            row.state = "error"
            row.detail = str(exc)[:500]
            db.commit()
    finally:
        db.close()
