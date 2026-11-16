from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from accounts.models import User
from audit.models import AuditLog
from audit.utils import log_event
from clinic import gemini

from .forms import AIConfigurationDraftForm
from .models import AIConfiguration, AIOperation

ai_engineer_required = role_required(User.Role.AI_ENGINEER)


def _configuration_form(configuration):
    model_names = gemini.approved_model_names()
    initial = {
        "translation_enabled": (
            configuration.draft_translation_enabled
            if configuration.has_draft
            else configuration.translation_enabled
        ),
        "selected_model": (
            configuration.draft_selected_model
            if configuration.has_draft
            else configuration.selected_model or gemini.model_name()
        ),
        "target_languages": (
            configuration.draft_target_languages
            if configuration.has_draft
            else configuration.target_languages
        ),
        "supplemental_instruction": (
            configuration.draft_supplemental_instruction
            if configuration.has_draft
            else configuration.supplemental_instruction
        ),
    }
    if initial["selected_model"] not in model_names:
        initial["selected_model"] = model_names[0]
    return AIConfigurationDraftForm(initial=initial, model_names=model_names)


@ai_engineer_required
def index(request):
    """Manage pending AI settings and show activity without prompt or patient data."""
    configuration, _ = AIConfiguration.objects.get_or_create(
        pk=1,
        defaults={"selected_model": gemini.model_name()},
    )
    operations = AIOperation.objects.all()[:100]
    context = {
        "operations": operations,
        "total_operations": AIOperation.objects.count(),
        "failed_operations": AIOperation.objects.filter(status=AIOperation.Status.FAILED).count(),
        "configuration": configuration,
        "active_model": configuration.selected_model or gemini.model_name(),
        "settings_form": _configuration_form(configuration),
        "approved_models": gemini.approved_model_names(),
    }
    return render(request, "aiops/index.html", context)


@ai_engineer_required
@require_POST
def save_settings_draft(request):
    configuration, _ = AIConfiguration.objects.get_or_create(
        pk=1,
        defaults={"selected_model": gemini.model_name()},
    )
    form = AIConfigurationDraftForm(request.POST, model_names=gemini.approved_model_names())
    if not form.is_valid():
        errors = "; ".join(
            f"{field}: {', '.join(error_list)}"
            for field, error_list in form.errors.items()
        )
        messages.error(request, f"Settings were not saved: {errors}")
        return redirect("aiops:index")

    configuration.draft_translation_enabled = form.cleaned_data["translation_enabled"]
    configuration.draft_selected_model = form.cleaned_data["selected_model"]
    configuration.draft_target_languages = form.cleaned_data["target_languages"]
    configuration.draft_supplemental_instruction = form.cleaned_data["supplemental_instruction"].strip()
    configuration.draft_updated_at = timezone.now()
    configuration.draft_updated_by = request.user
    configuration.save()
    log_event(
        request,
        AuditLog.Action.UPDATE_AI_SETTINGS,
        target=configuration,
        changed_fields=["translation_enabled", "selected_model", "target_languages", "supplemental_instruction"],
    )
    messages.success(request, "Draft AI settings saved. Publish them to make them active.")
    return redirect("aiops:index")


@ai_engineer_required
@require_POST
@transaction.atomic
def publish_settings(request):
    configuration, _ = AIConfiguration.objects.select_for_update().get_or_create(
        pk=1,
        defaults={"selected_model": gemini.model_name()},
    )
    if not configuration.has_draft:
        messages.error(request, "There are no draft settings to publish.")
        return redirect("aiops:index")

    draft_data = {
        "translation_enabled": configuration.draft_translation_enabled,
        "selected_model": configuration.draft_selected_model,
        "target_languages": configuration.draft_target_languages,
        "supplemental_instruction": configuration.draft_supplemental_instruction,
    }
    form = AIConfigurationDraftForm(draft_data, model_names=gemini.approved_model_names())
    if not form.is_valid():
        messages.error(request, "The saved draft is no longer valid. Save a corrected draft before publishing.")
        return redirect("aiops:index")

    configuration.translation_enabled = form.cleaned_data["translation_enabled"]
    configuration.selected_model = form.cleaned_data["selected_model"]
    configuration.target_languages = form.cleaned_data["target_languages"]
    configuration.supplemental_instruction = form.cleaned_data["supplemental_instruction"].strip()
    configuration.updated_by = request.user
    configuration.draft_translation_enabled = None
    configuration.draft_selected_model = ""
    configuration.draft_target_languages = None
    configuration.draft_supplemental_instruction = ""
    configuration.draft_updated_at = None
    configuration.draft_updated_by = None
    configuration.save()
    log_event(
        request,
        AuditLog.Action.PUBLISH_AI_SETTINGS,
        target=configuration,
        translation_enabled=configuration.translation_enabled,
        selected_model=configuration.selected_model,
        target_languages=configuration.target_languages,
    )
    messages.success(request, "Draft AI settings published and are now active.")
    return redirect("aiops:index")
