from django import forms

from clinic.seed_content import LANGUAGES


SUPPORTED_LANGUAGES = tuple(dict.fromkeys(LANGUAGES.values()))


class AIConfigurationDraftForm(forms.Form):
    translation_enabled = forms.BooleanField(
        required=False,
        label="Enable AI translation",
        help_text="When disabled and published, staff and patients cannot request translations.",
    )
    selected_model = forms.ChoiceField(label="Gemini model")
    target_languages = forms.MultipleChoiceField(
        choices=[(language, language) for language in SUPPORTED_LANGUAGES],
        label="Available translation languages",
        help_text="Patients can translate portal pages into their preferred language; staff use English.",
    )
    supplemental_instruction = forms.CharField(
        required=False,
        max_length=1000,
        label="Supplemental style or terminology guidance",
        help_text=(
            "Optional guidance appended to the fixed translation safeguards. Do not include patient data."
        ),
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(self, *args, model_names, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["selected_model"].choices = [(name, name) for name in model_names]
        self.fields["target_languages"].choices = [(language, language) for language in SUPPORTED_LANGUAGES]
        self.fields["translation_enabled"].widget.attrs["class"] = "input"
        self.fields["selected_model"].widget.attrs["class"] = "input"
        self.fields["target_languages"].widget.attrs.update({"class": "input", "size": len(SUPPORTED_LANGUAGES)})
        self.fields["supplemental_instruction"].widget.attrs["class"] = "input"

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("translation_enabled") and not cleaned_data.get("target_languages"):
            self.add_error("target_languages", "Select at least one language while translation is enabled.")
        return cleaned_data
