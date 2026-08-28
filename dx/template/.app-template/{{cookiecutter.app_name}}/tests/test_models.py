from django.test import TestCase
from {{ cookiecutter.app_name }}.models import {{ cookiecutter.model_name }}


class {{ cookiecutter.model_name }}TestCase(TestCase):
    def setUp(self):
        self.item = {{ cookiecutter.model_name }}.objects.create(
            title="Test {{ cookiecutter.model_name }}",
            description="Test description",
            is_active=True
        )

    def test_{{ cookiecutter.app_name }}_creation(self):
        """Test that {{ cookiecutter.model_name }} can be created"""
        self.assertIsNotNone(self.item.id)
        self.assertEqual(self.item.title, "Test {{ cookiecutter.model_name }}")
        self.assertEqual(self.item.description, "Test description")
        self.assertTrue(self.item.is_active)

    def test_{{ cookiecutter.app_name }}_str(self):
        """Test the string representation of {{ cookiecutter.model_name }}"""
        self.assertEqual(str(self.item), "Test {{ cookiecutter.model_name }}")