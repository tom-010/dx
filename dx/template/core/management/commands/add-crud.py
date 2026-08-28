import importlib
from pathlib import Path

import djclick as click
from django.apps import apps
from jinja2 import Template
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

console = Console()


# Jinja2 template for API endpoints
API_TEMPLATE = """

@api.post("/{{ model_name_lower }}", response=s.IdResult)
def create_{{ model_name_lower }}(request, payload: s.{{ model_name }}In):
    \"\"\"Create a new {{ model_name }}\"\"\"
    item = models.{{ model_name }}.objects.create(**payload.dict())
    return {"id": item.id}


@api.get("/{{ model_name_lower }}", response=list[s.{{ model_name }}Out])
def list_{{ model_name_lower }}(request):
    \"\"\"List all {{ model_name }}s\"\"\"
    return models.{{ model_name }}.objects.all()


@api.get("/{{ model_name_lower }}/{item_id}", response=s.{{ model_name }}Out)
def get_{{ model_name_lower }}(request, item_id: str):
    \"\"\"Get a specific {{ model_name }} by ID\"\"\"
    item = get_object_or_404(models.{{ model_name }}, id=item_id)
    return item


@api.put("/{{ model_name_lower }}/{item_id}", response=s.Success)
def update_{{ model_name_lower }}(request, item_id: str, payload: s.{{ model_name }}In):
    \"\"\"Update a {{ model_name }}\"\"\"
    item = get_object_or_404(models.{{ model_name }}, id=item_id)
    item.set_payload(payload)
    item.save()
    return {"success": True}


@api.patch("/{{ model_name_lower }}/{item_id}", response=s.Success)
def patch_{{ model_name_lower }}(request, item_id: str, payload: s.{{ model_name }}Patch):
    \"\"\"Partially update a {{ model_name }}\"\"\"
    item = get_object_or_404(models.{{ model_name }}, id=item_id)
    item.set_payload_partial(payload)
    item.save()
    return {"success": True}


@api.delete("/{{ model_name_lower }}/{item_id}", response=s.Success)
def delete_{{ model_name_lower }}(request, item_id: str):
    \"\"\"Delete a {{ model_name }}\"\"\"
    item = get_object_or_404(models.{{ model_name }}, id=item_id)
    item.delete()
    return {"success": True}
"""

# Jinja2 template for schemas
SCHEMA_TEMPLATE = """

class {{ model_name }}In(Schema):
    \"\"\"Input schema for creating/updating {{ model_name }}\"\"\"
    {%- for field_name, field_type in fields %}
    {{ field_name }}: {{ field_type }}
    {%- endfor %}


class {{ model_name }}Out({{ model_name }}In):
    \"\"\"Output schema for {{ model_name }} with ID\"\"\"
    id: str


class {{ model_name }}Patch(Schema):
    \"\"\"Schema for partial updates to {{ model_name }}\"\"\"
    {%- for field_name, field_type in optional_fields %}
    {{ field_name }}: {{ field_type }}
    {%- endfor %}
"""


def camel_to_snake(name):
    """Convert CamelCase to snake_case"""
    import re
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def get_field_type(field):
    """Map Django field types to Python/Pydantic types"""
    from django.db import models

    type_mapping = {
        models.CharField: "str",
        models.TextField: "str",
        models.IntegerField: "int",
        models.FloatField: "float",
        models.DecimalField: "float",
        models.BooleanField: "bool",
        models.DateField: "str",  # Use str for dates in API
        models.DateTimeField: "str",  # Use str for datetimes in API
        models.EmailField: "str",
        models.URLField: "str",
        models.SlugField: "str",
        models.UUIDField: "str",
        models.JSONField: "dict",
    }

    field_class = field.__class__

    # Check parent classes if direct match not found
    for field_type, python_type in type_mapping.items():
        if isinstance(field, field_type):
            return python_type

    return "str"  # Default to string


def get_default_value(field):
    """Get default value representation for a field"""
    from django.db import models

    if field.null or field.blank:
        return "None"
    elif hasattr(field, 'default') and field.default != models.NOT_PROVIDED:
        if isinstance(field.default, bool):
            return str(field.default)
        elif isinstance(field.default, (int, float)):
            return str(field.default)
        elif isinstance(field.default, str):
            return f'"{field.default}"'
        else:
            return '""'
    elif isinstance(field, models.BooleanField):
        return "False"
    elif isinstance(field, (models.CharField, models.TextField)):
        return '""'
    else:
        return None


def generate_crud_code(model_class):
    """Generate CRUD API endpoints and schemas for a model"""
    model_name = model_class.__name__
    model_name_lower = camel_to_snake(model_name)

    # Get model fields (excluding auto fields and relations)
    fields = []
    optional_fields = []

    for field in model_class._meta.get_fields():
        # Skip auto-generated fields and relations
        if (field.auto_created or field.primary_key or
            field.many_to_many or field.one_to_many or
            field.name in ['id', 'created', 'modified']):
            continue

        if field.many_to_one or field.one_to_one:
            # For foreign keys, use the ID
            field_name = f"{field.name}_id"
            field_type = "str"
        else:
            field_name = field.name
            field_type = get_field_type(field)

        # Get default value
        default = get_default_value(field)

        # Format for required fields (with defaults if applicable)
        if default is not None:
            fields.append((field_name, f"{field_type} = {default}"))
        else:
            fields.append((field_name, field_type))

        # Format for optional fields (Patch schema)
        optional_fields.append((field_name, f"Optional[{field_type}] = None"))

    # Generate API code
    api_template = Template(API_TEMPLATE)
    api_code = api_template.render(
        model_name=model_name,
        model_name_lower=model_name_lower
    )

    # Generate Schema code
    schema_template = Template(SCHEMA_TEMPLATE)
    schema_code = schema_template.render(
        model_name=model_name,
        fields=fields,
        optional_fields=optional_fields
    )

    return api_code, schema_code


@click.command()
@click.argument("model_path")
@click.option("--dry-run", is_flag=True, help="Show generated code without writing to files")
def command(model_path, dry_run):
    """
    Generate CRUD endpoints for a Django model.

    MODEL_PATH should be in the format: app_name.ModelName

    Example:
        python manage.py add-crud core.Todo
        python manage.py add-crud product_catalog.Product

    This will add CRUD endpoints to the app's api.py and schemas to schema.py
    """

    console.print(Panel.fit(
        "[bold cyan]CRUD Generator[/bold cyan]\n"
        "Generate all CRUD endpoints for your model!",
        border_style="cyan"
    ))

    try:
        # Parse model path
        if '.' not in model_path:
            console.print("[red]✗[/red] Model path must be in format: app_name.ModelName")
            return

        app_name, model_name = model_path.rsplit('.', 1)

        # Get the model class
        try:
            app = apps.get_app_config(app_name)
            model_class = app.get_model(model_name)
        except LookupError:
            console.print(f"[red]✗[/red] Could not find app '{app_name}'")
            return
        except LookupError:
            console.print(f"[red]✗[/red] Could not find model '{model_name}' in app '{app_name}'")
            return

        console.print(f"\n[cyan]Generating CRUD for:[/cyan] {app_name}.{model_name}")

        # Generate the code
        api_code, schema_code = generate_crud_code(model_class)

        if dry_run:
            console.print("\n[yellow]--- Generated API Code ---[/yellow]")
            # Use print() to avoid Rich markup interpretation of [type] annotations
            print(api_code)
            console.print("\n[yellow]--- Generated Schema Code ---[/yellow]")
            print(schema_code)
            return

        # Find the app directory
        app_dir = Path(app_name)
        if not app_dir.exists():
            console.print(f"[red]✗[/red] App directory '{app_name}' not found")
            return

        api_file = app_dir / "api.py"
        schema_file = app_dir / "schema.py"

        # Check if files exist
        if not api_file.exists():
            console.print(f"[red]✗[/red] {api_file} does not exist")
            return

        if not schema_file.exists():
            console.print(f"[red]✗[/red] {schema_file} does not exist")
            return

        # Show preview and confirm
        console.print("\n[cyan]This will add:[/cyan]")
        console.print(f"  • 6 API endpoints to {api_file}")
        console.print(f"  • 3 schema classes to {schema_file}")

        if not Confirm.ask("\nProceed with code generation?"):
            console.print("[yellow]Cancelled[/yellow]")
            return

        # Append to api.py
        with open(api_file, 'a') as f:
            f.write(api_code)
        console.print(f"[green]✓[/green] Added CRUD endpoints to {api_file}")

        # Append to schema.py
        # First check if we need to add Optional import
        schema_content = schema_file.read_text()
        if "Optional" not in schema_content and "optional_fields" in schema_code.lower():
            # Add Optional import at the top
            lines = schema_content.split('\n')
            for i, line in enumerate(lines):
                if line.startswith('from typing import'):
                    if 'Optional' not in line:
                        lines[i] = line.rstrip() + ', Optional'
                    break
                elif line.startswith('from ninja import'):
                    # Add typing import before ninja import
                    lines.insert(i, 'from typing import Optional')
                    break
            schema_file.write_text('\n'.join(lines))

        # Now append the schemas
        with open(schema_file, 'a') as f:
            f.write(schema_code)
        console.print(f"[green]✓[/green] Added schemas to {schema_file}")

        # Success message
        console.print(Panel(
            f"[bold green]✨ CRUD endpoints generated successfully![/bold green]\n\n"
            f"[cyan]Generated:[/cyan]\n"
            f"• create_{camel_to_snake(model_name)}\n"
            f"• list_{camel_to_snake(model_name)}\n"
            f"• get_{camel_to_snake(model_name)}\n"
            f"• update_{camel_to_snake(model_name)}\n"
            f"• patch_{camel_to_snake(model_name)}\n"
            f"• delete_{camel_to_snake(model_name)}\n\n"
            f"[cyan]Schemas:[/cyan]\n"
            f"• {model_name}In (for create/update)\n"
            f"• {model_name}Out (with ID)\n"
            f"• {model_name}Patch (for partial updates)\n\n"
            f"[yellow]Note:[/yellow] You may need to add missing imports:\n"
            f"• from django.shortcuts import get_object_or_404\n"
            f"• from typing import Optional\n"
            f"• Import your model if needed",
            border_style="green"
        ))

    except Exception as e:
        console.print(f"[red]✗[/red] Error: {e}")
        import traceback
        if dry_run:
            traceback.print_exc()