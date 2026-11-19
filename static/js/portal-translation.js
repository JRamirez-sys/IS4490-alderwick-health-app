(() => {
  const form = document.querySelector("form[data-translation-control]");
  const button = document.getElementById("portal-page-translate-button");
  const status = document.getElementById("portal-page-translation-status");
  if (!form || !button || !status) return;

  const excludedElements = "script, style, noscript, textarea, [data-translation-control]";
  const preferenceKey = `alderwick-portal-translation:${form.dataset.patientId}`;
  let originalEntries = null;

  function collectEntries() {
    const entries = [];
    const roots = [document.querySelector("#main"), document.querySelector(".subnav")].filter(Boolean);

    for (const root of roots) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          if (!node.nodeValue.trim() || node.parentElement.closest(excludedElements)) {
            return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      });

      while (walker.nextNode()) {
        const node = walker.currentNode;
        const original = node.nodeValue;
        entries.push({
          original,
          set: (value) => {
            node.nodeValue = value;
          },
        });
      }

      root.querySelectorAll("[placeholder], [title], [aria-label], [alt]").forEach((element) => {
        if (element.closest(excludedElements)) return;
        for (const attribute of ["placeholder", "title", "aria-label", "alt"]) {
          const original = element.getAttribute(attribute);
          if (original && original.trim()) {
            entries.push({
              original,
              set: (value) => {
                element.setAttribute(attribute, value);
              },
            });
          }
        }
      });
    }

    return entries;
  }

  function preserveWhitespace(original, translated) {
    const leading = original.match(/^\s*/)[0];
    const trailing = original.match(/\s*$/)[0];
    return `${leading}${translated.trim()}${trailing}`;
  }

  function isTranslationEnabled() {
    try {
      return window.sessionStorage.getItem(preferenceKey) === "true";
    } catch {
      status.textContent = "This browser cannot remember the translation setting for this session.";
      return false;
    }
  }

  function setTranslationEnabled(enabled) {
    try {
      if (enabled) {
        window.sessionStorage.setItem(preferenceKey, "true");
      } else {
        window.sessionStorage.removeItem(preferenceKey);
      }
      return true;
    } catch {
      status.textContent = "This browser cannot remember the translation setting for this session.";
      return false;
    }
  }

  function restoreOriginal() {
    originalEntries.forEach((entry) => entry.set(entry.original));
    originalEntries = null;
    button.setAttribute("aria-pressed", "false");
  }

  async function translateCurrentPage() {
    const entries = collectEntries();
    if (!entries.length) {
      status.textContent = "There is no page text to translate.";
      return false;
    }

    button.disabled = true;
    status.textContent = "Translating this page...";
    try {
      const csrfToken = form.querySelector("[name=csrfmiddlewaretoken]").value;
      const response = await fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ texts: entries.map((entry) => entry.original) }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Translation failed. Please try again.");
      if (
        !Array.isArray(result.translations)
        || result.translations.length !== entries.length
        || !result.translations.every((translation) => typeof translation === "string" && translation.trim())
      ) {
        throw new Error("The translation response was incomplete. Please try again.");
      }

      entries.forEach((entry, index) => {
        entry.set(preserveWhitespace(entry.original, result.translations[index]));
      });
      originalEntries = entries;
      button.setAttribute("aria-pressed", "true");
      status.textContent = `Translated to ${result.target_language}. Tap “Translate with AI” again to restore the original.`;
      return true;
    } catch (error) {
      status.textContent = error.message || "Translation failed. Please try again.";
      return false;
    } finally {
      button.disabled = false;
    }
  }

  button.addEventListener("click", async () => {
    if (originalEntries) {
      restoreOriginal();
      if (setTranslationEnabled(false)) {
        status.textContent = "Original page restored. Translation is off for the rest of this session.";
      }
      return;
    }

    if (!setTranslationEnabled(true)) return;
    if (!await translateCurrentPage()) setTranslationEnabled(false);
  });

  if (isTranslationEnabled()) {
    button.setAttribute("aria-pressed", "true");
    translateCurrentPage().then((success) => {
      if (!success) {
        setTranslationEnabled(false);
        button.setAttribute("aria-pressed", "false");
      }
    });
  }
})();
