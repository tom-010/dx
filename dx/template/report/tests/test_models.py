from django.test import TestCase
from report.models import Report


class ReportTestCase(TestCase):
    def setUp(self):
        self.item = Report.objects.create(
            title="Test Report",
            description="Test description",
            is_active=True
        )

    def test_report_creation(self):
        """Test that Report can be created"""
        self.assertIsNotNone(self.item.id)
        self.assertEqual(self.item.title, "Test Report")
        self.assertEqual(self.item.description, "Test description")
        self.assertTrue(self.item.is_active)

    def test_report_str(self):
        """Test the string representation of Report"""
        self.assertEqual(str(self.item), "Test Report")