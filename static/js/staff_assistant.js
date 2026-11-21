(() => {
  function initWidget(widget) {
    const form = widget.querySelector("[data-assistant-form]");
    if (!form) return;

    const input = widget.querySelector("[data-assistant-input]");
    const submit = widget.querySelector("[data-assistant-submit]");
    const status = widget.querySelector("[data-assistant-status]");
    const transcript = widget.querySelector("[data-assistant-messages]");
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

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!submit.disabled) form.requestSubmit(submit);
      }
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;

      const selectedMode = "auto";
      const requestMessages = selectedMode === "medicine"
        ? [{ role: "user", content: text }]
        : [...conversation, { role: "user", content: text }];
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
        input.focus();
      }
    });
  }

  document.querySelectorAll("[data-assistant-widget]").forEach(initWidget);

  document.querySelectorAll(".staff-assistant-float").forEach((floatWidget) => {
    const launcher = floatWidget.querySelector(".staff-assistant-launcher");
    const panel = floatWidget.querySelector(".staff-assistant-panel");
    const close = floatWidget.querySelector(".staff-assistant-close");

    launcher?.addEventListener("click", () => {
      const opening = panel.hidden;
      panel.hidden = !opening;
      launcher.setAttribute("aria-expanded", String(opening));
      if (opening) panel.querySelector("[data-assistant-input]")?.focus();
    });

    close?.addEventListener("click", () => {
      panel.hidden = true;
      launcher.setAttribute("aria-expanded", "false");
      launcher.focus();
    });
  });
})();
