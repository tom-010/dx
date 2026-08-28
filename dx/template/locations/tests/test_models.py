from django.test import TestCase
from locations.models import Locations


class LocationsTestCase(TestCase):
    def setUp(self):
        self.item = Locations.objects.create(
            title="Test Locations",
            description="Test description",
            is_active=True
        )

    def test_locations_creation(self):
        """Test that Locations can be created"""
        self.assertIsNotNone(self.item.id)
        self.assertEqual(self.item.title, "Test Locations")
        self.assertEqual(self.item.description, "Test description")
        self.assertTrue(self.item.is_active)

    def test_locations_str(self):
        """Test the string representation of Locations"""
        self.assertEqual(str(self.item), "Test Locations")