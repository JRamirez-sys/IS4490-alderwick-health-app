"""Rule-based FAQ engine for the public website chatbot.

Each visitor message is matched against a fixed set of topics ("intents")
using keywords. Facts such as office hours, insurance plans, and specialties
are read from the database, so answers stay in step with the rest of the site.

The chatbot is for the public: it never looks up or discusses patient records,
and it never gives medical advice.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.urls import reverse

from clinic.context_processors import PRACTICE
from clinic.models import InsurancePlan, Office, PolicyDocument, Provider, Specialty

MAX_MESSAGE_LENGTH = 500

DEFAULT_SUGGESTIONS = [
    "Office hours",
    "Insurance we accept",
    "Book an appointment",
    "Patient portal help",
    "Talk to staff",
]

DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Checked before anything else. Any match returns the emergency reply.
CRISIS_PATTERNS = [
    r"suicid", r"kill (my ?self|me)\b", r"end (my|it) (life|all)", r"self[- ]harm", r"hurt(ing)? myself",
    r"want to die", r"overdos",
]
EMERGENCY_PATTERNS = [
    r"chest pain", r"heart attack", r"can'?t breathe", r"cannot breathe", r"trouble breathing",
    r"short(ness)? of breath", r"\bstroke\b", r"seizure", r"unconscious", r"passed out", r"not breathing",
    r"(severe|heavy|bad) bleeding", r"bleeding (a lot|heavily|badly)", r"anaphyla", r"allergic reaction",
    r"\b911\b", r"\bemergency\b", r"\bdying\b",
]

# (intent, label, strong keywords [2 points], weak keywords [1 point])
# Order matters only to break ties: earlier topics win.
INTENTS = [
    ("human", "Talk to staff",
     ["human", "real person", "representative", "talk to someone", "speak to someone", "talk to staff",
      "speak with", "talk to a person", "call me", "call back", "callback", "front desk", "front office"],
     ["staff", "person", "someone", "agent", "contact", "talk to", "speak to"]),
    ("cancel", "Cancel or reschedule",
     ["cancel", "reschedule", "no-show", "no show", "move my appointment", "change my appointment"],
     ["late"]),
    ("refills", "Prescription refills",
     ["refill", "prescription", "pharmacy"],
     ["medication", "meds", "rx"]),
    ("results", "Test results",
     ["lab result", "test result", "results"],
     ["labs"]),
    ("records", "Records and forms",
     ["medical record", "records", "release form", "transfer my", "paperwork"],
     ["form", "record"]),
    ("telehealth", "Video visits",
     ["video", "telehealth", "virtual", "online visit", "zoom", "remote visit"],
     ["online"]),
    ("after_hours", "After-hours care",
     ["after hours", "after-hours", "nurse line", "urgent care", "middle of the night"],
     ["night", "urgent", "evening"]),
    ("portal", "Patient portal",
     ["portal", "log in", "login", "sign in", "signin", "password", "mychart", "my chart", "username",
      "secure message", "message my doctor"],
     ["account", "online access"]),
    ("billing", "Billing and payments",
     ["bill", "billing", "payment", "copay", "co-pay", "self-pay", "self pay", "statement", "balance",
      "how much", "price", "cost"],
     ["pay", "charge", "fee"]),
    ("insurance", "Insurance",
     ["insurance", "in network", "in-network", "medicare", "medicaid", "coverage", "covered"],
     ["plan", "accept", "ppo", "hmo", "epo"]),
    ("providers", "Find a provider",
     ["accepting new", "doctor", "provider", "physician", "pediatrician", "specialist", "nurse practitioner",
      "physician assistant", "speaks", "language"],
     ["specialty", "specialties", "who should i see", "female", "male"]),
    ("booking", "Book an appointment",
     ["appointment", "book", "new patient", "appt", "first visit", "see a doctor", "become a patient"],
     ["schedule", "visit", "available", "availability"]),
    ("hours", "Office hours",
     ["hours", "open", "closing", "opening", "what time"],
     ["close", "saturday", "sunday", "weekend", "holiday", "today", "tomorrow"]),
    ("locations", "Locations",
     ["location", "address", "where are you", "where is", "directions", "parking", "closest", "nearest",
      "phone number"],
     ["office", "clinic", "near", "phone", "located", "map"]),
    ("services", "Services",
     ["services", "do you offer", "do you treat", "flu shot", "vaccin", "physical", "well-child", "well child"],
     ["service", "offer", "treat", "care for"]),
    ("privacy", "Privacy",
     ["privacy", "hipaa"],
     ["confidential", "my data"]),
    ("medical_advice", "Medical question",
     ["should i take", "is it normal", "diagnos", "side effect", "dosage", "what medicine", "what should i do"],
     ["symptom", "fever", "rash", "pain", "sick", "hurt", "cough"]),
    ("thanks", "Thanks",
     ["thank", "thanks", "appreciate"],
     ["thx", "great", "perfect"]),
    ("greeting", "Greeting",
     ["hello", "good morning", "good afternoon", "good evening"],
     ["hi", "hey", "help"]),
]

INTENT_LABELS = {name: label for name, label, _strong, _weak in INTENTS}
INTENT_LABELS.update({"emergency": "Emergency", "fallback": "Not understood"})


@dataclass
class Reply:
    text: str
    intent: str = "fallback"
    links: list = field(default_factory=list)
    suggestions: list = field(default_factory=lambda: list(DEFAULT_SUGGESTIONS))
    offer_contact: bool = False
    urgent: bool = False

    def add_link(self, label, url):
        if url:
            self.links.append({"label": label, "url": url})
        return self

    def as_dict(self):
        return {
            "reply": self.text,
            "intent": self.intent,
            "topic": INTENT_LABELS.get(self.intent, ""),
            "links": self.links,
            "suggestions": self.suggestions,
            "offer_contact": self.offer_contact,
            "urgent": self.urgent,
        }


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def normalize(text):
    text = (text or "")[:MAX_MESSAGE_LENGTH].lower()
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def _has(text, keyword):
    """True when keyword starts a word in text. Short keywords must match a whole word."""
    end = r"(?![a-z0-9])" if len(keyword) <= 3 else ""
    return re.search(r"(?<![a-z0-9])" + re.escape(keyword) + end, text) is not None


def _any(text, patterns):
    return any(re.search(p, text) for p in patterns)


def _match_offices(text):
    found = []
    for office in Office.objects.prefetch_related("hours"):
        names = {office.slug.replace("-", " "), office.name.lower()}
        if office.is_virtual:
            names.discard("virtual care")  # "virtual" is handled by the video-visit topic
        if any(name and _has(text, name) for name in names):
            found.append(office)
    return found


def _match_plans(text):
    """Insurance plans named in the message, by plan name, carrier, or Medicare/Medicaid."""
    plans = list(InsurancePlan.objects.filter(is_active=True).exclude(plan_type=InsurancePlan.PlanType.SELF_PAY))
    exact = [p for p in plans if _has(text, p.name.lower())]
    if exact:
        return exact
    by_carrier = []
    for plan in plans:
        carrier = plan.carrier.lower()
        first_word = carrier.split()[0] if carrier else ""
        if carrier and (_has(text, carrier) or (len(first_word) >= 5 and _has(text, first_word))):
            by_carrier.append(plan)
    if by_carrier:
        return by_carrier
    for keyword, plan_type in (("medicare", InsurancePlan.PlanType.MEDICARE_ADVANTAGE),
                               ("medicaid", InsurancePlan.PlanType.MEDICAID)):
        if _has(text, keyword):
            return [p for p in plans if p.plan_type == plan_type]
    return []


def _match_specialty(text):
    for specialty in Specialty.objects.all():
        name = specialty.name.lower()
        stem = name.split()[0][:6]
        # Only distinctive words get stem matching ("pediatrician" -> Pediatrics), not "family".
        if _has(text, name) or (len(name.split()[0]) >= 8 and _has(text, stem)):
            return specialty
    return None


def _all_languages():
    languages = set()
    for value in Provider.objects.filter(is_active=True).values_list("languages", flat=True):
        languages.update(lang.strip() for lang in value.split(",") if lang.strip())
    return sorted(languages)


def _match_language(text):
    for language in _all_languages():
        if language.lower() != "english" and _has(text, language.lower()):
            return language
    return None


def classify(text, *, offices=(), plans=(), specialty=None, language=None):
    scores = {}
    for name, _label, strong, weak in INTENTS:
        score = sum(2 for kw in strong if _has(text, kw)) + sum(1 for kw in weak if _has(text, kw))
        if score:
            scores[name] = score
    # Named things in the message are strong hints about the topic.
    if offices:
        for name in ("hours", "locations"):
            scores[name] = scores.get(name, 0) + 1
    if plans:
        scores["insurance"] = scores.get("insurance", 0) + 3
    if specialty or language:
        scores["providers"] = scores.get("providers", 0) + 2
    if not scores:
        return "fallback"
    order = [name for name, *_rest in INTENTS]
    return max(scores, key=lambda name: (scores[name], -order.index(name)))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _time(value):
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}"


def format_hours(office):
    """Summarize office hours, e.g. 'Mon-Fri 8:00 AM-5:00 PM; Sat 9:00 AM-1:00 PM'."""
    groups = []
    for row in sorted(office.hours.all(), key=lambda h: h.weekday):
        if groups and groups[-1][1] == row.weekday - 1 and groups[-1][2:] == [row.opens, row.closes]:
            groups[-1][1] = row.weekday
        else:
            groups.append([row.weekday, row.weekday, row.opens, row.closes])
    parts = []
    for first, last, opens, closes in groups:
        days = DAY_ABBR[first] if first == last else f"{DAY_ABBR[first]}-{DAY_ABBR[last]}"
        parts.append(f"{days} {_time(opens)}-{_time(closes)}")
    return "; ".join(parts) or "Please call for hours"


def _bullets(lines):
    return "\n".join(f"• {line}" for line in lines)


def _doc_url(slug):
    if PolicyDocument.objects.filter(slug=slug, is_public=True).exists():
        return reverse("public:resource_detail", args=[slug])
    return None


def _url(name, *args, **query):
    url = reverse(name, args=args)
    return f"{url}?{urlencode(query)}" if query else url


def _office_details(office):
    lines = [office.name, office.full_address]
    if office.phone:
        lines.append(f"Phone: {office.phone}")
    lines.append(f"Hours: {format_hours(office)}")
    if office.parking_notes:
        lines.append(f"Parking: {office.parking_notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

def _emergency(crisis):
    if crisis:
        text = (
            "If you are thinking about harming yourself or are in crisis, please call or text 988 "
            "(Suicide & Crisis Lifeline) now. If you are in immediate danger, call 911.\n\n"
            "This chat is automated and is not monitored by a person."
        )
    else:
        text = (
            "If this is a medical emergency, call 911 now or go to the nearest emergency room.\n\n"
            f"For urgent questions that aren't emergencies, call our after-hours nurse line at {PRACTICE['nurse_line']}. "
            "This chat is automated and is not monitored by a person."
        )
    reply = Reply(text, intent="emergency", urgent=True, suggestions=[])
    return reply.add_link("After-hours care and emergencies", _doc_url("after-hours-and-emergencies"))


def _answer_greeting(ctx):
    return Reply(
        "Hi! I'm the Alderwick Family Health virtual assistant. I can help with office hours, locations, "
        "insurance, finding a provider, booking an appointment, and the patient portal. What can I help you with?",
        intent="greeting",
    )


def _answer_thanks(ctx):
    return Reply("You're welcome! Is there anything else I can help you with?", intent="thanks")


def _answer_hours(ctx):
    offices = ctx["offices"]
    if offices:
        reply = Reply("\n\n".join(_office_details(o) for o in offices), intent="hours")
        for office in offices:
            reply.add_link(office.name, office.get_absolute_url())
        return reply
    lines = [f"{o.name}: {format_hours(o)}" for o in Office.objects.prefetch_related("hours")]
    reply = Reply(
        "Here are our clinic hours:\n" + _bullets(lines)
        + f"\n\nAfter hours, call our nurse line at {PRACTICE['nurse_line']}.",
        intent="hours",
    )
    return reply.add_link("All locations", reverse("public:location_list"))


def _answer_locations(ctx):
    offices = ctx["offices"]
    if offices:
        reply = Reply("\n\n".join(_office_details(o) for o in offices), intent="locations")
        for office in offices:
            reply.add_link(office.name, office.get_absolute_url())
        return reply
    lines = []
    for office in Office.objects.all():
        detail = office.full_address + (f" · {office.phone}" if office.phone else "")
        lines.append(f"{office.name}: {detail}")
    reply = Reply(
        "We have these locations:\n" + _bullets(lines) + "\n\nAsk me about a specific clinic for its hours and parking.",
        intent="locations",
    )
    return reply.add_link("All locations", reverse("public:location_list"))


def _answer_insurance(ctx):
    plans = ctx["plans"]
    if len(plans) == 1:
        plan = plans[0]
        count = plan.providers.filter(is_active=True).count()
        if count:
            text = (
                f"Yes, we accept {plan.name} ({plan.get_plan_type_display()}). "
                f"{count} of our providers are in network for this plan."
            )
        else:
            text = f"{plan.name} is on our list of accepted plans. Please call {PRACTICE['main_phone']} to confirm which providers are in network."
        reply = Reply(text + " Coverage can differ by provider, so please confirm with your plan too.", intent="insurance")
        reply.add_link(f"Providers who take {plan.name}", _url("public:provider_list", insurance=plan.code))
        return reply.add_link("Insurance we accept", reverse("public:insurance"))
    if plans:
        reply = Reply(
            "We accept these matching plans:\n" + _bullets(p.name for p in plans)
            + "\n\nTell me the exact plan name to see how many of our providers take it.",
            intent="insurance",
        )
        return reply.add_link("Insurance we accept", reverse("public:insurance"))
    names = [p.name for p in InsurancePlan.objects.filter(is_active=True)]
    reply = Reply(
        "We accept these plans:\n" + _bullets(names)
        + "\n\nCoverage can differ by provider. Don't see your plan? Call billing at "
        f"{PRACTICE['billing_phone']}, or choose \"Plan not listed\" when you request an appointment.",
        intent="insurance",
    )
    reply.add_link("Insurance we accept", reverse("public:insurance"))
    return reply.add_link("Insurance and billing FAQ", _doc_url("insurance-billing"))


def _answer_billing(ctx):
    reply = Reply(
        f"For questions about a bill, payments, or self-pay pricing, call our billing office at {PRACTICE['billing_phone']}. "
        "Our Insurance and Billing FAQ covers copays, self-pay pricing, and payment plans.",
        intent="billing",
    )
    reply.add_link("Insurance and billing FAQ", _doc_url("insurance-billing"))
    return reply.add_link("Insurance we accept", reverse("public:insurance"))


def _answer_booking(ctx):
    specialty = ctx["specialty"]
    reply = Reply(
        "New patients: request an appointment online. Our patient services team will call you within one business "
        "day to confirm a time, verify your insurance, and set up your patient portal.\n\n"
        f"Current patients: request an appointment in the patient portal, or call us at {PRACTICE['main_phone']}.",
        intent="booking",
    )
    query = {"specialty": specialty.slug} if specialty else {}
    reply.add_link("Request an appointment", _url("public:request_appointment", **query))
    reply.add_link("Patient portal", reverse("portal:dashboard"))
    return reply.add_link("New patient information", _doc_url("new-patients"))


def _answer_cancel(ctx):
    reply = Reply(
        "Please give us at least 24 hours' notice to cancel or reschedule. Fees may apply for late cancellations "
        "and missed visits.\n\n"
        "Patients can cancel online in the patient portal up to 24 hours before a visit. "
        f"Within 24 hours, please call {PRACTICE['main_phone']}.",
        intent="cancel",
    )
    reply.add_link("Cancellation policy", _doc_url("cancellation-no-show"))
    return reply.add_link("Patient portal", reverse("portal:dashboard"))


def _answer_providers(ctx):
    specialty, language = ctx["specialty"], ctx["language"]
    active = Provider.objects.filter(is_active=True)
    if specialty:
        providers = active.filter(specialty=specialty)
        total, accepting = providers.count(), providers.filter(accepting_new_patients=True).count()
        reply = Reply(
            f"We have {total} {specialty.name} provider{'s' if total != 1 else ''}, and {accepting} "
            f"{'is' if accepting == 1 else 'are'} accepting new patients."
            + (f"\n\n{specialty.description}" if specialty.description else ""),
            intent="providers",
        )
        reply.add_link(f"See {specialty.name} providers", _url("public:provider_list", specialty=specialty.slug))
        return reply.add_link("Request an appointment", _url("public:request_appointment", specialty=specialty.slug))
    if language:
        providers = active.filter(languages__icontains=language)
        total = providers.count()
        reply = Reply(
            f"{total} of our providers speak {language}. You can also filter the provider directory by language, "
            "location, insurance, and video visits.",
            intent="providers",
        )
        return reply.add_link(f"Providers who speak {language}", _url("public:provider_list", language=language))
    specialties = Specialty.objects.annotate(n=Count("providers", filter=Q(providers__is_active=True)))
    reply = Reply(
        f"We have {active.count()} doctors, physician assistants, and nurse practitioners, and "
        f"{active.filter(accepting_new_patients=True).count()} are accepting new patients. Our specialties:\n"
        + _bullets(f"{s.name} ({s.n})" for s in specialties)
        + "\n\nYou can filter providers by specialty, location, insurance, language, and video visits.",
        intent="providers",
    )
    reply.add_link("Find a provider", reverse("public:provider_list"))
    return reply.add_link("Providers accepting new patients", _url("public:provider_list", accepting="1"))


def _answer_services(ctx):
    names = [s.name for s in Specialty.objects.all()]
    reply = Reply(
        "We offer care in these areas:\n" + _bullets(names)
        + "\n\nEach clinic's page lists the services offered at that location.",
        intent="services",
    )
    reply.add_link("Locations and services", reverse("public:location_list"))
    return reply.add_link("Find a provider", reverse("public:provider_list"))


def _answer_portal(ctx):
    reply = Reply(
        "In the patient portal you can see upcoming appointments, visit summaries, and lab results, request "
        "appointments, and send secure messages to your care team. New patients get portal access when their "
        "first appointment is set up.\n\n"
        f"Trouble signing in or forgot your password? Call us at {PRACTICE['main_phone']} and we'll help you get back in.",
        intent="portal",
    )
    reply.add_link("Sign in", reverse("accounts:login"))
    return reply.add_link("Portal and secure messaging", _doc_url("patient-portal-and-messaging"))


def _answer_refills(ctx):
    reply = Reply(
        "Current patients can request a refill by sending a secure message to their care team in the patient portal. "
        "Our refill policy explains how long refills take and when a visit is needed.\n\n"
        f"If you're about to run out of an important medication after hours, call our nurse line at {PRACTICE['nurse_line']}.",
        intent="refills",
    )
    reply.add_link("Prescription refill policy", _doc_url("prescription-refills"))
    return reply.add_link("Patient portal", reverse("portal:dashboard"))


def _answer_results(ctx):
    reply = Reply(
        "Released lab and test results appear in the patient portal. If you have questions about a result, send a "
        "secure message to your care team from the portal.",
        intent="results",
    )
    return reply.add_link("Patient portal", reverse("portal:dashboard"))


def _answer_records(ctx):
    reply = Reply(
        "For medical records or forms, current patients can send a secure message in the patient portal "
        f"(choose \"Records and forms\") or call {PRACTICE['main_phone']}. "
        "New patients can read how we transfer records in our New Patient Policy.",
        intent="records",
    )
    reply.add_link("Patient portal", reverse("portal:dashboard"))
    return reply.add_link("New patient policy", _doc_url("new-patients"))


def _answer_telehealth(ctx):
    count = Provider.objects.filter(is_active=True, offers_telehealth=True).count()
    reply = Reply(
        f"We offer secure video visits from anywhere in Utah, and {count} of our providers offer them. "
        "Our Video Visits page explains which concerns are a good fit and how to join.",
        intent="telehealth",
    )
    reply.add_link("Video visits", _doc_url("video-visits"))
    return reply.add_link("Providers offering video visits", _url("public:provider_list", telehealth="1"))


def _answer_after_hours(ctx):
    reply = Reply(
        "For a medical emergency, call 911.\n\n"
        f"For urgent questions when our clinics are closed, call our after-hours nurse line at {PRACTICE['nurse_line']}.",
        intent="after_hours",
    )
    return reply.add_link("After-hours care and emergencies", _doc_url("after-hours-and-emergencies"))


def _answer_medical_advice(ctx):
    reply = Reply(
        "I'm an automated assistant, so I can't give medical advice. "
        f"For a health concern, call our nurse line at {PRACTICE['nurse_line']} or request an appointment. "
        "If it's an emergency, call 911.",
        intent="medical_advice",
    )
    reply.add_link("Request an appointment", reverse("public:request_appointment"))
    return reply.add_link("After-hours care", _doc_url("after-hours-and-emergencies"))


def _answer_privacy(ctx):
    reply = Reply(
        "Our Notice of Privacy Practices explains how we use and protect your health information. "
        "Please don't share detailed health information in this chat.",
        intent="privacy",
    )
    return reply.add_link("Privacy notice", _doc_url("privacy-notice"))


def _answer_human(ctx):
    return Reply(
        "I can pass your question to our front office team. Fill in the short form and someone will get back to you "
        f"during business hours. You can also call us at {PRACTICE['main_phone']}.",
        intent="human",
        offer_contact=True,
        suggestions=[],
    )


def _answer_fallback(ctx):
    return Reply(
        "Sorry, I didn't quite understand that. I can help with office hours, locations, insurance, finding a "
        "provider, booking, and the patient portal. You can also send your question to our staff.",
        intent="fallback",
        offer_contact=True,
    )


ANSWERS = {
    "greeting": _answer_greeting,
    "thanks": _answer_thanks,
    "hours": _answer_hours,
    "locations": _answer_locations,
    "insurance": _answer_insurance,
    "billing": _answer_billing,
    "booking": _answer_booking,
    "cancel": _answer_cancel,
    "providers": _answer_providers,
    "services": _answer_services,
    "portal": _answer_portal,
    "refills": _answer_refills,
    "results": _answer_results,
    "records": _answer_records,
    "telehealth": _answer_telehealth,
    "after_hours": _answer_after_hours,
    "medical_advice": _answer_medical_advice,
    "privacy": _answer_privacy,
    "human": _answer_human,
    "fallback": _answer_fallback,
}


def respond(message):
    """Return a Reply for one visitor message."""
    text = normalize(message)
    if not text:
        return _answer_greeting({})
    crisis = _any(text, CRISIS_PATTERNS)
    if crisis or _any(text, EMERGENCY_PATTERNS):
        return _emergency(crisis)
    ctx = {
        "offices": _match_offices(text),
        "plans": _match_plans(text),
        "specialty": _match_specialty(text),
        "language": _match_language(text),
    }
    intent = classify(text, **ctx)
    return ANSWERS[intent](ctx)
