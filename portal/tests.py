import datetime
import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog
from clinic import gemini
from clinic.models import Allergy, Appointment, AppointmentRequest, LabResult, Message, MessageThread, Visit
from clinic.testing import (
    make_appointment,
    make_patient,
    make_provider,
    make_thread,
    make_user,
    make_visit,
)


class PortalTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.provider = make_provider()
        cls.patient = make_patient(provider=cls.provider, first_name="Mike")
        cls.other = make_patient(provider=cls.provider, first_name="Other")

    def setUp(self):
        self.client.force_login(self.patient.user)

    def future(self, hours):
        start = timezone.now() + datetime.timedelta(hours=hours)
        return start.replace(minute=0, second=0, microsecond=0)


class PortalAccessTests(PortalTestCase):
    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("portal:dashboard"))
        self.assertRedirects(response, f"{reverse('accounts:login')}?next={reverse('portal:dashboard')}")

    def test_staff_roles_are_denied(self):
        for role in ("front_office", "doctor", "ai_engineer"):
            with self.subTest(role=role):
                self.client.force_login(make_user(role))
                self.assertEqual(self.client.get(reverse("portal:dashboard")).status_code, 403)

    def test_patient_account_without_record_gets_404(self):
        self.client.force_login(make_user("patient"))
        self.assertEqual(self.client.get(reverse("portal:dashboard")).status_code, 404)

    def test_every_page_renders(self):
        for name in ("dashboard", "appointments", "request_appointment", "health_summary", "lab_results", "visits",
                     "messages", "new_message", "profile"):
            with self.subTest(page=name):
                response = self.client.get(reverse(f"portal:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Translate with AI")
                self.assertContains(response, "portal-translation.js")


class PortalPageTranslationTests(PortalTestCase):
    @mock.patch(
        "portal.views.gemini.translate_text_batch",
        return_value=["Inicio", "Resumen de salud"],
    )
    def test_page_translation_uses_profile_language_and_audits_without_text(self, translate_text_batch):
        self.patient.preferred_language = "Spanish"
        self.patient.save(update_fields=["preferred_language"])
        texts = ["Home", "Health summary"]
        response = self.client.post(
            reverse("portal:translate_page"),
            data=json.dumps({"texts": texts, "target_language": "French"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"translations": ["Inicio", "Resumen de salud"], "target_language": "Spanish"},
        )
        translate_text_batch.assert_called_once_with(
            texts,
            "Spanish",
            actor_role="patient",
            operation="portal_page_translation",
        )
        event = AuditLog.objects.get(action=AuditLog.Action.TRANSLATE_PORTAL_PAGE, patient=self.patient)
        self.assertEqual(event.details, {"target_language": "Spanish", "text_segment_count": 2})

    @mock.patch("portal.views.gemini.translate_text_batch", return_value=["Inicio"])
    def test_staff_cannot_access_patient_page_translation(self, translate_text_batch):
        self.client.force_login(make_user("doctor"))

        response = self.client.post(
            reverse("portal:translate_page"),
            data=json.dumps({"texts": ["Home"]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        translate_text_batch.assert_not_called()

    @mock.patch("portal.views.gemini.translate_text_batch")
    def test_page_translation_rejects_invalid_input(self, translate_text_batch):
        response = self.client.post(
            reverse("portal:translate_page"),
            data=json.dumps({"texts": [""]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        translate_text_batch.assert_not_called()


class PortalAppointmentTests(PortalTestCase):
    def test_lists_only_own_appointments(self):
        mine = make_appointment(patient=self.patient, provider=self.provider, start=self.future(48))
        make_appointment(patient=self.other, provider=self.provider, start=self.future(72))
        response = self.client.get(reverse("portal:appointments"))
        self.assertEqual(list(response.context["upcoming"]), [mine])
        dashboard = self.client.get(reverse("portal:dashboard"))
        self.assertEqual(list(dashboard.context["upcoming"]), [mine])

    def test_cancel_with_notice(self):
        appointment = make_appointment(patient=self.patient, provider=self.provider, start=self.future(72))
        response = self.client.post(reverse("portal:cancel_appointment", args=[appointment.pk]), {"reason": "Travel"})
        self.assertRedirects(response, reverse("portal:appointments"))
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertEqual(appointment.cancellation_reason, "Travel")
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.CANCEL_APPOINTMENT, patient=self.patient).exists())

    def test_cannot_cancel_within_24_hours(self):
        appointment = make_appointment(patient=self.patient, provider=self.provider, start=self.future(5))
        response = self.client.post(reverse("portal:cancel_appointment", args=[appointment.pk]), follow=True)
        self.assertContains(response, "can&#x27;t be cancelled online")
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.SCHEDULED)

    def test_cannot_cancel_someone_elses_appointment(self):
        appointment = make_appointment(patient=self.other, provider=self.provider, start=self.future(72))
        response = self.client.post(reverse("portal:cancel_appointment", args=[appointment.pk]))
        self.assertEqual(response.status_code, 404)

    def test_cancel_requires_post(self):
        appointment = make_appointment(patient=self.patient, provider=self.provider, start=self.future(72))
        self.assertEqual(self.client.get(reverse("portal:cancel_appointment", args=[appointment.pk])).status_code, 405)

    def test_request_appointment_copies_patient_details(self):
        response = self.client.post(
            reverse("portal:request_appointment"),
            {"preferred_provider": self.provider.pk, "preferred_times": "Mornings", "reason": "BP check"},
        )
        self.assertRedirects(response, reverse("portal:appointments"))
        request = AppointmentRequest.objects.get()
        self.assertEqual(request.patient, self.patient)
        self.assertEqual(request.source, AppointmentRequest.Source.PORTAL)
        self.assertFalse(request.is_new_patient)
        self.assertEqual(request.date_of_birth, self.patient.date_of_birth)

    def test_request_requires_reason(self):
        response = self.client.post(reverse("portal:request_appointment"), {"reason": ""})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AppointmentRequest.objects.exists())


class PortalRecordTests(PortalTestCase):
    def test_health_summary_shows_own_record_and_is_audited(self):
        Allergy.objects.create(patient=self.patient, substance="Penicillin", reaction="Hives")
        Allergy.objects.create(patient=self.other, substance="Latex", reaction="Rash")
        response = self.client.get(reverse("portal:health_summary"))
        self.assertContains(response, "Penicillin")
        self.assertNotContains(response, "Latex")
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.VIEW_OWN_RECORD, patient=self.patient).exists())

    def test_lab_results_grouped_by_panel(self):
        day = timezone.localdate()
        for test in ("LDL", "HDL"):
            LabResult.objects.create(patient=self.patient, panel="Lipid panel", test_name=test, value="1", collected_on=day)
        LabResult.objects.create(patient=self.other, panel="Lipid panel", test_name="Secret", value="1", collected_on=day)
        response = self.client.get(reverse("portal:lab_results"))
        groups = list(response.context["grouped"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0][1]), 2)
        self.assertNotContains(response, "Secret")

    def test_only_signed_own_visits_are_visible(self):
        signed = make_visit(patient=self.patient, provider=self.provider)
        draft = make_visit(patient=self.patient, provider=self.provider, status=Visit.Status.DRAFT)
        others = make_visit(patient=self.other, provider=self.provider)
        response = self.client.get(reverse("portal:visits"))
        self.assertEqual(list(response.context["visits"]), [signed])
        self.assertEqual(self.client.get(reverse("portal:visit_detail", args=[signed.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("portal:visit_detail", args=[draft.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("portal:visit_detail", args=[others.pk])).status_code, 404)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.VIEW_VISIT, object_id=str(signed.pk)).exists())


class PortalMessagingTests(PortalTestCase):
    def test_new_clinical_message_routes_to_primary_provider(self):
        response = self.client.post(
            reverse("portal:new_message"),
            {"category": "prescription", "subject": "Refill", "body": "Please refill"},
        )
        thread = MessageThread.objects.get()
        self.assertRedirects(response, reverse("portal:thread", args=[thread.pk]))
        self.assertEqual(thread.queue, MessageThread.Queue.CLINICAL)
        self.assertEqual(thread.assigned_provider, self.provider)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.SEND_MESSAGE).exists())

    def test_new_billing_message_goes_to_front_office(self):
        self.client.post(reverse("portal:new_message"), {"category": "billing", "subject": "Bill", "body": "Why?"})
        self.assertEqual(MessageThread.objects.get().queue, MessageThread.Queue.FRONT_OFFICE)

    def test_category_prefill(self):
        response = self.client.get(reverse("portal:new_message") + "?category=test_results")
        self.assertEqual(response.context["form"].initial["category"], "test_results")

    def test_reply_and_unread_flag(self):
        thread = make_thread(patient=self.patient)
        thread.patient_has_unread = True
        thread.save()
        self.client.get(reverse("portal:thread", args=[thread.pk]))
        thread.refresh_from_db()
        self.assertFalse(thread.patient_has_unread)
        self.client.post(reverse("portal:thread", args=[thread.pk]), {"body": "Thanks!"})
        self.assertEqual(thread.messages.count(), 2)
        self.assertTrue(thread.messages.last().from_patient)

    def test_cannot_read_other_patients_threads(self):
        thread = make_thread(patient=self.other)
        self.assertEqual(self.client.get(reverse("portal:thread", args=[thread.pk])).status_code, 404)
        listing = self.client.get(reverse("portal:messages"))
        self.assertNotIn(thread, listing.context["threads"])

    def test_patient_can_translate_any_message_into_preferred_language(self):
        self.patient.preferred_language = "Spanish"
        self.patient.save(update_fields=["preferred_language"])
        thread = make_thread(patient=self.patient, category="clinical", body="Necesito ayuda.")
        care_team_message = Message.objects.create(
            thread=thread,
            sender_name=self.provider.display_name,
            from_patient=False,
            body="Please call us about your test result.",
        )
        patient_message = thread.messages.get(from_patient=True)

        page = self.client.get(reverse("portal:thread", args=[thread.pk]))
        self.assertContains(page, "Translate with AI", count=3)

        with mock.patch(
            "portal.views.gemini.translate_to_language",
            side_effect=["Por favor llámenos sobre su resultado.", "Necesito ayuda."],
        ) as translate_to_language:
            response = self.client.post(
                reverse("portal:translate_message", args=[thread.pk, care_team_message.pk])
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "AI-generated Spanish translation")
            self.assertContains(response, "Por favor llámenos sobre su resultado.")

            response = self.client.post(
                reverse("portal:translate_message", args=[thread.pk, patient_message.pk])
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Necesito ayuda.")

        translate_to_language.assert_has_calls(
            [
                mock.call(care_team_message.body, "Spanish", actor_role="patient"),
                mock.call(patient_message.body, "Spanish", actor_role="patient"),
            ]
        )
        care_team_message.refresh_from_db()
        patient_message.refresh_from_db()
        self.assertEqual(care_team_message.body, "Please call us about your test result.")
        self.assertEqual(patient_message.body, "Necesito ayuda.")
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.TRANSLATE_MESSAGE, patient=self.patient).count(),
            2,
        )

    def test_patient_cannot_translate_another_patients_message(self):
        thread = make_thread(patient=self.other)
        message = thread.messages.get()

        response = self.client.post(
            reverse("portal:translate_message", args=[thread.pk, message.pk])
        )

        self.assertEqual(response.status_code, 404)

    @mock.patch("portal.views.gemini.translate_to_language", side_effect=gemini.GeminiError("unavailable"))
    def test_translation_failure_keeps_message_unchanged(self, translate_to_language):
        thread = make_thread(patient=self.patient)
        message = thread.messages.get()

        response = self.client.post(
            reverse("portal:translate_message", args=[thread.pk, message.pk]),
            follow=True,
        )

        self.assertContains(response, "translation service is unavailable")
        message.refresh_from_db()
        self.assertEqual(message.body, "Hello")
