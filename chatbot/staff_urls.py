from django.urls import path

from . import views

app_name = "chatbot_staff"

urlpatterns = [
    path("", views.chat_request_list, name="chat_requests"),
    path("<int:pk>/", views.chat_request_detail, name="chat_request_detail"),
]
