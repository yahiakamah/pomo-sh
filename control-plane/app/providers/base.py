from abc import ABC, abstractmethod

from ..schemas import EnvironmentCreate, EnvironmentInfo


class InfrastructureProvider(ABC):
    @abstractmethod
    def create_environment(self, spec: EnvironmentCreate) -> EnvironmentInfo: ...

    @abstractmethod
    def destroy_environment(self, slug: str, drop_data: bool = False) -> None: ...

    @abstractmethod
    def get_environment(self, slug: str) -> EnvironmentInfo | None: ...

    @abstractmethod
    def list_environments(self) -> list[EnvironmentInfo]: ...

    @abstractmethod
    def start_environment(self, slug: str) -> None: ...

    @abstractmethod
    def stop_environment(self, slug: str) -> None: ...

    @abstractmethod
    def restart_environment(self, slug: str) -> None: ...

    def redeploy_environment(self, slug: str, **kwargs) -> None:
        raise NotImplementedError("redeploy_environment: NOT IMPLEMENTED")

    def scale_environment(self, slug: str, **kwargs) -> None:
        raise NotImplementedError("scale_environment: NOT IMPLEMENTED")

    def backup_environment(self, slug: str, **kwargs) -> None:
        raise NotImplementedError("backup_environment: NOT IMPLEMENTED")

    def list_backups(self, slug: str) -> list:
        raise NotImplementedError("list_backups: NOT IMPLEMENTED")

    def restore_environment(self, slug: str, **kwargs) -> None:
        raise NotImplementedError("restore_environment: NOT IMPLEMENTED")
