from django.test import TestCase
from iternary.models import Iternary


class IternaryTestCase(TestCase):
    def setUp(self):
        self.item = Iternary.objects.create(
            title="Test Iternary",
            description="Test description",
            is_active=True
        )

    def test_iternary_creation(self):
        """Test that Iternary can be created"""
        self.assertIsNotNone(self.item.id)
        self.assertEqual(self.item.title, "Test Iternary")
        self.assertEqual(self.item.description, "Test description")
        self.assertTrue(self.item.is_active)

    def test_iternary_str(self):
        """Test the string representation of Iternary"""
        self.assertEqual(str(self.item), "Test Iternary")