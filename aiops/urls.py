from django.urls import path

from . import views

app_name = "aiops"

urlpatterns = [
    path("", views.index, name="index"),
    path("settings/draft/", views.save_settings_draft, name="save_settings_draft"),
    path("settings/publish/", views.publish_settings, name="publish_settings"),
]
