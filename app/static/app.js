const gridForm = document.getElementById("gridForm");
const gridSelect = document.getElementById("gridSelect");
const sourceSrs = document.getElementById("sourceSrs");
const sourceSrsHint = document.getElementById("sourceSrsHint");
const jobStatusList = document.getElementById("jobStatusList");
const reprocessDialog = document.getElementById("reprocessDialog");
const reprocessGridList = document.getElementById("reprocessGridList");
const cancelReprocessButton = document.getElementById("cancelReprocessButton");
const confirmReprocessButton = document.getElementById("confirmReprocessButton");

const tabManual = document.getElementById("tabManual");
const tabCsv = document.getElementById("tabCsv");
const panelManual = document.getElementById("panelManual");
const panelCsv = document.getElementById("panelCsv");
const csvForm = document.getElementById("csvForm");
const batchProgress = document.getElementById("batchProgress");
const batchTitle = document.getElementById("batchTitle");
const batchCounter = document.getElementById("batchCounter");
const batchBar = document.getElementById("batchBar");
const batchErrors = document.getElementById("batchErrors");

const gridSourceSrs = window.GRID_SOURCE_SRS || {};
const pollTimers = new Map();
let batchPollTimer = null;

if (!gridForm || !gridSelect) {
  throw new Error("A tela de seleção de grids não foi carregada corretamente.");
}

// --- Tab switching ---
tabManual.addEventListener("click", () => {
  tabManual.classList.add("active");
  tabCsv.classList.remove("active");
  panelManual.classList.remove("hidden");
  panelCsv.classList.add("hidden");
});

tabCsv.addEventListener("click", () => {
  tabCsv.classList.add("active");
  tabManual.classList.remove("active");
  panelCsv.classList.remove("hidden");
  panelManual.classList.add("hidden");
});

function clearPollTimers() {
  pollTimers.forEach((timer) => clearInterval(timer));
  pollTimers.clear();
}

function getSelectedGridNames() {
  return Array.from(gridSelect.selectedOptions).map((option) => option.value);
}

function inferGridSourceSrs(gridName) {
  const upperGridName = gridName.toUpperCase();
  const prefix = Object.keys(gridSourceSrs).find((item) => upperGridName.startsWith(item));
  return prefix ? gridSourceSrs[prefix] : "";
}

function updateSourceProjectionPreview() {
  const selectedGrids = getSelectedGridNames();
  const inferredValues = new Set(selectedGrids.map(inferGridSourceSrs).filter(Boolean));
  const hasUnknown = selectedGrids.some((gridName) => !inferGridSourceSrs(gridName));

  if (selectedGrids.length === 0) {
    sourceSrs.value = "";
    sourceSrsHint.textContent = "A origem e o destino serão inferidos pelo prefixo SF-22 ou SF-23.";
    return;
  }

  if (hasUnknown) {
    sourceSrs.value = "";
    sourceSrsHint.textContent = "Há grid sem prefixo reconhecido para inferir a projeção.";
    return;
  }

  if (inferredValues.size === 1) {
    const [value] = inferredValues;
    sourceSrs.value = value;
    sourceSrsHint.textContent = "CRS inferido automaticamente; o GeoTIFF será gerado nesse mesmo CRS.";
    return;
  }

  sourceSrs.value = "mixed";
  sourceSrsHint.textContent = "Cada grid será processado no CRS inferido a partir do seu nome.";
}

function showNotice(message) {
  jobStatusList.classList.remove("hidden");
  jobStatusList.innerHTML = "";
  const article = document.createElement("article");
  article.className = "job-card";
  const paragraph = document.createElement("p");
  paragraph.className = "job-message";
  paragraph.textContent = message;
  article.appendChild(paragraph);
  jobStatusList.appendChild(article);
}

function createJobCard(job) {
  const article = document.createElement("article");
  article.id = `job-${job.id}`;
  article.className = "job-card";
  article.innerHTML = `
    <div class="job-meta">
      <span class="job-title"></span>
      <span class="badge queued">queued</span>
    </div>
    <p class="job-message"></p>
    <p class="job-srs"></p>
    <div class="progress-track">
      <div class="progress-bar"></div>
    </div>
    <p class="progress-text">0%</p>
    <a class="download-link hidden" href="#">Baixar resultado .tiff</a>
  `;
  jobStatusList.appendChild(article);
  setJobView(job);
}

function setJobView(data) {
  const article = document.getElementById(`job-${data.id}`);
  if (!article) {
    return;
  }

  const badge = article.querySelector(".badge");
  article.querySelector(".job-title").textContent = `${data.filename} | Job: ${data.id}`;
  badge.textContent = data.status;
  badge.className = `badge ${data.status}`;
  article.querySelector(".job-message").textContent = data.message;
  article.querySelector(".job-srs").textContent = `CRS: ${data.target_srs || data.source_srs || "-"}`;
  article.querySelector(".progress-bar").style.width = `${data.progress}%`;
  article.querySelector(".progress-text").textContent = `${data.progress}%`;

  const downloadLink = article.querySelector(".download-link");
  if (data.download_url) {
    downloadLink.href = data.download_url;
    downloadLink.classList.remove("hidden");
  } else {
    downloadLink.classList.add("hidden");
  }
}

async function pollJob(jobId) {
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    if (!response.ok) {
      throw new Error("Falha ao consultar status do job.");
    }

    const data = await response.json();
    setJobView(data);

    if (data.status === "completed" || data.status === "failed") {
      clearInterval(pollTimers.get(jobId));
      pollTimers.delete(jobId);
    }
  } catch (error) {
    clearInterval(pollTimers.get(jobId));
    pollTimers.delete(jobId);

    const article = document.getElementById(`job-${jobId}`);
    if (article) {
      const badge = article.querySelector(".badge");
      badge.textContent = "failed";
      badge.className = "badge failed";
      article.querySelector(".job-message").textContent = error.message;
    }
  }
}

function openReprocessDialog(grids) {
  reprocessGridList.innerHTML = "";
  grids.forEach((grid) => {
    const item = document.createElement("li");
    item.textContent = grid;
    reprocessGridList.appendChild(item);
  });
  confirmReprocessButton.onclick = () => {
    reprocessDialog.close();
    submitJobs(true);
  };
  reprocessDialog.showModal();
}

async function submitJobs(approveReprocess = false) {
  const selectedGrids = getSelectedGridNames();
  if (selectedGrids.length === 0) {
    showNotice("Selecione ao menos um grid.");
    return;
  }

  clearPollTimers();
  showNotice(approveReprocess ? "Confirmando reprocessamento..." : "Agendando processamento...");

  const formData = new FormData(gridForm);
  formData.set("approve_reprocess", approveReprocess ? "true" : "false");

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: formData,
    });

    const data = await response.json();
    if (response.status === 409 && data.reprocess_required) {
      showNotice(data.detail);
      openReprocessDialog(data.grids || []);
      return;
    }

    if (!response.ok) {
      throw new Error(data.detail || "Falha ao agendar processamento.");
    }

    jobStatusList.classList.remove("hidden");
    jobStatusList.innerHTML = "";
    showNotice(data.message);
    data.jobs.forEach((job) => {
      createJobCard(job);
      const timer = setInterval(() => pollJob(job.id), 2000);
      pollTimers.set(job.id, timer);
      pollJob(job.id);
    });
  } catch (error) {
    showNotice(error.message);
  }
}

gridSelect.addEventListener("change", updateSourceProjectionPreview);

gridForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitJobs(false);
});

cancelReprocessButton.addEventListener("click", () => {
  reprocessDialog.close();
  showNotice("Reprocessamento cancelado. Nenhum novo job foi agendado.");
});

// confirmReprocessButton.onclick is set dynamically per dialog origin (manual or CSV)

// --- Batch progress ---
function updateBatchProgress(summary, totalFromCsv) {
  const total = totalFromCsv || summary.total;
  const done = summary.completed + summary.failed;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  batchTitle.textContent = `Lote: ${summary.completed} concluído(s) · ${summary.failed} falha(s) · ${summary.remaining} na fila`;
  batchCounter.textContent = `${done} / ${total} (${pct}%)`;
  batchBar.style.width = `${pct}%`;

  batchProgress.classList.remove("hidden");
}

function startBatchPolling(batchId, totalFromCsv) {
  if (batchPollTimer) clearInterval(batchPollTimer);

  async function poll() {
    try {
      const res = await fetch(`/api/batch/${batchId}/summary`);
      if (!res.ok) return;
      const summary = await res.json();
      updateBatchProgress(summary, totalFromCsv);
      if (summary.remaining === 0) {
        clearInterval(batchPollTimer);
        batchPollTimer = null;
      }
    } catch (_) { /* silent */ }
  }

  poll();
  batchPollTimer = setInterval(poll, 3000);
}

async function submitCsvBatch(approveReprocess = false) {
  const fileInput = document.getElementById("csvFile");
  if (!fileInput.files.length) {
    showNotice("Selecione um arquivo CSV.");
    return;
  }

  const formData = new FormData(csvForm);
  formData.set("approve_reprocess", approveReprocess ? "true" : "false");

  showNotice("Enviando CSV...");

  try {
    const response = await fetch("/api/batch", { method: "POST", body: formData });
    const data = await response.json();

    if (response.status === 409 && data.reprocess_required) {
      showNotice(data.detail);
      openReprocessDialogCsv(data.grids || []);
      return;
    }

    if (!response.ok) {
      showNotice(data.detail || "Falha ao processar CSV.");
      return;
    }

    jobStatusList.classList.remove("hidden");
    jobStatusList.innerHTML = "";
    showNotice(data.message);

    if (data.errors && data.errors.length > 0) {
      batchErrors.classList.remove("hidden");
      batchErrors.innerHTML = `<strong>Arquivos ignorados (${data.errors.length}):</strong><ul>${data.errors.map((e) => `<li>${e.filename}: ${e.error}</li>`).join("")}</ul>`;
    } else {
      batchErrors.classList.add("hidden");
    }

    startBatchPolling(data.batch_id, data.total);
  } catch (error) {
    showNotice(error.message);
  }
}

function openReprocessDialogCsv(grids) {
  reprocessGridList.innerHTML = "";
  grids.forEach((grid) => {
    const item = document.createElement("li");
    item.textContent = grid;
    reprocessGridList.appendChild(item);
  });
  confirmReprocessButton.onclick = () => {
    reprocessDialog.close();
    submitCsvBatch(true);
  };
  reprocessDialog.showModal();
}

csvForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitCsvBatch(false);
});

updateSourceProjectionPreview();
