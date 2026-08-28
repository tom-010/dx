from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from report.models import Report

User = get_user_model()


class ReportAPITestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='testuser',
            password='testpass123'
        )
        self.client.force_login(self.user)

        self.item = Report.objects.create(
            title="Test Report",
            description="Test description",
            is_active=True
        )

    def test_list_report(self):
        """Test listing all Report items"""
        response = self.client.get('/api/report/report')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)

    def test_get_report(self):
        """Test getting a specific Report item"""
        response = self.client.get(f'/api/report/report/{self.item.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['title'], "Test Report")

    def test_create_report(self):
        """Test creating a new Report item"""
        payload = {
            "title": "New Report",
            "description": "New description",
            "is_active": True
        }
        response = self.client.post(
            '/api/report/report',
            data=payload,
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('id', data)

        # Verify the item was created
        item = Report.objects.get(id=data['id'])
        self.assertEqual(item.title, "New Report")