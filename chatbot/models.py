"""Data stored by the public website chatbot.

The chatbot itself keeps no conversation history. The only thing saved is a
contact request, which a visitor chooses to send to the front office.
"""

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


class ChatContactRequest(models.Model):
    """A question a website visitor asked the chatbot to pass to staff."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Handled"

    reference = models.CharField(max_length=12, unique=True, editable=False)
    name = models.CharField(max_length=120)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    message = models.TextField(max_length=1000)
    topic = models.CharField(max_length=60, blank=True, help_text="What the chatbot was discussing when the visitor asked for staff.")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    staff_notes = models.TextField(blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    handled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "website chat request"

    def __str__(self):
        return f"{self.reference} {self.name}"

    @property
    def is_open(self):
        return self.status == self.Status.OPEN

    def save(self, *args, **kwargs):
        if not self.reference:
            while True:
                candidate = f"WC-{secrets.token_hex(3).upper()}"
                if not ChatContactRequest.objects.filter(reference=candidate).exists():
                    self.reference = candidate
                    break
        super().save(*args, **kwargs)
