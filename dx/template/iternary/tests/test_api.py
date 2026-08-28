from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from iternary.models import Iternary

User = get_user_model()


class IternaryAPITestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123'
        )
        self.client.force_login(self.user)

        self.item = Iternary.objects.create(
            title="Test Iternary",
            description="Test description",
            is_active=True
        )

    def test_list_iternary(self):
        """Test listing all Iternary items"""
        response = self.client.get('/api/iternary/iternary')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_get_iternary(self):
        """Test getting a specific Iternary item"""
        response = self.client.get(f'/api/iternary/iternary/{self.item.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['title'], "Test Iternary")

    def test_create_iternary(self):
        """Test creating a new Iternary item"""
        payload = {
            "title": "New Iternary",
            "description": "New description",
            "is_active": True
        }
        response = self.client.post(
            '/api/iternary/iternary',
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('id', data)

        # Verify the item was created
        item = Iternary.objects.get(id=data['id'])
        self.assertEqual(item.title, "New Iternary")