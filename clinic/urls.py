from django.urls import path

from . import medicine_lookup, staff_assistant

app_name = "clinic"

urlpatterns = [
    path("", staff_assistant.index, name="staff_assistant"),
    path("chat/", staff_assistant.chat, name="staff_assistant_chat"),
    path("medicine-lookup/", medicine_lookup.index, name="medicine_lookup"),
    path("medicine-lookup/search/", medicine_lookup.search, name="medicine_lookup_search"),
    path("medicine-lookup/allergy-matches/", medicine_lookup.allergy_matches, name="medicine_lookup_allergy_matches"),
]
