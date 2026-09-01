from unittest.mock import patch
from django.test import TestCase, Client


class HealthCheckTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_health_check_returns_200_when_all_healthy(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")

    @patch("apps.health.views.HealthCheckView._check_redis", return_value=False)
    def test_health_check_returns_503_when_redis_down(self, mock_redis):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["redis"], "down")