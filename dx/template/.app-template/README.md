# Django App Cookiecutter Template

This is a cookiecutter template for creating new Django apps with a structure similar to the core app.

## Usage

1. Install cookiecutter:
```bash
pip install cookiecutter
```

2. Generate a new app from this template:
```bash
cookiecutter .app-template/
```

3. Enter the app name when prompted (use snake_case, e.g., `product_catalog`, `user_profile`)

4. Move the generated app to your Django project

5. Add the app to `INSTALLED_APPS` in your Django settings:
```python
INSTALLED_APPS = [
    # ...
    'your_app_name',
]
```

6. Add the API router to your main urls.py:
```python
from your_app_name import api as your_app_api

api.add_router("/your_app_name", your_app_api.api, tags=["your_app_name"])
```

7. Run migrations:
```bash
python manage.py makemigrations your_app_name
python manage.py migrate
```

## What's Generated

- **models.py**: A model named after your app (snake_case converted to CamelCase)
- **api.py**: Full CRUD API endpoints using Django Ninja
- **schema.py**: Input/Output schemas for the API
- **tasks.py**: Sample Celery tasks
- **admin.py**: Auto-registration of models using core helper
- **apps.py**: Django app configuration
- **tests/**: Basic test files for models and API

## Example

If you enter `product_catalog` as the app name:
- Model: `ProductCatalog`
- API endpoints: `/product_catalog`
- App config: `ProductCatalogConfig`