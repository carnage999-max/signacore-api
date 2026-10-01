(function signacoreSignerPortal() {
  const app = document.querySelector(".signacore-app");
  if (!app) return;

  const state = {
    context: null,
    sessionToken: "",
    values: {},
    fieldErrors: {},
    activeFieldId: "",
    sheetFieldId: null,
    signatureMode: "draw",
    typedSignature: "",
    resendTimerId: 0,
    submitted: false,
    currentPage: 1,
  };

  const pageCardNodes = new Map();
  const fieldInputNodes = new Map();
  const THUMBNAIL_WIDTH = 150;

  const nodes = {
    notice: document.getElementById("notice"),
    statusBadge: document.getElementById("status-badge"),
    signerName: document.getElementById("signer-name"),
    expiresAt: document.getElementById("expires-at"),
    documentTitle: document.getElementById("document-title"),
    sendOtpButton: document.getElementById("send-otp-button"),
    otpTarget: document.getElementById("otp-target"),
    otpInput: document.getElementById("otp-input"),
    verifyOtpButton: document.getElementById("verify-otp-button"),
    verifyAccessPanel: document.getElementById("verify-access-panel"),
    otpEntryPanel: document.getElementById("otp-entry-panel"),
    fieldProgressPanel: document.getElementById("field-progress-panel"),
    fieldList: document.getElementById("field-list"),
    submitButton: document.getElementById("submit-button"),
    pagesRoot: document.getElementById("pages-root"),
    highlightFieldsToggle: document.getElementById("highlight-fields-toggle"),
    documentMeta: document.getElementById("document-meta"),
    pageThumbs: document.getElementById("page-thumbs"),
    pageCounter: document.getElementById("page-counter"),
    pageFirst: document.getElementById("page-first"),
    pagePrev: document.getElementById("page-prev"),
    pageNext: document.getElementById("page-next"),
    pageLast: document.getElementById("page-last"),
    nextRequiredButton: document.getElementById("next-required-button"),
    progressSummary: document.getElementById("progress-summary"),
    submitModal: document.getElementById("submit-modal"),
    submitModalCopy: document.getElementById("submit-modal-copy"),
    submitModalList: document.getElementById("submit-modal-list"),
    submitModalTitle: document.getElementById("submit-modal-title"),
    closeSubmitModalButton: document.getElementById("close-submit-modal-button"),
    modalDownloadButton: document.getElementById("modal-download-button"),
    cancelSubmitButton: document.getElementById("cancel-submit-button"),
    confirmSubmitButton: document.getElementById("confirm-submit-button"),
    documentPanel: document.getElementById("document-panel"),
    fieldRail: document.getElementById("field-rail"),
    signedPanel: document.getElementById("signed-panel"),
    signedCopyCopy: document.getElementById("signed-copy-copy"),
    downloadSignedButton: document.getElementById("download-signed-button"),
    fieldSheet: document.getElementById("field-sheet"),
    fieldSheetTitle: document.getElementById("field-sheet-title"),
    fieldSheetHint: document.getElementById("field-sheet-hint"),
    fieldSheetLabel: document.getElementById("field-sheet-label"),
    fieldSheetInput: document.getElementById("field-sheet-input"),
    fieldSheetTextarea: document.getElementById("field-sheet-textarea"),
    closeFieldSheetButton: document.getElementById("close-field-sheet-button"),
    cancelFieldSheetButton: document.getElementById("cancel-field-sheet-button"),
    saveFieldSheetButton: document.getElementById("save-field-sheet-button"),
    signatureModal: document.getElementById("signature-modal"),
    closeModalButton: document.getElementById("close-modal-button"),
    drawModeButton: document.getElementById("draw-mode-button"),
    typeModeButton: document.getElementById("type-mode-button"),
    drawPane: document.getElementById("draw-pane"),
    typePane: document.getElementById("type-pane"),
    signatureCanvas: document.getElementById("signature-canvas"),
    typedSignatureInput: document.getElementById("typed-signature-input"),
    typedPreview: document.getElementById("typed-preview"),
    reuseSignatureRow: document.getElementById("reuse-signature-row"),
    reuseSignatureCheckbox: document.getElementById("reuse-signature-checkbox"),
    reuseSignatureLabel: document.getElementById("reuse-signature-label"),
    clearSignatureButton: document.getElementById("clear-signature-button"),
    saveSignatureButton: document.getElementById("save-signature-button"),
  };

  const canvasContext = nodes.signatureCanvas.getContext("2d");
  let isDrawing = false;
  let isSavingSignature = false;
  let isSubmitting = false;

  function formatDate(value) {
    if (!value) return "Not set";
    return new Intl.DateTimeFormat("en-US", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(value));
  }

  function setNotice(message, tone) {
    if (!message) {
      nodes.notice.hidden = true;
      nodes.notice.className = "notice";
      nodes.notice.textContent = "";
      return;
    }

    nodes.notice.hidden = false;
    nodes.notice.className = `notice ${tone === "success" ? "notice-success" : "notice-error"}`;
    nodes.notice.textContent = message;
  }

  function startVerificationCooldown(seconds) {
    window.clearInterval(state.resendTimerId);
    let remaining = Math.max(0, Number(seconds) || 60);

    function renderCooldown() {
      if (remaining <= 0) {
        window.clearInterval(state.resendTimerId);
        state.resendTimerId = 0;
        nodes.sendOtpButton.disabled = false;
        nodes.sendOtpButton.textContent = "Send verification code";
        return;
      }

      nodes.sendOtpButton.disabled = true;
      nodes.sendOtpButton.textContent = `Send again in ${remaining}s`;
      remaining -= 1;
    }

    renderCooldown();
    state.resendTimerId = window.setInterval(renderCooldown, 1000);
  }

  async function request(url, init) {
    const response = await fetch(url, init);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const message =
        typeof payload === "string"
          ? payload
          : payload.detail || payload.otp?.[0] || payload.session_token?.[0] || "Request failed.";
      if (response.status === 429 && typeof payload !== "string" && payload.retry_after) {
        startVerificationCooldown(payload.retry_after);
      }
      throw new Error(message);
    }
    return payload;
  }

  function getFieldValue(fieldId) {
    return state.values[fieldId] || null;
  }

  function fieldIsComplete(field) {
    const value = getFieldValue(field.id);
    if (!value) return false;
    if (field.field_type === "TEXT" || field.field_type === "MULTILINE") {
      return Boolean(value.textValue && value.textValue.trim());
    }
    if (field.field_type === "DROPDOWN") {
      return Boolean(value.textValue && value.textValue.trim());
    }
    if (field.field_type === "CHECKBOX" || field.field_type === "RADIO") {
      if (field.is_required) return Boolean(value.checked);
      return typeof value.checked === "boolean";
    }
    return Boolean(value.imageBlob || value.imageUrl);
  }

  function updateSubmitState() {
    if (state.submitted || !state.context || !state.context.is_verified || state.context.access_message) {
      nodes.submitButton.disabled = true;
      return;
    }

    // The button stays enabled so pressing it can explain what is still missing, rather than
    // leaving a signer with a dead control and no reason for it.
    nodes.submitButton.disabled = false;
    updateProgressSummary();
  }

  function outstandingRequiredFields() {
    if (!state.context) return [];
    return state.context.fields
      .filter((field) => field.is_required && !fieldIsComplete(field))
      .sort((first, second) => first.page - second.page || first.order - second.order);
  }

  function updateProgressSummary() {
    if (!nodes.progressSummary || !state.context) return;
    const total = state.context.fields.filter((field) => field.is_required).length;
    const outstanding = outstandingRequiredFields().length;
    if (!total) {
      nodes.progressSummary.textContent = `${state.context.fields.length} field${state.context.fields.length === 1 ? "" : "s"} to review`;
      return;
    }
    nodes.progressSummary.textContent =
      outstanding === 0
        ? `All ${total} required field${total === 1 ? "" : "s"} complete`
        : `${total - outstanding} of ${total} required fields complete`;
  }

  function openSubmitModal() {
    const outstanding = outstandingRequiredFields();
    nodes.submitModalList.innerHTML = "";
    setModalEyebrow("Before you submit");
    nodes.modalDownloadButton.hidden = true;
    nodes.cancelSubmitButton.hidden = false;
    nodes.closeSubmitModalButton.hidden = false;

    if (outstanding.length) {
      nodes.submitModalTitle.textContent = "Some required fields are empty";
      nodes.submitModalCopy.textContent =
        `You still have ${outstanding.length} required field${outstanding.length === 1 ? "" : "s"} to complete. ` +
        "Choose one to jump to it.";
      outstanding.forEach((field) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "submit-issue";
        item.innerHTML = `<strong></strong><span></span>`;
        item.querySelector("strong").textContent = field.label;
        item.querySelector("span").textContent = `${field.field_type} · page ${field.page}`;
        item.addEventListener("click", () => {
          closeSubmitModal();
          focusField(field);
        });
        nodes.submitModalList.appendChild(item);
      });
      nodes.confirmSubmitButton.hidden = true;
      nodes.cancelSubmitButton.textContent = "Keep editing";
    } else {
      const completed = state.context.fields.filter((field) => fieldIsComplete(field)).length;
      nodes.submitModalTitle.textContent = "Submit this document?";
      nodes.submitModalCopy.textContent =
        `You are about to submit ${completed} completed field${completed === 1 ? "" : "s"}. ` +
        "Once submitted you cannot change your answers.";
      nodes.confirmSubmitButton.hidden = false;
      nodes.cancelSubmitButton.textContent = "Cancel";
    }

    nodes.submitModal.hidden = false;
  }

  function closeSubmitModal() {
    nodes.submitModal.hidden = true;
  }

  function setModalEyebrow(text) {
    const eyebrow = nodes.submitModal?.querySelector(".modal-header .eyebrow");
    if (eyebrow) eyebrow.textContent = text;
  }

  /** Hold the dialog open through the request, so pressing Submit visibly does something.
   *
   * Confirming used to close the dialog and write the outcome to the notice at the top of the
   * page. Submitting happens from the bottom, where the button is, so the one thing that said
   * whether a signature had been accepted was off screen: the page locked and nothing announced
   * it. The dialog the press came from is where the answer belongs.
   */
  function showSubmitProgress() {
    setModalEyebrow("Submitting");
    nodes.submitModalTitle.textContent = "Submitting your signature…";
    nodes.submitModalCopy.textContent = "This takes a moment. Please keep this page open.";
    nodes.submitModalList.innerHTML = "";
    nodes.confirmSubmitButton.disabled = true;
    nodes.confirmSubmitButton.textContent = "Submitting…";
    nodes.cancelSubmitButton.hidden = true;
    nodes.closeSubmitModalButton.hidden = true;
    nodes.modalDownloadButton.hidden = true;
    nodes.submitModal.hidden = false;
  }

  function showSubmitOutcome({ title, copy, tone }) {
    setModalEyebrow(tone === "success" ? "Signed" : "Not submitted");
    nodes.submitModalTitle.textContent = title;
    nodes.submitModalCopy.textContent = copy;
    nodes.submitModalList.innerHTML = "";
    nodes.confirmSubmitButton.hidden = true;
    nodes.confirmSubmitButton.disabled = false;
    nodes.confirmSubmitButton.textContent = "Submit document";
    nodes.cancelSubmitButton.hidden = false;
    nodes.cancelSubmitButton.textContent = "Close";
    nodes.closeSubmitModalButton.hidden = false;
    nodes.modalDownloadButton.hidden = !(tone === "success" && state.context?.signed_copy_ready);
    nodes.submitModal.hidden = false;
  }

  function focusField(field) {
    const input = fieldInputNodes.get(field.id);
    if (!input) {
      goToPage(field.page);
      return;
    }
    input.scrollIntoView({ behavior: "smooth", block: "center" });
    setCurrentPage(field.page);
    window.setTimeout(() => input.focus({ preventScroll: true }), 320);
  }

  function renderFieldList() {
    if (!state.context) return;
    nodes.fieldList.innerHTML = "";

    state.context.fields.forEach((field) => {
      const item = document.createElement("div");
      const complete = fieldIsComplete(field);
      item.className = "field-list-item";
      const label = document.createElement("strong");
      label.textContent = field.label;
      const fieldStatus = document.createElement("div");
      fieldStatus.className = `field-status ${complete ? "field-status-complete" : ""}`;
      fieldStatus.textContent = `${field.field_type} · page ${field.page} · ${complete ? "Completed" : field.is_required ? "Required" : "Optional"}`;
      item.append(label, fieldStatus);
      nodes.fieldList.appendChild(item);
    });
    renderFieldRail();
    updateDocumentMeta();
  }

  /**
   * The narrow-screen counterpart of the field list.
   *
   * The list itself scrolls, and on a phone it fills the screen, so reaching it stops the page
   * scrolling and traps the reader in it. The rail carries the same information in the margin: one
   * mark per field, numbered until it is filled and ticked afterwards, tapped to jump to it.
   */
  function renderFieldRail() {
    if (!nodes.fieldRail || !state.context) return;

    const fields = state.context.fields;
    nodes.fieldRail.hidden = state.submitted || fields.length === 0;
    nodes.fieldRail.innerHTML = "";

    fields.forEach((field, index) => {
      const complete = fieldIsComplete(field);
      const mark = document.createElement("button");
      mark.type = "button";
      mark.className = `rail-mark ${complete ? "rail-mark-complete" : ""} ${
        field.is_required && !complete ? "rail-mark-required" : ""
      }`;
      mark.textContent = complete ? "\u2713" : String(index + 1);
      mark.title = field.label;
      mark.setAttribute(
        "aria-label",
        `${field.label}, page ${field.page}, ${complete ? "completed" : field.is_required ? "required" : "optional"}`,
      );
      mark.addEventListener("click", () => focusField(field));
      nodes.fieldRail.appendChild(mark);
    });
  }

  function markFieldFilled(node, isFilled) {
    const overlay = node.closest(".field-overlay");
    if (overlay) {
      overlay.classList.toggle("field-filled", Boolean(isFilled));
    }
  }

  // A field is drawn at the size of the box printed on the page, which on a form is a few
  // millimetres. Focusing an input smaller than 16px makes iOS Safari zoom the page to meet it,
  // and the reader is left zoomed in with the document half off screen. Below this width the tap
  // opens a sheet instead, where the field is legible and nothing is zoomed.
  const narrowViewport = window.matchMedia("(max-width: 900px)");

  function usesFieldSheet(field) {
    return narrowViewport.matches && (field.field_type === "TEXT" || field.field_type === "MULTILINE");
  }

  function handOffToFieldSheet(field, control) {
    control.addEventListener("pointerdown", (event) => {
      if (!usesFieldSheet(field)) return;
      // Taking the tap before it lands is what stops the field being focused, and so stops the
      // zoom; blurring after the fact is already too late.
      event.preventDefault();
      control.blur();
      openFieldSheet(field.id);
    });
  }

  function openFieldSheet(fieldId) {
    const field = state.context?.fields.find((entry) => entry.id === fieldId);
    if (!field) return;
    state.sheetFieldId = fieldId;

    const multiline = field.field_type === "MULTILINE";
    const control = multiline ? nodes.fieldSheetTextarea : nodes.fieldSheetInput;
    nodes.fieldSheetInput.hidden = multiline;
    nodes.fieldSheetTextarea.hidden = !multiline;
    nodes.fieldSheetInput.parentElement.hidden = multiline;

    nodes.fieldSheetTitle.textContent = field.label;
    nodes.fieldSheetLabel.textContent = field.label;
    nodes.fieldSheetHint.textContent = field.is_required
      ? "This field is required."
      : "You can leave this blank.";
    control.value = getFieldValue(fieldId)?.textValue || "";
    if (field.max_length) {
      control.maxLength = field.max_length;
    } else {
      control.removeAttribute("maxlength");
    }

    nodes.fieldSheet.hidden = false;
    window.setTimeout(() => control.focus(), 60);
  }

  function closeFieldSheet() {
    nodes.fieldSheet.hidden = true;
    state.sheetFieldId = null;
  }

  function saveFieldSheet() {
    const fieldId = state.sheetFieldId;
    const field = state.context?.fields.find((entry) => entry.id === fieldId);
    if (!field) return closeFieldSheet();

    const control = field.field_type === "MULTILINE" ? nodes.fieldSheetTextarea : nodes.fieldSheetInput;
    const value = control.value;
    state.values[fieldId] = { type: "TEXT", textValue: value };
    delete state.fieldErrors[fieldId];

    const inline = fieldInputNodes.get(fieldId);
    if (inline) {
      inline.value = value;
      markFieldFilled(inline, value.trim());
    }
    closeFieldSheet();
    renderFieldList();
    updateSubmitState();
  }

  function buildTextField(field) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "text-field-input";
    input.placeholder = field.label;
    input.value = getFieldValue(field.id)?.textValue || "";
    if (field.max_length) {
      input.maxLength = field.max_length;
    }
    if (field.is_comb && field.max_length) {
      // One character per printed cell: centre each glyph on the cell pitch.
      input.classList.add("comb-field-input");
      input.style.setProperty("--comb-cells", String(field.max_length));
    }
    handOffToFieldSheet(field, input);
    input.addEventListener("input", () => {
      state.values[field.id] = {
        type: "TEXT",
        textValue: input.value,
      };
      delete state.fieldErrors[field.id];
      markFieldFilled(input, input.value.trim());
      renderFieldList();
      updateSubmitState();
    });
    return input;
  }

  function buildMultilineField(field) {
    const textarea = document.createElement("textarea");
    textarea.className = "text-field-input multiline-field-input";
    textarea.placeholder = field.label;
    textarea.value = getFieldValue(field.id)?.textValue || "";
    handOffToFieldSheet(field, textarea);
    textarea.addEventListener("input", () => {
      state.values[field.id] = {
        type: "TEXT",
        textValue: textarea.value,
      };
      delete state.fieldErrors[field.id];
      markFieldFilled(textarea, textarea.value.trim());
      renderFieldList();
      updateSubmitState();
    });
    return textarea;
  }

  function buildCheckboxField(field) {
    const checked = Boolean(getFieldValue(field.id)?.checked);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "checkbox-input";
    button.setAttribute("role", "checkbox");
    button.setAttribute("aria-checked", checked ? "true" : "false");
    button.setAttribute("aria-label", field.label);
    button.innerHTML =
      '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
      '<path d="M2.5 8.5 L6.2 12.2 L13.5 3.8" />' +
      "</svg>";
    button.addEventListener("click", () => {
      const next = button.getAttribute("aria-checked") !== "true";
      button.setAttribute("aria-checked", next ? "true" : "false");
      state.values[field.id] = {
        type: "CHECKBOX",
        checked: next,
      };
      delete state.fieldErrors[field.id];
      markFieldFilled(button, next);
      renderFieldList();
      updateSubmitState();
    });
    return button;
  }

  function showSignedState() {
    if (!nodes.signedPanel) return;
    const ready = Boolean(state.context?.signed_copy_ready);
    nodes.signedPanel.hidden = false;
    nodes.signedCopyCopy.textContent = ready
      ? "Your signature is recorded. Keep a copy for your records."
      : "Your signature is recorded. The completed copy is being prepared and will be emailed to you.";
    nodes.downloadSignedButton.hidden = !ready;
    if (nodes.fieldProgressPanel) {
      nodes.fieldProgressPanel.hidden = true;
    }
    if (nodes.fieldRail) {
      nodes.fieldRail.hidden = true;
    }
    lockDocument();
  }

  /** Fetch the completed copy and check it is one before handing it to the browser.
   *
   * This was an anchor carrying the download attribute, pointed straight at the API. When the
   * endpoint answered with anything other than the file - the session had lapsed, or the copy was
   * not packaged yet - it answered a navigation, so it rendered the error as a web page, and the
   * download attribute saved that page to disk. The signer got an HTML file named like their
   * agreement and nothing telling them otherwise. Reading the response first means an error can
   * be shown as an error and only a PDF is ever saved.
   */
  async function downloadSignedCopy(button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Preparing…";
    try {
      const response = await fetch(`/api/sign/${app.dataset.signingToken}/signed/`, {
        credentials: "same-origin",
        // Both, deliberately. DRF negotiates before the handler runs, so asking only for a PDF
        // fails with 406 even when the file is there; the JSON is what an error comes back as.
        headers: { Accept: "application/pdf, application/json" },
      });
      if (!response.ok) {
        throw new Error(await downloadErrorMessage(response));
      }
      const blob = await response.blob();
      if (!blob.type.includes("application/pdf")) {
        throw new Error("The completed copy is not ready yet. Please try again shortly.");
      }
      saveBlob(blob, filenameFromResponse(response));
      setNotice("Your signed copy has been downloaded.", "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : "The copy could not be downloaded.";
      showSubmitOutcome({ title: "Could not download your copy", copy: message, tone: "error" });
      setNotice(message, "error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function downloadErrorMessage(response) {
    if (response.status === 403) {
      return "Your verified session has expired. Reload this page and verify your email to download the copy.";
    }
    if (response.status === 404) {
      return "The completed copy is not ready yet. It will be emailed to you as soon as it is.";
    }
    // Anything else may carry a reason worth repeating, but only if it came back as data. An
    // error page is not a message to a person.
    if ((response.headers.get("content-type") || "").includes("application/json")) {
      const payload = await response.json().catch(() => null);
      if (payload?.detail) return payload.detail;
    }
    return "The copy could not be downloaded. Please try again shortly.";
  }

  function filenameFromResponse(response) {
    const disposition = response.headers.get("content-disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(disposition);
    return match ? match[1] : "signed-document.pdf";
  }

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Revoked on the next frame; revoking immediately can cancel the save in some browsers.
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function lockDocument() {
    // Every control is disabled rather than removed, so the page still reads as the document that
    // was signed rather than an empty form.
    nodes.pagesRoot?.querySelectorAll("input, textarea, select, button").forEach((control) => {
      control.disabled = true;
    });
    nodes.pagesRoot?.classList.add("pages-root-signed");
  }

  function buildDropdownField(field) {
    const select = document.createElement("select");
    select.className = "dropdown-input";
    select.setAttribute("aria-label", field.label);

    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "";
    select.appendChild(blank);
    (field.options || []).forEach((option) => {
      const node = document.createElement("option");
      node.value = option;
      node.textContent = option;
      select.appendChild(node);
    });
    select.value = getFieldValue(field.id)?.textValue || "";

    select.addEventListener("change", () => {
      state.values[field.id] = { type: "TEXT", textValue: select.value };
      delete state.fieldErrors[field.id];
      markFieldFilled(select, Boolean(select.value));
      renderFieldList();
      updateSubmitState();
    });
    return select;
  }

  function buildRadioField(field) {
    const input = document.createElement("input");
    input.type = "radio";
    input.className = "radio-input";
    // Sharing the group name is what makes the browser clear the other options for us, which is
    // the behaviour the form's own script or radio group expressed.
    input.name = `group-${field.group_key}`;
    input.checked = Boolean(getFieldValue(field.id)?.checked);
    input.setAttribute("aria-label", field.option_value || field.label);

    input.addEventListener("change", () => {
      groupMembers(field).forEach((member) => {
        state.values[member.id] = { type: "CHECKBOX", checked: member.id === field.id };
        delete state.fieldErrors[member.id];
        const node = fieldInputNodes.get(member.id);
        if (node) markFieldFilled(node, member.id === field.id);
      });
      renderFieldList();
      updateSubmitState();
    });
    return input;
  }

  function groupMembers(field) {
    if (!state.context || !field.group_key) return [field];
    return state.context.fields.filter(
      (candidate) => candidate.field_type === "RADIO" && candidate.group_key === field.group_key,
    );
  }

  function getReusableSignatureTargets(activeField) {
    if (!state.context || !activeField) return [];
    return state.context.fields.filter(
      (field) =>
        field.id !== activeField.id &&
        field.field_type === activeField.field_type &&
        (field.field_type === "SIGNATURE" || field.field_type === "INITIALS") &&
        !fieldIsComplete(field),
    );
  }

  function buildSignatureValue(field, blob, imageUrl) {
    return {
      type: field.field_type === "INITIALS" ? "INITIALS_PNG" : "SIGNATURE_PNG",
      imageBlob: blob,
      imageUrl,
      typedText: state.signatureMode === "type" ? state.typedSignature : "",
    };
  }

  function openSignatureModal(fieldId) {
    state.activeFieldId = fieldId;
    nodes.signatureModal.dataset.activeFieldId = fieldId;
    const field = state.context.fields.find((entry) => entry.id === fieldId);
    const currentValue = getFieldValue(fieldId);
    state.typedSignature = currentValue?.typedText || "";
    nodes.typedSignatureInput.value = state.typedSignature;
    nodes.typedPreview.textContent = state.typedSignature || (field?.field_type === "INITIALS" ? "Type initials" : "Type signature");
    clearCanvas();
    const reusableTargets = getReusableSignatureTargets(field);
    nodes.reuseSignatureCheckbox.checked = false;
    nodes.reuseSignatureRow.hidden = reusableTargets.length === 0;
    nodes.reuseSignatureLabel.textContent =
      field?.field_type === "INITIALS"
        ? `Use these initials for ${reusableTargets.length} remaining initials field${reusableTargets.length === 1 ? "" : "s"}`
        : `Use this signature for ${reusableTargets.length} remaining signature field${reusableTargets.length === 1 ? "" : "s"}`;
    nodes.signatureModal.hidden = false;
  }

  function closeSignatureModal() {
    nodes.signatureModal.hidden = true;
    delete nodes.signatureModal.dataset.activeFieldId;
    state.activeFieldId = "";
  }

  function buildSignatureField(field) {
    const value = getFieldValue(field.id);
    if (value?.imageUrl) {
      const image = document.createElement("img");
      image.src = value.imageUrl;
      image.alt = field.label;
      image.className = "signature-preview-image";
      image.addEventListener("click", () => openSignatureModal(field.id));
      return image;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "signature-field-button";
    button.textContent = field.field_type === "INITIALS" ? "Add initials" : "Add signature";
    button.addEventListener("click", () => openSignatureModal(field.id));
    return button;
  }

  function renderPages() {
    if (!state.context) return;
    // Emptying the list collapses the document to nothing, and the browser clamps the scroll
    // position to the height that is left. Saving a signature rebuilds the pages, so without
    // this the reader is thrown back up the document every time they sign.
    const previousScrollY = window.scrollY;
    nodes.pagesRoot.innerHTML = "";
    pageCardNodes.clear();
    fieldInputNodes.clear();

    state.context.pages.forEach((pageData) => {
      const pageCard = document.createElement("article");
      pageCard.className = "page-card";

      const pageImage = document.createElement("img");
      pageImage.className = "page-image";
      pageImage.alt = `${state.context.document_title} page ${pageData.number}`;
      // The page's own proportions, so the card reserves its full height before the image
      // arrives. Without them every page is zero-high until it loads, and a long document
      // re-flows under the reader on each one, which reads as scrolling that will not settle.
      pageImage.width = Math.round(pageData.width);
      pageImage.height = Math.round(pageData.height);
      // A 31-page packet is ~230 MiB of bitmap once decoded, which a phone cannot hold at once.
      pageImage.loading = "lazy";
      pageImage.decoding = "async";
      pageImage.src = pageData.preview_url;

      const overlay = document.createElement("div");
      overlay.className = "page-overlay";

      state.context.fields
        .filter((field) => field.page === pageData.number)
        .forEach((field) => {
          const fieldNode = document.createElement("div");
          const top = ((pageData.height - field.y - field.height) / pageData.height) * 100;
          const left = (field.x / pageData.width) * 100;
          const width = (field.width / pageData.width) * 100;
          const height = (field.height / pageData.height) * 100;
          const isTextual = field.field_type === "TEXT" || field.field_type === "MULTILINE";
          const typeClassName = isTextual
            ? "field-overlay-text"
            : field.field_type === "DROPDOWN"
              ? "field-overlay-text"
              : field.field_type === "RADIO"
                ? "field-overlay-checkbox"
                : field.field_type === "CHECKBOX"
              ? "field-overlay-checkbox"
              : "field-overlay-signature";
          const isTickBox = field.field_type === "CHECKBOX" || field.field_type === "RADIO";
          const minHeightPercent = isTextual || field.field_type === "DROPDOWN" ? 1.15 : isTickBox ? 1.2 : 1.8;

          fieldNode.className = `field-overlay ${typeClassName} ${field.is_required ? "field-overlay-required" : ""} ${
            state.fieldErrors[field.id] ? "field-error" : ""
          } ${fieldIsComplete(field) ? "field-filled" : ""}`;
          fieldNode.style.top = `${top}%`;
          fieldNode.style.left = `${left}%`;
          fieldNode.style.width = `${width}%`;
          fieldNode.style.height = `${Math.max(height, minHeightPercent)}%`;
          fieldNode.style.setProperty("--field-font-size", `${Math.max(7, Math.min(field.height * 0.72, 13))}px`);
          fieldNode.style.setProperty("--field-button-size", `${Math.max(7, Math.min(field.height * 0.38, 11))}px`);
          fieldNode.title = field.label;

          let fieldContent;
          if (field.field_type === "TEXT") {
            fieldContent = buildTextField(field);
          } else if (field.field_type === "MULTILINE") {
            fieldContent = buildMultilineField(field);
          } else if (field.field_type === "DROPDOWN") {
            fieldContent = buildDropdownField(field);
          } else if (field.field_type === "RADIO") {
            fieldContent = buildRadioField(field);
          } else if (field.field_type === "CHECKBOX") {
            fieldContent = buildCheckboxField(field);
          } else {
            fieldContent = buildSignatureField(field);
          }
          fieldInputNodes.set(field.id, fieldContent);
          fieldNode.appendChild(fieldContent);
          overlay.appendChild(fieldNode);
        });

      pageCard.appendChild(pageImage);
      pageCard.appendChild(overlay);
      pageCardNodes.set(pageData.number, pageCard);
      nodes.pagesRoot.appendChild(pageCard);
    });
    applyFieldHighlighting();
    renderPageRail();
    observePageVisibility();
    if (previousScrollY > 0 && window.scrollY !== previousScrollY) {
      window.scrollTo({ top: previousScrollY });
    }
  }

  function renderPageRail() {
    if (!nodes.pageThumbs || !state.context) return;
    nodes.pageThumbs.innerHTML = "";

    state.context.pages.forEach((pageData) => {
      const thumb = document.createElement("button");
      thumb.type = "button";
      thumb.className = "page-thumb";
      thumb.dataset.page = String(pageData.number);
      thumb.setAttribute("aria-label", `Go to page ${pageData.number}`);

      const image = document.createElement("img");
      image.loading = "lazy";
      image.alt = "";
      image.src = `${pageData.preview_url}?width=${THUMBNAIL_WIDTH}`;

      const badge = document.createElement("span");
      badge.className = "page-thumb-number";
      badge.textContent = String(pageData.number);

      thumb.append(badge, image);
      thumb.addEventListener("click", () => goToPage(pageData.number));
      nodes.pageThumbs.appendChild(thumb);
    });

    setCurrentPage(1, { scrollRail: false });
  }

  function setCurrentPage(pageNumber, options) {
    const total = state.context?.pages.length || 0;
    if (!total) return;
    const bounded = Math.min(Math.max(pageNumber, 1), total);
    state.currentPage = bounded;

    if (nodes.pageCounter) {
      nodes.pageCounter.textContent = `Page ${bounded} of ${total}`;
    }
    if (nodes.pageFirst) nodes.pageFirst.disabled = bounded <= 1;
    if (nodes.pagePrev) nodes.pagePrev.disabled = bounded <= 1;
    if (nodes.pageNext) nodes.pageNext.disabled = bounded >= total;
    if (nodes.pageLast) nodes.pageLast.disabled = bounded >= total;

    nodes.pageThumbs?.querySelectorAll(".page-thumb").forEach((thumb) => {
      const isCurrent = Number(thumb.dataset.page) === bounded;
      thumb.classList.toggle("page-thumb-current", isCurrent);
      if (isCurrent && options?.scrollRail !== false) {
        revealThumbInRail(thumb);
      }
    });
  }

  function revealThumbInRail(thumb) {
    const rail = nodes.pageThumbs;
    if (!rail) return;
    // Only the rail moves. scrollIntoView would scroll every ancestor that can scroll, the
    // window included, and the rail sits above the pages once the layout is a single column -
    // so following the page being read would drag the reader back up to the rail, every time
    // the page changed. Whichever axis the rail scrolls on, the other clamps to no movement.
    const railBox = rail.getBoundingClientRect();
    const thumbBox = thumb.getBoundingClientRect();
    rail.scrollLeft += thumbBox.left - railBox.left - (railBox.width - thumbBox.width) / 2;
    rail.scrollTop += thumbBox.top - railBox.top - (railBox.height - thumbBox.height) / 2;
  }

  function goToPage(pageNumber) {
    const card = pageCardNodes.get(pageNumber);
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    setCurrentPage(pageNumber);
  }

  function observePageVisibility() {
    if (typeof IntersectionObserver === "undefined") return;
    if (state.pageObserver) state.pageObserver.disconnect();

    state.pageObserver = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((first, second) => second.intersectionRatio - first.intersectionRatio)[0];
        if (visible) {
          setCurrentPage(Number(visible.target.dataset.page));
        }
      },
      { threshold: [0.25, 0.5, 0.75] },
    );
    pageCardNodes.forEach((card, pageNumber) => {
      card.dataset.page = String(pageNumber);
      state.pageObserver.observe(card);
    });
  }

  function focusNextRequiredField() {
    if (!state.context) return;
    const outstanding = state.context.fields
      .filter((field) => field.is_required && !fieldIsComplete(field))
      .sort((first, second) => first.page - second.page || first.order - second.order);
    const target = outstanding.find((field) => field.page >= state.currentPage) || outstanding[0];
    if (target) focusField(target);
  }

  function updateDocumentMeta() {
    if (!nodes.documentMeta || !state.context) return;
    const pageCount = state.context.pages.length;
    const requiredFields = state.context.fields.filter((field) => field.is_required);
    const outstanding = requiredFields.filter((field) => !fieldIsComplete(field)).length;
    const pageLabel = `${pageCount} page${pageCount === 1 ? "" : "s"}`;
    if (!requiredFields.length) {
      nodes.documentMeta.textContent = `${pageLabel} · no required fields`;
    } else if (outstanding) {
      nodes.documentMeta.textContent = `${pageLabel} · ${outstanding} required field${outstanding === 1 ? "" : "s"} left`;
    } else {
      nodes.documentMeta.textContent = `${pageLabel} · all required fields complete`;
    }
    if (nodes.nextRequiredButton) {
      nodes.nextRequiredButton.disabled = outstanding === 0;
    }
  }

  function applyFieldHighlighting() {
    const enabled = !nodes.highlightFieldsToggle || nodes.highlightFieldsToggle.checked;
    nodes.pagesRoot.classList.toggle("pages-root-plain", !enabled);
  }

  /** Notice that this document moved on while the tab was sitting in the background.
   *
   * The page reads its state once, when it loads, and then holds it. A signer who leaves the tab
   * open keeps whatever was true at that moment: after an administrator asks for a re-sign, the
   * document is theirs to fill in again, but the tab still reads "This document has already been
   * signed" over a locked copy, and nothing on the page will ever say otherwise.
   *
   * Only a change in status reloads. Nothing is re-rendered while the two agree, so returning to
   * the tab cannot disturb a half-filled form - and when the status has changed, what was being
   * filled in could no longer be submitted anyway.
   */
  async function refreshIfStale() {
    if (document.visibilityState !== "visible" || isSubmitting || !state.context) return;
    try {
      const latest = await request(app.dataset.contextUrl);
      if (latest.status !== state.context.status || latest.document_status !== state.context.document_status) {
        window.location.reload();
      }
    } catch (error) {
      // A failed check is not worth interrupting anyone over; the page is no more wrong than it
      // already was, and the next look will try again.
      void error;
    }
  }

  async function loadContext() {
    state.context = await request(app.dataset.contextUrl);
    nodes.statusBadge.textContent = state.context.status.replaceAll("_", " ");
    nodes.signerName.textContent = state.context.signer_name || "Signer";
    nodes.expiresAt.textContent = formatDate(state.context.expires_at);
    nodes.documentTitle.textContent = state.context.document_title;
    nodes.otpTarget.textContent = state.context.masked_email
      ? `Verification code will be sent to ${state.context.masked_email}.`
      : "Verification code will be sent to the email address assigned to this request.";
    nodes.verifyAccessPanel.hidden = state.context.is_verified;
    nodes.otpEntryPanel.hidden = state.context.is_verified;
    nodes.fieldProgressPanel.hidden = !state.context.is_verified;
    nodes.documentPanel.hidden = !state.context.is_verified;

    if (state.context.has_signed) {
      // "This document has already been signed" is the reason nobody may fill it in, and it was
      // shown in the colour of a refusal. To the person who just signed it, that reads as their
      // signature having been rejected - which is what the one visible message said after a
      // submission that had in fact succeeded.
      setNotice("You have signed this document. It is now locked.", "success");
    } else if (state.context.access_message) {
      setNotice(state.context.access_message, "error");
    } else if (state.context.is_verified) {
      setNotice("Email verified. Complete the remaining fields and submit the document.", "success");
    } else {
      setNotice("", "success");
    }

    renderFieldList();
    renderPages();
    updateSubmitState();

    // A signer returning to a document they already signed gets what they signed, not a form to
    // fill in again.
    if (state.context.has_signed) {
      state.submitted = true;
      showSignedState();
    }
  }

  function setSignatureMode(mode) {
    state.signatureMode = mode;
    nodes.drawPane.hidden = mode !== "draw";
    nodes.typePane.hidden = mode !== "type";
    nodes.drawModeButton.classList.toggle("mode-button-active", mode === "draw");
    nodes.typeModeButton.classList.toggle("mode-button-active", mode === "type");
  }

  function clearCanvas() {
    canvasContext.fillStyle = "#ffffff";
    canvasContext.fillRect(0, 0, nodes.signatureCanvas.width, nodes.signatureCanvas.height);
    canvasContext.strokeStyle = "#17324d";
    canvasContext.lineWidth = 5;
    canvasContext.lineCap = "round";
  }

  function getCanvasPoint(event) {
    const rect = nodes.signatureCanvas.getBoundingClientRect();
    const scaleX = nodes.signatureCanvas.width / rect.width;
    const scaleY = nodes.signatureCanvas.height / rect.height;
    return {
      x: (event.clientX - rect.left) * scaleX,
      y: (event.clientY - rect.top) * scaleY,
    };
  }

  function startDrawing(event) {
    isDrawing = true;
    const point = getCanvasPoint(event);
    canvasContext.beginPath();
    canvasContext.moveTo(point.x, point.y);
  }

  function draw(event) {
    if (!isDrawing) return;
    const point = getCanvasPoint(event);
    canvasContext.lineTo(point.x, point.y);
    canvasContext.stroke();
  }

  function stopDrawing() {
    isDrawing = false;
  }

  function renderTypedSignaturePreview() {
    nodes.typedPreview.textContent =
      state.typedSignature || "Type your signature or initials here";
  }

  function renderTypedSignatureImage(field) {
    const canvas = document.createElement("canvas");
    canvas.width = 1200;
    canvas.height = field.field_type === "INITIALS" ? 320 : 420;
    const context = canvas.getContext("2d");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#16314d";
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.font = field.field_type === "INITIALS" ? "120px cursive" : "160px cursive";
    context.fillText(state.typedSignature || "", canvas.width / 2, canvas.height / 2);
    return canvasToBlob(canvas);
  }

  function dataUrlToBlob(dataUrl) {
    const [meta, data] = dataUrl.split(",");
    const mimeMatch = /data:(.*?);base64/.exec(meta || "");
    const mimeType = mimeMatch?.[1] || "image/png";
    const binary = atob(data || "");
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return new Blob([bytes], { type: mimeType });
  }

  function canvasToBlob(canvas) {
    return new Promise((resolve, reject) => {
      try {
        if (typeof canvas.toBlob === "function") {
          let settled = false;
          const fallbackTimer = window.setTimeout(() => {
            if (settled) return;
            settled = true;
            try {
              resolve(dataUrlToBlob(canvas.toDataURL("image/png")));
            } catch (fallbackError) {
              reject(fallbackError);
            }
          }, 250);

          canvas.toBlob((blob) => {
            if (settled) return;
            settled = true;
            window.clearTimeout(fallbackTimer);
            if (blob) {
              resolve(blob);
              return;
            }

            try {
              resolve(dataUrlToBlob(canvas.toDataURL("image/png")));
            } catch (fallbackError) {
              reject(fallbackError);
            }
          }, "image/png");
          return;
        }

        resolve(dataUrlToBlob(canvas.toDataURL("image/png")));
      } catch (error) {
        reject(error);
      }
    });
  }

  async function saveSignatureField() {
    const activeFieldId = state.activeFieldId || nodes.signatureModal.dataset.activeFieldId || "";
    if (isSavingSignature) return;
    if (!activeFieldId || !state.context) {
      closeSignatureModal();
      setNotice("Unable to save this signature field right now. Re-open the field and try again.", "error");
      return;
    }

    state.activeFieldId = activeFieldId;
    const field = state.context.fields.find((entry) => entry.id === activeFieldId);
    if (!field) {
      closeSignatureModal();
      setNotice("The selected signature field could not be found. Re-open the field and try again.", "error");
      return;
    }

    let blob = null;

    try {
      isSavingSignature = true;
      nodes.saveSignatureButton.disabled = true;

      if (state.signatureMode === "type") {
        if (!state.typedSignature.trim()) {
          setNotice("Type a value before saving this field.", "error");
          return;
        }
        blob = await renderTypedSignatureImage(field);
      } else {
        blob = await canvasToBlob(nodes.signatureCanvas);
      }

      if (!blob) {
        setNotice("Unable to generate the signature image. Please try again.", "error");
        return;
      }

      const imageUrl = URL.createObjectURL(blob);
      const reusableTargets = nodes.reuseSignatureCheckbox.checked ? getReusableSignatureTargets(field) : [];
      state.values[activeFieldId] = buildSignatureValue(field, blob, imageUrl);
      delete state.fieldErrors[activeFieldId];

      reusableTargets.forEach((targetField) => {
        state.values[targetField.id] = buildSignatureValue(targetField, blob, imageUrl);
        delete state.fieldErrors[targetField.id];
      });

      closeSignatureModal();
      renderFieldList();
      renderPages();
      updateSubmitState();
      setNotice(
        reusableTargets.length > 0
          ? `${field.label} saved and applied to ${reusableTargets.length} more field${reusableTargets.length === 1 ? "" : "s"}.`
          : `${field.label} saved.`,
        "success",
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unable to save this signature field.";
      setNotice(message, "error");
    } finally {
      isSavingSignature = false;
      nodes.saveSignatureButton.disabled = false;
    }
  }

  async function submitDocument() {
    if (isSubmitting || state.submitted || !state.context || !state.context.is_verified) return;
    // Set after the guard, so a press that does nothing cannot leave the dialog reading
    // "Submitting" with its close controls hidden.
    showSubmitProgress();
    const formData = new FormData();
    // The HttpOnly signer cookie keeps resumed sessions valid after a refresh.
    if (state.sessionToken) {
      formData.append("session_token", state.sessionToken);
    }
    state.fieldErrors = {};

    state.context.fields.forEach((field) => {
      const value = getFieldValue(field.id);
      if (!value) return;

      formData.append(`field_${field.id}_type`, value.type);
      if (value.type === "TEXT") {
        formData.append(`field_${field.id}_value`, value.textValue || "");
      } else if (value.type === "CHECKBOX") {
        formData.append(`field_${field.id}_checked`, value.checked ? "true" : "false");
      } else if (value.imageBlob) {
        formData.append(`field_${field.id}_image`, value.imageBlob, `${field.id}.png`);
      }
    });

    try {
      isSubmitting = true;
      nodes.submitButton.disabled = true;
      const payload = await request(app.dataset.submitUrl, {
        method: "POST",
        body: formData,
      });
      state.submitted = true;
      nodes.submitButton.textContent = "Document submitted";
      setNotice(payload.message || "Document signed successfully.", "success");
      // The document is no longer something to fill in. Reloading gets the completed copy's
      // whereabouts rather than guessing at it, and leaves the page showing what was signed.
      try {
        await loadContext();
      } catch (reloadError) {
        void reloadError;
        showSignedState();
      }
      // After the reload, so the dialog knows whether there is a copy to offer.
      showSubmitOutcome({
        title: "Your signature has been recorded",
        copy: state.context?.signed_copy_ready
          ? "This document is now locked. You can download your copy, and it has also been emailed to you."
          : payload.message ||
            "This document is now locked. Your completed copy is being prepared and will be emailed to you.",
        tone: "success",
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "The document could not be submitted.";
      try {
        await loadContext();
      } catch (reloadError) {
        void reloadError;
      }
      // After the reload, which writes a notice of its own and would otherwise bury this one.
      setNotice(message, "error");
      showSubmitOutcome({ title: "Your document was not submitted", copy: message, tone: "error" });
    } finally {
      isSubmitting = false;
      if (!state.submitted) {
        updateSubmitState();
      }
    }
  }

  nodes.sendOtpButton.addEventListener("click", async () => {
    try {
      nodes.sendOtpButton.disabled = true;
      const payload = await request(app.dataset.otpSendUrl, { method: "POST" });
      setNotice(payload.message || `Verification code sent to ${payload.masked_email}.`, "success");
      startVerificationCooldown(payload.retry_after || 60);
      nodes.otpInput.focus();
    } catch (error) {
      setNotice(error.message, "error");
    } finally {
      if (!state.resendTimerId) {
        nodes.sendOtpButton.disabled = false;
      }
    }
  });

  nodes.verifyOtpButton.addEventListener("click", async () => {
    try {
      nodes.verifyOtpButton.disabled = true;
      const payload = await request(app.dataset.otpVerifyUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ otp: nodes.otpInput.value.trim() }),
      });
      state.sessionToken = payload.session_token;
      setNotice("Email verified. Complete the remaining fields and submit the document.", "success");
      await loadContext();
    } catch (error) {
      setNotice(error.message, "error");
    } finally {
      nodes.verifyOtpButton.disabled = false;
    }
  });

  if (nodes.highlightFieldsToggle) {
    nodes.highlightFieldsToggle.addEventListener("change", applyFieldHighlighting);
  }

  nodes.pageFirst?.addEventListener("click", () => goToPage(1));
  nodes.pagePrev?.addEventListener("click", () => goToPage(state.currentPage - 1));
  nodes.pageNext?.addEventListener("click", () => goToPage(state.currentPage + 1));
  nodes.pageLast?.addEventListener("click", () => goToPage(state.context?.pages.length || 1));
  nodes.nextRequiredButton?.addEventListener("click", focusNextRequiredField);

  nodes.submitButton.addEventListener("click", openSubmitModal);
  nodes.confirmSubmitButton?.addEventListener("click", () => {
    void submitDocument();
  });
  nodes.downloadSignedButton?.addEventListener("click", () => {
    void downloadSignedCopy(nodes.downloadSignedButton);
  });
  nodes.modalDownloadButton?.addEventListener("click", () => {
    void downloadSignedCopy(nodes.modalDownloadButton);
  });
  nodes.cancelSubmitButton?.addEventListener("click", closeSubmitModal);
  nodes.closeSubmitModalButton?.addEventListener("click", closeSubmitModal);
  nodes.submitModal?.addEventListener("click", (event) => {
    if (event.target instanceof HTMLElement && event.target.dataset.closeSubmitModal === "true") {
      closeSubmitModal();
    }
  });

  document.addEventListener("visibilitychange", () => {
    void refreshIfStale();
  });
  // Restoring from the back/forward cache does not re-run this script, so the page comes back
  // exactly as it was left however long ago that was.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) void refreshIfStale();
  });

  nodes.closeFieldSheetButton?.addEventListener("click", closeFieldSheet);
  nodes.cancelFieldSheetButton?.addEventListener("click", closeFieldSheet);
  nodes.saveFieldSheetButton?.addEventListener("click", saveFieldSheet);
  nodes.fieldSheet?.addEventListener("click", (event) => {
    if (event.target instanceof HTMLElement && event.target.dataset.closeFieldSheet) {
      closeFieldSheet();
    }
  });
  nodes.fieldSheetInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveFieldSheet();
    }
  });

  nodes.closeModalButton.addEventListener("click", closeSignatureModal);
  nodes.signatureModal.addEventListener("click", (event) => {
    if (event.target instanceof HTMLElement && event.target.dataset.closeModal === "true") {
      closeSignatureModal();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && nodes.submitModal && !nodes.submitModal.hidden) {
      closeSubmitModal();
      return;
    }
    if (event.key === "Escape" && !nodes.signatureModal.hidden) {
      closeSignatureModal();
    }
  });
  nodes.drawModeButton.addEventListener("click", () => setSignatureMode("draw"));
  nodes.typeModeButton.addEventListener("click", () => setSignatureMode("type"));
  nodes.clearSignatureButton.addEventListener("click", () => {
    clearCanvas();
    state.typedSignature = "";
    nodes.typedSignatureInput.value = "";
    renderTypedSignaturePreview();
  });
  nodes.saveSignatureButton.addEventListener("click", () => {
    void saveSignatureField();
  });
  nodes.typedSignatureInput.addEventListener("input", () => {
    state.typedSignature = nodes.typedSignatureInput.value;
    renderTypedSignaturePreview();
  });

  nodes.signatureCanvas.addEventListener("pointerdown", startDrawing);
  nodes.signatureCanvas.addEventListener("pointermove", draw);
  nodes.signatureCanvas.addEventListener("pointerup", stopDrawing);
  nodes.signatureCanvas.addEventListener("pointerleave", stopDrawing);

  clearCanvas();
  renderTypedSignaturePreview();
  setSignatureMode("draw");
  void loadContext().catch((error) => {
    setNotice(error.message || "Unable to load signing session.", "error");
  });
})();
