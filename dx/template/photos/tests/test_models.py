from django.test import TestCase
from photos.models import Photos


class PhotosTestCase(TestCase):
    def setUp(self):
        self.item = Photos.objects.create(
            title="Test Photos",
            description="Test description",
            is_active=True
        )

    def test_photos_creation(self):
        """Test that Photos can be created"""
        self.assertIsNotNone(self.item.id)
        self.assertEqual(self.item.title, "Test Photos")
        self.assertEqual(self.item.description, "Test description")
        self.assertTrue(self.item.is_active)

    def test_photos_str(self):
        """Test the string representation of Photos"""
        self.assertEqual(str(self.item), "Test Photos")