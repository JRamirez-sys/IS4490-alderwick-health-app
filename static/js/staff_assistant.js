(() => {
  const form = document.getElementById("assistant-form");
  if (!form) return;

  const input = document.getElementById("assistant-input");
  const mode = document.getElementById("assistant-mode");
  const submit = document.getElementById("assistant-submit");
  const status = document.getElementById("assistant-status");
  const transcript = document.getElementById("assistant-messages");
  const csrfToken = form.querySelector("[name=csrfmiddlewaretoken]").value;
  const conversation = [];

  function addMessage(role, text, sources = []) {
    const message = document.createElement("article");
    message.className = `assistant-message ${role === "user" ? "assistant-message-user" : "assistant-message-bot"}`;

    const label = document.createElement("strong");
    label.textContent = role === "user" ? "You" : "Employee assistant";
    message.appendChild(label);

    const body = document.createElement("p");
    body.textContent = text;
    message.appendChild(body);

    if (sources.length) {
      const sourceLabel = document.createElement("strong");
      sourceLabel.textContent = "Sources";
      message.appendChild(sourceLabel);
      const list = document.createElement("ul");
      for (const source of sources) {
        const item = document.createElement("li");
        const link = document.createElement("a");
        link.href = source.url;
        link.textContent = source.title;
        if (new URL(source.url, window.location.origin).origin !== window.location.origin) {
          link.target = "_blank";
          link.rel = "noopener noreferrer";
        }
        item.appendChild(link);
        list.appendChild(item);
      }
      message.appendChild(list);
    }

    transcript.appendChild(message);
    transcript.scrollTop = transcript.scrollHeight;
  }

  mode.addEventListener("change", () => {
    conversation.length = 0;
    transcript.replaceChildren();
    const welcome = mode.value === "medicine"
      ? "Ask one general question about a medicine at a time. Do not include patient details."
      : mode.value === "draft"
        ? "Describe a de-identified situation and I’ll draft a response for staff review."
        : "Ask about published practice policies or procedures. I’ll cite the supporting documents.";
    addMessage("assistant", welcome);
    input.placeholder = mode.value === "medicine"
      ? "For example, what are common side effects of metformin?"
      : mode.value === "draft"
        ? "Describe the response you need without patient details…"
        : "Ask about a policy or procedure…";
    input.maxLength = mode.value === "medicine" ? 120 : 4000;
    status.textContent = "";
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;

    const selectedMode = mode.value;
    const requestMessages = selectedMode === "medicine"
      ? [{ role: "user", content: text }]
      : [...conversation, { role: "user", content: text }];
    addMessage("user", text);
    input.value = "";
    submit.disabled = true;
    mode.disabled = true;
    status.textContent = "Thinking…";

    try {
      const response = await fetch(form.dataset.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ mode: selectedMode, messages: requestMessages }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "The assistant could not complete that request.");

      if (selectedMode !== "medicine") {
        conversation.push({ role: "user", content: text }, { role: "assistant", content: result.answer });
        while (conversation.length > 8) conversation.splice(0, 2);
      }
      addMessage("assistant", result.answer, result.sources);
      status.textContent = selectedMode === "medicine"
        ? "Medicine questions are handled individually and are not saved."
        : "Conversation is kept only in this page and is not saved.";
    } catch (error) {
      addMessage("assistant", error.message || "The assistant could not complete that request. Please try again.");
      status.textContent = "";
    } finally {
      submit.disabled = false;
      mode.disabled = false;
      input.focus();
    }
  });
})();
