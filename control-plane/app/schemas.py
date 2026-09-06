import datetime as dt
import re

from pydantic import BaseModel, ConfigDict, field_validator

from .config import settings

SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")


class EnvironmentCreate(BaseModel):
    slug: str
    odoo_version: str = "18"
    project_name: str = "default"
    stage: str = "development"

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
    created_at: dt.datetime | None = None


class ProjectInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    created_at: dt.datetime | None = None
    environment_count: int = 0
