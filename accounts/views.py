from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils.html import format_html
from django.shortcuts import redirect
from django.urls import reverse

from .models import User
from .forms import SignInForm


class SignInView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = SignInForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        if self.request.user.role in (User.Role.FRONT_OFFICE, User.Role.DOCTOR):
            assistant_url = reverse("clinic:staff_assistant")
            messages.info(
                self.request,
                format_html(
                    "<strong>New: Employee assistant.</strong> Ask about published practice policies and "
                    "procedures, get general medicine information, or draft a response for staff review. "
                    "<a href='{}'>See what it can do</a>.",
                    assistant_url,
                ),
            )
        return response


class SignOutView(auth_views.LogoutView):
    pass


@login_required
def home(request):
    """Send each user to the right workspace for their role."""
    return redirect(request.user.home_url())
