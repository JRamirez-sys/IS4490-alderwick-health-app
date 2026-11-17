(() => {
  const result = document.getElementById("daily-operations-brief-result");
  if (!result) return;

  const closeButton = result.querySelector("[data-close-operations-brief]");
  const generateButton = document.querySelector("[data-generate-operations-brief]");
  if (!closeButton || !generateButton) return;

  closeButton.addEventListener("click", () => {
    result.remove();
    generateButton.focus();
  });
})();
