from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from persons.models import Persons

User = get_user_model()


class PersonsAPITestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123'
        )
        self.client.force_login(self.user)

        self.item = Persons.objects.create(
            title="Test Persons",
            description="Test description",
            is_active=True
        )

    def test_list_persons(self):
        """Test listing all Persons items"""
        response = self.client.get('/api/persons/persons')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_get_persons(self):
        """Test getting a specific Persons item"""
        response = self.client.get(f'/api/persons/persons/{self.item.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['title'], "Test Persons")

    def test_create_persons(self):
        """Test creating a new Persons item"""
        payload = {
            "title": "New Persons",
            "description": "New description",
            "is_active": True
        }
        response = self.client.post(
            '/api/persons/persons',
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('id', data)

        # Verify the item was created
        item = Persons.objects.get(id=data['id'])
        self.assertEqual(item.title, "New Persons")