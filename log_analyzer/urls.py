from django.contrib import admin
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.urls import path, include
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

@require_http_methods(["GET"])
def health_check(request):
    """Health check endpoint for Docker/Kubernetes"""
    return JsonResponse({"status": "healthy"})

@require_http_methods(["GET"])
def ready_check(request):
    """Readiness check endpoint for Docker/Kubernetes"""
    from django.db import connection
    try:
        connection.ensure_connection()
        return JsonResponse({"status": "ready"})
    except Exception as e:
        return JsonResponse({"status": "not ready", "error": str(e)}, status=503)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health"),
    path("ready/", ready_check, name="ready"),
    path("", include("core.urls")),
]
