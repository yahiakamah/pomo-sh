import datetime
import json
import os
import secrets

import docker
from docker.errors import APIError, NotFound

from ..config import settings
from ..db import PgAdmin
from ..schemas import EnvironmentCreate, EnvironmentInfo
from .base import InfrastructureProvider

MANAGED_LABEL = "pomo.managed"
SLUG_LABEL = "pomo.slug"
VERSION_LABEL = "pomo.odoo_version"

_STATE_MAP = {
    "running": "running",
    "restarting": "provisioning",
    "created": "stopped",
    "exited": "stopped",
    "paused": "stopped",
    "dead": "error",
}

GIT_IMAGE = "alpine/git"
REPO_MOUNT = "/mnt/repo"


def _container_name(slug: str) -> str:
    return f"pomo-inst-{slug}"


def _db_name(slug: str) -> str:
    return slug


def _role_name(slug: str) -> str:
    return f"odoo_{slug}"


def _data_volume(slug: str) -> str:
    return f"pomo_odoo_{slug}"


def _src_volume(slug: str) -> str:
    return f"pomo_src_{slug}"


def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fsize(path: str) -> int:
    return os.path.getsize(path) if os.path.isfile(path) else 0


class DockerProvider(InfrastructureProvider):
    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None
        self.pg = PgAdmin()

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env(version=settings.docker_api_version)
        return self._client

    def _image(self, version: str) -> str:
        return settings.default_odoo_image.format(version=version)

    def _url(self, slug: str) -> str:
        return f"https://{slug}.{settings.base_domain}"

    def _odoo_env(self, slug: str, password: str) -> dict:
        return {
            "HOST": settings.postgres_host,
            "PORT": str(settings.postgres_port),
            "USER": _role_name(slug),
            "PASSWORD": password,
        }

    def _addons_path(self, subdir: str) -> str:
        repo_path = REPO_MOUNT + (f"/{subdir}" if subdir else "")
        return f"--addons-path={settings.odoo_core_addons_path},{repo_path}"

    def _traefik_labels(self, slug: str, version: str) -> dict:
        router = f"pomo-{slug}"
        host = f"{slug}.{settings.base_domain}"
        return {
            MANAGED_LABEL: "true",
            SLUG_LABEL: slug,
            VERSION_LABEL: version,
            "traefik.enable": "true",
            "traefik.docker.network": settings.edge_network,
            f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
            f"traefik.http.routers.{router}.entrypoints": "websecure",
            f"traefik.http.routers.{router}.tls.certresolver": settings.cert_resolver,
            f"traefik.http.services.{router}.loadbalancer.server.port": "8069",
        }

    def _find(self, slug: str):
        found = self.client.containers.list(all=True, filters={"label": f"{SLUG_LABEL}={slug}"})
        return found[0] if found else None

    def _info(self, container) -> EnvironmentInfo:
        slug = container.labels.get(SLUG_LABEL, "?")
        return EnvironmentInfo(
            slug=slug,
            state=_STATE_MAP.get(container.status, container.status),
            url=self._url(slug),
            odoo_version=container.labels.get(VERSION_LABEL),
            container_id=container.short_id,
        )

    def _clone_repo(self, slug: str, repo_url: str, branch: str, token: str | None) -> str:
        vol = _src_volume(slug)
        try:
            self.client.volumes.get(vol).remove(force=True)
        except NotFound:
            pass

        if token:
            auth_url = repo_url.replace("https://", f"https://{token}@", 1)
            script = (
                f"set -e; git clone --branch {branch} --depth 1 {auth_url} /repo; "
                f"git -C /repo remote set-url origin {repo_url}"
            )
        else:
            script = f"set -e; git clone --branch {branch} --depth 1 {repo_url} /repo"

        try:
            self.client.containers.run(
                GIT_IMAGE,
                entrypoint="",
                command=["sh", "-c", script],
                volumes={vol: {"bind": "/repo", "mode": "rw"}},
                remove=True,
                detach=False,
            )
        except docker.errors.DockerException:
            raise RuntimeError(
                f"git clone failed for {repo_url} (branch '{branch}'). "
                "Check the URL, branch, and token/permissions."
            ) from None
        return vol

    def create_environment(self, spec: EnvironmentCreate) -> EnvironmentInfo:
        slug, version = spec.slug, spec.odoo_version
        if self._find(slug):
            raise ValueError(f"environment '{slug}' already exists")

        image = self._image(version)
        db, role = _db_name(slug), _role_name(slug)
        password = secrets.token_urlsafe(18)

        try:
            self.client.images.get(image)
        except NotFound:
            self.client.images.pull(image)

        src_vol = None
        odoo_volumes = {_data_volume(slug): {"bind": "/var/lib/odoo", "mode": "rw"}}
        addons_arg = None
        if spec.repo_url:
            src_vol = self._clone_repo(slug, spec.repo_url, spec.git_branch, spec.git_token)
            odoo_volumes[src_vol] = {"bind": REPO_MOUNT, "mode": "rw"}
            addons_arg = self._addons_path(spec.addons_subdir)

        self.pg.ensure_role_and_db(db, role, password)
        env = self._odoo_env(slug, password)

        init_cmd = ["odoo", "-d", db, "-i", ",".join(["base", *spec.modules]),
                    "--stop-after-init", "--no-http"]
        if addons_arg:
            init_cmd.append(addons_arg)
        init_volumes = {}
        if src_vol:
            init_volumes[src_vol] = {"bind": REPO_MOUNT, "mode": "rw"}
        self.client.containers.run(
            image, command=init_cmd, environment=env,
            network=settings.internal_network, volumes=init_volumes,
            remove=True, detach=False,
        )

        run_cmd = ["odoo", "--proxy-mode", f"--db-filter=^{db}$"]
        if addons_arg:
            run_cmd.append(addons_arg)
        container = self.client.containers.create(
            image,
            name=_container_name(slug),
            command=run_cmd,
            environment=env,
            labels=self._traefik_labels(slug, version),
            volumes=odoo_volumes,
            network=settings.internal_network,
            restart_policy={"Name": "unless-stopped"},
            detach=True,
        )
        self.client.networks.get(settings.edge_network).connect(container)
        container.start()

        container.reload()
        return self._info(container)

    def destroy_environment(self, slug: str, drop_data: bool = False) -> None:
        container = self._find(slug)
        if container:
            try:
                container.remove(force=True)
            except APIError:
                pass
        if drop_data:
            self.pg.drop_db(_db_name(slug))
            self.pg.drop_role(_role_name(slug))
            for vol in (_data_volume(slug), _src_volume(slug)):
                try:
                    self.client.volumes.get(vol).remove(force=True)
                except NotFound:
                    pass

    def get_environment(self, slug: str) -> EnvironmentInfo | None:
        container = self._find(slug)
        return self._info(container) if container else None

    def list_environments(self) -> list[EnvironmentInfo]:
        containers = self.client.containers.list(all=True, filters={"label": f"{MANAGED_LABEL}=true"})
        return [self._info(c) for c in containers]

    def start_environment(self, slug: str) -> None:
        container = self._find(slug)
        if not container:
            raise ValueError(f"environment '{slug}' not found")
        container.start()

    def stop_environment(self, slug: str) -> None:
        container = self._find(slug)
        if not container:
            raise ValueError(f"environment '{slug}' not found")
        container.stop()

    def restart_environment(self, slug: str) -> None:
        container = self._find(slug)
        if not container:
            raise ValueError(f"environment '{slug}' not found")
        container.restart()


    def redeploy_environment(self, slug, branch="main", modules=None, addons_subdir=""):
        container = self._find(slug)
        if not container:
            raise ValueError(f"environment '{slug}' not found")
        if not self.client.volumes.list(filters={"name": _src_volume(slug)}):
            raise RuntimeError(f"environment '{slug}' has no git source")

        raw = container.attrs.get("Config", {}).get("Env", []) or []
        envd = dict(e.split("=", 1) for e in raw if "=" in e)
        env = {k: envd[k] for k in ("HOST", "PORT", "USER", "PASSWORD") if k in envd}
        image = container.attrs.get("Config", {}).get("Image") or self._image("18")
        db = _db_name(slug)
        src_vol = _src_volume(slug)

        pull = (f"set -e; git -C /repo fetch --depth 1 origin {branch}; "
                f"git -C /repo reset --hard origin/{branch}")
        try:
            self.client.containers.run(
                GIT_IMAGE, entrypoint="", command=["sh", "-c", pull],
                volumes={src_vol: {"bind": "/repo", "mode": "rw"}},
                remove=True, detach=False,
            )
        except docker.errors.DockerException:
            raise RuntimeError(f"git pull failed for '{slug}' (branch '{branch}')") from None

        mods = ",".join(modules) if modules else "all"
        upd = ["odoo", "-d", db, "-u", mods, "--stop-after-init", "--no-http",
               self._addons_path(addons_subdir or "")]
        self.client.containers.run(
            image, command=upd, environment=env,
            network=settings.internal_network,
            volumes={src_vol: {"bind": REPO_MOUNT, "mode": "rw"}},
            remove=True, detach=False,
        )

        container.restart()
        container.reload()
        return self._info(container)


    def backup_environment(self, slug, **kwargs):
        if not self._find(slug):
            raise ValueError(f"environment '{slug}' not found")
        ts = _ts()
        db = _db_name(slug)
        host_dir = os.path.join(settings.host_backups_dir, slug, ts)
        local_dir = os.path.join(settings.backups_dir, slug, ts)
        os.makedirs(local_dir, exist_ok=True)

        self.client.containers.run(
            "postgres:16",
            command=["pg_dump", "-h", settings.postgres_host, "-p", str(settings.postgres_port),
                     "-U", settings.postgres_admin_user, "-d", db,
                     "--no-owner", "--no-privileges", "-f", "/out/dump.sql"],
            environment={"PGPASSWORD": settings.postgres_password},
            network=settings.internal_network,
            volumes={host_dir: {"bind": "/out", "mode": "rw"}},
            remove=True, detach=False,
        )

        self.client.containers.run(
            "alpine", entrypoint="",
            command=["sh", "-c", "tar czf /out/filestore.tar.gz -C /data . 2>/dev/null || true"],
            volumes={_data_volume(slug): {"bind": "/data", "mode": "ro"},
                     host_dir: {"bind": "/out", "mode": "rw"}},
            remove=True, detach=False,
        )

        meta = {
            "slug": slug, "timestamp": ts, "db": db,
            "dump_bytes": _fsize(os.path.join(local_dir, "dump.sql")),
            "filestore_bytes": _fsize(os.path.join(local_dir, "filestore.tar.gz")),
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with open(os.path.join(local_dir, "meta.json"), "w") as f:
            json.dump(meta, f)
        return meta

    def list_backups(self, slug):
        base_dir = os.path.join(settings.backups_dir, slug)
        out = []
        if not os.path.isdir(base_dir):
            return out
        for ts in sorted(os.listdir(base_dir), reverse=True):
            meta_p = os.path.join(base_dir, ts, "meta.json")
            if os.path.isfile(meta_p):
                try:
                    with open(meta_p) as f:
                        out.append(json.load(f))
                except Exception:  # noqa: BLE001
                    pass
        return out


    def restore_environment(self, slug, timestamp, **kwargs):
        container = self._find(slug)
        if not container:
            raise ValueError(f"environment '{slug}' not found")
        local_dir = os.path.join(settings.backups_dir, slug, timestamp)
        host_dir = os.path.join(settings.host_backups_dir, slug, timestamp)
        if not os.path.isfile(os.path.join(local_dir, "dump.sql")):
            raise RuntimeError(f"backup '{timestamp}' not found for '{slug}'")

        db = _db_name(slug)
        role = _role_name(slug)
        raw = container.attrs.get("Config", {}).get("Env", []) or []
        envd = dict(e.split("=", 1) for e in raw if "=" in e)
        role_pw = envd.get("PASSWORD", "")

        container.stop()
        self.pg.recreate_database(db, role)

        self.client.containers.run(
            "postgres:16",
            command=["psql", "-h", settings.postgres_host, "-p", str(settings.postgres_port),
                     "-U", role, "-d", db, "-v", "ON_ERROR_STOP=1",
                     "-q", "-f", "/in/dump.sql"],
            environment={"PGPASSWORD": role_pw},
            network=settings.internal_network,
            volumes={host_dir: {"bind": "/in", "mode": "ro"}},
            remove=True, detach=False,
        )

        if os.path.isfile(os.path.join(local_dir, "filestore.tar.gz")):
            self.client.containers.run(
                "alpine", entrypoint="",
                command=["sh", "-c",
                         "rm -rf /data/* /data/..?* 2>/dev/null; "
                         "tar xzf /in/filestore.tar.gz -C /data 2>/dev/null || true"],
                volumes={_data_volume(slug): {"bind": "/data", "mode": "rw"},
                         host_dir: {"bind": "/in", "mode": "ro"}},
                remove=True, detach=False,
            )

        container.start()
        container.reload()
        return self._info(container)
