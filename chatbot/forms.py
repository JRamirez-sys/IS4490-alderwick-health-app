from django import forms

from .models import ChatContactRequest


class ChatContactForm(forms.ModelForm):
    # Honeypot: hidden from people, often filled in by spam bots.
    website = forms.CharField(required=False)

    class Meta:
        model = ChatContactRequest
        fields = ["name", "email", "phone", "message"]

    def clean_message(self):
        message = self.cleaned_data["message"].strip()
        if not message:
            raise forms.ValidationError("Please type your question.")
        return message

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("email") and not cleaned.get("phone"):
            raise forms.ValidationError("Please give us an email address or phone number so we can reach you.")
        return cleaned

    @property
    def is_spam(self):
        return bool(self.cleaned_data.get("website"))
