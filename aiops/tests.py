import os
import datetime
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from aiops.models import AIConfiguration, AIOperation
from audit.models import AuditLog
from clinic import gemini
from clinic.testing import make_user


class AIOperationsTests(TestCase):
    def test_ai_engineer_sees_dashboard(self):
        self.client.force_login(make_user("ai_engineer"))
        response = self.client.get(reverse("aiops:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent AI activity")

    def test_dashboard_shows_operation_metadata_without_content(self):
        AIOperation.objects.create(
            operation=AIOperation.Operation.MESSAGE_TRANSLATION,
            status=AIOperation.Status.SUCCEEDED,
            model="gemini-test",
            actor_role="doctor",
            target_language="English",
            input_count=1,
            duration_ms=123,
        )
        self.client.force_login(make_user("ai_engineer"))

        response = self.client.get(reverse("aiops:index"))

        self.assertContains(response, "Message translation")
        self.assertContains(response, "gemini-test")
        self.assertContains(response, "doctor")
        self.assertContains(response, "English")
        self.assertContains(response, "123 ms")
        self.assertNotContains(response, "Prompt text")

    def test_dashboard_shows_failure_metadata(self):
        AIOperation.objects.create(
            operation=AIOperation.Operation.PORTAL_PAGE_TRANSLATION,
            status=AIOperation.Status.FAILED,
            model="gemini-test",
            actor_role="patient",
            target_language="Spanish",
            input_count=12,
            duration_ms=45,
            error_type="GeminiError",
        )
        self.client.force_login(make_user("ai_engineer"))

        response = self.client.get(reverse("aiops:index"))

        self.assertContains(response, "Failed model calls")
        self.assertContains(response, "Portal page translation")
        self.assertContains(response, "GeminiError")

    def test_dashboard_catalog_describes_agents_and_tool_access(self):
        self.client.force_login(make_user("ai_engineer"))

        response = self.client.get(reverse("aiops:index"))

        self.assertContains(response, "AI agent inventory")
        self.assertContains(response, "Employee assistant")
        self.assertContains(response, "Published policy retrieval")
        self.assertContains(response, "Daily operations brief")
        self.assertContains(response, "Medicine lookup")
        self.assertContains(response, "Google Search grounding")
        self.assertContains(response, "Authorized patient allergy match")
        self.assertContains(response, "Translation assistant")
        self.assertContains(response, "portal page text may include patient-specific content")

    def test_dashboard_analytics_are_grouped_by_agent_for_last_30_days(self):
        recent_operations = [
            {
                "operation": AIOperation.Operation.MEDICINE_LOOKUP,
                "status": AIOperation.Status.SUCCEEDED,
                "duration_ms": 100,
            },
            {
                "operation": AIOperation.Operation.MEDICINE_LOOKUP,
                "status": AIOperation.Status.FAILED,
                "duration_ms": 300,
            },
            {
                "operation": AIOperation.Operation.STAFF_POLICY_ASSISTANT,
                "status": AIOperation.Status.SUCCEEDED,
                "duration_ms": 200,
            },
            {
                "operation": AIOperation.Operation.TEXT_GENERATION,
                "status": AIOperation.Status.SUCCEEDED,
                "duration_ms": 100,
            },
        ]
        for values in recent_operations:
            AIOperation.objects.create(model="gemini-test", actor_role="doctor", **values)
        old_operation = AIOperation.objects.create(
            operation=AIOperation.Operation.MEDICINE_LOOKUP,
            status=AIOperation.Status.SUCCEEDED,
            model="gemini-test",
            actor_role="doctor",
            duration_ms=150,
        )
        AIOperation.objects.filter(pk=old_operation.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=31)
        )
        self.client.force_login(make_user("ai_engineer"))

        response = self.client.get(reverse("aiops:index"))

        analytics = response.context["agent_analytics"]
        agents = {agent["slug"]: agent for agent in analytics["agents"]}
        self.assertEqual(analytics["calls"], 4)
        self.assertEqual(analytics["failures"], 1)
        self.assertEqual(analytics["success_rate"], 75.0)
        self.assertEqual(analytics["average_duration_ms"], 175)
        self.assertEqual(agents["medicine-lookup"]["calls"], 2)
        self.assertEqual(agents["medicine-lookup"]["failures"], 1)
        self.assertEqual(agents["medicine-lookup"]["success_rate"], 50.0)
        self.assertEqual(agents["medicine-lookup"]["average_duration_ms"], 200)
        self.assertEqual(agents["staff-policy-assistant"]["calls"], 1)
        self.assertEqual(agents["other-ai-operations"]["calls"], 1)
        self.assertEqual(agents["other-ai-operations"]["operation_labels"], "Text generation")

    def test_dashboard_shows_only_aggregate_allergy_tool_analytics(self):
        engineer = make_user("ai_engineer")
        doctor = make_user("doctor")
        for matches_found in (True, True, False):
            AuditLog.objects.create(
                user=doctor,
                username=doctor.username,
                role=doctor.role,
                action=AuditLog.Action.SEARCH_ALLERGY_RECORDS,
                details={"matches_found": matches_found},
            )
        old_event = AuditLog.objects.create(
            user=doctor,
            username=doctor.username,
            role=doctor.role,
            action=AuditLog.Action.SEARCH_ALLERGY_RECORDS,
            details={"matches_found": True},
        )
        AuditLog.objects.filter(pk=old_event.pk).update(
            timestamp=timezone.now() - datetime.timedelta(days=31)
        )
        self.client.force_login(engineer)

        response = self.client.get(reverse("aiops:index"))

        analytics = response.context["agent_analytics"]["allergy_tool_analytics"]
        self.assertEqual(analytics["checks"], 3)
        self.assertEqual(analytics["checks_with_matches"], 2)
        self.assertEqual(analytics["match_rate"], 66.7)
        self.assertContains(response, "Clinician allergy-check usage")
        self.assertContains(response, "Checks with matches")
        self.assertNotContains(response, "Metformin")
        self.assertNotContains(response, "Test Patient")

    @mock.patch.dict(os.environ, {"GEMINI_APPROVED_MODELS": ""}, clear=False)
    def test_engineer_saves_draft_then_publishes_settings(self):
        engineer = make_user("ai_engineer")
        self.client.force_login(engineer)
        settings_data = {
            "translation_enabled": "",
            "selected_model": gemini.model_name(),
            "target_languages": ["Spanish", "Vietnamese"],
            "supplemental_instruction": "Prefer plain-language wording.",
        }

        draft_response = self.client.post(reverse("aiops:save_settings_draft"), settings_data)

        self.assertRedirects(draft_response, reverse("aiops:index"))
        configuration = AIConfiguration.objects.get(pk=1)
        self.assertTrue(configuration.translation_enabled)
        self.assertTrue(configuration.has_draft)
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.PUBLISH_AI_SETTINGS).exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.UPDATE_AI_SETTINGS, user=engineer).exists())

        publish_response = self.client.post(reverse("aiops:publish_settings"))

        self.assertRedirects(publish_response, reverse("aiops:index"))
        configuration.refresh_from_db()
        self.assertFalse(configuration.translation_enabled)
        self.assertEqual(configuration.selected_model, gemini.model_name())
        self.assertEqual(configuration.target_languages, ["Spanish", "Vietnamese"])
        self.assertEqual(configuration.supplemental_instruction, "Prefer plain-language wording.")
        self.assertFalse(configuration.has_draft)
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.PUBLISH_AI_SETTINGS, user=engineer).exists())

    @mock.patch.dict(os.environ, {"GEMINI_APPROVED_MODELS": ""}, clear=False)
    def test_unapproved_model_cannot_be_saved(self):
        self.client.force_login(make_user("ai_engineer"))

        response = self.client.post(
            reverse("aiops:save_settings_draft"),
            {
                "translation_enabled": "on",
                "selected_model": "unapproved-model",
                "target_languages": ["English"],
                "supplemental_instruction": "",
            },
            follow=True,
        )

        self.assertContains(response, "not saved")
        self.assertFalse(AIConfiguration.objects.filter(pk=1, draft_updated_at__isnull=False).exists())

    def test_translation_settings_are_only_drafts_until_published(self):
        configuration = AIConfiguration.objects.create(
            translation_enabled=True,
            selected_model=gemini.model_name(),
            target_languages=["Spanish"],
            supplemental_instruction="Use simple wording.",
        )
        self.client.force_login(make_user("ai_engineer"))
        draft_data = {
            "translation_enabled": "on",
            "selected_model": gemini.model_name(),
            "target_languages": ["Spanish"],
            "supplemental_instruction": "Use formal wording.",
        }

        self.client.post(reverse("aiops:save_settings_draft"), draft_data)

        configuration.refresh_from_db()
        self.assertEqual(configuration.supplemental_instruction, "Use simple wording.")
        self.assertEqual(configuration.draft_supplemental_instruction, "Use formal wording.")

    def test_only_ai_engineers_can_manage_settings(self):
        self.client.force_login(make_user("doctor"))

        response = self.client.post(reverse("aiops:save_settings_draft"), {})

        self.assertEqual(response.status_code, 403)

    def test_other_roles_are_denied(self):
        for role in ("patient", "front_office", "doctor"):
            with self.subTest(role=role):
                self.client.force_login(make_user(role))
                self.assertEqual(self.client.get(reverse("aiops:index")).status_code, 403)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("aiops:index"))
        self.assertRedirects(response, f"{reverse('accounts:login')}?next={reverse('aiops:index')}")
