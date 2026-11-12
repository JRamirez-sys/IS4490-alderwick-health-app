import json
import logging

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from accounts.models import User
from clinic.context_processors import PRACTICE
from frontdesk.forms import StaffNotesForm

from . import engine
from .forms import ChatContactForm
from .models import ChatContactRequest

logger = logging.getLogger("alderwick.chatbot")

SESSION_TOPIC_KEY = "chatbot_last_topic"
SESSION_CONTACT_COUNT_KEY = "chatbot_contact_count"
MAX_CONTACTS_PER_SESSION = 3

front_office_required = role_required(User.Role.FRONT_OFFICE)


# ---------------------------------------------------------------------------
# Public endpoints used by the homepage chat widget
# ---------------------------------------------------------------------------

@require_POST
def message(request):
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid request."}, status=400)
    text = str(data.get("message", "") if isinstance(data, dict) else "").strip()
    if not text:
        return JsonResponse({"error": "Please type a question."}, status=400)

    reply = engine.respond(text[: engine.MAX_MESSAGE_LENGTH])
    request.session[SESSION_TOPIC_KEY] = engine.INTENT_LABELS.get(reply.intent, "")
    # Never log what the visitor typed: it may contain health information.
    logger.info("chatbot reply intent=%s chars=%d", reply.intent, len(text))
    return JsonResponse(reply.as_dict())


@require_POST
def contact(request):
    sent = request.session.get(SESSION_CONTACT_COUNT_KEY, 0)
    if sent >= MAX_CONTACTS_PER_SESSION:
        return JsonResponse(
            {"error": f"We've received your messages. For anything else, please call {PRACTICE['main_phone']}."},
            status=429,
        )
    form = ChatContactForm(request.POST)
    if not form.is_valid():
        errors = [e for field_errors in form.errors.values() for e in field_errors]
        return JsonResponse({"error": " ".join(errors)}, status=400)

    reference = ""
    if not form.is_spam:
        contact_request = form.save(commit=False)
        contact_request.topic = request.session.get(SESSION_TOPIC_KEY, "")[:60]
        contact_request.save()
        reference = contact_request.reference
        logger.info("chatbot contact request created reference=%s", reference)
    request.session[SESSION_CONTACT_COUNT_KEY] = sent + 1
    return JsonResponse({
        "ok": True,
        "reference": reference,
        "reply": (
            "Thanks! Your question has been sent to our front office team"
            + (f" (reference {reference})" if reference else "")
            + ". Someone will contact you during business hours. "
            f"If it's urgent, call {PRACTICE['main_phone']}, or 911 for an emergency."
        ),
    })


# ---------------------------------------------------------------------------
# Front-office queue
# ---------------------------------------------------------------------------

@front_office_required
def chat_request_list(request):
    status = request.GET.get("status", ChatContactRequest.Status.OPEN)
    items = ChatContactRequest.objects.select_related("handled_by")
    if status in ChatContactRequest.Status.values:
        items = items.filter(status=status)
    page = Paginator(items, 20).get_page(request.GET.get("page"))
    context = {"page": page, "status": status, "status_choices": ChatContactRequest.Status.choices}
    return render(request, "chatbot/chat_request_list.html", context)


@front_office_required
def chat_request_detail(request, pk):
    item = get_object_or_404(ChatContactRequest.objects.select_related("handled_by"), pk=pk)
    form = StaffNotesForm(request.POST or None, initial={"staff_notes": item.staff_notes})
    if request.method == "POST" and form.is_valid():
        item.staff_notes = form.cleaned_data["staff_notes"]
        action = request.POST.get("action")
        if action == "close":
            item.status = ChatContactRequest.Status.CLOSED
            item.handled_by = request.user
            item.handled_at = timezone.now()
            messages.success(request, f"{item.reference} marked as handled.")
        elif action == "reopen":
            item.status = ChatContactRequest.Status.OPEN
            item.handled_by = None
            item.handled_at = None
            messages.success(request, f"{item.reference} reopened.")
        else:
            messages.success(request, "Notes saved.")
        item.save()
        return redirect("chatbot_staff:chat_request_detail", pk=item.pk)
    return render(request, "chatbot/chat_request_detail.html", {"item": item, "form": form})
