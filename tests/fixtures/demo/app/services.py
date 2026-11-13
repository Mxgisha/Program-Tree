"""Service layer."""

from __future__ import annotations

from .models import Admin, User, Base
from . import util
from .util import slugify as slug


class Repository:
    def __init__(self) -> None:
        self._items: dict[str, User] = {}

    def add(self, user: User) -> None:
        self._items[user.username] = user

    def get(self, username: str) -> User | None:
        return self._items.get(username)


class UserService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def create(self, username: str) -> User:
        user = User(username=username)
        self.repo.add(user)
        return user

    def find(self, username: str) -> User | None:
        return self.repo.get(username)

    def describe(self, user: User) -> str:
        text = user.describe()
        return slug(text)

    def count(self) -> int:
        callback = self.repo.get
        users = [u for u in self.repo._items.values() if callback(u.username)]
        return len(users)


def build() -> UserService:
    repo = Repository()
    admin = Admin("root")
    repo.add(admin)
    return UserService(repo)
