from fastapi import APIRouter

from app.services.task_service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])
service = TaskService()


@router.get("/{task_id}")
async def get_task(task_id: int):
    """Fetch one task."""
    return service.list_tasks()


@router.post("")
async def create_task():
    """Create a task."""
    return service.list_tasks()
