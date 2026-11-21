import os
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from aiops.models import AIOperation, AIConfiguration
from clinic import gemini


@override_settings(DEBUG=True)
class GeminiCheckPageTests(TestCase):
    url = reverse("devtools:gemini_check")

    def test_hidden_when_debug_is_off(self):
        with self.settings(DEBUG=False):
            self.assertEqual(self.client.get(self.url).status_code, 404)

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False)
    def test_shows_missing_key(self):
        response = self.client.get(self.url)
        self.assertContains(response, "not set")

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-1234"}, clear=False)
    def test_shows_key_hint_without_revealing_key(self):
        response = self.client.get(self.url)
        self.assertContains(response, "ends in ...1234")
        self.assertNotContains(response, "test-key-1234")

    @mock.patch("clinic.gemini.generate_text", return_value="Message received.")
    def test_post_shows_reply(self, generate_text):
        response = self.client.post(self.url, {"prompt": "Hello"})
        generate_text.assert_called_once_with("Hello")
        self.assertContains(response, "It works.")
        self.assertContains(response, "Message received.")

    @mock.patch("clinic.gemini.generate_text", side_effect=gemini.GeminiError("PermissionDenied: bad key"))
    def test_post_shows_error(self, generate_text):
        response = self.client.post(self.url, {"prompt": "Hello"})
        self.assertContains(response, "Request failed")
        self.assertContains(response, "PermissionDenied: bad key")


class GeminiClientTests(TestCase):
    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}, clear=True)
    def test_missing_key_raises(self):
        with self.assertRaises(gemini.GeminiError):
            gemini.generate_text("Hello")
        operation = AIOperation.objects.get()
        self.assertEqual(operation.status, AIOperation.Status.FAILED)
        self.assertEqual(operation.error_type, "GeminiError")

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k", "GEMINI_MODEL": "test-model"}, clear=False)
    @mock.patch("clinic.gemini.genai.Client")
    def test_generate_text_returns_reply(self, client_class):
        client_class.return_value.models.generate_content.return_value = mock.Mock(text="Hi there")
        self.assertEqual(gemini.generate_text("Hello"), "Hi there")
        operation = AIOperation.objects.get()
        self.assertEqual(operation.status, AIOperation.Status.SUCCEEDED)
        self.assertEqual(operation.model, "test-model")
        client_class.return_value.models.generate_content.assert_called_once_with(
            model="test-model", contents="Hello", config=None
        )

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k", "langsmithkey": "ls-test"}, clear=True)
    @mock.patch("clinic.gemini._get_langsmith_client")
    @mock.patch("clinic.gemini._traced_generate_content", return_value=mock.Mock(text="Hi there"))
    @mock.patch("clinic.gemini.genai.Client")
    @mock.patch("clinic.gemini.tracing_context")
    def test_configured_langsmith_key_traces_model_call(
        self, tracing_context, client_class, traced_call, langsmith_client_factory
    ):
        langsmith_client = mock.Mock()
        langsmith_client_factory.return_value = langsmith_client

        self.assertEqual(gemini.generate_text("Hello"), "Hi there")

        langsmith_client_factory.assert_called_once_with("ls-test")
        tracing_context.assert_called_once_with(
            client=langsmith_client,
            project_name="alderwick-health",
            enabled=True,
        )
        traced_call.assert_called_once_with(
            client_class.return_value,
            model=gemini.model_name(),
            prompt="Hello",
            config=None,
            operation="text_generation",
        )

    def test_langsmith_trace_payload_excludes_prompts_and_replies(self):
        response = mock.Mock(
            text="A confidential reply",
            usage_metadata=mock.Mock(
                prompt_token_count=12,
                candidates_token_count=8,
                total_token_count=20,
            ),
        )

        inputs = gemini._trace_inputs(
            {
                "model": "gemini-test",
                "operation": "message_translation",
                "prompt": "A confidential prompt",
                "config": {"system_instruction": "Confidential instructions"},
            }
        )
        outputs = gemini._trace_outputs(response)

        self.assertEqual(inputs, {"model": "gemini-test", "operation": "message_translation"})
        self.assertEqual(
            outputs,
            {
                "output_length": len("A confidential reply"),
                "prompt_token_count": 12,
                "candidates_token_count": 8,
                "total_token_count": 20,
            },
        )
        self.assertNotIn("A confidential prompt", repr(inputs))
        self.assertNotIn("A confidential reply", repr(outputs))

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=False)
    @mock.patch("clinic.gemini.genai.Client")
    def test_generate_text_supports_json_responses(self, client_class):
        client_class.return_value.models.generate_content.return_value = mock.Mock(text='{"translations": []}')
        gemini.generate_text(
            "[]",
            system_instruction="Translate the input.",
            response_mime_type="application/json",
        )

        config = client_class.return_value.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.system_instruction, "Translate the input.")
        self.assertEqual(config.response_mime_type, "application/json")

        operation = AIOperation.objects.get()
        self.assertEqual(operation.operation, AIOperation.Operation.TEXT_GENERATION)
        self.assertEqual(operation.status, AIOperation.Status.SUCCEEDED)

    @mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=False)
    @mock.patch("clinic.gemini.genai.Client")
    def test_api_failure_raises_gemini_error(self, client_class):
        client_class.return_value.models.generate_content.side_effect = RuntimeError("boom")
        with self.assertRaises(gemini.GeminiError):
            gemini.generate_text("Hello")
        operation = AIOperation.objects.get()
        self.assertEqual(operation.status, AIOperation.Status.FAILED)
        self.assertEqual(operation.error_type, "RuntimeError")


class GeminiTranslationTests(TestCase):
    @mock.patch(
        "clinic.gemini.generate_text",
        return_value='{"translations": ["Inicio", "Resumen de salud"]}',
    )
    def test_translate_text_batch_preserves_order(self, generate_text):
        result = gemini.translate_text_batch(["Home", "Health summary"], "Spanish")

        self.assertEqual(result, ["Inicio", "Resumen de salud"])
        self.assertEqual(generate_text.call_args.kwargs["response_mime_type"], "application/json")
        self.assertEqual(
            generate_text.call_args.kwargs["operation"],
            "portal_page_translation",
        )
        self.assertEqual(generate_text.call_args.kwargs["input_count"], 2)
        self.assertIn('"target_language": "Spanish"', generate_text.call_args.args[0])

    @mock.patch("clinic.gemini.generate_text", return_value="Hola")
    def test_message_translation_passes_operation_metadata(self, generate_text):
        self.assertEqual(gemini.translate_to_language("Hello", "Spanish", actor_role="patient"), "Hola")
        self.assertEqual(generate_text.call_args.kwargs["operation"], "message_translation")
        self.assertEqual(generate_text.call_args.kwargs["actor_role"], "patient")
        self.assertEqual(generate_text.call_args.kwargs["target_language"], "Spanish")
        self.assertEqual(generate_text.call_args.kwargs["model"], gemini.model_name())

    @mock.patch.dict(os.environ, {"GEMINI_APPROVED_MODELS": "approved-model"}, clear=False)
    @mock.patch("clinic.gemini.generate_text")
    def test_translation_uses_published_configuration(self, generate_text):
        AIConfiguration.objects.create(
            selected_model="approved-model",
            target_languages=["Spanish"],
            supplemental_instruction="Use plain language.",
        )
        generate_text.return_value = "Hola"

        result = gemini.translate_to_language("Hello", "Spanish")

        self.assertEqual(result, "Hola")
        self.assertEqual(generate_text.call_args.kwargs["model"], "approved-model")
        self.assertIn("Use plain language.", generate_text.call_args.kwargs["system_instruction"])
        self.assertIn("Fixed translation requirements", generate_text.call_args.kwargs["system_instruction"])

    @mock.patch("clinic.gemini.generate_text")
    def test_disabled_or_unavailable_translations_never_call_model(self, generate_text):
        AIConfiguration.objects.create(translation_enabled=False)
        with self.assertRaises(gemini.TranslationUnavailableError):
            gemini.translate_to_language("Hello", "Spanish")

        configuration = AIConfiguration.objects.get(pk=1)
        configuration.translation_enabled = True
        configuration.target_languages = ["Spanish"]
        configuration.save()
        with self.assertRaises(gemini.TranslationUnavailableError):
            gemini.translate_to_language("Hello", "French")

        generate_text.assert_not_called()

    @mock.patch("clinic.gemini.generate_text", return_value='{"translations": ["Inicio"]}')
    def test_translate_text_batch_rejects_incomplete_response(self, generate_text):
        with self.assertRaises(gemini.GeminiError):
            gemini.translate_text_batch(["Home", "Health summary"], "Spanish")
