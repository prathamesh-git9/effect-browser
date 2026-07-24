const state = { tasks: [], selected: null, detail: null, profiles: [], profile: null };
const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      "X-Actor-ID": "dashboard-operator",
      ...(options.headers || {}),
    },
    ...options,
  });
  const body = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(body?.error?.detail || `HTTP ${response.status}`);
  return body;
}

function escapeHtml(value) {
  const chars = { "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" };
  return String(value ?? "").replace(/[&<>'"]/g, (character) => chars[character]);
}

function short(value, size = 12) {
  return value ? `${value.slice(0, size)}...` : "-";
}

function baseName(value) {
  return String(value ?? "").split(/[\\/]/).pop();
}

function renderOutgoingReview(review) {
  if (!review) return "";
  const requests = review.requests ?? [];
  const fields = review.fields.length
    ? `<table class="review-table"><thead><tr><th>Observed field</th><th>Live value</th></tr></thead>
      <tbody>${review.fields.map((field) => `<tr><td>${escapeHtml(field.label)}</td>
      <td>${escapeHtml(field.value)}</td></tr>`).join("")}</tbody></table>`
    : '<p class="empty">No text fields are included in this commit.</p>';
  const documents = review.document_sha256s.length
    ? `<p><strong>Document SHA-256:</strong><br>${review.document_sha256s
      .map((value) => `<code>${escapeHtml(value)}</code>`).join("<br>")}</p>`
    : "";
  const network = requests.map((request) => {
    const requestFields = request.fields.length
      ? `<table class="review-table"><thead><tr><th>Outgoing field</th><th>Exact value</th></tr></thead>
        <tbody>${request.fields.map((field) => `<tr><td>${escapeHtml(field.name)}</td>
        <td>${field.redacted
          ? `<em>redacted</em> &middot; SHA-256 ${escapeHtml(short(field.value_sha256))}`
          : escapeHtml(field.value)}</td></tr>`).join("")}</tbody></table>`
      : '<p class="empty">This request has no query or body fields.</p>';
    return `<article><p><strong>${escapeHtml(request.method)}
      ${escapeHtml(request.target)}</strong><br>
      Content-Type: ${escapeHtml(request.content_type || "none")}</p>
      ${requestFields}
      <p><strong>Canonical body SHA-256:</strong><br>
      <code>${escapeHtml(request.body_sha256)}</code><br>
      ${request.wire_body_sha256
        ? `<strong>Preview wire-body SHA-256:</strong><br>
          <code>${escapeHtml(request.wire_body_sha256)}</code><br>`
        : ""}
      <strong>Request fingerprint SHA-256:</strong><br>
      <code>${escapeHtml(request.request_sha256)}</code></p></article>`;
  }).join("");
  const kicker = requests.length
    ? "ABORTED NETWORK PREVIEW &middot; NOTHING SENT"
    : "OBSERVED LIVE FORM VALUES";
  return `<section class="outgoing-review"><p class="kicker">${kicker}</p>
    ${network}
    ${fields}${documents}
    <p><strong>Observed DOM SHA-256:</strong><br>
    <code title="${escapeHtml(review.observation_sha256)}">${escapeHtml(review.observation_sha256)}</code></p>
    <p><strong>Payload SHA-256:</strong><br>
    <code title="${escapeHtml(review.payload_sha256)}">${escapeHtml(review.payload_sha256)}</code></p>
    </section>`;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2800);
}

async function mutate(button, work) {
  button.disabled = true;
  try {
    await work();
    await loadTasks();
    if (state.selected) await selectTask(state.selected);
    await verifyAudit();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function loadTasks() {
  state.tasks = await api("/v1/tasks");
  $("#tasks").innerHTML = state.tasks.length
    ? state.tasks.map((task) => `<button class="task-button ${task.id === state.selected ? "active" : ""}" data-task="${task.id}"><strong>${escapeHtml(task.instruction)}</strong><small>${escapeHtml(task.status)} / ${task.id.slice(0, 8)}</small></button>`).join("")
    : '<p class="empty">No operations yet.</p>';
  document.querySelectorAll("[data-task]").forEach((button) => {
    button.addEventListener("click", () => selectTask(button.dataset.task));
  });
}

async function selectTask(id) {
  state.selected = id;
  state.detail = await api(`/v1/tasks/${id}`);
  await loadTasks();
  render();
}

function render() {
  const { task, actions, events } = state.detail;
  $("#empty-state").hidden = true;
  $("#task-view").hidden = false;
  $("#task-id").textContent = `TASK ${task.id}`;
  $("#task-title").textContent = task.instruction;
  $("#task-status").textContent = task.status.replaceAll("_", " ");
  $("#action-count").textContent = `${actions.length} actions / cursor ${task.current_ordinal}`;
  $("#actions").innerHTML = actions.map((action, index) => `
    <article class="action">
      <span class="ordinal">${String(index + 1).padStart(2, "0")}</span>
      <div><h3>${escapeHtml(action.proposal.description)}</h3>
      <p>${escapeHtml(action.proposal.kind)}${action.proposal.effect_key ? ` / effect ${escapeHtml(action.proposal.effect_key)}` : ""}</p>
      <code>${short(action.action_sha256, 20)}</code></div>
      <span class="action-state">${escapeHtml(action.state.replaceAll("_", " "))}</span>
    </article>`).join("");
  $("#events").innerHTML = events.map((event) => `
    <div class="event"><span>${String(event.sequence).padStart(3, "0")}</span>
    ${escapeHtml(event.kind)} <span>${short(event.event_hash)}</span></div>`).join("");
  renderGate(actions[task.current_ordinal]);
}

function renderGate(action) {
  const gate = $("#gate");
  gate.innerHTML = "";
  gate.className = "";
  if (!action) return;
  if (action.state === "approval_required") {
    const isUpload = action.proposal.kind === "upload";
    const details = isUpload
      ? `<p><strong>Document:</strong> ${escapeHtml(baseName(action.proposal.file_path))}<br>
        <strong>Document SHA-256:</strong>
        <code>${escapeHtml(action.proposal.document_sha256)}</code><br>
        <strong>Observed URL:</strong> ${escapeHtml(action.observation_url)}</p>`
      : `<p><strong>Expected:</strong> ${escapeHtml(action.proposal.expected_outcome)}<br>
        <strong>Effect key:</strong> ${escapeHtml(action.proposal.effect_key)}<br>
        <strong>Observed URL:</strong> ${escapeHtml(action.observation_url)}</p>`;
    gate.className = "gate";
    gate.innerHTML = `
      <p class="kicker">${isUpload ? "LOCAL FILE-SELECTION GATE" : "EXTERNAL COMMIT GATE"}</p>
      <h3>Operator decision required</h3>
      <p>${escapeHtml(action.proposal.description)} ${isUpload
        ? "File selection is approved, but any unreviewed auto-upload request is blocked."
        : "The approval binds this exact action to the observed page state; any drift invalidates it."}</p>
      <div class="hashes"><div><small>ACTION SHA-256</small><code title="${action.action_sha256}">${action.action_sha256}</code></div>
      <div><small>OBSERVATION SHA-256</small><code title="${action.observation_sha256}">${action.observation_sha256}</code></div></div>
      ${details}
      ${renderOutgoingReview(action.proposal.outgoing_review)}
      <div class="gate-actions"><button class="primary" id="approve">${isUpload
        ? "Approve local file selection"
        : "Approve exact commit"}</button>
      <button class="primary danger" id="reject">Reject</button></div>`;
    $("#approve").addEventListener("click", (event) => mutate(
      event.currentTarget,
      () => api(`/v1/actions/${action.id}/approve`, {
        method: "POST", body: JSON.stringify({ expected_version: action.version }),
      }),
    ));
    $("#reject").addEventListener("click", (event) => mutate(
      event.currentTarget,
      () => api(`/v1/actions/${action.id}/reject`, {
        method: "POST", body: JSON.stringify({ expected_version: action.version }),
      }),
    ));
  } else if (["outcome_unknown", "dispatching"].includes(action.state)) {
    const isUpload = action.proposal.kind === "upload";
    const canReconcile = Boolean(action.proposal.reconciliation);
    gate.className = "gate unknown";
    gate.innerHTML = `
      <p class="kicker">AMBIGUOUS REMOTE OUTCOME</p><h3>Automatic retry is blocked</h3>
      <p>${escapeHtml(action.failure || "A worker disappeared at the commit boundary.")}
      ${isUpload
        ? " Reattaching the file could transmit it twice."
        : " Clicking again could duplicate the effect. Query the stable business reference first."}</p>
      <p><strong>${isUpload ? "Document SHA-256" : "Reference"}:</strong>
      ${escapeHtml(isUpload ? action.proposal.document_sha256 : action.proposal.effect_key)}</p>
      <div class="gate-actions">${canReconcile
        ? '<button class="primary" id="reconcile">Reconcile target receipt</button>'
        : ""}
      <button class="secondary" id="not-committed">Mark not committed</button></div>`;
    if (canReconcile) {
      $("#reconcile").addEventListener("click", (event) => mutate(
        event.currentTarget,
        () => api(`/v1/actions/${action.id}/reconcile`, { method: "POST" }),
      ));
    }
    $("#not-committed").addEventListener("click", (event) => mutate(
      event.currentTarget,
      () => api(`/v1/actions/${action.id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ expected_version: action.version, resolution: "not_committed" }),
      }),
    ));
  } else if (action.state === "input_required") {
    gate.className = "gate unknown";
    gate.innerHTML = `
      <p class="kicker">VERIFIED INPUT OR HUMAN STEP REQUIRED</p>
      <h3>Automation stopped without guessing</h3>
      <p>${escapeHtml(action.failure || action.proposal.description)}</p>
      <p>Update the task's factual profile or complete the named human-only step,
      then explicitly resume re-planning. This does not submit anything.</p>
      <div class="gate-actions">
        <button class="secondary" id="resume-input">Resume after resolution</button>
      </div>`;
    $("#resume-input").addEventListener("click", (event) => mutate(
      event.currentTarget,
      () => api(`/v1/actions/${action.id}/resume-input`, {
        method: "POST",
        body: JSON.stringify({ expected_version: action.version }),
      }),
    ));
  }
}

async function loadProfiles() {
  state.profiles = await api("/v1/profiles");
  const select = $("#profile-select");
  const selected = select.value;
  select.innerHTML = '<option value="">Create or select a profile</option>'
    + state.profiles.map((profile) => `<option value="${profile.id}">${escapeHtml(profile.name)} (${profile.version})</option>`).join("");
  select.value = state.profiles.some((profile) => profile.id === selected) ? selected : "";
  if (select.value) await loadProfile(select.value);
}

async function loadProfile(id) {
  if (!id) {
    state.profile = null;
    $("#answer-form").hidden = true;
    $("#profile-answers").innerHTML = '<p class="empty">No profile selected.</p>';
    return;
  }
  state.profile = await api(`/v1/profiles/${id}`);
  $("#answer-form").hidden = false;
  $("#profile-answers").innerHTML = state.profile.answers.length
    ? state.profile.answers.map((answer) => `<div class="event"><strong>${escapeHtml(answer.field_name)}</strong><br>${escapeHtml(answer.value)}<br><small>${escapeHtml(answer.verification_state)} / ${escapeHtml(answer.sensitivity)}</small></div>`).join("")
    : '<p class="empty">No answers yet.</p>';
}

async function verifyAudit() {
  try {
    const audit = await api("/v1/audit/verify");
    $("#audit-light").classList.toggle("valid", audit.valid);
    $("#audit-state").textContent = audit.valid ? "Ledger verified" : "Ledger integrity failed";
    $("#audit-detail").textContent = `${audit.event_count} events / ${short(audit.head_hash)}`;
  } catch (_error) {
    $("#audit-state").textContent = "Ledger unavailable";
  }
}

$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await mutate(event.submitter, async () => {
    const task = await api("/v1/tasks", {
      method: "POST",
      body: JSON.stringify({
        instruction: $("#instruction").value,
        start_url: $("#start-url").value,
        provider: $("#provider").value,
        profile_id: $("#profile-id").value.trim() || null,
        document_path: $("#document-path").value.trim() || null,
        document_sha256: $("#document-sha256").value.trim() || null,
      }),
    });
    state.selected = task.id;
    toast("Durable plan created");
  });
});
$("#profile-select").addEventListener("change", (event) => loadProfile(event.target.value).catch((error) => toast(error.message)));
$("#profile-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await mutate(event.submitter, async () => {
    const profile = await api("/v1/profiles", {
      method: "POST", body: JSON.stringify({ name: $("#profile-name").value }),
    });
    $("#profile-name").value = "";
    await loadProfiles();
    $("#profile-select").value = profile.id;
    await loadProfile(profile.id);
    toast("Profile created");
  });
});
$("#answer-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.profile) return;
  await mutate(event.submitter, async () => {
    const answer = {
      value: $("#answer-value").value,
      source: { kind: $("#answer-source").value },
      sensitivity: $("#answer-sensitivity").value,
      verification_state: $("#answer-verified").checked ? "verified" : "unverified",
      expected_version: state.profile.profile.version,
    };
    await api(`/v1/profiles/${state.profile.profile.id}/answers/${encodeURIComponent($("#answer-field").value)}`, {
      method: "PUT", body: JSON.stringify(answer),
    });
    await loadProfiles();
    await loadProfile(state.profile.profile.id);
    $("#answer-field").value = "";
    $("#answer-value").value = "";
    $("#answer-verified").checked = false;
    toast("Profile answer saved");
  });
});
$("#run-task").addEventListener("click", (event) => mutate(
  event.currentTarget,
  () => api(`/v1/tasks/${state.selected}/run`, { method: "POST" }),
));
$("#refresh").addEventListener("click", async () => {
  await loadTasks();
  if (state.selected) await selectTask(state.selected);
  await verifyAudit();
});
Promise.all([loadTasks(), loadProfiles(), verifyAudit()]).catch((error) => toast(error.message));
