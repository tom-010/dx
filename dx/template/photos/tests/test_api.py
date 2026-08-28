from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from photos.models import Photos

User = get_user_model()


class PhotosAPITestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123'
        )
        self.client.force_login(self.user)

        self.item = Photos.objects.create(
            title="Test Photos",
            description="Test description",
            is_active=True
        )

    def test_list_photos(self):
        """Test listing all Photos items"""
        response = self.client.get('/api/photos/photos')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_get_photos(self):
        """Test getting a specific Photos item"""
        response = self.client.get(f'/api/photos/photos/{self.item.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['title'], "Test Photos")

    def test_create_photos(self):
        """Test creating a new Photos item"""
        payload = {
            "title": "New Photos",
            "description": "New description",
            "is_active": True
        }
        response = self.client.post(
            '/api/photos/photos',
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('id', data)

        # Verify the item was created
        item = Photos.objects.get(id=data['id'])
        self.assertEqual(item.title, "New Photos")