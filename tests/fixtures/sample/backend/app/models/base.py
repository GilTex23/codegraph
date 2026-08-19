"""Declarative base."""


class Base:
    """Root of the model hierarchy."""

    def save(self) -> None:
        """Persist the row."""
        commit()


def commit() -> None:
    """Flush the session."""
