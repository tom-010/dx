from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from {{ cookiecutter.app_name }}.models import {{ cookiecutter.model_name }}

User = get_user_model()


class {{ cookiecutter.model_name }}APITestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123'
        )
        self.client.force_login(self.user)

        self.item = {{ cookiecutter.model_name }}.objects.create(
            title="Test {{ cookiecutter.model_name }}",
            description="Test description",
            is_active=True
        )

    def test_list_{{ cookiecutter.app_name }}(self):
        """Test listing all {{ cookiecutter.model_name }} items"""
        response = self.client.get('/api/{{ cookiecutter.app_name }}/{{ cookiecutter.app_name }}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_get_{{ cookiecutter.app_name }}(self):
        """Test getting a specific {{ cookiecutter.model_name }} item"""
        response = self.client.get(f'/api/{{ cookiecutter.app_name }}/{{ cookiecutter.app_name }}/{self.item.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['title'], "Test {{ cookiecutter.model_name }}")

    def test_create_{{ cookiecutter.app_name }}(self):
        """Test creating a new {{ cookiecutter.model_name }} item"""
        payload = {
            "title": "New {{ cookiecutter.model_name }}",
            "description": "New description",
            "is_active": True
        }
        response = self.client.post(
            '/api/{{ cookiecutter.app_name }}/{{ cookiecutter.app_name }}',
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('id', data)

        # Verify the item was created
        item = {{ cookiecutter.model_name }}.objects.get(id=data['id'])
        self.assertEqual(item.title, "New {{ cookiecutter.model_name }}")