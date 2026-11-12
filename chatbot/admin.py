from django.contrib import admin

from .models import ChatContactRequest


@admin.register(ChatContactRequest)
class ChatContactRequestAdmin(admin.ModelAdmin):
    list_display = ("reference", "name", "email", "phone", "topic", "status", "created_at")
    list_filter = ("status", "topic")
    search_fields = ("reference", "name", "email", "phone")
    readonly_fields = ("reference", "created_at", "handled_at")
