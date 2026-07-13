(function signacoreSignerPortal() {
  const app = document.querySelector(".signacore-app");
  if (!app) return;

  const state = {
    context: null,
    sessionToken: sessionStorage.getItem(`signacore-session:${app.dataset.signingToken}`) || "",
    values: {},
    fieldErrors: {},
    activeFieldId: "",
    signatureMode: "draw",
    typedSignature: "",
  };

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
    fieldList: document.getElementById("field-list"),
    submitButton: document.getElementById("submit-button"),
    pagesRoot: document.getElementById("pages-root"),
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

  async function request(url, init) {
    const response = await fetch(url, init);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const message =
        typeof payload === "string"
          ? payload
          : payload.detail || payload.otp?.[0] || payload.session_token?.[0] || "Request failed.";
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
    if (field.field_type === "TEXT") return Boolean(value.textValue && value.textValue.trim());
    if (field.field_type === "CHECKBOX") {
      if (field.is_required) return Boolean(value.checked);
      return typeof value.checked === "boolean";
    }
    return Boolean(value.imageBlob || value.imageUrl);
  }

  function updateSubmitState() {
    if (!state.context || !state.sessionToken || state.context.access_message) {
      nodes.submitButton.disabled = true;
      return;
    }

    const hasMissingRequiredField = state.context.fields.some(
      (field) => field.is_required && !fieldIsComplete(field),
    );
    nodes.submitButton.disabled = hasMissingRequiredField;
  }

  function renderFieldList() {
    if (!state.context) return;
    nodes.fieldList.innerHTML = "";

    state.context.fields.forEach((field) => {
      const item = document.createElement("div");
      const complete = fieldIsComplete(field);
      item.className = "field-list-item";
      item.innerHTML = `
        <strong>${field.label}</strong>
        <div class="field-status ${complete ? "field-status-complete" : ""}">
          ${field.field_type} · page ${field.page} · ${complete ? "Completed" : field.is_required ? "Required" : "Optional"}
        </div>
      `;
      nodes.fieldList.appendChild(item);
    });
  }

  function buildTextField(field) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "text-field-input";
    input.placeholder = field.label;
    input.value = getFieldValue(field.id)?.textValue || "";
    input.addEventListener("input", () => {
      state.values[field.id] = {
        type: "TEXT",
        textValue: input.value,
      };
      delete state.fieldErrors[field.id];
      renderFieldList();
      updateSubmitState();
    });
    return input;
  }

  function buildCheckboxField(field) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(getFieldValue(field.id)?.checked);
    input.className = "checkbox-input";
    input.setAttribute("aria-label", field.label);
    input.addEventListener("change", () => {
      state.values[field.id] = {
        type: "CHECKBOX",
        checked: input.checked,
      };
      delete state.fieldErrors[field.id];
      renderFieldList();
      updateSubmitState();
    });
    return input;
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
    nodes.pagesRoot.innerHTML = "";

    state.context.pages.forEach((pageData) => {
      const pageCard = document.createElement("article");
      pageCard.className = "page-card";

      const pageImage = document.createElement("img");
      pageImage.className = "page-image";
      pageImage.alt = `${state.context.document_title} page ${pageData.number}`;
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
          const typeClassName =
            field.field_type === "TEXT"
              ? "field-overlay-text"
              : field.field_type === "CHECKBOX"
                ? "field-overlay-checkbox"
                : "field-overlay-signature";
          const minHeightPercent =
            field.field_type === "TEXT"
              ? 1.15
              : field.field_type === "CHECKBOX"
                ? 1.2
                : 1.8;

          fieldNode.className = `field-overlay ${typeClassName} ${field.is_required ? "field-overlay-required" : ""} ${
            state.fieldErrors[field.id] ? "field-error" : ""
          }`;
          fieldNode.style.top = `${top}%`;
          fieldNode.style.left = `${left}%`;
          fieldNode.style.width = `${width}%`;
          fieldNode.style.height = `${Math.max(height, minHeightPercent)}%`;
          fieldNode.title = field.label;

          let fieldContent;
          if (field.field_type === "TEXT") {
            fieldContent = buildTextField(field);
          } else if (field.field_type === "CHECKBOX") {
            fieldContent = buildCheckboxField(field);
          } else {
            fieldContent = buildSignatureField(field);
          }
          fieldNode.appendChild(fieldContent);
          overlay.appendChild(fieldNode);
        });

      pageCard.appendChild(pageImage);
      pageCard.appendChild(overlay);
      nodes.pagesRoot.appendChild(pageCard);
    });
  }

  async function loadContext() {
    state.context = await request(app.dataset.contextUrl);
    nodes.statusBadge.textContent = state.context.status.replaceAll("_", " ");
    nodes.signerName.textContent = state.context.signer_name || "Signer";
    nodes.expiresAt.textContent = formatDate(state.context.expires_at);
    nodes.documentTitle.textContent = state.context.document_title;
    nodes.otpTarget.textContent = `Verification code will be sent to ${state.context.masked_email}.`;

    if (state.context.access_message) {
      setNotice(state.context.access_message, "error");
    } else if (state.sessionToken) {
      setNotice("Email verified. Complete the remaining fields and submit the document.", "success");
    } else {
      setNotice("", "success");
    }

    renderFieldList();
    renderPages();
    updateSubmitState();
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
    if (!state.context || !state.sessionToken) return;
    const formData = new FormData();
    formData.append("session_token", state.sessionToken);
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
      nodes.submitButton.disabled = true;
      const payload = await request(app.dataset.submitUrl, {
        method: "POST",
        body: formData,
      });
      setNotice(payload.message || "Document signed successfully.", "success");
      await loadContext();
    } catch (error) {
      if (error instanceof Error) {
        setNotice(error.message, "error");
      }
      try {
        await loadContext();
      } catch (reloadError) {
        void reloadError;
      }
    } finally {
      updateSubmitState();
    }
  }

  nodes.sendOtpButton.addEventListener("click", async () => {
    try {
      nodes.sendOtpButton.disabled = true;
      const payload = await request(app.dataset.otpSendUrl, { method: "POST" });
      setNotice(payload.message || `OTP sent to ${payload.masked_email}.`, "success");
    } catch (error) {
      setNotice(error.message, "error");
    } finally {
      nodes.sendOtpButton.disabled = false;
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
      sessionStorage.setItem(`signacore-session:${app.dataset.signingToken}`, state.sessionToken);
      setNotice("Email verified. Complete the remaining fields and submit the document.", "success");
      await loadContext();
    } catch (error) {
      setNotice(error.message, "error");
    } finally {
      nodes.verifyOtpButton.disabled = false;
    }
  });

  nodes.submitButton.addEventListener("click", () => {
    void submitDocument();
  });

  nodes.closeModalButton.addEventListener("click", closeSignatureModal);
  nodes.signatureModal.addEventListener("click", (event) => {
    if (event.target instanceof HTMLElement && event.target.dataset.closeModal === "true") {
      closeSignatureModal();
    }
  });
  document.addEventListener("keydown", (event) => {
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
