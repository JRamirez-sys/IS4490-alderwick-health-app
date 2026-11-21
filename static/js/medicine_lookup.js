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

  function renderAnswer(container, answer) {
    const sections = [];
    const unsectionedLines = [];
    let currentSection = null;
    let foundHeading = false;

    for (const rawLine of answer.split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) continue;

      const heading = line.match(/^(?:#{1,3}\s*)?(?:\*\*)?(Overview|Common side effects|Important warnings)\s*:?(?:\*\*)?\s*(.*)$/i);
      if (heading) {
        foundHeading = true;
        currentSection = {
          title: heading[1].replace(/\b\w/g, (letter) => letter.toUpperCase()),
          lines: [],
        };
        sections.push(currentSection);
        if (heading[2]) currentSection.lines.push(heading[2]);
      } else if (currentSection) {
        currentSection.lines.push(line);
      } else {
        unsectionedLines.push(line);
      }
    }

    if (!foundHeading) {
      const paragraph = document.createElement("p");
      paragraph.className = "medicine-result-copy";
      paragraph.textContent = answer.trim();
      container.appendChild(paragraph);
      return;
    }

    if (unsectionedLines.length) {
      sections.unshift({ title: "Summary", lines: unsectionedLines });
    }

    for (const sectionData of sections) {
      if (!sectionData.lines.length) continue;
      const section = document.createElement("section");
      section.className = "medicine-result-section";
      const heading = document.createElement("h3");
      heading.textContent = sectionData.title;
      section.appendChild(heading);

      let list = null;
      for (const line of sectionData.lines) {
        const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.+)$/);
        if (bullet) {
          if (!list) {
            list = document.createElement("ul");
            section.appendChild(list);
          }
          const item = document.createElement("li");
          item.textContent = bullet[1];
          list.appendChild(item);
        } else {
          list = null;
          const paragraph = document.createElement("p");
          paragraph.textContent = line;
          section.appendChild(paragraph);
        }
      }
      container.appendChild(section);
    }
  }

  function appendOfficialImage(container, image) {
    if (
      !image ||
      typeof image.url !== "string" ||
      typeof image.source_url !== "string" ||
      typeof image.caption !== "string"
    ) {
      return;
    }
    let imageUrl;
    let sourceUrl;
    try {
      imageUrl = new URL(image.url);
      sourceUrl = new URL(image.source_url);
    } catch {
      return;
    }
    if (
      imageUrl.origin !== "https://dailymed.nlm.nih.gov" ||
      imageUrl.pathname !== "/dailymed/image.cfm" ||
      sourceUrl.origin !== "https://dailymed.nlm.nih.gov"
    ) {
      return;
    }

    const figure = document.createElement("figure");
    figure.className = "medicine-result-image";
    const img = document.createElement("img");
    img.src = imageUrl.href;
    img.alt = typeof image.alt === "string" ? image.alt : "Image from the official medicine label";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    figure.appendChild(img);

    const caption = document.createElement("figcaption");
    const link = document.createElement("a");
    link.href = sourceUrl.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = `Official label image: ${image.caption}`;
    caption.appendChild(link);
    const note = document.createElement("span");
    note.textContent = "Not for pill identification; product details may vary.";
    caption.appendChild(note);
    figure.appendChild(caption);
    container.appendChild(figure);
  }

  function showResult(medicine, answer, sources, image) {
    const article = document.createElement("article");
    article.className = "medicine-result-card";

    const header = document.createElement("header");
    header.className = "medicine-result-header";
    const label = document.createElement("span");
    label.textContent = "General medicine information";
    const heading = document.createElement("h2");
    heading.textContent = medicine;
    header.append(label, heading);
    article.appendChild(header);

    const content = document.createElement("div");
    content.className = image ? "medicine-result-content has-image" : "medicine-result-content";
    const information = document.createElement("div");
    information.className = "medicine-result-information";
    renderAnswer(information, answer);
    content.appendChild(information);
    appendOfficialImage(content, image);
    article.appendChild(content);

    const sourceHeading = document.createElement("h3");
    sourceHeading.className = "medicine-result-sources-heading";
    sourceHeading.textContent = "Trusted sources";
    article.appendChild(sourceHeading);

    const list = document.createElement("ul");
    list.className = "medicine-result-sources";
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

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      if (!submit.disabled) form.requestSubmit(submit);
    }
  });

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

      showResult(medicine, data.answer, data.sources, data.image);
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
