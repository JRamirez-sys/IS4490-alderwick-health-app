import json
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from google.genai import types

from accounts.models import User
from audit.models import AuditLog
from clinic import gemini
from clinic.medicine_lookup import _grounded_sources, _official_dailymed_image
from clinic.models import Allergy
from clinic.testing import make_patient, make_provider, make_user


class MedicineLookupTests(TestCase):
    def setUp(self):
        self.front_office = make_user(User.Role.FRONT_OFFICE)
        self.doctor = make_user(User.Role.DOCTOR)

    def post_lookup(self, medicine="metformin"):
        return self.client.post(
            reverse("clinic:medicine_lookup_search"),
            json.dumps({"medicine": medicine}),
            content_type="application/json",
        )

    def grounded_response(self, sources, answer="Metformin is a prescription medicine."):
        chunks = [
            SimpleNamespace(web=SimpleNamespace(uri=url, title=title))
            for url, title in sources
        ]
        return SimpleNamespace(
            text=answer,
            candidates=[
                SimpleNamespace(
                    grounding_metadata=SimpleNamespace(grounding_chunks=chunks),
                )
            ],
        )

    def test_front_office_and_doctor_can_open_lookup(self):
        for user in (self.front_office, self.doctor):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.get(reverse("clinic:medicine_lookup"))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Medicine lookup")
                self.assertContains(response, "Enter only a medicine name")
                self.assertContains(
                    response,
                    f'href="{reverse("clinic:medicine_lookup")}"',
                )
                if user.role == User.Role.DOCTOR:
                    self.assertContains(response, "Check recorded allergies in my patient panel")
                else:
                    self.assertNotContains(response, "Check recorded allergies in my patient panel")

    def test_search_uses_only_medicine_name_and_returns_trusted_grounding_sources(self):
        self.client.force_login(self.doctor)
        generated = self.grounded_response(
            [
                ("https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=example", "DailyMed label"),
                ("https://fda.gov.evil.example/fake", "Untrusted lookalike"),
                ("http://medlineplus.gov/example", "Unencrypted source"),
            ]
        )
        with mock.patch(
            "clinic.medicine_lookup.gemini.generate_with_google_search",
            return_value=generated,
        ) as generate:
            response = self.post_lookup()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "answer": "Metformin is a prescription medicine.",
                "sources": [
                    {
                        "title": "DailyMed label",
                        "url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=example",
                    }
                ],
                "image": None,
            },
        )
        self.assertEqual(json.loads(generate.call_args.args[0]), {"medicine": "metformin"})
        self.assertIn("Do not provide patient-specific advice", generate.call_args.kwargs["system_instruction"])
        self.assertEqual(generate.call_args.kwargs["operation"], "medicine_lookup")
        self.assertEqual(generate.call_args.kwargs["actor_role"], User.Role.DOCTOR)

    def test_search_fails_closed_without_trusted_grounding_citations(self):
        self.client.force_login(self.front_office)
        generated = self.grounded_response([("https://example.com/medicine", "Unverified source")])
        with mock.patch(
            "clinic.medicine_lookup.gemini.generate_with_google_search",
            return_value=generated,
        ):
            response = self.post_lookup()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"],
            "Medicine lookup is unavailable right now. Please try again later.",
        )

    def test_google_grounding_redirect_to_trusted_source_is_accepted(self):
        self.client.force_login(self.front_office)
        redirect_url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token"
        trusted_url = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=example"
        generated = self.grounded_response([(redirect_url, "DailyMed label")])
        headers = Message()
        headers["Location"] = trusted_url
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(redirect_url, 302, "Found", headers, BytesIO())
        with (
            mock.patch("clinic.medicine_lookup.gemini.generate_with_google_search", return_value=generated),
            mock.patch("clinic.medicine_lookup.build_opener", return_value=opener),
        ):
            response = self.post_lookup()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sources"][0]["url"], trusted_url)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)

    def test_google_grounding_redirect_to_untrusted_source_is_rejected(self):
        redirect_url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/token"
        generated = self.grounded_response([(redirect_url, "Untrusted source")])
        headers = Message()
        headers["Location"] = "https://www.drugs.com/lyrica.html"
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(redirect_url, 302, "Found", headers, BytesIO())

        with mock.patch("clinic.medicine_lookup.build_opener", return_value=opener):
            sources = _grounded_sources(generated)

        self.assertEqual(sources, [])

    def test_official_label_image_requires_one_exact_dailymed_match(self):
        setid = "9187708f-cc7b-4fe9-828e-9a77e98354ad"
        source_url = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"
        image_url = (
            f"https://dailymed.nlm.nih.gov/dailymed/image.cfm?setid={setid}&name=lyrica-01.jpg"
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "data": {
                    "setid": setid,
                    "title": "LYRICA (PREGABALIN) CAPSULE",
                    "media": [{"mime_type": "image/jpeg", "url": image_url}],
                }
            }
        ).encode()
        opener = mock.Mock()
        opener.open.return_value = response

        with mock.patch("clinic.medicine_lookup.build_opener", return_value=opener):
            image = _official_dailymed_image("Lyrica", [{"url": source_url}])

        self.assertEqual(
            image,
            {
                "url": image_url,
                "alt": "Image from the DailyMed label for LYRICA (PREGABALIN) CAPSULE",
                "caption": "LYRICA (PREGABALIN) CAPSULE",
                "source_url": source_url,
            },
        )

    def test_official_label_image_is_omitted_for_ambiguous_products(self):
        setids = (
            "9187708f-cc7b-4fe9-828e-9a77e98354ad",
            "e8089a1f-6a21-4d4d-bb7f-7fbeef213eab",
        )
        sources = [
            {"url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"}
            for setid in setids
        ]
        with mock.patch("clinic.medicine_lookup.build_opener") as build_opener:
            image = _official_dailymed_image("Lyrica", sources)

        self.assertIsNone(image)
        build_opener.assert_not_called()

    def test_official_label_image_is_omitted_when_label_name_does_not_match(self):
        setid = "9187708f-cc7b-4fe9-828e-9a77e98354ad"
        source_url = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {"data": {"setid": setid, "title": "PREGABALIN CAPSULE", "media": []}}
        ).encode()
        opener = mock.Mock()
        opener.open.return_value = response

        with mock.patch("clinic.medicine_lookup.build_opener", return_value=opener):
            image = _official_dailymed_image("Lyrica", [{"url": source_url}])

        self.assertIsNone(image)

    def test_invalid_medicine_is_rejected_without_calling_model(self):
        self.client.force_login(self.front_office)
        with mock.patch("clinic.medicine_lookup.gemini.generate_with_google_search") as generate:
            response = self.post_lookup("m" * 121)

        self.assertEqual(response.status_code, 400)
        generate.assert_not_called()

    def test_patient_details_are_rejected_without_calling_model(self):
        self.client.force_login(self.front_office)
        with mock.patch("clinic.medicine_lookup.gemini.generate_with_google_search") as generate:
            response = self.post_lookup("medicine for patient born 01/02/1980")

        self.assertEqual(response.status_code, 400)
        self.assertIn("Do not include patient", response.json()["error"])
        generate.assert_not_called()

    def test_non_staff_roles_are_denied(self):
        for role in (User.Role.PATIENT, User.Role.AI_ENGINEER, User.Role.ADMIN):
            with self.subTest(role=role):
                self.client.force_login(make_user(role))
                self.assertEqual(self.client.get(reverse("clinic:medicine_lookup")).status_code, 403)
                self.assertEqual(self.post_lookup().status_code, 403)
                self.assertEqual(
                    self.client.post(
                        reverse("clinic:medicine_lookup_allergy_matches"),
                        json.dumps({"medicine": "metformin"}),
                        content_type="application/json",
                    ).status_code,
                    403,
                )


class MedicineLookupAllergyTests(TestCase):
    def setUp(self):
        self.provider = make_provider()
        self.doctor = self.provider.user
        self.patient = make_patient(provider=self.provider)
        Allergy.objects.create(
            patient=self.patient,
            substance="Metformin",
            severity=Allergy.Severity.SEVERE,
        )
        self.other_provider = make_provider()
        self.outside_patient = make_patient(provider=self.other_provider)
        Allergy.objects.create(
            patient=self.outside_patient,
            substance="Metformin",
            severity=Allergy.Severity.MODERATE,
        )

    def post_matches(self, medicine="metformin"):
        return self.client.post(
            reverse("clinic:medicine_lookup_allergy_matches"),
            json.dumps({"medicine": medicine}),
            content_type="application/json",
        )

    def test_doctor_sees_exact_matches_only_in_authorized_patient_panel(self):
        self.client.force_login(self.doctor)
        with mock.patch("clinic.medicine_lookup.gemini.generate_with_google_search") as generate:
            response = self.post_matches()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["match_count"], 1)
        self.assertEqual(
            response.json()["matches"],
            [
                {
                    "patient_name": self.patient.full_name,
                    "mrn": self.patient.mrn,
                    "chart_url": reverse("clinician:chart", kwargs={"pk": self.patient.pk}),
                    "allergies": [{"substance": "Metformin", "severity": "Severe"}],
                }
            ],
        )
        generate.assert_not_called()
        audit_event = AuditLog.objects.get(action=AuditLog.Action.VIEW_CHART, patient=self.patient)
        self.assertEqual(audit_event.details, {"section": "allergies", "source": "medicine_lookup"})
        self.assertFalse(AuditLog.objects.filter(patient=self.outside_patient).exists())

    def test_allergy_match_is_exact_and_case_insensitive(self):
        Allergy.objects.create(patient=self.patient, substance="Metformin ER")
        self.client.force_login(self.doctor)

        response = self.post_matches("metformin")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["match_count"], 1)
        self.assertEqual(len(response.json()["matches"][0]["allergies"]), 1)

    def test_doctor_without_provider_profile_cannot_search_allergies(self):
        self.client.force_login(make_user(User.Role.DOCTOR))

        response = self.post_matches()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["error"],
            "A clinician provider profile is required to search allergy records.",
        )

    def test_front_office_cannot_search_patient_allergies(self):
        self.client.force_login(make_user(User.Role.FRONT_OFFICE))

        response = self.post_matches()

        self.assertEqual(response.status_code, 403)


class GeminiGoogleSearchTests(TestCase):
    def test_google_search_grounding_tool_is_attached_and_logged(self):
        client = mock.Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="General information.")
        with (
            mock.patch("clinic.gemini.get_client", return_value=client),
            mock.patch("clinic.gemini._record_operation") as record_operation,
        ):
            response = gemini.generate_with_google_search(
                '{"medicine":"metformin"}',
                operation="medicine_lookup",
                actor_role=User.Role.DOCTOR,
            )

        self.assertEqual(response.text, "General information.")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsInstance(config.tools[0].google_search, types.GoogleSearch)
        self.assertEqual(record_operation.call_args.kwargs["operation"], "medicine_lookup")
        self.assertEqual(record_operation.call_args.kwargs["status"], "succeeded")
