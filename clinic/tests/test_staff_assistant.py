import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from clinic.models import PolicyDocument
from clinic.testing import make_user


class StaffPolicyAssistantTests(TestCase):
    def setUp(self):
        self.public_policy = PolicyDocument.objects.create(
            title="Appointment Policy",
            slug="appointment-policy",
            category=PolicyDocument.Category.PATIENT_POLICY,
            summary="Appointment changes",
            body="Please give at least 24 hours' notice to cancel.",
            effective_date=timezone.localdate(),
        )
        self.private_policy = PolicyDocument.objects.create(
            title="Internal Draft",
            slug="internal-draft",
            category=PolicyDocument.Category.PATIENT_POLICY,
            summary="Not for publication",
            body="This content must not be sent to the assistant.",
            effective_date=timezone.localdate(),
            is_public=False,
        )

    def post_chat(self, messages, mode="answer"):
        return self.client.post(
            reverse("clinic:staff_assistant_chat"),
            json.dumps({"mode": mode, "messages": messages}),
            content_type="application/json",
        )

    def test_front_office_and_doctor_can_open_assistant(self):
        self.client.force_login(make_user(User.Role.FRONT_OFFICE))
        front_office_response = self.client.get(reverse("clinic:staff_assistant"))
        self.assertEqual(front_office_response.status_code, 200)
        self.assertContains(front_office_response, "Policy assistant")
        self.assertContains(front_office_response, "Draft a response")
        self.assertLess(
            front_office_response.content.index(b'Policy assistant</span><span class="ai-badge">AI</span></a>'),
            front_office_response.content.index(b'<div class="nav-label">Practice</div>'),
        )

        self.client.force_login(make_user(User.Role.DOCTOR))
        doctor_response = self.client.get(reverse("clinic:staff_assistant"))
        self.assertEqual(doctor_response.status_code, 200)
        self.assertContains(doctor_response, "Policy assistant")
        self.assertContains(doctor_response, "Draft a response")

    def test_front_office_dashboard_links_to_assistant_page(self):
        self.client.force_login(make_user(User.Role.FRONT_OFFICE))

        response = self.client.get(reverse("frontdesk:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'placeholder="Name, MRN, DOB, or phone"')
        self.assertContains(response, 'class="search-icon"')
        self.assertContains(response, 'class="ai-sparkle"')
        self.assertContains(response, 'class="ai-badge">AI</span>')
        self.assertContains(
            response,
            f'class="btn btn-secondary assistant-link" href="{reverse("clinic:staff_assistant")}"',
        )
        self.assertNotContains(response, 'id="assistant-form"')
        self.assertLess(
            response.content.index(b'id="dq"'),
            response.content.index(
                b'class="btn btn-secondary assistant-link"',
                response.content.index(b'id="dq"'),
            ),
        )

    def test_chat_uses_only_public_policies_and_returns_verified_links(self):
        self.client.force_login(make_user(User.Role.DOCTOR))
        answer = {"answer": "Please call the clinic within 24 hours.", "sources": [self.public_policy.slug]}
        with mock.patch("clinic.staff_assistant.gemini.generate_text", return_value=json.dumps(answer)) as generate:
            response = self.post_chat([{"role": "user", "content": "What is the cancellation notice?"}])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "answer": answer["answer"],
                "sources": [
                    {
                        "title": self.public_policy.title,
                        "url": self.public_policy.get_absolute_url(),
                    }
                ],
            },
        )
        prompt = json.loads(generate.call_args.args[0])
        self.assertEqual([doc["slug"] for doc in prompt["published_policies"]], [self.public_policy.slug])
        self.assertNotIn(self.private_policy.body, generate.call_args.args[0])
        self.assertEqual(generate.call_args.kwargs["actor_role"], User.Role.DOCTOR)
        self.assertEqual(generate.call_args.kwargs["operation"], "staff_policy_assistant")

    def test_draft_mode_uses_staff_review_instructions(self):
        self.client.force_login(make_user(User.Role.FRONT_OFFICE))
        with mock.patch(
            "clinic.staff_assistant.gemini.generate_text",
            return_value=json.dumps({"answer": "Draft response.", "sources": []}),
        ) as generate:
            response = self.post_chat(
                [{"role": "user", "content": "Please draft a response about the cancellation policy."}],
                mode="draft",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Do not send the response", generate.call_args.kwargs["system_instruction"])

    def test_invalid_conversation_is_rejected_without_calling_model(self):
        self.client.force_login(make_user(User.Role.FRONT_OFFICE))
        with mock.patch("clinic.staff_assistant.gemini.generate_text") as generate:
            response = self.post_chat(
                [
                    {"role": "user", "content": "First question"},
                    {"role": "user", "content": "Unexpected second user message"},
                ]
            )

        self.assertEqual(response.status_code, 400)
        generate.assert_not_called()

    def test_non_staff_roles_are_denied(self):
        for role in (User.Role.PATIENT, User.Role.AI_ENGINEER, User.Role.ADMIN):
            with self.subTest(role=role):
                self.client.force_login(make_user(role))
                self.assertEqual(self.client.get(reverse("clinic:staff_assistant")).status_code, 403)
                self.assertEqual(
                    self.post_chat([{"role": "user", "content": "A policy question"}]).status_code,
                    403,
                )

    def test_unpublished_or_invalid_model_response_fails_closed(self):
        self.client.force_login(make_user(User.Role.DOCTOR))
        with mock.patch(
            "clinic.staff_assistant.gemini.generate_text",
            return_value=json.dumps({"answer": "Unsupported answer.", "sources": ["internal-draft"]}),
        ):
            response = self.post_chat([{"role": "user", "content": "What is the policy?"}])

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "The assistant is unavailable right now. Please try again later.")
