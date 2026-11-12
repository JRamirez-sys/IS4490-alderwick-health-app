from django.urls import path

from . import views

app_name = "chatbot"

urlpatterns = [
    path("message/", views.message, name="message"),
    path("contact/", views.contact, name="contact"),
]
