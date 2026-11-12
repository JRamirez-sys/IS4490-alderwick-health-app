import datetime
import json

from django.test import TestCase
from django.urls import reverse

from clinic.models import OfficeHours
from clinic.testing import make_office, make_plan, make_provider, make_specialty, make_user

from . import engine
from .models import ChatContactRequest


class ChatbotDataMixin:
    @classmethod
    def setUpTestData(cls):
        cls.office = make_office(name="Sandy Clinic", slug="sandy", parking_notes="Free lot behind the building.")
        for weekday in range(5):
            OfficeHours.objects.create(office=cls.office, weekday=weekday,
                                       opens=datetime.time(8, 0), closes=datetime.time(17, 0))
        OfficeHours.objects.create(office=cls.office, weekday=5, opens=datetime.time(9, 0), closes=datetime.time(13, 0))
        cls.plan = make_plan(name="SilverPeak PPO", carrier="SilverPeak Health", code="silverpeak-ppo")
        cls.specialty = make_specialty(name="Pediatrics", slug="pediatrics")
        cls.provider = make_provider(office=cls.office, specialty=cls.specialty, plans=[cls.plan],
                                     languages="English, Spanish", offers_telehealth=True)


class EngineTests(ChatbotDataMixin, TestCase):
    def ask(self, text):
        return engine.respond(text)

    def test_office_hours_are_grouped_by_day(self):
        self.assertEqual(engine.format_hours(self.office), "Mon-Fri 8:00 AM-5:00 PM; Sat 9:00 AM-1:00 PM")

    def test_hours_for_named_office(self):
        reply = self.ask("What are the hours at your Sandy office?")
        self.assertEqual(reply.intent, "hours")
        self.assertIn("Mon-Fri 8:00 AM-5:00 PM", reply.text)
        self.assertIn("Free lot", reply.text)

    def test_general_hours(self):
        reply = self.ask("are you open on saturday")
        self.assertEqual(reply.intent, "hours")
        self.assertIn("Sandy Clinic", reply.text)

    def test_named_insurance_plan(self):
        reply = self.ask("Do you take SilverPeak PPO?")
        self.assertEqual(reply.intent, "insurance")
        self.assertIn("Yes, we accept SilverPeak PPO", reply.text)
        self.assertTrue(any("insurance=silverpeak-ppo" in link["url"] for link in reply.links))

    def test_general_insurance_lists_plans(self):
        reply = self.ask("what insurance do you accept")
        self.assertEqual(reply.intent, "insurance")
        self.assertIn("SilverPeak PPO", reply.text)

    def test_specialty_question(self):
        reply = self.ask("Do you have a pediatrician accepting new patients?")
        self.assertEqual(reply.intent, "providers")
        self.assertIn("Pediatrics", reply.text)

    def test_language_question(self):
        reply = self.ask("Do any doctors speak Spanish?")
        self.assertEqual(reply.intent, "providers")
        self.assertIn("speak Spanish", reply.text)

    def test_booking_links_to_request_form(self):
        reply = self.ask("How do I book an appointment?")
        self.assertEqual(reply.intent, "booking")
        self.assertIn(reverse("public:request_appointment"), [link["url"] for link in reply.links])

    def test_portal_help(self):
        self.assertEqual(self.ask("I forgot my portal password").intent, "portal")

    def test_cancel(self):
        self.assertEqual(self.ask("I need to cancel my appointment").intent, "cancel")

    def test_emergency_is_checked_first(self):
        reply = self.ask("I have chest pain, what are your hours?")
        self.assertEqual(reply.intent, "emergency")
        self.assertTrue(reply.urgent)
        self.assertIn("911", reply.text)

    def test_crisis_points_to_988(self):
        reply = self.ask("I want to kill myself")
        self.assertTrue(reply.urgent)
        self.assertIn("988", reply.text)

    def test_medical_questions_are_not_answered(self):
        reply = self.ask("Should I take ibuprofen for my rash?")
        self.assertEqual(reply.intent, "medical_advice")
        self.assertIn("can't give medical advice", reply.text)

    def test_ask_for_a_person_offers_contact_form(self):
        reply = self.ask("Can I talk to a real person?")
        self.assertEqual(reply.intent, "human")
        self.assertTrue(reply.offer_contact)

    def test_unknown_question_falls_back(self):
        reply = self.ask("qwerty zxcv")
        self.assertEqual(reply.intent, "fallback")
        self.assertTrue(reply.offer_contact)


class MessageEndpointTests(ChatbotDataMixin, TestCase):
    url = reverse("chatbot:message")

    def post(self, payload):
        return self.client.post(self.url, json.dumps(payload), content_type="application/json")

    def test_returns_reply_json(self):
        response = self.post({"message": "What insurance do you accept?"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["intent"], "insurance")
        self.assertIn("reply", data)
        self.assertIn("links", data)

    def test_empty_message_rejected(self):
        self.assertEqual(self.post({"message": "  "}).status_code, 400)

    def test_bad_json_rejected(self):
        response = self.client.post(self.url, "not json", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_csrf_is_enforced(self):
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(self.url, json.dumps({"message": "hi"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)


class ContactEndpointTests(ChatbotDataMixin, TestCase):
    url = reverse("chatbot:contact")

    def test_creates_request_with_last_topic(self):
        self.client.post(reverse("chatbot:message"), json.dumps({"message": "billing question"}),
                         content_type="application/json")
        response = self.client.post(self.url, {"name": "Rae Visitor", "phone": "(801) 555-0123",
                                               "message": "Can someone call me about a bill?"})
        self.assertEqual(response.status_code, 200)
        item = ChatContactRequest.objects.get()
        self.assertTrue(item.reference.startswith("WC-"))
        self.assertEqual(item.topic, "Billing and payments")
        self.assertEqual(item.status, ChatContactRequest.Status.OPEN)
        self.assertIn(item.reference, response.json()["reply"])

    def test_requires_email_or_phone(self):
        response = self.client.post(self.url, {"name": "Rae", "message": "Hello"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ChatContactRequest.objects.exists())

    def test_honeypot_is_silently_dropped(self):
        response = self.client.post(self.url, {"name": "Bot", "email": "bot@example.com", "message": "spam",
                                               "website": "http://spam.example"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ChatContactRequest.objects.exists())

    def test_limited_per_session(self):
        data = {"name": "Rae", "email": "rae@example.com", "message": "Question"}
        for _ in range(3):
            self.assertEqual(self.client.post(self.url, data).status_code, 200)
        self.assertEqual(self.client.post(self.url, data).status_code, 429)
        self.assertEqual(ChatContactRequest.objects.count(), 3)


class HomepageWidgetTests(TestCase):
    def test_widget_is_on_homepage(self):
        response = self.client.get(reverse("public:home"))
        self.assertContains(response, 'id="chatbot"')
        self.assertContains(response, "chatbot/chatbot.js")

    def test_widget_is_not_on_other_pages(self):
        response = self.client.get(reverse("public:insurance"))
        self.assertNotContains(response, 'id="chatbot"')


class StaffQueueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = make_user("front_office")
        cls.item = ChatContactRequest.objects.create(name="Rae Visitor", email="rae@example.com", message="Question")

    def test_front_office_can_view_and_close(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("chatbot_staff:chat_requests"))
        self.assertContains(response, "Rae Visitor")
        detail = reverse("chatbot_staff:chat_request_detail", args=[self.item.pk])
        self.assertContains(self.client.get(detail), "Question")
        self.client.post(detail, {"action": "close", "staff_notes": "Called back."})
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ChatContactRequest.Status.CLOSED)
        self.assertEqual(self.item.handled_by, self.staff)
        self.assertEqual(self.item.staff_notes, "Called back.")

    def test_other_roles_are_denied(self):
        for role in ("patient", "doctor", "ai_engineer"):
            with self.subTest(role=role):
                self.client.force_login(make_user(role))
                self.assertEqual(self.client.get(reverse("chatbot_staff:chat_requests")).status_code, 403)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("chatbot_staff:chat_requests"))
        self.assertEqual(response.status_code, 302)
