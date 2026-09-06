from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import tasks
from .config import settings
from .database import SessionLocal, init_db
from .models import Environment, Project
from .providers.docker_provider import DockerProvider
from .queue import task_queue
from .schemas import EnvironmentCreate, EnvironmentInfo, ProjectInfo


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
        elif row.state not in ("queued", "provisioning", "error", "destroying"):
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
    if env.state not in ("queued", "provisioning", "error", "destroying"):
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
