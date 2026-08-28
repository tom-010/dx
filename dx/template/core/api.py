from django.shortcuts import get_object_or_404
from ninja import Router
from logging import getLogger
import core.schema as s
from . import models, tasks
from celery.result import AsyncResult
from celery_progress.backend import Progress
import json

api = Router()

logger = getLogger(__name__)

# set logger level info


@api.get("/boom", response=s.Success, auth=None)
def boom(request):
    # with tracer.start_as_current_span("custom-span"):
    #     # print(models.Todo.objects.count())
    #     raise Exception("Boom! This is an exception")
    raise Exception("Boom! This is an exception")


@api.get("/add", response=s.IntResult, auth=None)
def add(request, a: int, b: int):
    return {"result": a + b}


@api.post("/todos", response=s.IdResult)
def create_todo(request, payload: s.TodoIn):
    todo = models.Todo.objects.create(**payload.dict())
    return {"id": todo.id}


@api.get("/todos", response=list[s.TodoOut])
def list_todos(request):
    return models.Todo.objects.all()


@api.get("/todos/stats", response=s.TodoStats)
def get_todo_stats(request):
    total = models.Todo.objects.count()
    completed = models.Todo.objects.filter(done=True).count()
    pending = total - completed
    completion_rate = (completed / total * 100) if total > 0 else 0

    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "completion_rate": round(completion_rate, 1)
    }


@api.get("/todos/history", response=list[s.TodoHistory])
def get_todo_history(request, limit: int = 10):
    todos = models.Todo.objects.order_by("-modified")[:limit]
    return [
        {
            "id": todo.id,
            "title": todo.title,
            "description": todo.description,
            "done": todo.done,
            "created": todo.created.isoformat(),
            "modified": todo.modified.isoformat()
        }
        for todo in todos
    ]


@api.get("/todos/{todo_id}", response=s.TodoOut)
def get_todo(request, todo_id: str):
    todo = get_object_or_404(models.Todo, id=todo_id)
    return todo


@api.put("/todos/{todo_id}/set_done")
def set_todo_done(request, todo_id: str, payload: s.SetDone):
    todo = get_object_or_404(models.Todo, id=todo_id)
    todo.done = payload.done
    todo.save()
    return {"success": True, "done": todo.done}


@api.put("/todos/{todo_id}", response=s.Success)
def update_todo(request, todo_id: str, payload: s.TodoIn):
    todo = get_object_or_404(models.Todo, id=todo_id)
    todo.set_payload(payload)
    todo.save()
    return {"success": True}


@api.delete("/todos/{todo_id}")
def delete_todo(request, todo_id: str):
    todo = get_object_or_404(models.Todo, id=todo_id)
    todo.delete()
    return {"success": True}


@api.get("/me", response=s.UserSchema)
def get_me(request):
    return request.user

