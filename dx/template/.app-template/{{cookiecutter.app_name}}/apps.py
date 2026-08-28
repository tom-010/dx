from django.apps import AppConfig


class {{ cookiecutter.app_name_capitalized }}Config(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "{{ cookiecutter.app_name }}"