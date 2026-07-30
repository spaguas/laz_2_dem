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
const tabBacia = document.getElementById("tabBacia");
const tabQueue = document.getElementById("tabQueue");
const panelManual = document.getElementById("panelManual");
const panelCsv = document.getElementById("panelCsv");
const panelBacia = document.getElementById("panelBacia");
const panelQueue = document.getElementById("panelQueue");
const queueTableBody = document.getElementById("queueTableBody");
const csvForm = document.getElementById("csvForm");
const batchProgress = document.getElementById("batchProgress");
const batchTitle = document.getElementById("batchTitle");
const batchCounter = document.getElementById("batchCounter");
const batchBar = document.getElementById("batchBar");
const batchErrors = document.getElementById("batchErrors");
const errorLogDialog = document.getElementById("errorLogDialog");
const errorLogFilename = document.getElementById("errorLogFilename");
const errorLogContent = document.getElementById("errorLogContent");
const closeErrorLogButton = document.getElementById("closeErrorLogButton");

// --- Bacia hidrográfica (MDT por bacia) ---
const methodButtons = document.querySelectorAll("#panelBacia .method-tabs .tab-btn");
const baciaMethodCoord = document.getElementById("baciaMethodCoord");
const baciaMethodCode = document.getElementById("baciaMethodCode");
const baciaMethodMap = document.getElementById("baciaMethodMap");
const baciaLat = document.getElementById("baciaLat");
const baciaLon = document.getElementById("baciaLon");
const baciaLookupCoordButton = document.getElementById("baciaLookupCoordButton");
const baciaUcInput = document.getElementById("baciaUcInput");
const baciaLookupUcButton = document.getElementById("baciaLookupUcButton");
const baciaLookupMessage = document.getElementById("baciaLookupMessage");
const baciaResult = document.getElementById("baciaResult");
const baciaResultText = document.getElementById("baciaResultText");
const baciaScheduleButton = document.getElementById("baciaScheduleButton");
const mdtBaciaDialog = document.getElementById("mdtBaciaDialog");
const mdtBaciaUcLabel = document.getElementById("mdtBaciaUcLabel");
const mdtBaciaResolution = document.getElementById("mdtBaciaResolution");
const mdtBaciaSaveToServer = document.getElementById("mdtBaciaSaveToServer");
const mdtBaciaExistingWarning = document.getElementById("mdtBaciaExistingWarning");
const mdtBaciaExistingPath = document.getElementById("mdtBaciaExistingPath");
const cancelMdtBaciaButton = document.getElementById("cancelMdtBaciaButton");
const confirmMdtBaciaButton = document.getElementById("confirmMdtBaciaButton");

let selectedBaciaUc = null;
let mdtBaciaFileExists = false;
let baciaMap = null;
let baciaGeojsonLayer = null;
let selectedMapLayer = null;
let resolutionCheckTimer = null;

const gridNumberSrs = window.GRID_NUMBER_SRS || {};
const gridPrefixPattern = new RegExp(window.GRID_PREFIX_PATTERN || "^S[A-Z]-(22|23)-", "i");
const pollTimers = new Map();
let batchPollTimer = null;
let queuePollTimer = null;

if (!gridForm || !gridSelect) {
  throw new Error("A tela de seleção de grids não foi carregada corretamente.");
}

// --- Tab switching ---
function showTab(activeTab) {
  [tabManual, tabCsv, tabBacia, tabQueue].forEach((tab) => tab.classList.toggle("active", tab === activeTab));
  panelManual.classList.toggle("hidden", activeTab !== tabManual);
  panelCsv.classList.toggle("hidden", activeTab !== tabCsv);
  panelBacia.classList.toggle("hidden", activeTab !== tabBacia);
  panelQueue.classList.toggle("hidden", activeTab !== tabQueue);

  if (activeTab === tabQueue) {
    refreshQueue();
    if (!queuePollTimer) {
      queuePollTimer = setInterval(refreshQueue, 3000);
    }
  } else if (queuePollTimer) {
    clearInterval(queuePollTimer);
    queuePollTimer = null;
  }

  if (activeTab === tabBacia && baciaMethodMap.classList.contains("hidden") === false) {
    initBaciaMap();
  }
}

tabManual.addEventListener("click", () => showTab(tabManual));
tabCsv.addEventListener("click", () => showTab(tabCsv));
tabBacia.addEventListener("click", () => showTab(tabBacia));
tabQueue.addEventListener("click", () => showTab(tabQueue));

function clearPollTimers() {
  pollTimers.forEach((timer) => clearInterval(timer));
  pollTimers.clear();
}

function getSelectedGridNames() {
  return Array.from(gridSelect.selectedOptions).map((option) => option.value);
}

function inferGridSourceSrs(gridName) {
  const match = gridName.toUpperCase().match(gridPrefixPattern);
  return match ? gridNumberSrs[match[1]] || "" : "";
}

function updateSourceProjectionPreview() {
  const selectedGrids = getSelectedGridNames();
  const inferredValues = new Set(selectedGrids.map(inferGridSourceSrs).filter(Boolean));
  const hasUnknown = selectedGrids.some((gridName) => !inferGridSourceSrs(gridName));

  if (selectedGrids.length === 0) {
    sourceSrs.value = "";
    sourceSrsHint.textContent = "A origem e o destino serão inferidos pelo prefixo S?-22 ou S?-23 (a segunda letra pode variar).";
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
    <div class="spinner hidden"></div>
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

  const isIndeterminate = data.kind === "mdt_bacia" && data.status === "processing";
  article.querySelector(".spinner").classList.toggle("hidden", !isIndeterminate);
  article.querySelector(".progress-track").classList.toggle("hidden", isIndeterminate);
  article.querySelector(".progress-text").classList.toggle("hidden", isIndeterminate);
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

async function submitCsvBatch() {
  const fileInput = document.getElementById("csvFile");
  if (!fileInput.files.length) {
    showNotice("Selecione um arquivo CSV.");
    return;
  }

  const formData = new FormData(csvForm);
  formData.set("approve_reprocess", "false");

  showNotice("Enviando CSV...");

  try {
    const response = await fetch("/api/batch", { method: "POST", body: formData });
    const data = await response.json();

    if (!response.ok) {
      showNotice(data.detail || "Falha ao processar CSV.");
      return;
    }

    if (!data.batch_id) {
      showNotice(data.detail || "Nenhum grid novo para processar.");
      return;
    }

    jobStatusList.classList.remove("hidden");
    jobStatusList.innerHTML = "";
    showNotice(data.message);

    const skipped = data.skipped || [];
    const errors = data.errors || [];
    if (errors.length > 0 || skipped.length > 0) {
      batchErrors.classList.remove("hidden");
      let html = "";
      if (skipped.length > 0) {
        html += `<strong>Já processados e ignorados (${skipped.length}):</strong><ul>${skipped.map((g) => `<li>${g}</li>`).join("")}</ul>`;
      }
      if (errors.length > 0) {
        html += `<strong>Arquivos ignorados (${errors.length}):</strong><ul>${errors.map((e) => `<li>${e.filename}: ${e.error}</li>`).join("")}</ul>`;
      }
      batchErrors.innerHTML = html;
    } else {
      batchErrors.classList.add("hidden");
    }

    startBatchPolling(data.batch_id, data.total);
  } catch (error) {
    showNotice(error.message);
  }
}

csvForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitCsvBatch();
});

// --- Fila de processamento ---
function formatDateTime(isoString) {
  if (!isoString) return "-";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("pt-BR");
}

async function showErrorLog(jobId, filename) {
  errorLogFilename.textContent = filename;
  errorLogContent.textContent = "Carregando log...";
  errorLogDialog.showModal();
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    if (!response.ok) {
      throw new Error("Falha ao consultar o job.");
    }
    const data = await response.json();
    errorLogContent.textContent = data.error_log || data.message || "Nenhum log disponível.";
  } catch (error) {
    errorLogContent.textContent = `Não foi possível carregar o log: ${error.message}`;
  }
}

closeErrorLogButton.addEventListener("click", () => errorLogDialog.close());

function renderQueueTable(jobs) {
  if (jobs.length === 0) {
    queueTableBody.innerHTML = '<tr><td colspan="4" class="empty-state">Nenhum job na fila de processamento.</td></tr>';
    return;
  }

  queueTableBody.innerHTML = "";
  jobs.forEach((job) => {
    const row = document.createElement("tr");

    const fileCell = document.createElement("td");
    fileCell.textContent = job.filename;
    row.appendChild(fileCell);

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${job.status}`;
    badge.textContent = job.status;
    statusCell.appendChild(badge);
    row.appendChild(statusCell);

    const completedCell = document.createElement("td");
    completedCell.textContent = formatDateTime(job.completed_at);
    row.appendChild(completedCell);

    const logCell = document.createElement("td");
    if (job.status === "failed" && job.has_error_log) {
      const logButton = document.createElement("button");
      logButton.type = "button";
      logButton.className = "secondary-button log-button";
      logButton.textContent = "Ver log";
      logButton.addEventListener("click", () => showErrorLog(job.id, job.filename));
      logCell.appendChild(logButton);
    } else {
      logCell.textContent = "-";
    }
    row.appendChild(logCell);

    queueTableBody.appendChild(row);
  });
}

async function refreshQueue() {
  try {
    const response = await fetch("/api/jobs");
    if (!response.ok) {
      throw new Error("Falha ao consultar a fila de processamento.");
    }
    const data = await response.json();
    renderQueueTable(data.jobs || []);
  } catch (error) {
    queueTableBody.innerHTML = `<tr><td colspan="4" class="empty-state">${error.message}</td></tr>`;
  }
}

updateSourceProjectionPreview();

// --- MDT por bacia hidrográfica ---
function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function switchBaciaMethod(method) {
  methodButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.method === method));
  baciaMethodCoord.classList.toggle("hidden", method !== "coord");
  baciaMethodCode.classList.toggle("hidden", method !== "code");
  baciaMethodMap.classList.toggle("hidden", method !== "map");

  if (method === "map") {
    initBaciaMap();
  }
}

methodButtons.forEach((btn) => {
  btn.addEventListener("click", () => switchBaciaMethod(btn.dataset.method));
});

function showBaciaMessage(message) {
  baciaLookupMessage.textContent = message || "";
}

function showBaciaResult(uc, data) {
  selectedBaciaUc = uc;
  const areaText = data.upst_area != null ? `${data.upst_area.toFixed(2)} km²` : "não informada";
  baciaResultText.textContent = `Bacia UC ${uc} · Área de contribuição: ${areaText} · Tiles MDT na bacia: ${data.tile_count}`;
  baciaResult.classList.remove("hidden");
}

async function lookupByUc(uc) {
  showBaciaMessage("Buscando bacia...");
  baciaResult.classList.add("hidden");
  try {
    const response = await fetch(`/api/bacias/by-uc/${encodeURIComponent(uc)}`);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Bacia não encontrada.");
    }
    showBaciaMessage("");
    showBaciaResult(data.uc, data);
  } catch (error) {
    showBaciaMessage(error.message);
  }
}

async function lookupByPoint(lat, lon) {
  showBaciaMessage("Buscando bacia...");
  baciaResult.classList.add("hidden");
  try {
    const response = await fetch(`/api/bacias/by-point?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Nenhuma bacia encontrada para essa coordenada.");
    }
    showBaciaMessage("");
    showBaciaResult(data.uc, data);
  } catch (error) {
    showBaciaMessage(error.message);
  }
}

baciaLookupCoordButton.addEventListener("click", () => {
  const lat = parseFloat(baciaLat.value);
  const lon = parseFloat(baciaLon.value);
  if (Number.isNaN(lat) || Number.isNaN(lon)) {
    showBaciaMessage("Informe latitude e longitude válidas.");
    return;
  }
  lookupByPoint(lat, lon);
});

baciaLookupUcButton.addEventListener("click", () => {
  const uc = parseInt(baciaUcInput.value, 10);
  if (Number.isNaN(uc)) {
    showBaciaMessage("Informe um código UC válido.");
    return;
  }
  lookupByUc(uc);
});

// --- Mapa (Leaflet) ---
function baciaLayerStyle(selected) {
  return selected
    ? { color: "#23d18b", weight: 3, fillColor: "#23d18b", fillOpacity: 0.35 }
    : { color: "#4fa3d1", weight: 1, fillColor: "#4fa3d1", fillOpacity: 0.08 };
}

async function initBaciaMap() {
  if (baciaMap || typeof L === "undefined") {
    return;
  }

  baciaMap = L.map("baciaMap").setView([-22.4, -48.5], 6);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(baciaMap);

  try {
    const response = await fetch("/api/bacias/geojson");
    if (!response.ok) {
      throw new Error("Falha ao carregar as bacias no mapa.");
    }
    const geojson = await response.json();
    baciaGeojsonLayer = L.geoJSON(geojson, {
      style: () => baciaLayerStyle(false),
      onEachFeature: (feature, layer) => {
        layer.on("click", () => {
          if (selectedMapLayer) {
            selectedMapLayer.setStyle(baciaLayerStyle(false));
          }
          layer.setStyle(baciaLayerStyle(true));
          selectedMapLayer = layer;
          lookupByUc(feature.properties.uc);
        });
      },
    }).addTo(baciaMap);
  } catch (error) {
    showBaciaMessage(error.message);
  }
}

// --- Dialog de agendamento do merge ---
async function refreshExistingWarning() {
  const uc = selectedBaciaUc;
  const resolution = parseFloat(mdtBaciaResolution.value) || 1;
  try {
    const response = await fetch(`/api/mdt-bacia/check?uc=${encodeURIComponent(uc)}&resolution=${encodeURIComponent(resolution)}`);
    const data = await response.json();
    mdtBaciaFileExists = Boolean(data.exists);
    mdtBaciaExistingWarning.classList.toggle("hidden", !mdtBaciaFileExists);
    if (mdtBaciaFileExists) {
      mdtBaciaExistingPath.textContent = `${data.filename} (${data.path})`;
      confirmMdtBaciaButton.textContent = "Processar mesmo assim";
    } else {
      confirmMdtBaciaButton.textContent = "Agendar merge";
    }
  } catch (_) {
    /* silent */
  }
}

const debouncedRefreshExistingWarning = debounce(refreshExistingWarning, 400);
mdtBaciaResolution.addEventListener("input", debouncedRefreshExistingWarning);

function openMdtBaciaDialog(uc) {
  mdtBaciaUcLabel.textContent = uc;
  mdtBaciaResolution.value = "1";
  mdtBaciaSaveToServer.checked = false;
  mdtBaciaExistingWarning.classList.add("hidden");
  confirmMdtBaciaButton.textContent = "Agendar merge";
  refreshExistingWarning();
  mdtBaciaDialog.showModal();
}

baciaScheduleButton.addEventListener("click", () => {
  if (selectedBaciaUc == null) {
    return;
  }
  openMdtBaciaDialog(selectedBaciaUc);
});

cancelMdtBaciaButton.addEventListener("click", () => {
  mdtBaciaDialog.close();
});

async function submitMdtBaciaJob() {
  const uc = selectedBaciaUc;
  const resolution = parseFloat(mdtBaciaResolution.value) || 1;
  const saveToServer = mdtBaciaSaveToServer.checked;
  const approveOverwrite = mdtBaciaFileExists;

  const formData = new FormData();
  formData.set("uc", uc);
  formData.set("resolution", resolution);
  formData.set("save_to_server", saveToServer ? "true" : "false");
  formData.set("approve_overwrite", approveOverwrite ? "true" : "false");

  try {
    const response = await fetch("/api/mdt-bacia/jobs", { method: "POST", body: formData });
    const data = await response.json();

    if (response.status === 409 && data.reprocess_required) {
      mdtBaciaFileExists = true;
      mdtBaciaExistingWarning.classList.remove("hidden");
      mdtBaciaExistingPath.textContent = `${data.filename} (${data.path})`;
      confirmMdtBaciaButton.textContent = "Processar mesmo assim";
      return;
    }

    if (!response.ok) {
      throw new Error(data.detail || "Falha ao agendar o merge da bacia.");
    }

    mdtBaciaDialog.close();
    clearPollTimers();
    let message = data.message;
    if (data.missing_tiles && data.missing_tiles.length > 0) {
      message += ` (${data.missing_tiles.length} tile(s) não encontrado(s) no servidor)`;
    }
    showNotice(message);
    createJobCard(data);
    const timer = setInterval(() => pollJob(data.id), 2000);
    pollTimers.set(data.id, timer);
    pollJob(data.id);
  } catch (error) {
    showBaciaMessage(error.message);
  }
}

confirmMdtBaciaButton.addEventListener("click", () => {
  submitMdtBaciaJob();
});
