from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..models import Candidate


class Provider(ABC):
    name: ClassVar[str]

    @property
    @abstractmethod
    def configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def search(self, query: str, limit: int) -> list[Candidate]:
        raise NotImplementedError
