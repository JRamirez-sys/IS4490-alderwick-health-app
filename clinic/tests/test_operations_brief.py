import datetime
import json
from unittest import mock

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from clinic import gemini
from clinic.models import Appointment, AppointmentRequest, InsurancePlan, MessageThread, PolicyDocument, Provider, Visit
from clinic.operations_brief import (
    generate_operations_brief,
    generate_operations_summary,
    get_operations_calendar,
    get_recent_operations_changes,
)
from clinic.testing import local_dt, make_appointment, make_patient, make_plan, make_provider, make_thread, make_visit


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
        self.assertEqual(metrics["clinical_replies"]["unread_count"], 1)
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

    def test_calendar_is_seven_days_and_scoped_to_the_provider(self):
        provider = make_provider()
        colleague = make_provider()
        patient = make_patient(provider=provider)
        colleague_patient = make_patient(provider=colleague)
        today = timezone.localdate()
        mine = make_appointment(
            patient=patient,
            provider=provider,
            start=local_dt(today, 10),
            status=Appointment.Status.SCHEDULED,
        )
        make_appointment(
            patient=colleague_patient,
            provider=colleague,
            start=local_dt(today, 11),
            status=Appointment.Status.SCHEDULED,
        )
        make_appointment(
            patient=patient,
            provider=provider,
            start=local_dt(today, 12),
            status=Appointment.Status.CANCELLED,
        )

        calendar = get_operations_calendar(provider=provider)

        self.assertEqual(len(calendar["days"]), 7)
        self.assertEqual(calendar["days"][0]["appointments"], [mine])
        self.assertEqual(calendar["summary"]["appointment_count"], 1)
        self.assertEqual(calendar["summary"]["patient_count"], 1)
        self.assertEqual(calendar["summary"]["daily_counts"][0]["count"], 1)

    def test_recent_change_feed_uses_admin_history_and_excludes_patient_records(self):
        provider = make_provider()
        plan = make_plan()
        policy = PolicyDocument.objects.create(
            title="Updated cancellation policy",
            slug="updated-cancellation-policy",
            category=PolicyDocument.Category.PATIENT_POLICY,
            summary="Summary",
            body="Policy text",
            effective_date=timezone.localdate(),
        )
        now = timezone.now()
        for model, obj in ((Provider, provider), (InsurancePlan, plan), (PolicyDocument, policy)):
            LogEntry.objects.create(
                user=provider.user,
                content_type=ContentType.objects.get_for_model(model),
                object_id=str(obj.pk),
                object_repr=str(obj),
                action_flag=CHANGE,
                change_message="[]",
                action_time=now,
            )
        patient = make_patient(provider=provider)
        LogEntry.objects.create(
            user=provider.user,
            content_type=ContentType.objects.get_for_model(patient),
            object_id=str(patient.pk),
            object_repr=patient.full_name,
            action_flag=CHANGE,
            change_message="[]",
            action_time=now,
        )
        LogEntry.objects.create(
            user=provider.user,
            content_type=ContentType.objects.get_for_model(Provider),
            object_id=str(provider.pk),
            object_repr="Old provider edit",
            action_flag=CHANGE,
            change_message="[]",
            action_time=now - datetime.timedelta(days=8),
        )

        change_feed = get_recent_operations_changes(now=now)
        changes = change_feed["events"]

        self.assertEqual(len(changes), 3)
        self.assertEqual({change["record_type"] for change in changes}, {"provider", "insurance plan", "policy"})
        self.assertTrue(all(change["action"] == "Updated" for change in changes))
        self.assertNotIn(patient.full_name, str(changes))
        self.assertEqual(
            {
                (change["record_type"], change["action"], change["count"])
                for change in change_feed["counts"]
            },
            {("provider", "Updated", 1), ("insurance plan", "Updated", 1), ("policy", "Updated", 1)},
        )
        self.assertFalse(change_feed["truncated"])

    def test_change_feed_caps_display_but_counts_every_recent_change(self):
        provider = make_provider()
        content_type = ContentType.objects.get_for_model(Provider)
        now = timezone.now()
        LogEntry.objects.bulk_create(
            [
                LogEntry(
                    user=provider.user,
                    content_type=content_type,
                    object_id=str(provider.pk),
                    object_repr=f"Provider update {index}",
                    action_flag=CHANGE,
                    change_message="[]",
                    action_time=now,
                )
                for index in range(26)
            ]
        )

        change_feed = get_recent_operations_changes(now=now)

        self.assertEqual(len(change_feed["events"]), 25)
        self.assertEqual(change_feed["counts"], [{"record_type": "provider", "action": "Updated", "count": 26}])
        self.assertTrue(change_feed["truncated"])

    def test_ai_summary_receives_only_aggregate_counts_and_change_categories(self):
        tasks = [
            {
                "key": "front_office_replies",
                "label": "Patient messages",
                "count": 2,
                "over_24h": 1,
                "unread_count": 2,
                "patient_name": "Private Patient",
                "message_body": "Private message contents",
            }
        ]
        calendar_summary = {
            "appointment_count": 3,
            "patient_count": 2,
            "daily_counts": [{"date": "2025-01-01", "count": 3}],
            "patient_name": "Private Patient",
        }
        change_counts = [
            {
                "record_type": "provider",
                "action": "Updated",
                "count": 1,
                "object_repr": "Private Patient",
            }
        ]
        with mock.patch(
            "clinic.operations_brief.gemini.generate_text",
            return_value="Three appointments are scheduled this week.",
        ) as generate:
            summary = generate_operations_summary(
                tasks,
                calendar_summary,
                change_counts,
                User.Role.FRONT_OFFICE,
            )

        prompt = json.loads(generate.call_args.args[0])
        self.assertEqual(summary, "Three appointments are scheduled this week.")
        self.assertEqual(
            prompt["appointments"],
            {
                "appointment_count": 3,
                "patient_count": 2,
                "daily_counts": [{"date": "2025-01-01", "count": 3}],
            },
        )
        self.assertEqual(
            prompt["work_queues"],
            [
                {"key": "appointment_requests", "count": 0, "over_24h": 0, "unread_count": 0},
                {"key": "front_office_replies", "count": 2, "over_24h": 1, "unread_count": 2},
            ],
        )
        self.assertEqual(
            prompt["recent_admin_changes"],
            [{"record_type": "provider", "action": "Updated", "count": 1}],
        )
        self.assertNotIn("Private Patient", generate.call_args.args[0])
        self.assertNotIn("Private message contents", generate.call_args.args[0])
