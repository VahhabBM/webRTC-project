import logging
from django.db import connections
from django.db.utils import OperationalError
from django.http import JsonResponse
from django.views import View
import redis
from django.conf import settings

logger = logging.getLogger("health")


class HealthCheckView(View):
    def get(self, request):
        db_status = self._check_database()
        redis_status = self._check_redis()

        overall_healthy = db_status and redis_status
        status_code = 200 if overall_healthy else 503

        payload = {
            "status": "healthy" if overall_healthy else "unhealthy",
            "database": "up" if db_status else "down",
            "redis": "up" if redis_status else "down",
        }

        log_level = logging.INFO if overall_healthy else logging.ERROR
        logger.log(
            log_level,
            "health check performed",
            extra={"request_id": getattr(request, "request_id", "-")},
        )

        return JsonResponse(payload, status=status_code)

    def _check_database(self):
        try:
            connections["default"].cursor()
            return True
        except OperationalError:
            return False

    def _check_redis(self):
        try:
            client = redis.Redis.from_url(settings.REDIS_URL)
            client.ping()
            return True
        except redis.RedisError:
            return False