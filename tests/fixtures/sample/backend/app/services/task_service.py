from app.models import Task


class TaskService:
    """Business rules for tasks."""

    def list_tasks(self) -> list[Task]:
        """Return every task."""
        return [Task()]

    def complete_task(self, task: Task) -> None:
        task.complete()
