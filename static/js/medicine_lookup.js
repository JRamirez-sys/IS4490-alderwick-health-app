(() => {
  const form = document.getElementById("medicine-lookup-form");
  if (!form) return;

  const input = document.getElementById("medicine-lookup-input");
  const submit = document.getElementById("medicine-lookup-submit");
  const status = document.getElementById("medicine-lookup-status");
  const result = document.getElementById("medicine-lookup-result");
  const csrfToken = form.querySelector("[name=csrfmiddlewaretoken]").value;
  const allergyCheck = document.getElementById("medicine-allergy-check");
  const allergyForm = document.getElementById("medicine-allergy-form");
  const allergySubmit = document.getElementById("medicine-allergy-submit");
  const allergyStatus = document.getElementById("medicine-allergy-status");
  const allergyResults = document.getElementById("medicine-allergy-results");
  let currentMedicine = "";

  function showResult(medicine, answer, sources) {
    const article = document.createElement("article");
    article.className = "assistant-message assistant-message-bot";

    const heading = document.createElement("strong");
    heading.textContent = "General information";
    article.appendChild(heading);

    const body = document.createElement("p");
    body.textContent = answer;
    article.appendChild(body);

    const sourceHeading = document.createElement("strong");
    sourceHeading.textContent = "Sources";
    article.appendChild(sourceHeading);

    const list = document.createElement("ul");
    for (const source of sources) {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = source.title;
      item.appendChild(link);
      list.appendChild(item);
    }
    article.appendChild(list);

    result.replaceChildren(article);
    result.hidden = false;
    if (allergyCheck) {
      currentMedicine = medicine;
      allergyCheck.hidden = false;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const medicine = input.value.trim();
    if (!medicine) return;

    result.replaceChildren();
    result.hidden = true;
    currentMedicine = "";
    if (allergyCheck) {
      allergyCheck.hidden = true;
      allergyResults.replaceChildren();
      allergyStatus.textContent = "";
    }
    submit.disabled = true;
    status.textContent = "Searching trusted sources…";

    try {
      const response = await fetch(form.dataset.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({ medicine }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Medicine lookup could not complete.");

      showResult(medicine, data.answer, data.sources);
      status.textContent = "Check the linked sources before using this information.";
    } catch (error) {
      status.textContent = error.message || "Medicine lookup could not complete. Please try again.";
    } finally {
      submit.disabled = false;
      input.focus();
    }
  });

  if (allergyForm) {
    allergyForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!currentMedicine) return;

      allergyResults.replaceChildren();
      allergySubmit.disabled = true;
      allergyStatus.textContent = "Checking authorized patient records…";

      try {
        const response = await fetch(allergyCheck.dataset.endpoint, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
          },
          body: JSON.stringify({ medicine: currentMedicine }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Allergy lookup could not complete.");

        const notice = document.createElement("p");
        notice.className = "muted";
        notice.textContent = `Exact recorded allergy matches for ${currentMedicine}: ${data.match_count}.`;
        allergyResults.appendChild(notice);

        if (data.matches.length) {
          const list = document.createElement("ul");
          for (const match of data.matches) {
            const item = document.createElement("li");
            const link = document.createElement("a");
            link.href = match.chart_url;
            link.textContent = `${match.patient_name} (${match.mrn})`;
            item.appendChild(link);
            item.appendChild(document.createTextNode(": "));
            item.appendChild(
              document.createTextNode(
                match.allergies.map((allergy) => `${allergy.substance} — ${allergy.severity}`).join(", "),
              ),
            );
            list.appendChild(item);
          }
          allergyResults.appendChild(list);
        } else {
          const empty = document.createElement("p");
          empty.textContent = "No exact recorded allergy matches were found in your authorized patient panel.";
          allergyResults.appendChild(empty);
        }
        allergyStatus.textContent = "Review each chart; this search does not confirm the absence of an allergy.";
      } catch (error) {
        allergyStatus.textContent = error.message || "Allergy lookup could not complete. Please try again.";
      } finally {
        allergySubmit.disabled = false;
      }
    });
  }
})();
