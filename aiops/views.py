from django.contrib import messages
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Max, Q, Sum
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
AI_AGENT_ANALYTICS_DAYS = 30
AI_AGENT_CATALOG = (
    {
        "slug": "staff-policy-assistant",
        "name": "Employee assistant",
        "description": "Answers policy and procedure questions from published documents, routes general medicine questions to grounded lookup, and drafts staff replies.",
        "operation_codes": (AIOperation.Operation.STAFF_POLICY_ASSISTANT,),
        "tools": (
            {
                "name": "Published policy retrieval",
                "description": "Loads published policy documents from the practice library for the current request.",
            },
            {
                "name": "Gemini text generation",
                "description": "Answers policy questions or drafts text; it cannot send messages or change records.",
            },
            {
                "name": "Web-grounded medicine lookup",
                "description": "Uses public-source citations for general medicine questions; it does not access patient charts.",
            },
        ),
        "data_access": "Published practice documents or one general medicine question; no patient-chart retrieval or patient-specific medical advice.",
    },
    {
        "slug": "daily-operations-brief",
        "name": "Daily operations brief",
        "description": "Prioritizes staff work queues by aggregate task counts and age.",
        "operation_codes": (AIOperation.Operation.DAILY_OPERATIONS_BRIEF,),
        "tools": (
            {
                "name": "Role-scoped queue aggregation",
                "description": "Reads queue status and age counts; does not send task, note, or message contents.",
            },
            {
                "name": "Gemini text generation",
                "description": "Ranks the supplied aggregate queues; it cannot modify or complete tasks.",
            },
        ),
        "data_access": "Aggregate queue counts and ages only; no patient names or clinical content.",
    },
    {
        "slug": "medicine-lookup",
        "name": "Medicine lookup",
        "description": "Summarizes general medicine information with web-grounded public-source citations.",
        "operation_codes": (AIOperation.Operation.MEDICINE_LOOKUP,),
        "tools": (
            {
                "name": "Google Search grounding",
                "description": "Searches public web sources and returns grounding metadata for citations.",
            },
            {
                "name": "Citation domain filter",
                "description": "Displays HTTPS citations only from FDA, DailyMed, MedlinePlus, or NIH domains.",
            },
            {
                "name": "Authorized patient allergy match",
                "description": "Performs an exact local allergy-name match only within a doctor's authorized patient panel.",
            },
        ),
        "data_access": "Web search receives the medicine name only. The optional allergy check is clinician-only, read-only, audited, and never sends patient data to Gemini.",
    },
    {
        "slug": "translation",
        "name": "Translation assistant",
        "description": "Translates messages, appointment requests, and patient-portal page text.",
        "operation_codes": (
            AIOperation.Operation.MESSAGE_TRANSLATION,
            AIOperation.Operation.APPOINTMENT_REQUEST_TRANSLATION,
            AIOperation.Operation.PORTAL_PAGE_TRANSLATION,
        ),
        "tools": (
            {
                "name": "Gemini text generation",
                "description": "Translates the selected message, request, or page text into an enabled language.",
            },
            {
                "name": "Published translation settings",
                "description": "Applies the enabled-language list and approved model configuration.",
            },
        ),
        "data_access": "Receives the text selected for translation; portal page text may include patient-specific content. No web-search or chart-search tool.",
    },
)


def _agent_usage_metrics(operation_codes, operation_metrics):
    metrics = [operation_metrics[code] for code in operation_codes if code in operation_metrics]
    calls = sum(item["calls"] for item in metrics)
    failures = sum(item["failures"] for item in metrics)
    duration_total = sum(item["duration_total"] for item in metrics)
    last_used_values = [item["last_used"] for item in metrics if item["last_used"]]
    return {
        "calls": calls,
        "failures": failures,
        "success_rate": round((calls - failures) * 100 / calls, 1) if calls else None,
        "average_duration_ms": round(duration_total / calls) if calls else None,
        "last_used": max(last_used_values) if last_used_values else None,
    }


def _ai_agent_analytics():
    period_start = timezone.now() - timedelta(days=AI_AGENT_ANALYTICS_DAYS)
    recent_operations = AIOperation.objects.filter(created_at__gte=period_start)
    allergy_searches = AuditLog.objects.filter(
        action=AuditLog.Action.SEARCH_ALLERGY_RECORDS,
        timestamp__gte=period_start,
    )
    allergy_summary = allergy_searches.aggregate(
        checks=Count("id"),
        checks_with_matches=Count("id", filter=Q(details__matches_found=True)),
    )
    allergy_checks = allergy_summary["checks"]
    summary = recent_operations.aggregate(
        calls=Count("id"),
        failures=Count("id", filter=Q(status=AIOperation.Status.FAILED)),
        duration_total=Sum("duration_ms"),
    )
    operation_metrics = {
        item["operation"]: item
        for item in recent_operations.values("operation").annotate(
            calls=Count("id"),
            failures=Count("id", filter=Q(status=AIOperation.Status.FAILED)),
            duration_total=Sum("duration_ms"),
            last_used=Max("created_at"),
        )
    }

    agents = []
    mapped_operations = set()
    for agent in AI_AGENT_CATALOG:
        mapped_operations.update(agent["operation_codes"])
        agents.append({**agent, **_agent_usage_metrics(agent["operation_codes"], operation_metrics)})

    other_operations = tuple(
        operation
        for operation, _ in AIOperation.Operation.choices
        if operation not in mapped_operations and operation in operation_metrics
    )
    if other_operations:
        agents.append(
            {
                "slug": "other-ai-operations",
                "name": "Other AI operations",
                "description": "Logged model calls not associated with a named staff-facing agent.",
                "tools": (
                    {
                        "name": "Gemini text generation",
                        "description": "Uses the shared Gemini text-generation helper.",
                    },
                ),
                "data_access": "Varies by caller; prompts and responses are not stored in operation logs.",
                "operation_labels": ", ".join(
                    label for code, label in AIOperation.Operation.choices if code in other_operations
                ),
                **_agent_usage_metrics(other_operations, operation_metrics),
            }
        )

    calls = summary["calls"]
    failures = summary["failures"]
    return {
        "period_start": period_start,
        "calls": calls,
        "failures": failures,
        "success_rate": round((calls - failures) * 100 / calls, 1) if calls else None,
        "average_duration_ms": round(summary["duration_total"] / calls) if calls else None,
        "allergy_tool_analytics": {
            "checks": allergy_checks,
            "checks_with_matches": allergy_summary["checks_with_matches"],
            "match_rate": (
                round(allergy_summary["checks_with_matches"] * 100 / allergy_checks, 1)
                if allergy_checks
                else None
            ),
        },
        "agents": agents,
    }


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
    analytics = _ai_agent_analytics()
    operations = AIOperation.objects.all()[:100]
    context = {
        "operations": operations,
        "total_operations": AIOperation.objects.count(),
        "failed_operations": AIOperation.objects.filter(status=AIOperation.Status.FAILED).count(),
        "agent_analytics": analytics,
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
