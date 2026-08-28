# Django Backend API

This is the Django backend service providing RESTful APIs using Django Ninja.

## Quick Commands

```bash
# Development server
poetry run python manage.py runserver

# Database operations
poetry run python manage.py makemigrations
poetry run python manage.py migrate

# Get auth token for testing
TOKEN=$(poetry run python3 manage.py token 2> /dev/null)

# Run tests
./scripts/test.sh           # Unit tests
./scripts/test_api.sh        # API tests
./scripts/coverage.sh        # Coverage report

# Code quality
./scripts/format.sh          # Format code
./scripts/linter.sh          # Run linter
```

## Directory Structure

```
api/
├── config/                  # Django configuration
│   ├── settings.py         # Main settings file
│   ├── urls.py            # Root URL configuration
│   └── wsgi.py            # WSGI application
│
├── core/                   # Main app (start here!)
│   ├── api.py             # API endpoints (Django Ninja)
│   ├── schema.py          # Request/response schemas
│   ├── models.py          # Database models
│   ├── services/          # Business logic (stateless)
│   ├── tasks.py           # Celery task definitions
│   └── management/        # Custom Django commands
│       └── commands/
│
├── scripts/               # Development scripts
└── tests/                # Test files

```

## Development Guidelines

### Where to Put Code

1. **API Endpoints** → `api.py`
   - REST endpoints using Django Ninja
   - Keep endpoint logic minimal
   - Use proper HTTP verbs and status codes

2. **Data Schemas** → `schema.py`
   - Request/response models
   - Django Ninja schemas (Pydantic-based)
   - Validation rules

3. **Database Models** → `models.py`
   - Django ORM models
   - Minimal business logic
   - Use properties for computed fields

4. **Business Logic** → `services/`
   - Stateless service functions
   - Complex computations
   - Reusable logic across endpoints

5. **Background Tasks** → `tasks.py`
   - Celery task wrappers only
   - Call service functions for logic
   - Keep tasks thin

6. **CLI Commands** → `management/commands/`
   - Django-click commands
   - Development utilities
   - Admin operations

## Working with APIs

### Creating a New Endpoint

```python
# In core/api.py
@router.post("/items", response=ItemResponse)
def create_item(request, data: ItemCreate):
    # Simple logic here
    item = Item.objects.create(**data.dict())
    return item

# In core/schema.py
class ItemCreate(Schema):
    name: str
    description: Optional[str] = None

class ItemResponse(Schema):
    id: int
    name: str
    created_at: datetime
```

### Testing Your Endpoint

```bash
# Get auth token
TOKEN=$(poetry run python3 manage.py token 2> /dev/null)

# Test the endpoint
curl -X POST "localhost:8000/api/items" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Test Item"}'
```

### Complex Logic Pattern

```python
# In services/item_service.py
def calculate_item_price(item: Item, discount: float = 0) -> Decimal:
    """Complex pricing logic goes here"""
    base_price = item.base_price
    # Complex calculations...
    return final_price

# In api.py - keep it simple
@router.get("/items/{item_id}/price")
def get_item_price(request, item_id: int, discount: float = 0):
    item = get_object_or_404(Item, id=item_id)
    price = calculate_item_price(item, discount)
    return {"price": price}
```

## Database Patterns

### JSONField with Types

```python
# models.py
class Settings(models.Model):
    config_json = models.JSONField(default=dict)

    @property
    def config(self) -> ConfigSchema:
        try:
            return ConfigSchema(**self.config_json)
        except ValidationError:
            return ConfigSchema()  # Graceful fallback

    @config.setter
    def config(self, value: ConfigSchema):
        self.config_json = value.dict()
```

### Migrations

```bash
# After changing models.py
poetry run python manage.py makemigrations
poetry run python manage.py migrate

# Check migration SQL
poetry run python manage.py sqlmigrate core 0001
```

## Custom Management Commands

### Creating a Command

```python
# management/commands/process_items.py
import djclick as click

@click.command()
@click.option('--dry-run', is_flag=True)
def command(dry_run):
    """Process all pending items"""
    items = Item.objects.filter(status='pending')

    for item in items:
        click.echo(f"Processing {item.name}")
        if not dry_run:
            process_item(item)
```

### Running Commands

```bash
poetry run python manage.py process_items --dry-run
poetry run python manage.py createadmin
poetry run python manage.py token
```

## Testing

### Unit Tests

```python
# tests/test_services.py
import pytest
from core.services import calculate_item_price

def test_calculate_price():
    item = ItemFactory()
    price = calculate_item_price(item, discount=0.1)
    assert price == Decimal("90.00")
```

### API Tests

```python
# tests/test_api.py
import pytest
from django.test import Client

@pytest.mark.api
def test_create_item(auth_client):
    response = auth_client.post('/api/items', {
        'name': 'Test Item'
    })
    assert response.status_code == 200
```

## Celery Tasks

### Task Pattern

```python
# services/email_service.py
def send_welcome_email(user_id: int):
    """Business logic for sending email"""
    user = User.objects.get(id=user_id)
    # Email logic here

# tasks.py - thin wrapper
from celery import shared_task

@shared_task
def send_welcome_email_task(user_id: int):
    """Celery task wrapper"""
    from .services import send_welcome_email
    return send_welcome_email(user_id)

# api.py - trigger the task
@router.post("/users", response=UserResponse)
def create_user(request, data: UserCreate):
    user = User.objects.create(**data.dict())
    send_welcome_email_task.delay(user.id)
    return user
```

## Code Generation Tools

### Creating New Django Apps

The `create-app` command scaffolds a complete Django app with all the boilerplate:

```bash
poetry run python manage.py create-app product_catalog

# Interactive mode (default):
# - Prompts for app name if not provided
# - Asks for confirmation before creating
# - Offers to run migrations

# Options:
--no-migrate        # Skip running migrations
--no-interactive    # Skip all prompts, use defaults

# What it does:
# 1. Creates app from .app-template cookiecutter template
# 2. Adds to INSTALLED_APPS in settings.py (finds needle comment)
# 3. Registers API router in urls.py (finds needle comment)
# 4. Creates and runs initial migrations
# 5. Sets up complete structure:
#    - api.py with Django Ninja router
#    - schema.py for request/response models
#    - models.py with example model
#    - services/ for business logic
#    - tests/ with test examples
#    - management/commands/ for CLI tools
```

Example workflow:
```bash
# Create the app
poetry run python manage.py create-app inventory

# Check generated files
ls inventory/
# api.py  models.py  schema.py  services/  tests/  management/

# Customize the model
edit inventory/models.py

# Generate CRUD endpoints (see below)
poetry run python manage.py add-crud inventory.Product
```

### Generating CRUD Endpoints

The `add-crud` command generates complete CRUD operations for existing models:

```bash
poetry run python manage.py add-crud core.Todo

# Format: app_name.ModelName
# Examples:
poetry run python manage.py add-crud core.User
poetry run python manage.py add-crud inventory.Product
poetry run python manage.py add-crud blog.Article

# Options:
--dry-run           # Preview generated code without writing files
```

What it generates:

**In api.py (6 endpoints):**
```python
# POST /api/todo - Create
@api.post("/todo", response=s.IdResult)
def create_todo(request, payload: s.TodoIn):
    item = models.Todo.objects.create(**payload.dict())
    return {"id": item.id}

# GET /api/todo - List all
@api.get("/todo", response=list[s.TodoOut])
def list_todo(request):
    return models.Todo.objects.all()

# GET /api/todo/{id} - Get one
@api.get("/todo/{item_id}", response=s.TodoOut)
def get_todo(request, item_id: str):
    item = get_object_or_404(models.Todo, id=item_id)
    return item

# PUT /api/todo/{id} - Full update
@api.put("/todo/{item_id}", response=s.Success)
def update_todo(request, item_id: str, payload: s.TodoIn):
    item = get_object_or_404(models.Todo, id=item_id)
    item.set_payload(payload)
    item.save()
    return {"success": True}

# PATCH /api/todo/{id} - Partial update
@api.patch("/todo/{item_id}", response=s.Success)
def patch_todo(request, item_id: str, payload: s.TodoPatch):
    item = get_object_or_404(models.Todo, id=item_id)
    item.set_payload_partial(payload)
    item.save()
    return {"success": True}

# DELETE /api/todo/{id} - Delete
@api.delete("/todo/{item_id}", response=s.Success)
def delete_todo(request, item_id: str):
    item = get_object_or_404(models.Todo, id=item_id)
    item.delete()
    return {"success": True}
```

**In schema.py (3 schemas):**
```python
class TodoIn(Schema):
    """Input schema for creating/updating Todo"""
    title: str
    completed: bool = False

class TodoOut(TodoIn):
    """Output schema with ID"""
    id: str

class TodoPatch(Schema):
    """Schema for partial updates"""
    title: Optional[str] = None
    completed: Optional[bool] = None
```

The generator:
- Analyzes your model fields automatically
- Skips auto-generated fields (id, created, modified)
- Handles foreign keys properly (uses field_id)
- Sets appropriate defaults for optional fields
- Adds proper type hints and documentation

After generation, you may need to:
1. Add missing imports (`from django.shortcuts import get_object_or_404`)
2. Implement `set_payload()` and `set_payload_partial()` methods on your model
3. Customize the generated code to your needs
4. Run `cd ../app && ./scripts/sync_schema.sh` to update frontend SDK

## Environment Variables

Key settings from `.env`:

```env
# Database
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_DB=starter

# Django
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# Redis (for Celery)
REDIS_URL=redis://localhost:6379/0
```

## Performance Tips

1. **Use select_related/prefetch_related** for ORM queries
2. **Paginate large responses** using Django Ninja's pagination
3. **Cache expensive computations** with Redis
4. **Use database indexes** for frequently queried fields
5. **Keep API responses lean** - only return needed fields

## Security Notes

- JWT authentication is configured by default
- Always validate input using schemas
- Use Django's ORM to prevent SQL injection
- Never expose sensitive fields in API responses
- Rate limiting is configured for API endpoints

## Debugging

```bash
# Django shell
poetry run python manage.py shell

# Database shell
poetry run python manage.py dbshell

# Show SQL queries
# Set DEBUG=True and install django-debug-toolbar

# Check migrations
poetry run python manage.py showmigrations
```

## Common Issues

### Import Errors
- Ensure you're in the virtual environment
- Run `poetry install` to install dependencies

### Migration Conflicts
```bash
poetry run python manage.py migrate --fake-zero core
poetry run python manage.py migrate
```

### Celery Not Running
- In development, Celery runs synchronously
- For async: `./scripts/celery_worker.sh` in separate terminal