from django.contrib import admin
from django.urls import include, path

from core import api as core_api
from report import api as report_api
from iternary import api as iternary_api
from persons import api as persons_api
from photos import api as photos_api
from photos import api as photos_api
from locations import api as locations_api
from trips import api as trips_api
from config import auth_api
from config.ninja import api

api.add_router("/auth", auth_api.api, tags=["auth"])
api.add_router("/core", core_api.api, tags=["core"])
api.add_router("/report", report_api.api, tags=["report"])
api.add_router("/iternary", iternary_api.api, tags=["iternary"])
api.add_router("/persons", persons_api.api, tags=["persons"])
api.add_router("/photos", photos_api.api, tags=["photos"])
api.add_router("/locations", locations_api.api, tags=["locations"])
api.add_router("/trips", trips_api.api, tags=["trips"])
# needle: add-api-router

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("celery-progress/", include("celery_progress.urls")),
    path("ht/", include("health_check.urls")),
]

# Custom error handlers for content-aware responses
handler400 = 'config.views.custom_400_handler'
handler403 = 'config.views.custom_403_handler'
handler404 = 'config.views.custom_404_handler'
handler500 = 'config.views.custom_500_handler'
