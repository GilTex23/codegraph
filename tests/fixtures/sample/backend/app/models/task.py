from .base import Base

STATUS_DONE = "done"


class Task(Base):
    """A unit of work."""

    def complete(self) -> None:
        """Mark the task done."""
        self.save()
