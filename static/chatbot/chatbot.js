/* Alderwick website chatbot widget (rule-based FAQ). */
(function () {
  "use strict";

  var root = document.getElementById("chatbot");
  if (!root) return;

  var launcher = root.querySelector(".chatbot-launcher");
  var panel = root.querySelector(".chatbot-panel");
  var closeButton = root.querySelector(".chatbot-close");
  var log = root.querySelector(".chatbot-log");
  var suggestionsEl = root.querySelector(".chatbot-suggestions");
  var inputForm = root.querySelector(".chatbot-input");
  var input = inputForm.querySelector("input");
  var contactForm = root.querySelector(".chatbot-contact");
  var contactError = contactForm.querySelector(".chatbot-error");
  var csrfToken = contactForm.querySelector("input[name=csrfmiddlewaretoken]").value;
  var started = false;
  var busy = false;
  var lastQuestion = "";

  function scrollToEnd() {
    log.scrollTop = log.scrollHeight;
  }

  function addMessage(who, text, links, urgent) {
    var item = document.createElement("div");
    item.className = "chatbot-msg chatbot-msg-" + who + (urgent ? " is-urgent" : "");
    var body = document.createElement("p");
    body.textContent = text;
    item.appendChild(body);
    if (links && links.length) {
      var list = document.createElement("div");
      list.className = "chatbot-links";
      links.forEach(function (link) {
        if (!link.url || link.url.charAt(0) !== "/") return; // only links to this site
        var a = document.createElement("a");
        a.href = link.url;
        a.textContent = link.label;
        list.appendChild(a);
      });
      item.appendChild(list);
    }
    log.appendChild(item);
    scrollToEnd();
    return item;
  }

  function setSuggestions(items, offerContact) {
    suggestionsEl.textContent = "";
    if (offerContact) {
      var staff = document.createElement("button");
      staff.type = "button";
      staff.className = "chatbot-chip chatbot-chip-primary";
      staff.textContent = "Send my question to staff";
      staff.addEventListener("click", showContactForm);
      suggestionsEl.appendChild(staff);
    }
    (items || []).forEach(function (label) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chatbot-chip";
      chip.textContent = label;
      chip.addEventListener("click", function () { send(label, false, true); });
      suggestionsEl.appendChild(chip);
    });
  }

  function send(text, silent, fromChip) {
    text = (text || "").trim();
    if (!text || busy) return;
    if (!silent) {
      addMessage("user", text);
      if (!fromChip) lastQuestion = text;
    }
    busy = true;
    setSuggestions([]);
    var typing = addMessage("bot", "Typing...");
    typing.classList.add("is-typing");
    fetch(root.dataset.messageUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify({ message: text })
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        typing.remove();
        addMessage("bot", data.reply, data.links, data.urgent);
        setSuggestions(data.suggestions, data.offer_contact);
        scrollToEnd();
        if (data.intent === "human") showContactForm();
      })
      .catch(function () {
        typing.remove();
        addMessage("bot", "Sorry, something went wrong. Please try again, or call us at " + root.dataset.phone + ".");
        setSuggestions([], true);
      })
      .finally(function () {
        busy = false;
      });
  }

  function showContactForm() {
    contactError.textContent = "";
    var message = contactForm.elements.message;
    if (!message.value && lastQuestion) message.value = lastQuestion;
    contactForm.hidden = false;
    inputForm.hidden = true;
    suggestionsEl.hidden = true;
    contactForm.elements.name.focus();
    scrollToEnd();
  }

  function hideContactForm() {
    contactForm.hidden = true;
    inputForm.hidden = false;
    suggestionsEl.hidden = false;
    input.focus();
  }

  function openPanel() {
    panel.hidden = false;
    root.classList.add("is-open");
    launcher.setAttribute("aria-expanded", "true");
    if (!started) {
      started = true;
      send("hello", true);
    }
    input.focus();
  }

  function closePanel() {
    panel.hidden = true;
    root.classList.remove("is-open");
    launcher.setAttribute("aria-expanded", "false");
    launcher.focus();
  }

  launcher.addEventListener("click", function () {
    if (panel.hidden) openPanel(); else closePanel();
  });
  closeButton.addEventListener("click", closePanel);
  root.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) closePanel();
  });

  inputForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = input.value;
    input.value = "";
    send(text);
  });

  contactForm.querySelector("[data-action=cancel-contact]").addEventListener("click", hideContactForm);

  contactForm.addEventListener("submit", function (event) {
    event.preventDefault();
    contactError.textContent = "";
    var data = new FormData(contactForm);
    if (!String(data.get("name")).trim() || !String(data.get("message")).trim()) {
      contactError.textContent = "Please add your name and your question.";
      return;
    }
    if (!String(data.get("email")).trim() && !String(data.get("phone")).trim()) {
      contactError.textContent = "Please give us an email address or phone number so we can reach you.";
      return;
    }
    var submit = contactForm.querySelector("button[type=submit]");
    submit.disabled = true;
    fetch(root.dataset.contactUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken },
      body: data
    })
      .then(function (response) {
        return response.json().then(function (body) { return { ok: response.ok, body: body }; });
      })
      .then(function (result) {
        if (!result.ok) {
          contactError.textContent = result.body.error || "Something went wrong. Please try again.";
          return;
        }
        contactForm.reset();
        hideContactForm();
        addMessage("bot", result.body.reply);
        setSuggestions(["Office hours", "Book an appointment"]);
      })
      .catch(function () {
        contactError.textContent = "Something went wrong. Please call us at " + root.dataset.phone + ".";
      })
      .finally(function () {
        submit.disabled = false;
      });
  });
})();
