import os
from unittest import mock

from django.test import TestCase
from django.urls import reverse

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
