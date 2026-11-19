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
    label.textContent = role === "user" ? "You" : "Policy assistant";
    message.appendChild(label);

    const body = document.createElement("p");
    body.textContent = text;
    message.appendChild(body);

    if (sources.length) {
      const sourceLabel = document.createElement("strong");
      sourceLabel.textContent = "Policies referenced";
      message.appendChild(sourceLabel);
      const list = document.createElement("ul");
      for (const source of sources) {
        const item = document.createElement("li");
        const link = document.createElement("a");
        link.href = source.url;
        link.textContent = source.title;
        item.appendChild(link);
        list.appendChild(item);
      }
      message.appendChild(list);
    }

    transcript.appendChild(message);
    transcript.scrollTop = transcript.scrollHeight;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;

    const requestMessages = [...conversation, { role: "user", content: text }];
    addMessage("user", text);
    input.value = "";
    submit.disabled = true;
    status.textContent = "Thinking…";

    try {
      const response = await fetch(form.dataset.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ mode: mode.value, messages: requestMessages }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "The assistant could not complete that request.");

      conversation.push({ role: "user", content: text }, { role: "assistant", content: result.answer });
      while (conversation.length > 8) conversation.splice(0, 2);
      addMessage("assistant", result.answer, result.sources);
      status.textContent = "Conversation is kept only in this page and is not saved.";
    } catch (error) {
      addMessage("assistant", error.message || "The assistant could not complete that request. Please try again.");
      status.textContent = "";
    } finally {
      submit.disabled = false;
      input.focus();
    }
  });
})();
