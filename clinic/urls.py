from django.urls import path

from . import staff_assistant

app_name = "clinic"

urlpatterns = [
    path("", staff_assistant.index, name="staff_assistant"),
    path("chat/", staff_assistant.chat, name="staff_assistant_chat"),
]
