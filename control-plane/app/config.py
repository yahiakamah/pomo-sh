import os


class Settings:
    postgres_host: str = os.getenv("POSTGRES_HOST", "postgres")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_admin_user: str = os.getenv("POSTGRES_ADMIN_USER", "postgres")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "")

    control_db: str = os.getenv("CONTROL_DB", "pomo_control")
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")

    base_domain: str = os.getenv("BASE_DOMAIN", "pomotech.sh")
    edge_network: str = os.getenv("EDGE_NETWORK", "pomo_edge")
    internal_network: str = os.getenv("INTERNAL_NETWORK", "pomo_internal")
    cert_resolver: str = os.getenv("CERT_RESOLVER", "cloudflare")

    default_odoo_image: str = os.getenv("DEFAULT_ODOO_IMAGE", "odoo:{version}")
    enterprise_odoo_image: str = os.getenv("ENTERPRISE_ODOO_IMAGE", "pomo-odoo-ee:{version}")
    odoo_core_addons_path: str = os.getenv("ODOO_CORE_ADDONS_PATH", "/usr/lib/python3/dist-packages/odoo/addons")
    docker_api_version: str = os.getenv("DOCKER_API_VERSION", "auto")
    backups_dir: str = os.getenv("BACKUPS_DIR", "/backups")
    host_backups_dir: str = os.getenv("HOST_BACKUPS_DIR", "/opt/pomo/backups")
    jwt_secret: str = os.getenv("JWT_SECRET", "")
    jwt_expire_hours: int = int(os.getenv("JWT_EXPIRE_HOURS", "12"))
    admin_email: str = os.getenv("ADMIN_EMAIL", "")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")

    supported_versions = {"16", "17", "18", "19"}

    @property
    def database_url(self) -> str:
        override = os.getenv("DATABASE_URL")
        if override:
            return override
        return (
            f"postgresql+psycopg://{self.postgres_admin_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.control_db}"
        )


settings = Settings()
