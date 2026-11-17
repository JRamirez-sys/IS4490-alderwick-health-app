"""Aggregate, role-scoped inputs for the daily staff operations brief."""

import datetime
import json

from django.db.models import Min, OuterRef, Subquery
from django.utils import timezone

from aiops.models import AIOperation
from accounts.models import User
from clinic import gemini
from clinic.models import AppointmentRequest, Message, MessageThread, Visit

AGING_THRESHOLD = datetime.timedelta(hours=24)


def _task_summary(key, label, queryset, age_field, now):
    oldest = queryset.aggregate(oldest=Min(age_field))["oldest"]
    count = queryset.count()
    if oldest is None:
        oldest_age_hours = 0
    else:
        oldest_age_hours = max(0, int((now - oldest).total_seconds() // 3600))
    aging_filter = {f"{age_field}__lt": now - AGING_THRESHOLD}
    return {
        "key": key,
        "label": label,
        "count": count,
        "over_24h": queryset.filter(**aging_filter).count(),
        "oldest_age_hours": oldest_age_hours,
    }


def _awaiting_reply_threads(queue, provider=None):
    latest_message_from_patient = Subquery(
        Message.objects.filter(thread_id=OuterRef("pk"))
        .order_by("-created_at", "-pk")
        .values("from_patient")[:1]
    )
    threads = (
        MessageThread.objects.filter(queue=queue, status=MessageThread.Status.OPEN)
        .annotate(latest_message_from_patient=latest_message_from_patient)
        .filter(latest_message_from_patient=True)
    )
    if provider is not None:
        threads = threads.filter(assigned_provider=provider)
    return threads


def get_operations_tasks(role, *, provider=None):
    now = timezone.now()
    if role == User.Role.FRONT_OFFICE:
        requests = _task_summary(
            "appointment_requests",
            "Open appointment requests",
            AppointmentRequest.objects.filter(status__in=AppointmentRequest.OPEN_STATUSES),
            "created_at",
            now,
        )
        requests["new_count"] = AppointmentRequest.objects.filter(
            status=AppointmentRequest.Status.SUBMITTED
        ).count()
        requests["under_review_count"] = AppointmentRequest.objects.filter(
            status=AppointmentRequest.Status.UNDER_REVIEW
        ).count()
        awaiting_replies = _awaiting_reply_threads(MessageThread.Queue.FRONT_OFFICE)
        messages = _task_summary(
            "front_office_replies",
            "Patient messages awaiting a front-office reply",
            awaiting_replies,
            "updated_at",
            now,
        )
        messages["unread_count"] = awaiting_replies.filter(staff_has_unread=True).count()
        tasks = [requests, messages]
    elif role == User.Role.DOCTOR and provider is not None:
        tasks = [
            _task_summary(
                "unsigned_notes",
                "Unsigned notes",
                Visit.objects.filter(provider=provider, status=Visit.Status.DRAFT),
                "visit_date",
                now,
            ),
            _task_summary(
                "clinical_replies",
                "Patient messages awaiting your reply",
                _awaiting_reply_threads(MessageThread.Queue.CLINICAL, provider=provider),
                "updated_at",
                now,
            ),
        ]
    else:
        raise ValueError("A front-office role or clinician with a provider profile is required.")
    return [task for task in tasks if task["count"]]


def prioritize_operations_tasks(tasks, role):
    """Return tasks in AI-suggested priority order."""
    if not tasks:
        return []

    prompt_queues = []
    for task in tasks:
        queue = {
            "key": task["key"],
            "count": task["count"],
            "over_24h": task["over_24h"],
            "oldest_age_hours": task["oldest_age_hours"],
        }
        for field in ("new_count", "under_review_count", "unread_count"):
            if field in task:
                queue[field] = task[field]
        prompt_queues.append(queue)

    prompt = json.dumps(
        {
            "aging_threshold_hours": int(AGING_THRESHOLD.total_seconds() // 3600),
            "queues": prompt_queues,
        }
    )
    response = gemini.generate_text(
        prompt,
        system_instruction=(
            "Prioritize the supplied work queues for a staff member using only their counts and ages. "
            "Favor queues with older items and more items over 24 hours; use total count to break ties. "
            "Do not infer clinical urgency or invent facts. Return only a JSON object with one key, "
            '"priority_order", containing every supplied queue key exactly once, in priority order.'
        ),
        response_mime_type="application/json",
        operation=AIOperation.Operation.DAILY_OPERATIONS_BRIEF,
        actor_role=role,
        input_count=len(tasks),
    )
    try:
        result = json.loads(response)
    except (json.JSONDecodeError, TypeError) as exc:
        raise gemini.GeminiError("Gemini returned an invalid operations brief.") from exc

    order = result.get("priority_order") if isinstance(result, dict) else None
    valid_keys = {task["key"] for task in tasks}
    if (
        not isinstance(order, list)
        or len(order) != len(valid_keys)
        or not all(isinstance(key, str) for key in order)
        or len(set(order)) != len(order)
        or set(order) != valid_keys
    ):
        raise gemini.GeminiError("Gemini returned an incomplete operations brief.")

    tasks_by_key = {task["key"]: task for task in tasks}
    return [tasks_by_key[key] for key in order]


def generate_operations_brief(role, *, provider=None):
    """Return role-scoped work queues in AI-suggested priority order."""
    return prioritize_operations_tasks(get_operations_tasks(role, provider=provider), role)
