import datetime
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from clinic import gemini
from clinic.models import Appointment, AppointmentRequest, Message, MessageThread, Patient, Problem
from clinic.operations_brief import operations_brief_session_key
from clinic.services import available_slots
from clinic.testing import (
    local_dt,
    make_appointment,
    make_appointment_type,
    make_patient,
    make_plan,
    make_provider,
    make_thread,
    make_user,
    tomorrow,
)


class FrontDeskTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.plan = make_plan()
        cls.provider = make_provider(plans=[cls.plan])
        cls.office = cls.provider.offices.first()
        cls.staff = make_user("front_office", office=cls.office, first_name="Brit", last_name="Hale")
        cls.patient = make_patient(provider=cls.provider, first_name="Mike", last_name="Anderson", insurance_plan=cls.plan)
        cls.type = make_appointment_type()

    def setUp(self):
        self.client.force_login(self.staff)

    def new_request(self, **kwargs):
        defaults = {
            "first_name": "Rae",
            "last_name": "Newcomer",
            "date_of_birth": datetime.date(1991, 2, 3),
            "email": "rae@example.com",
            "reason": "Physical",
            "insurance_plan": self.plan,
        }
        defaults.update(kwargs)
        return AppointmentRequest.objects.create(**defaults)

    def open_slot(self, provider=None):
        provider = provider or self.provider
        return available_slots(provider, tomorrow(), days=1, appointment_type=self.type)[0]

    def action(self, req, **data):
        return self.client.post(reverse("frontdesk:request_action", args=[req.pk]), data)


class FrontDeskAccessTests(FrontDeskTestCase):
    def test_other_roles_are_denied(self):
        for role in ("patient", "doctor", "ai_engineer"):
            with self.subTest(role=role):
                self.client.force_login(make_user(role))
                self.assertEqual(self.client.get(reverse("frontdesk:dashboard")).status_code, 403)
                self.assertEqual(self.client.get(reverse("frontdesk:operations_brief")).status_code, 403)

    def test_anonymous_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("frontdesk:dashboard")).status_code, 302)
        self.assertEqual(self.client.get(reverse("frontdesk:operations_brief")).status_code, 302)

    def test_pages_render(self):
        for name in ("dashboard", "operations_brief", "requests", "schedule", "messages", "patient_search"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"frontdesk:{name}")).status_code, 200)


class DashboardTests(FrontDeskTestCase):
    def test_counts(self):
        self.new_request()
        self.new_request(status=AppointmentRequest.Status.UNDER_REVIEW)
        make_thread(patient=self.patient, category="billing")
        start = local_dt(timezone.localdate(), 23, 0)
        make_appointment(patient=self.patient, provider=self.provider, start=start, status=Appointment.Status.CHECKED_IN)
        counts = self.client.get(reverse("frontdesk:dashboard")).context["counts"]
        self.assertEqual(counts["new_requests"], 1)
        self.assertEqual(counts["under_review"], 1)
        self.assertEqual(counts["unread_messages"], 1)
        self.assertEqual(counts["todays_appointments"], 1)
        self.assertEqual(counts["checked_in"], 1)

    def test_dashboard_links_to_separate_operations_brief(self):
        response = self.client.get(reverse("frontdesk:dashboard"))

        self.assertContains(response, reverse("frontdesk:operations_brief"))
        self.assertNotContains(response, 'data-generate-operations-brief')

    def test_operations_calendar_shows_assigned_office_appointments(self):
        appointment = make_appointment(
            patient=self.patient,
            provider=self.provider,
            start=local_dt(timezone.localdate(), 10),
        )
        other_provider = make_provider()
        other_patient = make_patient(provider=other_provider, first_name="Other", last_name="Location")
        make_appointment(
            patient=other_patient,
            provider=other_provider,
            start=local_dt(timezone.localdate(), 11),
        )

        response = self.client.get(reverse("frontdesk:operations_brief"))

        self.assertIn(appointment, response.context["operations_calendar_days"][0]["appointments"])
        self.assertContains(response, self.patient.full_name)
        self.assertNotContains(response, other_patient.full_name)

    def test_existing_brief_session_without_new_summary_fields_still_loads(self):
        session = self.client.session
        session[operations_brief_session_key(self.staff)] = {
            "items": [
                {
                    "key": "front_office_replies",
                    "label": "Patient messages",
                    "count": 1,
                    "over_24h": 0,
                    "oldest_age_hours": 1,
                    "unread_count": 1,
                    "url": reverse("frontdesk:messages"),
                }
            ],
            "error": "",
            "ai_prioritized": True,
        }
        session.save()

        response = self.client.get(reverse("frontdesk:operations_brief"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Patient messages")
        self.assertNotContains(response, "AI-generated overview")

    @mock.patch(
        "frontdesk.views.get_operations_tasks",
        return_value=[
            {
                "key": "appointment_requests",
                "label": "Open appointment requests",
                "count": 2,
                "over_24h": 1,
                "oldest_age_hours": 36,
            }
        ],
    )
    @mock.patch("frontdesk.views.generate_operations_summary", return_value="One appointment this week.")
    @mock.patch(
        "frontdesk.views.prioritize_operations_tasks",
        side_effect=lambda tasks, role: tasks,
    )
    def test_operations_brief_generates_and_links_task_queues(self, prioritize, generate_summary, get_tasks):
        post_response = self.client.post(
            reverse("frontdesk:operations_brief"),
            {"action": "generate_operations_brief"},
        )

        self.assertRedirects(post_response, reverse("frontdesk:operations_brief"))
        response = self.client.get(reverse("frontdesk:operations_brief"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["operations_brief"][0]["url"], reverse("frontdesk:requests") + "?status=open")
        self.assertContains(response, "AI-prioritized")
        self.assertContains(response, "One appointment this week.")
        self.assertContains(response, "data-clear-operations-brief")
        self.assertContains(response, "Saved for this signed-in session")
        get_tasks.assert_called_once_with(self.staff.role)
        prioritize.assert_called_once()
        generate_summary.assert_called_once()

        self.client.get(reverse("frontdesk:dashboard"))
        self.assertContains(self.client.get(reverse("frontdesk:operations_brief")), "AI-prioritized")

        clear_response = self.client.post(
            reverse("frontdesk:operations_brief"),
            {"action": "clear_operations_brief"},
        )
        self.assertRedirects(clear_response, reverse("frontdesk:operations_brief"))
        response = self.client.get(reverse("frontdesk:operations_brief"))
        self.assertNotContains(response, "data-clear-operations-brief")

    def test_operations_brief_tab_is_active_and_close_is_not_shown_until_generated(self):
        response = self.client.get(reverse("frontdesk:operations_brief"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "class=\"nav-item active\" href=\"/staff/front-office/operations-brief/\"")
        self.assertContains(response, "data-generate-operations-brief")
        self.assertNotContains(response, "data-clear-operations-brief")

    @mock.patch(
        "frontdesk.views.prioritize_operations_tasks",
        side_effect=gemini.GeminiError("Gemini is not configured."),
    )
    @mock.patch("frontdesk.views.generate_operations_summary", return_value="")
    def test_operations_brief_shows_counts_if_ai_prioritization_is_unavailable(self, generate_summary, prioritize):
        self.new_request()
        make_thread(patient=self.patient, category=MessageThread.Category.APPOINTMENTS)

        post_response = self.client.post(
            reverse("frontdesk:operations_brief"),
            {"action": "generate_operations_brief"},
        )

        self.assertRedirects(post_response, reverse("frontdesk:operations_brief"))
        response = self.client.get(reverse("frontdesk:operations_brief"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Queue snapshot")
        self.assertContains(response, "data-clear-operations-brief")
        self.assertContains(response, "1 new")
        self.assertContains(response, "0 under review")
        self.assertContains(response, "1 unread")
        self.assertContains(response, "AI prioritization is unavailable")
        prioritize.assert_called_once()


class RequestQueueTests(FrontDeskTestCase):
    def test_default_shows_open_requests(self):
        open_req = self.new_request()
        self.new_request(status=AppointmentRequest.Status.DECLINED)
        response = self.client.get(reverse("frontdesk:requests"))
        self.assertEqual(list(response.context["page"]), [open_req])

    def test_filters(self):
        declined = self.new_request(status=AppointmentRequest.Status.DECLINED)
        phone = self.new_request(source=AppointmentRequest.Source.PHONE, last_name="Caller")
        url = reverse("frontdesk:requests")
        self.assertEqual(list(self.client.get(url + "?status=declined").context["page"]), [declined])
        self.assertEqual(list(self.client.get(url + "?source=phone").context["page"]), [phone])
        self.assertEqual(list(self.client.get(url + "?q=caller").context["page"]), [phone])
        self.assertEqual(len(self.client.get(url + "?status=all").context["page"]), 2)

    def test_detail_shows_slots_and_insurance_warning(self):
        req = self.new_request(insurance_plan=make_plan())
        response = self.client.get(
            reverse("frontdesk:request_detail", args=[req.pk]) + f"?provider={self.provider.pk}&appointment_type={self.type.pk}"
        )
        self.assertTrue(response.context["slots_by_day"])
        self.assertTrue(response.context["insurance_warning"])

    def test_detail_suggests_matching_existing_patient(self):
        req = self.new_request(first_name="Mike", last_name="anderson", date_of_birth=self.patient.date_of_birth)
        response = self.client.get(reverse("frontdesk:request_detail", args=[req.pk]))
        self.assertIn(self.patient, list(response.context["matches"]))

    @mock.patch(
        "frontdesk.views.gemini.translate_text_batch",
        return_value=["I need an appointment.", "In the mornings", "Health Insurance"],
    )
    def test_front_office_can_translate_patient_request_fields_without_changing_request(self, translate_text_batch):
        req = self.new_request(
            reason="Necesito una cita.",
            preferred_times="Por las mañanas",
            insurance_other="Seguro Salud",
        )

        response = self.client.post(reverse("frontdesk:translate_request", args=[req.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI-generated English translation")
        self.assertContains(response, "I need an appointment.")
        self.assertContains(response, "Health Insurance")
        translate_text_batch.assert_called_once_with(
            ["Necesito una cita.", "Por las mañanas", "Seguro Salud"],
            "English",
            actor_role="front_office",
            operation="appointment_request_translation",
        )
        req.refresh_from_db()
        self.assertEqual(req.reason, "Necesito una cita.")
        self.assertEqual(req.preferred_times, "Por las mañanas")
        self.assertEqual(req.insurance_other, "Seguro Salud")
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.TRANSLATE_APPOINTMENT_REQUEST,
                object_id=str(req.pk),
            ).exists()
        )

    @mock.patch("frontdesk.views.gemini.translate_text_batch", return_value=["Necesito una cita."])
    def test_patient_role_cannot_use_request_translation(self, translate_text_batch):
        req = self.new_request()
        self.client.force_login(make_user("patient"))

        response = self.client.post(reverse("frontdesk:translate_request", args=[req.pk]))

        self.assertEqual(response.status_code, 403)
        translate_text_batch.assert_not_called()

    @mock.patch("frontdesk.views.gemini.translate_text_batch", return_value=["I need an appointment."])
    def test_another_front_office_user_can_translate_request(self, translate_text_batch):
        req = self.new_request(reason="Necesito una cita.")
        self.client.force_login(make_user("front_office"))

        response = self.client.post(reverse("frontdesk:translate_request", args=[req.pk]))

        self.assertEqual(response.status_code, 200)
        translate_text_batch.assert_called_once()

    @mock.patch(
        "frontdesk.views.gemini.translate_text_batch",
        side_effect=gemini.GeminiError("unavailable"),
    )
    def test_request_translation_failure_preserves_request(self, translate_text_batch):
        req = self.new_request(reason="Necesito una cita.")

        response = self.client.post(
            reverse("frontdesk:translate_request", args=[req.pk]),
            follow=True,
        )

        self.assertContains(response, "translation service is unavailable")
        req.refresh_from_db()
        self.assertEqual(req.reason, "Necesito una cita.")


class RequestActionTests(FrontDeskTestCase):
    def test_start_review_and_notes(self):
        req = self.new_request()
        self.action(req, action="start_review")
        self.action(req, action="save_notes", staff_notes="Left voicemail")
        req.refresh_from_db()
        self.assertEqual(req.status, AppointmentRequest.Status.UNDER_REVIEW)
        self.assertEqual(req.handled_by, self.staff)
        self.assertEqual(req.staff_notes, "Left voicemail")
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.UPDATE_APPOINTMENT_REQUEST).count(), 2)

    def test_decline_requires_reason(self):
        req = self.new_request()
        self.action(req, action="decline", decline_reason="")
        req.refresh_from_db()
        self.assertEqual(req.status, AppointmentRequest.Status.SUBMITTED)
        self.action(req, action="decline", decline_reason="We don't offer that service")
        req.refresh_from_db()
        self.assertEqual(req.status, AppointmentRequest.Status.DECLINED)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DECLINE_APPOINTMENT_REQUEST).exists())

    def test_closed_request_rejects_changes(self):
        req = self.new_request(status=AppointmentRequest.Status.DECLINED)
        response = self.client.post(reverse("frontdesk:request_action", args=[req.pk]), {"action": "start_review"}, follow=True)
        self.assertContains(response, "already closed")

    def test_unknown_action(self):
        req = self.new_request()
        response = self.action(req, action="explode")
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, AppointmentRequest.Status.SUBMITTED)

    def test_link_existing_patient(self):
        req = self.new_request()
        self.action(req, action="link_patient", patient_id=self.patient.pk)
        req.refresh_from_db()
        self.assertEqual(req.patient, self.patient)

    def test_book_for_existing_patient(self):
        req = self.new_request(patient=self.patient, is_new_patient=False)
        slot = self.open_slot()
        self.action(req, action="book", provider=self.provider.pk, appointment_type=self.type.pk, slot=slot.value)
        req.refresh_from_db()
        self.assertEqual(req.status, AppointmentRequest.Status.SCHEDULED)
        self.assertEqual(req.appointment.start, slot.start)
        self.assertEqual(req.appointment.patient, self.patient)
        self.assertEqual(req.appointment.created_by, self.staff)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.SCHEDULE_APPOINTMENT).exists())

    def test_book_new_patient_registers_them(self):
        req = self.new_request()
        slot = self.open_slot()
        before = Patient.objects.count()
        self.action(req, action="book", provider=self.provider.pk, appointment_type=self.type.pk, slot=slot.value, sex="F")
        self.assertEqual(Patient.objects.count(), before + 1)
        req.refresh_from_db()
        new_patient = req.patient
        self.assertEqual(new_patient.last_name, "Newcomer")
        self.assertEqual(new_patient.sex, "F")
        self.assertEqual(new_patient.primary_provider, self.provider)
        self.assertRegex(new_patient.mrn, r"^AFH\d{6}$")
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.REGISTER_PATIENT).exists())

    def test_book_rejects_missing_or_taken_slot(self):
        req = self.new_request(patient=self.patient)
        self.action(req, action="book", provider=self.provider.pk, appointment_type=self.type.pk, slot="garbage")
        req.refresh_from_db()
        self.assertEqual(req.status, AppointmentRequest.Status.SUBMITTED)

        slot = self.open_slot()
        make_appointment(patient=self.patient, provider=self.provider, start=slot.start)
        response = self.client.post(
            reverse("frontdesk:request_action", args=[req.pk]),
            {"action": "book", "provider": self.provider.pk, "appointment_type": self.type.pk, "slot": slot.value},
            follow=True,
        )
        self.assertContains(response, "no longer available")
        req.refresh_from_db()
        self.assertIsNone(req.appointment)


class ScheduleTests(FrontDeskTestCase):
    def test_filters_by_day_and_office(self):
        day = tomorrow()
        mine = make_appointment(patient=self.patient, provider=self.provider, start=local_dt(day, 9))
        elsewhere_provider = make_provider()
        make_appointment(patient=self.patient, provider=elsewhere_provider, start=local_dt(day, 9))
        response = self.client.get(reverse("frontdesk:schedule") + f"?date={day.isoformat()}")
        self.assertEqual(list(response.context["appointments"]), [mine])
        response = self.client.get(reverse("frontdesk:schedule") + f"?date={day.isoformat()}&office=")
        self.assertEqual(len(response.context["appointments"]), 2)
        bad = self.client.get(reverse("frontdesk:schedule") + "?date=not-a-date")
        self.assertEqual(bad.context["day"], timezone.localdate())

    def post_action(self, appointment, **data):
        return self.client.post(reverse("frontdesk:appointment_action", args=[appointment.pk]), data)

    def test_check_in_no_show_and_cancel(self):
        start = local_dt(tomorrow(), 9)
        a = make_appointment(patient=self.patient, provider=self.provider, start=start)
        self.post_action(a, action="check_in")
        a.refresh_from_db()
        self.assertEqual(a.status, Appointment.Status.CHECKED_IN)
        self.assertIsNotNone(a.checked_in_at)

        b = make_appointment(patient=self.patient, provider=self.provider, start=start + datetime.timedelta(hours=1))
        self.post_action(b, action="no_show")
        b.refresh_from_db()
        self.assertEqual(b.status, Appointment.Status.NO_SHOW)

        c = make_appointment(patient=self.patient, provider=self.provider, start=start + datetime.timedelta(hours=2))
        self.post_action(c, action="cancel", reason="")
        c.refresh_from_db()
        self.assertEqual(c.status, Appointment.Status.SCHEDULED)
        self.post_action(c, action="cancel", reason="Patient sick")
        c.refresh_from_db()
        self.assertEqual(c.status, Appointment.Status.CANCELLED)
        self.assertEqual(c.cancellation_reason, "Patient sick")
        actions = set(AuditLog.objects.values_list("action", flat=True))
        self.assertTrue({"check_in", "mark_no_show", "cancel_appointment"} <= actions)

    def test_invalid_transition(self):
        a = make_appointment(
            patient=self.patient, provider=self.provider, start=local_dt(tomorrow(), 9), status=Appointment.Status.CANCELLED
        )
        response = self.client.post(
            reverse("frontdesk:appointment_action", args=[a.pk]), {"action": "check_in"}, follow=True
        )
        self.assertContains(response, "isn&#x27;t available")

    def test_unsafe_next_url_is_ignored(self):
        a = make_appointment(patient=self.patient, provider=self.provider, start=local_dt(tomorrow(), 9))
        response = self.post_action(a, action="check_in", next="https://evil.example.com/")
        self.assertEqual(response["Location"], reverse("frontdesk:schedule"))
        b = make_appointment(patient=self.patient, provider=self.provider, start=local_dt(tomorrow(), 10))
        response = self.post_action(b, action="check_in", next="/staff/front-office/schedule/?date=2030-01-01")
        self.assertEqual(response["Location"], "/staff/front-office/schedule/?date=2030-01-01")


class FrontDeskMessageTests(FrontDeskTestCase):
    def test_inbox_shows_only_front_office_queue(self):
        billing = make_thread(patient=self.patient, category="billing")
        clinical = make_thread(patient=self.patient, category="clinical")
        response = self.client.get(reverse("frontdesk:messages"))
        self.assertIn(billing, response.context["page"])
        self.assertNotIn(clinical, response.context["page"])
        self.assertEqual(self.client.get(reverse("frontdesk:thread", args=[clinical.pk])).status_code, 404)

    def test_reply_and_close(self):
        thread = make_thread(patient=self.patient, category="records")
        self.client.get(reverse("frontdesk:thread", args=[thread.pk]))
        thread.refresh_from_db()
        self.assertFalse(thread.staff_has_unread)
        self.client.post(reverse("frontdesk:thread", args=[thread.pk]), {"body": "Sent!"})
        reply = thread.messages.last()
        self.assertFalse(reply.from_patient)
        self.assertEqual(reply.sender_name, "Brit Hale, Front Office")
        thread.refresh_from_db()
        self.assertTrue(thread.patient_has_unread)
        self.client.post(reverse("frontdesk:thread", args=[thread.pk]), {"action": "close"})
        thread.refresh_from_db()
        self.assertEqual(thread.status, MessageThread.Status.CLOSED)

    def test_status_filter(self):
        closed = make_thread(patient=self.patient, category="billing", status=MessageThread.Status.CLOSED)
        response = self.client.get(reverse("frontdesk:messages") + "?status=closed")
        self.assertEqual(list(response.context["page"]), [closed])

    @mock.patch("frontdesk.views.gemini.translate_to_english", return_value="Hello, I need to reschedule.")
    def test_patient_message_can_be_translated_without_changing_original(self, generate_text):
        thread = make_thread(patient=self.patient, category="billing")
        message = Message.objects.create(
            thread=thread,
            sender_name="Mike Anderson",
            from_patient=True,
            body="Hola, necesito cambiar mi cita.",
        )

        response = self.client.post(
            reverse("frontdesk:translate_message", args=[thread.pk, message.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Translate with AI")
        self.assertContains(response, "AI-generated English translation")
        self.assertContains(response, "Hello, I need to reschedule.")
        generate_text.assert_called_once()
        message.refresh_from_db()
        self.assertEqual(message.body, "Hola, necesito cambiar mi cita.")
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.TRANSLATE_MESSAGE,
                patient=self.patient,
                object_id=str(message.pk),
            ).exists()
        )

    def test_only_patient_messages_in_front_office_threads_can_be_translated(self):
        thread = make_thread(patient=self.patient, category="billing")
        staff_message = Message.objects.create(
            thread=thread,
            sender_name="Brit Hale",
            from_patient=False,
            body="We can help with that.",
        )
        clinical_thread = make_thread(patient=self.patient, category="clinical")
        clinical_message = Message.objects.create(
            thread=clinical_thread,
            sender_name="Mike Anderson",
            from_patient=True,
            body="Hola.",
        )

        staff_response = self.client.post(
            reverse("frontdesk:translate_message", args=[thread.pk, staff_message.pk])
        )
        clinical_response = self.client.post(
            reverse("frontdesk:translate_message", args=[clinical_thread.pk, clinical_message.pk])
        )

        self.assertEqual(staff_response.status_code, 404)
        self.assertEqual(clinical_response.status_code, 404)

    @mock.patch("frontdesk.views.gemini.translate_to_english", return_value="Hello.")
    def test_patient_cannot_access_translation_endpoint(self, generate_text):
        thread = make_thread(patient=self.patient, category="billing")
        message = Message.objects.create(
            thread=thread,
            sender_name="Mike Anderson",
            from_patient=True,
            body="Hola.",
        )
        self.client.force_login(make_user("patient"))

        response = self.client.post(
            reverse("frontdesk:translate_message", args=[thread.pk, message.pk])
        )

        self.assertEqual(response.status_code, 403)
        generate_text.assert_not_called()

    @mock.patch("frontdesk.views.gemini.translate_to_english", side_effect=gemini.GeminiError("unavailable"))
    def test_translation_failure_keeps_original_and_shows_error(self, generate_text):
        thread = make_thread(patient=self.patient, category="billing")
        message = Message.objects.create(
            thread=thread,
            sender_name="Mike Anderson",
            from_patient=True,
            body="Hola.",
        )

        response = self.client.post(
            reverse("frontdesk:translate_message", args=[thread.pk, message.pk]),
            follow=True,
        )

        self.assertContains(response, "translation service is unavailable")
        message.refresh_from_db()
        self.assertEqual(message.body, "Hola.")


class PatientLookupTests(FrontDeskTestCase):
    def test_search_variants(self):
        url = reverse("frontdesk:patient_search")
        for query in ("anderson", self.patient.mrn, self.patient.date_of_birth.isoformat(), "Mike Anderson"):
            with self.subTest(query=query):
                self.assertIn(self.patient, list(self.client.get(url, {"q": query}).context["patients"]))
        self.assertEqual(list(self.client.get(url).context["patients"]), [])

    def test_profile_hides_clinical_data_and_is_audited(self):
        Problem.objects.create(patient=self.patient, description="Secret diagnosis", icd10_code="I10")
        response = self.client.get(reverse("frontdesk:patient_detail", args=[self.patient.pk]))
        self.assertContains(response, self.patient.mrn)
        self.assertNotContains(response, "Secret diagnosis")
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.VIEW_PATIENT_PROFILE, patient=self.patient).exists()
        )

    def test_phone_booking(self):
        url = reverse("frontdesk:book_for_patient", args=[self.patient.pk])
        page = self.client.get(url)
        self.assertEqual(page.context["picker_provider"], self.provider)
        slot = self.open_slot()
        response = self.client.post(
            url, {"provider": self.provider.pk, "appointment_type": self.type.pk, "slot": slot.value, "reason": "Cough"}
        )
        self.assertRedirects(response, reverse("frontdesk:patient_detail", args=[self.patient.pk]))
        appointment = self.patient.appointments.get()
        self.assertEqual(appointment.reason, "Cough")
        bad = self.client.post(url, {"provider": self.provider.pk, "appointment_type": self.type.pk, "slot": "x"})
        self.assertRedirects(bad, url)
