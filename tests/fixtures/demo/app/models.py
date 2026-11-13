"""Domain models."""

from dataclasses import dataclass, field


class Base:
    """Common base class."""

    kind = "base"

    def describe(self) -> str:
        return f"{self.kind}:{self.name()}"

    def name(self) -> str:
        raise NotImplementedError


@dataclass
class User(Base):
    username: str
    email: str = ""
    tags: list[str] = field(default_factory=list)

    kind = "user"

    def name(self) -> str:
        return self.username

    def label(self) -> str:
        return self.describe().upper()


class Admin(User):
    def __init__(self, username: str, level: int = 1) -> None:
        super().__init__(username)
        self.level = level

    def name(self) -> str:
        return f"admin:{self.username}"

    def label(self) -> str:
        return super().name().upper()

    def promote(self, role: "Role") -> "Role":
        return role
