from django.conf import settings
from django.db import models


def default_target_languages():
    return ["English", "Spanish", "Vietnamese", "Arabic", "Mandarin"]


class AIOperation(models.Model):
    class Operation(models.TextChoices):
        TEXT_GENERATION = "text_generation", "Text generation"
        MESSAGE_TRANSLATION = "message_translation", "Message translation"
        APPOINTMENT_REQUEST_TRANSLATION = "appointment_request_translation", "Appointment request translation"
        PORTAL_PAGE_TRANSLATION = "portal_page_translation", "Portal page translation"
        STAFF_POLICY_ASSISTANT = "staff_policy_assistant", "Staff policy assistant"
        DAILY_OPERATIONS_BRIEF = "daily_operations_brief", "Daily operations brief"
        MEDICINE_LOOKUP = "medicine_lookup", "Medicine lookup"

    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    operation = models.CharField(max_length=32, choices=Operation.choices, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, db_index=True)
    model = models.CharField(max_length=120)
    actor_role = models.CharField(max_length=20, blank=True)
    target_language = models.CharField(max_length=40, blank=True)
    input_count = models.PositiveIntegerField(default=1)
    duration_ms = models.PositiveIntegerField(default=0)
    error_type = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI operation"

    def __str__(self):
        return f"{self.get_operation_display()} ({self.get_status_display()})"


class AIConfiguration(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    translation_enabled = models.BooleanField(default=True)
    selected_model = models.CharField(max_length=120, blank=True)
    target_languages = models.JSONField(
        default=default_target_languages,
        blank=True,
    )
    supplemental_instruction = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    draft_translation_enabled = models.BooleanField(null=True, blank=True)
    draft_selected_model = models.CharField(max_length=120, blank=True)
    draft_target_languages = models.JSONField(null=True, blank=True)
    draft_supplemental_instruction = models.TextField(blank=True)
    draft_updated_at = models.DateTimeField(null=True, blank=True)
    draft_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "AI configuration"

    @property
    def has_draft(self):
        return self.draft_updated_at is not None

    def __str__(self):
        return "Alderwick AI configuration"
