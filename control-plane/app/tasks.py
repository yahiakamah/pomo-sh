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


def redeploy_environment(env_id: int) -> None:
    db = SessionLocal()
    try:
        env = db.get(Environment, env_id)
        if env is None:
            return
        if not env.repo_url:
            env.state = "error"
            env.detail = "no git source to redeploy"
            db.commit()
            return
        env.state = "deploying"
        env.detail = "git pull + update"
        db.commit()
        try:
            modules = [m for m in (env.modules or "").split(",") if m]
            provider.redeploy_environment(
                env.slug, branch=env.git_branch or "main",
                modules=modules, addons_subdir=env.addons_subdir or "",
                edition=env.edition or "community",
            )
            env.state = "running"
            env.detail = None
        except Exception as exc:  # noqa: BLE001
            env.state = "error"
            env.detail = str(exc)[:500]
        db.commit()
    finally:
        db.close()


def backup_environment(slug: str) -> None:
    try:
        provider.backup_environment(slug)
    except Exception as exc:  # noqa: BLE001
        print(f"[backup] failed for {slug}: {exc}", flush=True)


def restore_environment(env_id: int, timestamp: str) -> None:
    db = SessionLocal()
    try:
        env = db.get(Environment, env_id)
        if env is None:
            return
        env.state = "restoring"
        env.detail = f"restoring backup {timestamp}"
        db.commit()
        try:
            provider.restore_environment(env.slug, timestamp)
            env.state = "running"
            env.detail = None
        except Exception as exc:  # noqa: BLE001
            env.state = "error"
            env.detail = str(exc)[:500]
        db.commit()
    finally:
        db.close()
