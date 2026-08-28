from django.test import TestCase
from persons.models import Persons


class PersonsTestCase(TestCase):
    def setUp(self):
        self.item = Persons.objects.create(
            title="Test Persons",
            description="Test description",
            is_active=True
        )

    def test_persons_creation(self):
        """Test that Persons can be created"""
        self.assertIsNotNone(self.item.id)
        self.assertEqual(self.item.title, "Test Persons")
        self.assertEqual(self.item.description, "Test description")
        self.assertTrue(self.item.is_active)

    def test_persons_str(self):
        """Test the string representation of Persons"""
        self.assertEqual(str(self.item), "Test Persons")