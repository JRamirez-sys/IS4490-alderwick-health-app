import json
from collections import OrderedDict

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from accounts.models import User
from audit.models import AuditLog
from audit.utils import log_event
from clinic import gemini
from clinic.models import Appointment, AppointmentRequest, Message, MessageThread, Patient, Visit
from clinic.services import reply_to_thread, start_thread

from .forms import CancelAppointmentForm, NewMessageForm, PortalAppointmentRequestForm, ReplyForm

patient_required = role_required(User.Role.PATIENT)
MAX_PAGE_TRANSLATION_SEGMENTS = 300
MAX_PAGE_TRANSLATION_SEGMENT_LENGTH = 5000
MAX_PAGE_TRANSLATION_CHARACTERS = 30000
MAX_PAGE_TRANSLATION_REQUEST_BYTES = 100000


def current_patient(request):
    try:
        return request.user.patient
    except Patient.DoesNotExist as exc:
        raise Http404("No patient record is linked to this account.") from exc


@patient_required
def dashboard(request):
    patient = current_patient(request)
    now = timezone.now()
    context = {
        "patient": patient,
        "upcoming": patient.appointments.filter(start__gt=now, status=Appointment.Status.SCHEDULED)
        .select_related("provider", "office", "appointment_type")[:3],
        "unread_threads": patient.message_threads.filter(patient_has_unread=True).count(),
        "recent_labs": patient.lab_results.all()[:5],
        "open_requests": patient.appointment_requests.filter(status__in=AppointmentRequest.OPEN_STATUSES),
        "active_medications": patient.medications.filter(is_active=True).count(),
    }
    return render(request, "portal/dashboard.html", context)


@patient_required
def appointments(request):
    patient = current_patient(request)
    now = timezone.now()
    base = patient.appointments.select_related("provider", "office", "appointment_type")
    context = {
        "patient": patient,
        "upcoming": base.filter(start__gt=now).exclude(status=Appointment.Status.CANCELLED).order_by("start"),
        "past": base.filter(start__lte=now).order_by("-start")[:25],
        "cancelled": base.filter(start__gt=now, status=Appointment.Status.CANCELLED).order_by("start"),
        "requests": patient.appointment_requests.all()[:10],
        "cancel_form": CancelAppointmentForm(),
        "now": now,
    }
    return render(request, "portal/appointments.html", context)


@patient_required
@require_POST
def cancel_appointment(request, pk):
    patient = current_patient(request)
    appointment = get_object_or_404(Appointment, pk=pk, patient=patient)
    form = CancelAppointmentForm(request.POST)
    if not appointment.patient_can_cancel():
        messages.error(
            request,
            "Appointments within 24 hours can't be cancelled online. Please call your clinic at "
            f"{appointment.office.phone or '(801) 555-0100'}.",
        )
        return redirect("portal:appointments")
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    appointment.status = Appointment.Status.CANCELLED
    appointment.cancellation_reason = reason or "Cancelled by patient online"
    appointment.save(update_fields=["status", "cancellation_reason", "updated_at"])
    log_event(request, AuditLog.Action.CANCEL_APPOINTMENT, patient=patient, target=appointment, by="patient")
    messages.success(request, "Your appointment has been cancelled.")
    return redirect("portal:appointments")


@patient_required
def health_summary(request):
    patient = current_patient(request)
    log_event(request, AuditLog.Action.VIEW_OWN_RECORD, patient=patient, section="summary")
    context = {
        "patient": patient,
        "allergies": patient.allergies.all(),
        "active_problems": patient.problems.filter(status="active"),
        "resolved_problems": patient.problems.filter(status="resolved"),
        "medications": patient.medications.filter(is_active=True).select_related("prescriber"),
        "past_medications": patient.medications.filter(is_active=False),
    }
    return render(request, "portal/health_summary.html", context)


@patient_required
def lab_results(request):
    patient = current_patient(request)
    log_event(request, AuditLog.Action.VIEW_OWN_RECORD, patient=patient, section="labs")
    grouped = OrderedDict()
    for result in patient.lab_results.select_related("ordered_by"):
        grouped.setdefault((result.collected_on, result.panel, result.ordered_by), []).append(result)
    return render(request, "portal/lab_results.html", {"patient": patient, "grouped": grouped.items()})


@patient_required
def visits(request):
    patient = current_patient(request)
    visit_list = patient.visits.filter(status=Visit.Status.SIGNED).select_related("provider", "office")
    return render(request, "portal/visits.html", {"patient": patient, "visits": visit_list})


@patient_required
def visit_detail(request, pk):
    patient = current_patient(request)
    visit = get_object_or_404(Visit, pk=pk, patient=patient, status=Visit.Status.SIGNED)
    log_event(request, AuditLog.Action.VIEW_VISIT, patient=patient, target=visit)
    return render(request, "portal/visit_detail.html", {"patient": patient, "visit": visit})


@patient_required
def message_list(request):
    patient = current_patient(request)
    threads = patient.message_threads.prefetch_related("messages")
    return render(request, "portal/message_list.html", {"patient": patient, "threads": threads})


@patient_required
def new_message(request):
    patient = current_patient(request)
    form = NewMessageForm(request.POST or None, initial={"category": request.GET.get("category")})
    if request.method == "POST" and form.is_valid():
        thread = start_thread(
            patient=patient,
            category=form.cleaned_data["category"],
            subject=form.cleaned_data["subject"],
            body=form.cleaned_data["body"],
            sender=request.user,
        )
        log_event(request, AuditLog.Action.SEND_MESSAGE, patient=patient, target=thread, by="patient")
        messages.success(request, "Message sent. We respond within 2 business days.")
        return redirect("portal:thread", pk=thread.pk)
    return render(request, "portal/new_message.html", {"patient": patient, "form": form})


@patient_required
def thread_detail(request, pk):
    patient = current_patient(request)
    thread = get_object_or_404(MessageThread, pk=pk, patient=patient)
    form = ReplyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        reply_to_thread(
            thread=thread,
            body=form.cleaned_data["body"],
            sender=request.user,
            from_patient=True,
            sender_name=patient.full_name,
        )
        log_event(request, AuditLog.Action.SEND_MESSAGE, patient=patient, target=thread, by="patient")
        messages.success(request, "Reply sent.")
        return redirect("portal:thread", pk=thread.pk)
    if thread.patient_has_unread:
        thread.patient_has_unread = False
        thread.save(update_fields=["patient_has_unread"])
    return render(
        request,
        "portal/thread.html",
        {
            "patient": patient,
            "thread": thread,
            "thread_messages": thread.messages.all(),
            "form": form,
            "translation_url_name": "portal:translate_message",
            "translate_all_messages": True,
            "translation_target_language": patient.preferred_language,
            "translation_note": (
                "AI translations may contain errors. Contact your care team if you have questions "
                "about medical instructions."
            ),
        },
    )


@patient_required
@require_POST
def translate_message(request, pk, message_pk):
    patient = current_patient(request)
    thread = get_object_or_404(MessageThread.objects.select_related("patient"), pk=pk, patient=patient)
    message = get_object_or_404(Message, pk=message_pk, thread=thread)
    target_language = patient.preferred_language.strip()
    if not target_language:
        messages.error(request, "Add a preferred language to your profile before translating messages.")
        return redirect("portal:thread", pk=thread.pk)

    try:
        translation = gemini.translate_to_language(message.body, target_language, actor_role=request.user.role)
    except gemini.TranslationUnavailableError as exc:
        messages.error(request, str(exc))
        return redirect("portal:thread", pk=thread.pk)
    except gemini.GeminiError:
        messages.error(request, "The translation service is unavailable. The original message is unchanged.")
        return redirect("portal:thread", pk=thread.pk)

    if not translation:
        messages.error(request, "No translation was returned. The original message is unchanged.")
        return redirect("portal:thread", pk=thread.pk)

    log_event(
        request,
        AuditLog.Action.TRANSLATE_MESSAGE,
        patient=patient,
        target=message,
    )
    return render(
        request,
        "portal/thread.html",
        {
            "patient": patient,
            "thread": thread,
            "thread_messages": thread.messages.all(),
            "form": ReplyForm(),
            "translation": translation,
            "translated_message_id": message.pk,
            "translation_url_name": "portal:translate_message",
            "translate_all_messages": True,
            "translation_target_language": target_language,
            "translation_note": (
                "AI translations may contain errors. Contact your care team if you have questions "
                "about medical instructions."
            ),
        },
    )


@patient_required
@require_POST
def translate_page(request):
    patient = current_patient(request)
    if len(request.body) > MAX_PAGE_TRANSLATION_REQUEST_BYTES:
        return JsonResponse({"error": "This page contains too much text to translate at once."}, status=413)

    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid translation request."}, status=400)

    texts = payload.get("texts") if isinstance(payload, dict) else None
    if (
        not isinstance(texts, list)
        or not texts
        or len(texts) > MAX_PAGE_TRANSLATION_SEGMENTS
        or not all(
            isinstance(text, str) and text.strip() and len(text) <= MAX_PAGE_TRANSLATION_SEGMENT_LENGTH
            for text in texts
        )
        or sum(len(text) for text in texts if isinstance(text, str)) > MAX_PAGE_TRANSLATION_CHARACTERS
    ):
        return JsonResponse({"error": "The page text is empty or too large to translate."}, status=400)

    target_language = patient.preferred_language.strip()
    if not target_language:
        return JsonResponse({"error": "Add a preferred language to your profile before translating."}, status=400)

    try:
        translations = gemini.translate_text_batch(
            texts,
            target_language,
            actor_role=request.user.role,
            operation="portal_page_translation",
        )
    except gemini.TranslationUnavailableError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except gemini.GeminiError:
        return JsonResponse({"error": "The translation service is unavailable. Please try again."}, status=503)

    log_event(
        request,
        AuditLog.Action.TRANSLATE_PORTAL_PAGE,
        patient=patient,
        target=patient,
        target_language=target_language,
        text_segment_count=len(texts),
    )
    return JsonResponse({"translations": translations, "target_language": target_language})


@patient_required
def request_appointment(request):
    patient = current_patient(request)
    form = PortalAppointmentRequestForm(request.POST or None, initial={"preferred_provider": patient.primary_provider})
    if request.method == "POST" and form.is_valid():
        appointment_request = form.save(commit=False)
        appointment_request.patient = patient
        appointment_request.first_name = patient.first_name
        appointment_request.last_name = patient.last_name
        appointment_request.date_of_birth = patient.date_of_birth
        appointment_request.email = patient.email
        appointment_request.phone = patient.phone
        appointment_request.insurance_plan = patient.insurance_plan
        appointment_request.is_new_patient = False
        appointment_request.source = AppointmentRequest.Source.PORTAL
        appointment_request.save()
        log_event(
            request,
            AuditLog.Action.CREATE_APPOINTMENT_REQUEST,
            patient=patient,
            target=appointment_request,
            source="portal",
        )
        messages.success(
            request, f"Request {appointment_request.reference} received. Our team will contact you within 1 business day."
        )
        return redirect("portal:appointments")
    return render(request, "portal/request_appointment.html", {"patient": patient, "form": form})


@patient_required
def profile(request):
    patient = current_patient(request)
    return render(request, "portal/profile.html", {"patient": patient})
