# Django REST API Backend

This is the Django backend for a **Backend-for-Frontend (BFF)** architecture. This API serves as the primary business logic layer and data provider for the Next.js frontend (`/app`). The frontend calls this API through type-safe generated clients to get properly structured data.

## Development Philosophy

- **KISS (Keep It Simple, Stupid)**: Simplicity is king. The main goal is that the system is easy to understand
- **Start with Core**: Add all functionality to the `core` Django app first, then split into self-contained apps when needed
- **TDD for Complex Logic**: Implement complex things using TDD, but only complex things
- **Simple Things in api.py**: Simple logic can go directly in `api.py`; complex/reusable logic gets its own service in `services/`

## Bash Commands

```bash
# Development
poetry run python manage.py runserver              # Start Django dev server (localhost:8000)
poetry run python manage.py makemigrations         # Create new migrations
poetry run python manage.py migrate                # Run database migrations

# Testing & Quality
./scripts/test.sh                                   # Run unit tests
./scripts/test_api.sh                               # Run API tests
./scripts/coverage.sh                               # Run tests with coverage
./scripts/format.sh                                 # Format Python code with ruff
./scripts/linter.sh                                 # Run ruff linter

# Django Management
poetry run python manage.py createadmin            # Create Django superuser (admin/admin)
poetry run python manage.py token                  # Get JWT token for API testing
poetry run python manage.py create-app MyApp       # Create new Django app (uses cookiecutter template)
poetry run python manage.py add-crud core.ModelName  # Generate CRUD endpoints

# API Testing
TOKEN=$(poetry run python3 manage.py token 2> /dev/null)
curl -X "GET" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    "localhost:8000/api/your-endpoint"
```

## Code Style & Conventions

### Django Structure
- Use Django Ninja for API endpoints in `api.py`
- Define schemas/serializers in `schema.py`
- Business logic goes in `services/` directory (stateless functions)
- Models in `models.py` with minimal logic
- Commands using django-click in `management/commands/`
- Always validate API changes with curl before committing

### JSONField Pattern
When adding JSONField to models:
```python
# In models.py
class MyModel(models.Model):
    config_json = models.JSONField(default=dict)

    @property
    def config(self) -> MyConfigSchema:
        try:
            return MyConfigSchema(**self.config_json)
        except ValidationError:
            return MyConfigSchema()  # Handle corrupt data gracefully

    @config.setter
    def config(self, value: MyConfigSchema):
        self.config_json = value.dict()
```
- Suffix JSONField with `_json`
- Add typed property using Django Ninja schema
- Handle database corruption gracefully

## Workflows

### Backend Development Workflow
1. Update `models.py` → `poetry run python manage.py makemigrations && python manage.py migrate`
2. Add/update endpoints in `api.py` and schemas in `schema.py`
3. Add unit tests if complex logic was added to `services/`
4. Test with curl: `TOKEN=$(poetry run python3 manage.py token 2> /dev/null)`
5. `curl -H "Authorization: Bearer $TOKEN" localhost:8000/api/endpoint`
6. **Important**: If frontend integration needed, notify to run schema sync in `/app`

### Adding Celery Tasks
1. Create service function in `services/` (contains the actual logic)
2. Add task wrapper in `tasks.py` (no logic, just plumbing to call service)
3. Add management command in `management/commands/` (for CLI access)
4. Update `api.py` to expose task endpoint via REST API
5. **Important**: Frontend will need to regenerate SDK after API changes

## Project Structure

```
/api                           # You are here - Django backend
├── config/                    # Django configuration & settings
│   ├── settings.py           # Main Django settings
│   ├── urls.py               # URL routing
│   └── ...
├── core/                      # Main Django app (start here)
│   ├── api.py                # API endpoints (Django Ninja)
│   ├── schema.py             # API schemas/serializers
│   ├── models.py             # Database models
│   ├── services/             # Business logic (stateless functions)
│   ├── management/commands/  # Django click commands
│   ├── tasks.py              # Celery task definitions
│   └── tests/                # Unit tests
├── .app-template/            # Cookiecutter template for new apps
└── scripts/                  # Development scripts
```

## Common Patterns

### API Testing Pattern
Always test your API changes:
```bash
# Get token and test endpoint
TOKEN=$(poetry run python3 manage.py token 2> /dev/null)
curl -X "GET" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "localhost:8000/api/your-endpoint"

# For POST requests with data
curl -X "POST" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"field": "value"}' \
    "localhost:8000/api/your-endpoint"
```

### Django App Creation Pattern
1. **Start simple**: add functionality to `core` app first
2. **When it grows**: `python manage.py create-app NewAppName` (uses .app-template)
3. **Split functionality** into the new app when it becomes self-contained

### Business Logic Placement
- **Simple logic**: Direct in `api.py`
- **Complex/reusable logic**: Create service in `services/` directory
- **Long-running tasks**: Celery task in `tasks.py` that calls a service function
- **Reusable commands**: Django management command in `management/commands/`

### Model Development Pattern
```python
# 1. Create/update model in models.py
class MyModel(models.Model):
    name = models.CharField(max_length=100)
    config_json = models.JSONField(default=dict)

# 2. Create migration
# poetry run python manage.py makemigrations

# 3. Apply migration
# poetry run python manage.py migrate

# 4. Add to schema.py
class MyModelSchema(Schema):
    name: str
    config: dict

# 5. Add to api.py
@api.get("/mymodel", response=List[MyModelSchema])
def list_mymodels(request):
    return MyModel.objects.all()
```

## Development Environment

- Python 3.11+ with Poetry for dependency management
- PostgreSQL database
- Redis for Celery (tasks run synchronously in development mode)
- Default admin credentials: admin/admin
- Django admin available at: `localhost:8000/admin`
- API docs available at: `localhost:8000/api/docs`

## Integration Notes

This API is consumed by a Next.js frontend (`/app`) through:
- **Generated TypeScript SDK**: Frontend uses auto-generated client from OpenAPI specs
- **BFF Pattern**: Frontend API routes aggregate calls to this API and shape data for UI
- **Schema Synchronization**: When you change `api.py` or `schema.py`, frontend needs to regenerate types

**After API changes**: Let frontend team know to run `cd app && ./scripts/sync_schema.sh`