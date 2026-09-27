"""Trusted execution backend seam; generated code is always handled by an isolated backend."""

from dataclasses import dataclass
from typing import Protocol


class ExecutionBackend(Protocol):
    name: str
    version: int

    async def request(
        self, identity: str, payload: dict | None = None, *, acknowledge=False, cancel=False
    ) -> dict: ...


@dataclass(frozen=True)
class ProjectBroker:
    name: str = "python-project-v3"
    version: int = 1

    async def request(self, identity, payload=None, *, acknowledge=False, cancel=False):
        from .project_sandbox import project_request

        return await project_request(identity, payload, acknowledge=acknowledge, cancel=cancel)


class ExecutionBackends:
    def __init__(self, backends=()):
        self.backends = {}
        for backend in (ProjectBroker(), *backends):
            if backend.name in self.backends:
                raise ValueError("Duplicate execution backend")
            self.backends[backend.name] = backend

    def get(self, name, version=None):
        backend = self.backends.get(name)
        if backend is None or (version is not None and backend.version != version):
            raise ValueError("execution_backend_unavailable")
        return backend
