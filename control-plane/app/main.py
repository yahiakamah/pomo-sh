import hashlib
import hmac
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import tasks
from .config import settings
from .database import SessionLocal, init_db
from .models import Environment, Project, Repo
from .providers.docker_provider import DockerProvider
from .queue import task_queue
from .schemas import (
    BackupInfo,
    EnvironmentCreate,
    EnvironmentInfo,
    ProjectInfo,
    RepoCreate,
    RepoInfo,
    RestoreRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Pomo.sh Control Plane", version="0.4.0", lifespan=lifespan)

provider = DockerProvider()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _url(slug: str) -> str:
    return f"https://{slug}.{settings.base_domain}"


def _to_info(env: Environment) -> EnvironmentInfo:
    return EnvironmentInfo(
        slug=env.slug,
        state=env.state,
        stage=env.stage,
        url=env.url,
        odoo_version=env.odoo_version,
        container_id=env.container_id,
        detail=env.detail,
        project_name=env.project.name if env.project else None,
        repo_url=env.repo_url,
        git_branch=env.git_branch,
        created_at=env.created_at,
    )


def _get_or_create_project(db: Session, name: str) -> Project:
    project = db.scalar(select(Project).where(Project.name == name))
    if project is None:
        project = Project(name=name)
        db.add(project)
        db.flush()
    return project


def _repo_full_name(url: str) -> str:
    x = url.strip().rstrip("/")
    if x.endswith(".git"):
        x = x[:-4]
    parts = x.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else x


def _webhook_url() -> str:
    return f"https://api.{settings.base_domain}/api/v1/webhooks/github"


def _get_or_create_repo(db: Session, repo_url: str) -> Repo:
    import secrets as _secrets
    full_name = _repo_full_name(repo_url)
    repo = db.scalar(select(Repo).where(Repo.full_name == full_name))
    if repo is None:
        repo = Repo(full_name=full_name, webhook_secret=_secrets.token_hex(20))
        db.add(repo)
        db.flush()
    return repo


def _reconcile(db: Session) -> None:
    live = {e.slug: e for e in provider.list_environments()}
    rows = {e.slug: e for e in db.scalars(select(Environment)).all()}
    for slug, info in live.items():
        row = rows.get(slug)
        if row is None:
            project = _get_or_create_project(db, "adopted")
            db.add(
                Environment(
                    slug=slug,
                    state=info.state,
                    odoo_version=info.odoo_version or "18",
                    url=info.url,
                    container_id=info.container_id,
                    project_id=project.id,
                )
            )
        elif row.state not in ("queued", "provisioning", "deploying", "restoring", "error", "destroying"):
            row.state = info.state
            row.container_id = info.container_id
            row.url = info.url
    db.commit()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/projects", response_model=list[ProjectInfo])
def list_projects(db: Session = Depends(get_db)) -> list[ProjectInfo]:
    projects = db.scalars(select(Project)).all()
    return [
        ProjectInfo(name=p.name, created_at=p.created_at, environment_count=len(p.environments))
        for p in projects
    ]


@app.get("/api/v1/environments", response_model=list[EnvironmentInfo])
def list_environments(db: Session = Depends(get_db)) -> list[EnvironmentInfo]:
    _reconcile(db)
    envs = db.scalars(select(Environment)).all()
    return [_to_info(e) for e in envs]


@app.post("/api/v1/environments", response_model=EnvironmentInfo, status_code=202)
def create_environment(spec: EnvironmentCreate, db: Session = Depends(get_db)):
    existing = db.scalar(select(Environment).where(Environment.slug == spec.slug))
    if existing:
        raise HTTPException(409, f"environment '{spec.slug}' already exists")

    project = _get_or_create_project(db, spec.project_name)
    repo = _get_or_create_repo(db, spec.repo_url) if spec.repo_url else None
    env = Environment(
        slug=spec.slug,
        stage=spec.stage,
        state="queued",
        odoo_version=spec.odoo_version,
        url=_url(spec.slug),
        repo_url=spec.repo_url,
        git_branch=spec.git_branch,
        addons_subdir=spec.addons_subdir,
        modules=",".join(spec.modules),
        project_id=project.id,
        repo_id=repo.id if repo else None,
    )
    db.add(env)
    db.commit()
    db.refresh(env)

    task_queue.enqueue(tasks.provision_environment, env.id, spec.model_dump())
    return _to_info(env)


@app.get("/api/v1/environments/{slug}", response_model=EnvironmentInfo)
def get_environment(slug: str, db: Session = Depends(get_db)) -> EnvironmentInfo:
    env = db.scalar(select(Environment).where(Environment.slug == slug))
    if env is None:
        raise HTTPException(404, f"environment '{slug}' not found")
    if env.state not in ("queued", "provisioning", "deploying", "restoring", "error", "destroying"):
        info = provider.get_environment(slug)
        if info:
            env.state = info.state
            env.container_id = info.container_id
            db.commit()
    return _to_info(env)


@app.delete("/api/v1/environments/{slug}", status_code=202)
def delete_environment(slug: str, drop_data: bool = False, db: Session = Depends(get_db)) -> dict:
    env = db.scalar(select(Environment).where(Environment.slug == slug))
    if env is None:
        raise HTTPException(404, f"environment '{slug}' not found")
    env.state = "destroying"
    db.commit()
    task_queue.enqueue(tasks.destroy_environment, slug, drop_data)
    return {"slug": slug, "state": "destroying", "drop_data": drop_data}


@app.post("/api/v1/repos", response_model=RepoInfo)
def upsert_repo(body: RepoCreate, db: Session = Depends(get_db)) -> RepoInfo:
    repo = _get_or_create_repo(db, body.repo_url)
    db.commit()
    return RepoInfo(full_name=repo.full_name, webhook_url=_webhook_url(), secret=repo.webhook_secret)


@app.get("/api/v1/repos", response_model=list[RepoInfo])
def list_repos(db: Session = Depends(get_db)) -> list[RepoInfo]:
    repos = db.scalars(select(Repo)).all()
    return [RepoInfo(full_name=r.full_name, webhook_url=_webhook_url()) for r in repos]


def _valid_signature(secret: str, body: bytes, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@app.post("/api/v1/webhooks/github")
async def github_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    body = await request.body()
    event = request.headers.get("X-GitHub-Event", "")
    signature = request.headers.get("X-Hub-Signature-256", "")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "invalid json")

    full_name = (payload.get("repository") or {}).get("full_name")
    if not full_name:
        raise HTTPException(400, "missing repository")
    repo = db.scalar(select(Repo).where(Repo.full_name == full_name))
    if repo is None:
        raise HTTPException(404, f"repo '{full_name}' not registered")
    if not _valid_signature(repo.webhook_secret, body, signature):
        raise HTTPException(401, "invalid signature")

    if event == "ping":
        return {"pong": True}
    if event != "push":
        return {"ignored": event}

    ref = payload.get("ref", "")
    branch = ref.rsplit("/", 1)[-1] if ref else ""
    envs = db.scalars(
        select(Environment).where(
            Environment.repo_id == repo.id, Environment.git_branch == branch
        )
    ).all()
    redeployed = []
    for env in envs:
        env.state = "deploying"
        redeployed.append(env.slug)
    db.commit()
    for env in envs:
        task_queue.enqueue(tasks.redeploy_environment, env.id)
    return {"branch": branch, "redeployed": redeployed}


@app.post("/api/v1/environments/{slug}/redeploy", status_code=202)
def redeploy_environment(slug: str, db: Session = Depends(get_db)) -> dict:
    env = db.scalar(select(Environment).where(Environment.slug == slug))
    if env is None:
        raise HTTPException(404, f"environment '{slug}' not found")
    if not env.repo_url:
        raise HTTPException(400, f"environment '{slug}' has no git source")
    env.state = "deploying"
    db.commit()
    task_queue.enqueue(tasks.redeploy_environment, env.id)
    return {"slug": slug, "state": "deploying"}


@app.post("/api/v1/environments/{slug}/restore", status_code=202)
def restore_environment(slug: str, body: RestoreRequest, db: Session = Depends(get_db)) -> dict:
    env = db.scalar(select(Environment).where(Environment.slug == slug))
    if env is None:
        raise HTTPException(404, f"environment '{slug}' not found")
    env.state = "restoring"
    db.commit()
    task_queue.enqueue(tasks.restore_environment, env.id, body.timestamp)
    return {"slug": slug, "state": "restoring", "timestamp": body.timestamp}


@app.post("/api/v1/environments/{slug}/backup", status_code=202)
def backup_environment(slug: str, db: Session = Depends(get_db)) -> dict:
    env = db.scalar(select(Environment).where(Environment.slug == slug))
    if env is None:
        raise HTTPException(404, f"environment '{slug}' not found")
    task_queue.enqueue(tasks.backup_environment, slug)
    return {"slug": slug, "status": "backup queued"}


@app.get("/api/v1/environments/{slug}/backups", response_model=list[BackupInfo])
def list_backups(slug: str, db: Session = Depends(get_db)) -> list[BackupInfo]:
    env = db.scalar(select(Environment).where(Environment.slug == slug))
    if env is None:
        raise HTTPException(404, f"environment '{slug}' not found")
    return provider.list_backups(slug)


@app.post("/api/v1/environments/{slug}/{action}", response_model=EnvironmentInfo)
def environment_action(slug: str, action: str, db: Session = Depends(get_db)) -> EnvironmentInfo:
    env = db.scalar(select(Environment).where(Environment.slug == slug))
    if env is None:
        raise HTTPException(404, f"environment '{slug}' not found")
    actions = {
        "start": provider.start_environment,
        "stop": provider.stop_environment,
        "restart": provider.restart_environment,
    }
    if action not in actions:
        raise HTTPException(400, f"unknown action '{action}'")
    actions[action](slug)
    info = provider.get_environment(slug)
    if info:
        env.state = info.state
        env.container_id = info.container_id
        db.commit()
    return _to_info(env)


@app.get("/", include_in_schema=False)
def _root():
    return RedirectResponse(url="/ui/")


app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")
