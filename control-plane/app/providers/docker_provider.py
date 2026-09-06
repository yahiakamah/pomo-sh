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
