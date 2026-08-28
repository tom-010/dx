from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from locations.models import Locations

User = get_user_model()


class LocationsAPITestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123'
        )
        self.client.force_login(self.user)

        self.item = Locations.objects.create(
            title="Test Locations",
            description="Test description",
            is_active=True
        )

    def test_list_locations(self):
        """Test listing all Locations items"""
        response = self.client.get('/api/locations/locations')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_get_locations(self):
        """Test getting a specific Locations item"""
        response = self.client.get(f'/api/locations/locations/{self.item.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['title'], "Test Locations")

    def test_create_locations(self):
        """Test creating a new Locations item"""
        payload = {
            "title": "New Locations",
            "description": "New description",
            "is_active": True
        }
        response = self.client.post(
            '/api/locations/locations',
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('id', data)

        # Verify the item was created
        item = Locations.objects.get(id=data['id'])
        self.assertEqual(item.title, "New Locations")