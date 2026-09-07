import datetime as dt
import re

from pydantic import BaseModel, ConfigDict, field_validator

from .config import settings

SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
MODULE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SUBDIR_RE = re.compile(r"^[A-Za-z0-9._/-]*$")


class EnvironmentCreate(BaseModel):
    slug: str
    odoo_version: str = "18"
    project_name: str = "default"
    stage: str = "development"

    repo_url: str | None = None
    git_branch: str = "main"
    addons_subdir: str = ""
    modules: list[str] = []
    git_token: str | None = None

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not SLUG_RE.match(v):
            raise ValueError(
                "slug must be dns-safe: lowercase letters, digits and hyphens, "
                "starting and ending with a letter or digit"
            )
        return v

    @field_validator("odoo_version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        v = str(v).strip()
        if v not in settings.supported_versions:
            raise ValueError(f"unsupported odoo version '{v}'")
        return v

    @field_validator("stage")
    @classmethod
    def _validate_stage(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"development", "staging", "production"}:
            raise ValueError("stage must be development, staging or production")
        return v

    @field_validator("repo_url")
    @classmethod
    def _validate_repo(cls, v):
        if v is None or v.strip() == "":
            return None
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("repo_url must be an https:// git URL")
        return v

    @field_validator("addons_subdir")
    @classmethod
    def _validate_subdir(cls, v: str) -> str:
        v = (v or "").strip().strip("/")
        if ".." in v or not SUBDIR_RE.match(v):
            raise ValueError("invalid addons_subdir")
        return v

    @field_validator("modules")
    @classmethod
    def _validate_modules(cls, v: list[str]) -> list[str]:
        out = []
        for m in v or []:
            m = m.strip()
            if not MODULE_RE.match(m):
                raise ValueError(f"invalid module name '{m}'")
            out.append(m)
        return out


class EnvironmentInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    state: str
    stage: str | None = None
    url: str | None = None
    odoo_version: str | None = None
    container_id: str | None = None
    detail: str | None = None
    project_name: str | None = None
    repo_url: str | None = None
    git_branch: str | None = None
    created_at: dt.datetime | None = None


class ProjectInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    created_at: dt.datetime | None = None
    environment_count: int = 0


class RepoCreate(BaseModel):
    repo_url: str

    @field_validator("repo_url")
    @classmethod
    def _validate(cls, v: str) -> str:
        v = (v or "").strip()
        if not v.startswith("https://"):
            raise ValueError("repo_url must be an https:// git URL")
        return v


class RepoInfo(BaseModel):
    full_name: str
    webhook_url: str
    secret: str | None = None
