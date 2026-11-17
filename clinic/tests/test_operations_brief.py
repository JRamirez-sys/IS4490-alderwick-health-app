import datetime
import json
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from clinic import gemini
from clinic.models import AppointmentRequest, MessageThread, Visit
from clinic.operations_brief import generate_operations_brief
from clinic.testing import make_patient, make_provider, make_thread, make_visit


class OperationsBriefTests(TestCase):
    def test_front_office_brief_uses_aggregate_data_only(self):
        provider = make_provider()
        patient = make_patient(
            provider=provider,
            first_name="Private",
            last_name="Patient",
        )
        request = AppointmentRequest.objects.create(
            first_name=patient.first_name,
            last_name=patient.last_name,
            date_of_birth=patient.date_of_birth,
            reason="Private request details",
        )
        AppointmentRequest.objects.filter(pk=request.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=2)
        )
        thread = make_thread(
            patient=patient,
            category=MessageThread.Category.APPOINTMENTS,
            subject="Private message subject",
            body="Private message contents",
        )
        MessageThread.objects.filter(pk=thread.pk).update(
            updated_at=timezone.now() - datetime.timedelta(days=3)
        )

        model_response = json.dumps(
            {"priority_order": ["front_office_replies", "appointment_requests"]}
        )
        with mock.patch(
            "clinic.operations_brief.gemini.generate_text", return_value=model_response
        ) as generate:
            brief = generate_operations_brief(User.Role.FRONT_OFFICE)

        prompt = json.loads(generate.call_args.args[0])
        self.assertEqual([item["key"] for item in brief], ["front_office_replies", "appointment_requests"])
        self.assertEqual(prompt["queues"][0]["count"], 1)
        self.assertEqual(prompt["queues"][0]["over_24h"], 1)
        self.assertNotIn(patient.full_name, generate.call_args.args[0])
        self.assertNotIn("Private request details", generate.call_args.args[0])
        self.assertNotIn("Private message subject", generate.call_args.args[0])
        self.assertNotIn("Private message contents", generate.call_args.args[0])
        self.assertEqual(generate.call_args.kwargs["actor_role"], User.Role.FRONT_OFFICE)
        self.assertEqual(generate.call_args.kwargs["operation"], "daily_operations_brief")

    def test_clinician_brief_is_scoped_to_their_notes_and_messages(self):
        provider = make_provider()
        colleague = make_provider()
        patient = make_patient(provider=provider)
        colleague_patient = make_patient(provider=colleague)
        make_visit(
            patient=patient,
            provider=provider,
            status=Visit.Status.DRAFT,
            visit_date=timezone.now() - datetime.timedelta(days=3),
        )
        make_visit(
            patient=colleague_patient,
            provider=colleague,
            status=Visit.Status.DRAFT,
            visit_date=timezone.now() - datetime.timedelta(days=5),
        )
        make_thread(patient=patient, category=MessageThread.Category.CLINICAL)
        make_thread(patient=colleague_patient, category=MessageThread.Category.CLINICAL)

        with mock.patch(
            "clinic.operations_brief.gemini.generate_text",
            return_value=json.dumps({"priority_order": ["unsigned_notes", "clinical_replies"]}),
        ) as generate:
            brief = generate_operations_brief(User.Role.DOCTOR, provider=provider)

        prompt = json.loads(generate.call_args.args[0])
        metrics = {queue["key"]: queue for queue in prompt["queues"]}
        self.assertEqual([item["key"] for item in brief], ["unsigned_notes", "clinical_replies"])
        self.assertEqual(metrics["unsigned_notes"]["count"], 1)
        self.assertEqual(metrics["unsigned_notes"]["over_24h"], 1)
        self.assertEqual(metrics["clinical_replies"]["count"], 1)
        self.assertNotIn(colleague_patient.full_name, generate.call_args.args[0])

    def test_invalid_priority_response_fails_explicitly(self):
        provider = make_provider()
        patient = make_patient(provider=provider)
        make_visit(patient=patient, provider=provider, status=Visit.Status.DRAFT)

        with mock.patch(
            "clinic.operations_brief.gemini.generate_text",
            return_value=json.dumps({"priority_order": ["not_a_real_queue"]}),
        ):
            with self.assertRaises(gemini.GeminiError):
                generate_operations_brief(User.Role.DOCTOR, provider=provider)

    def test_no_open_work_does_not_call_gemini(self):
        with mock.patch("clinic.operations_brief.gemini.generate_text") as generate:
            brief = generate_operations_brief(User.Role.FRONT_OFFICE)

        self.assertEqual(brief, [])
        generate.assert_not_called()
