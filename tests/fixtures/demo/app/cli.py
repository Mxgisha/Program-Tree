"""A tiny CLI that consumes the package re-exports."""

from app import User
from app import util as u
from app.services import build


def make(name: str) -> User:
    user = User(username=name)
    return user


def render(value: str) -> str:
    return u.slugify(value)


def run(username: str) -> str:
    service = build()
    user = service.create(username)
    return render(user.name())
