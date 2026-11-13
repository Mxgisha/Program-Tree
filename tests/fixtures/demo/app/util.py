"""Helpers."""

import re

_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _PATTERN.sub("-", text.lower()).strip("-")


def unused_helper() -> None:
    return None


class Multiplier:
    """A callable object, the way many frameworks wrap handlers."""

    def __init__(self, factor: int) -> None:
        self.factor = factor

    def __call__(self, value: int) -> int:
        return value * self.factor


triple = Multiplier(3)


def scale(value: int) -> int:
    return triple(value)
