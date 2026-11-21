function makeStarterCode(growthSeconds) {
  return `# Plot coordinates are 1-based; home is the center plot (2, 2).
home()
plant()
move(1, 0)
plant()
move(0, 1)
plant()
move(-1, 0)
plant()
home()

log("Planted! Wait at least ${growthSeconds} seconds, then harvest these plots.")
`;
}

const editor = document.querySelector("#code");
const codeHighlight = document.querySelector("#code-highlight");
const runButton = document.querySelector("#run-button");
const stopButton = document.querySelector("#stop-button");
const testButton = document.querySelector("#test-button");
const confirmWrap = document.querySelector("#hardware-confirm-wrap");
const confirmCheckbox = document.querySelector("#hardware-confirm");
const consoleOutput = document.querySelector("#console-output");
const svgNamespace = "http://www.w3.org/2000/svg";
const FIELD_GRID_CELLS = 3;
const FIELD_SECTION_MM = 200;
const FIELD_LENGTH_MM = FIELD_GRID_CELLS * FIELD_SECTION_MM;
const API_FUNCTIONS = new Set([
  "move",
  "speed",
  "home",
  "plant",
  "harvest",
  "get_position",
  "wait",
  "log",
]);
let hardwareMode = false;
let running = false;
let previewMode = false;
let starterInitialized = false;
let previewPlayback = null;

editor.value = localStorage.getItem("gantry-code") || "";
editor.addEventListener("input", () => {
  localStorage.setItem("gantry-code", editor.value);
  updateCodeHighlight();
});
editor.addEventListener("scroll", syncCodeHighlightScroll);
updateCodeHighlight();
document.querySelector("#clear-button").addEventListener("click", () => {
  consoleOutput.replaceChildren();
});

function escapeCodeHtml(value) {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function updateCodeHighlight() {
  codeHighlight.innerHTML = highlightApiFunctions(editor.value);
  syncCodeHighlightScroll();
}

function syncCodeHighlightScroll() {
  codeHighlight.scrollTop = editor.scrollTop;
  codeHighlight.scrollLeft = editor.scrollLeft;
}

function highlightApiFunctions(source) {
  let html = "";
  let index = 0;
  while (index < source.length) {
    const character = source[index];
    if (character === "#") {
      const end = source.indexOf("\n", index);
      const commentEnd = end === -1 ? source.length : end;
      html += escapeCodeHtml(source.slice(index, commentEnd));
      index = commentEnd;
      continue;
    }
    if (character === "'" || character === '"') {
      const delimiter = character.repeat(source.startsWith(character.repeat(3), index) ? 3 : 1);
      let end = index + delimiter.length;
      while (end < source.length) {
        if (source[end] === "\\") {
          end += 2;
        } else if (source.startsWith(delimiter, end)) {
          end += delimiter.length;
          break;
        } else {
          end += 1;
        }
      }
      html += escapeCodeHtml(source.slice(index, end));
      index = end;
      continue;
    }
    if (/[A-Za-z_]/.test(character)) {
      let end = index + 1;
      while (end < source.length && /[A-Za-z0-9_]/.test(source[end])) end += 1;
      const identifier = source.slice(index, end);
      const followsCall = /^\s*\(/.test(source.slice(end));
      html += API_FUNCTIONS.has(identifier) && followsCall
        ? `<span class="syntax-api-function">${identifier}</span>`
        : identifier;
      index = end;
      continue;
    }
    html += escapeCodeHtml(character);
    index += 1;
  }
  return html;
}

function addOutput(message, kind = "") {
  const line = document.createElement("p");
  line.textContent = message;
  if (kind) line.className = kind;
  consoleOutput.append(line);
  consoleOutput.scrollTop = consoleOutput.scrollHeight;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error || `Request failed (${response.status})`);
    error.body = body;
    throw error;
  }
  return body;
}

function setRunning(value) {
  running = value;
  runButton.disabled = value;
  testButton.disabled = value;
  stopButton.disabled = !value;
}

function showPosition(position) {
  if (!Array.isArray(position)) return;
  const x = Number(position[0]);
  const y = Number(position[1]);
  const label = x === 2 && y === 2
    ? "Home (plot 2, 2)"
    : `Column ${Number(x.toFixed(1))} · row ${Number(y.toFixed(1))}`;
  document.querySelector("#position").textContent = label;
}

function startPreviewPlayback(status) {
  const trail = status?.trail;
  if (!previewMode || !Array.isArray(trail) || trail.length < 2) {
    previewPlayback = null;
    return;
  }
  previewPlayback = {
    status,
    trail,
    startedAt: performance.now(),
    duration: Math.min(
      8000,
      Math.max(700, (trail.length - 1) * 450 * 100 / Math.max(1, Number(status.speed_percent) || 100)),
    ),
  };
  const animate = (now) => {
    if (!previewPlayback) return;
    renderField(previewPlayback.status, now);
    if (now - previewPlayback.startedAt < previewPlayback.duration) {
      requestAnimationFrame(animate);
    } else {
      previewPlayback = null;
      renderField(status);
    }
  };
  requestAnimationFrame(animate);
}

function svgElement(name, attributes = {}, text = "") {
  const element = document.createElementNS(svgNamespace, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  if (text) element.textContent = text;
  return element;
}

function renderField(status, animationTime = performance.now()) {
  const grid = document.querySelector("#field-grid");
  const trailLayer = document.querySelector("#field-trail");
  const cropsLayer = document.querySelector("#field-crops");
  const targetLayer = document.querySelector("#field-target");
  const toolLayer = document.querySelector("#field-tool");
  grid.replaceChildren();
  trailLayer.replaceChildren();
  cropsLayer.replaceChildren();
  targetLayer.replaceChildren();
  toolLayer.replaceChildren();

  const plot = { x: 52, y: 22, width: 290, height: 290 };
  const mapX = (x) => plot.x + Math.max(0, Math.min(1, (x + FIELD_LENGTH_MM / 2) / FIELD_LENGTH_MM)) * plot.width;
  const mapY = (y) => plot.y + (1 - Math.max(0, Math.min(1, (y + FIELD_LENGTH_MM / 2) / FIELD_LENGTH_MM))) * plot.height;
  const plotCoordinateToMm = (coordinate) => (coordinate - 2) * FIELD_SECTION_MM;
  const mapPlotX = (column) => mapX(plotCoordinateToMm(column));
  const mapPlotY = (row) => mapY(plotCoordinateToMm(row));
  const labelNumber = (value) => Number(value.toFixed(1)).toString();

  for (let row = 0; row < FIELD_GRID_CELLS; row += 1) {
    for (let column = 0; column < FIELD_GRID_CELLS; column += 1) {
      const cellX = plot.x + plot.width * column / FIELD_GRID_CELLS;
      const cellY = plot.y + plot.height * row / FIELD_GRID_CELLS;
      const cellWidth = plot.width / FIELD_GRID_CELLS;
      const cellHeight = plot.height / FIELD_GRID_CELLS;
      const bedX = cellX + 4;
      const bedY = cellY + 4;
      const bedWidth = cellWidth - 8;
      const bedHeight = cellHeight - 8;
      const soilX = bedX + 4;
      const soilY = bedY + 4;
      const soilWidth = bedWidth - 8;
      const soilHeight = bedHeight - 8;
      const plotGroup = svgElement("g", { class: "field-plot-group" });
      plotGroup.append(
        svgElement("rect", {
          x: bedX + 1, y: bedY + 3, width: bedWidth, height: bedHeight, rx: 9,
          class: "field-plot-shadow",
        }),
        svgElement("rect", {
          x: bedX, y: bedY, width: bedWidth, height: bedHeight, rx: 9,
          class: "field-plot-frame",
        }),
        svgElement("rect", {
          x: soilX, y: soilY, width: soilWidth, height: soilHeight, rx: 6,
          class: "field-plot-soil",
        }),
      );
      for (let furrow = 1; furrow <= 3; furrow += 1) {
        const furrowY = soilY + soilHeight * furrow / 4;
        plotGroup.append(svgElement("path", {
          d: `M ${soilX + 5} ${furrowY} Q ${soilX + soilWidth / 2} ${furrowY - 2} ${soilX + soilWidth - 5} ${furrowY}`,
          class: "field-plot-furrow",
        }));
      }
      plotGroup.append(
        svgElement("rect", {
          x: bedX + 7, y: bedY + 6, width: 38, height: 14, rx: 6,
          class: "field-plot-label-badge",
        }),
        svgElement("text", {
          x: bedX + 26, y: bedY + 16, "text-anchor": "middle",
          class: "field-plot-label",
        }, `PLOT ${row * FIELD_GRID_CELLS + column + 1}`),
      );
      grid.append(plotGroup);
    }
  }
  for (let index = 1; index <= FIELD_GRID_CELLS; index += 1) {
    const x = mapPlotX(index);
    const y = mapPlotY(index);
    grid.append(
      svgElement("text", { x, y: plot.y + plot.height + 19, "text-anchor": "middle", class: "field-tick" }, String(index)),
      svgElement("text", { x: plot.x - 9, y: y + 3, "text-anchor": "end", class: "field-tick" }, String(index)),
    );
  }
  grid.append(
    svgElement("text", { x: plot.x + plot.width / 2, y: 361, "text-anchor": "middle", class: "field-axis-label" }, "X · plot column"),
    svgElement("text", { x: 13, y: plot.y + plot.height / 2, "text-anchor": "middle", transform: `rotate(-90 13 ${plot.y + plot.height / 2})`, class: "field-axis-label" }, "Y · plot row"),
  );
  const trail = Array.isArray(status.trail) ? status.trail : [];
  if (trail.length > 1) {
    const points = trail.map(([x, y]) => `${mapPlotX(Number(x))},${mapPlotY(Number(y))}`).join(" ");
    trailLayer.append(svgElement("polyline", { points, class: "field-trail-line" }));
    for (const [x, y] of trail.slice(1, -1)) {
      trailLayer.append(svgElement("circle", {
        cx: mapPlotX(Number(x)), cy: mapPlotY(Number(y)), r: 2.5, class: "field-trail-point",
      }));
    }
  }

  for (const crop of status.crops || []) {
    const x = mapPlotX(Number(crop.x));
    const y = mapPlotY(Number(crop.y));
    const state = crop.ready ? "ready" : "growing";
    const marker = svgElement("g", { "aria-label": crop.ready ? "Ready to harvest" : "Growing" });
    marker.append(
      svgElement("circle", { cx: x, cy: y, r: 11, class: `crop-marker ${state}` }),
      svgElement("line", { x1: x, y1: y + 5, x2: x, y2: y - 5, class: "crop-stem" }),
      svgElement("ellipse", {
        cx: x - 3, cy: y - 2, rx: 3.5, ry: 2, transform: `rotate(-30 ${x - 3} ${y - 2})`, class: "crop-leaf",
      }),
      svgElement("ellipse", {
        cx: x + 3, cy: y - 4, rx: 3.5, ry: 2, transform: `rotate(30 ${x + 3} ${y - 4})`, class: "crop-leaf",
      }),
      ...(crop.ready ? [svgElement("circle", { cx: x, cy: y - 8, r: 3, class: "crop-bloom" })] : []),
      svgElement("title", {}, crop.ready
        ? `Ready to harvest at (${crop.x}, ${crop.y})`
        : `Growing at (${crop.x}, ${crop.y}); ${Number(crop.remaining_seconds).toFixed(1)}s left`),
    );
    cropsLayer.append(marker);
  }

  let position = status.position.map(Number);
  if (previewMode && previewPlayback) {
    const elapsed = Math.min(1, (animationTime - previewPlayback.startedAt) / previewPlayback.duration);
    const segment = elapsed * (previewPlayback.trail.length - 1);
    const segmentIndex = Math.min(previewPlayback.trail.length - 2, Math.floor(segment));
    const segmentProgress = Math.min(1, segment - segmentIndex);
    const from = previewPlayback.trail[segmentIndex].map(Number);
    const to = previewPlayback.trail[segmentIndex + 1].map(Number);
    position = [
      from[0] + (to[0] - from[0]) * segmentProgress,
      from[1] + (to[1] - from[1]) * segmentProgress,
    ];
  }
  const fieldX = position[0].toFixed(1);
  const fieldY = position[1].toFixed(1);
  document.querySelector("#field-position").innerHTML = `X ${fieldX} <span>/</span> Y ${fieldY} plots`;
  document.querySelector("#field-speed").textContent =
    Number.isInteger(status.speed_percent) ? `${status.speed_percent}%` : "Default";
  if (Array.isArray(status.target_position)) {
    const targetX = mapPlotX(Number(status.target_position[0]));
    const targetY = mapPlotY(Number(status.target_position[1]));
    targetLayer.append(
      svgElement("line", {
        x1: mapPlotX(position[0]), y1: mapPlotY(position[1]), x2: targetX, y2: targetY,
        class: "field-target-line",
      }),
      svgElement("circle", { cx: targetX, cy: targetY, r: 11, class: "field-target-ring" }),
    );
  }
  const toolX = mapPlotX(position[0]);
  const toolY = mapPlotY(position[1]);
  toolLayer.append(
    svgElement("circle", { cx: toolX, cy: toolY, r: 8, class: "field-tool-ring" }),
    svgElement("circle", { cx: toolX, cy: toolY, r: 2.5, class: "field-tool-core" }),
    svgElement("line", { x1: toolX - 13, y1: toolY, x2: toolX + 13, y2: toolY, class: "field-tool-cross" }),
    svgElement("line", { x1: toolX, y1: toolY - 13, x2: toolX, y2: toolY + 13, class: "field-tool-cross" }),
    svgElement("title", {}, `Gantry at plot column ${position[0]}, row ${position[1]}`),
  );

  const cropCount = Number(status.crop_count || 0);
  const readyCount = Number(status.ready_count || 0);
  const growingCount = Number(status.growing_count || 0);
  document.querySelector("#crop-count").textContent = `${cropCount} planted`;
  document.querySelector("#crop-summary").textContent = cropCount
    ? `${readyCount} ready · ${growingCount} growing${cropCount > (status.crops || []).length ? " · map shows recent crops" : ""}`
    : "No crops in the field";

  const activity = status.activity || "Idle";
  const activityLabel = document.querySelector("#activity-label");
  activityLabel.textContent = previewMode ? `Test · ${activity}` : activity;
  activityLabel.classList.toggle("active", Boolean(status.running));
  document.querySelector("#field-action").textContent =
    previewMode ? `Simulation · ${activity}` : activity;
  const targetLabel = Array.isArray(status.target_position)
    ? status.target_position[0] === 2 && status.target_position[1] === 2
      ? "Destination home"
      : `Destination column ${Number(status.target_position[0]).toFixed(1)} · row ${Number(status.target_position[1]).toFixed(1)}`
    : activity === "Waiting"
      ? "Growth time is passing"
      : status.running ? "Program in progress" : "Waiting for a program";
  document.querySelector("#field-target-label").textContent = previewMode
    ? `Simulation only · ${targetLabel}`
    : targetLabel;
  document.querySelector("#map-note").textContent = previewMode
    ? "Test Code is simulation-only. No commands are sent to the Arduino."
    : "Each plot is 20 cm × 20 cm. Home is the center cell; moves use integer plot indices and safe limits remain in mm.";
}

async function refreshStatus() {
  try {
    const actualStatus = await request("/api/status");
    const status = previewMode ? await request("/api/preview") : actualStatus;
    hardwareMode = actualStatus.mode === "arduino";
    const dot = document.querySelector("#status-dot");
    dot.className = `status-dot${hardwareMode ? " hardware" : ""}`;
    document.querySelector("#mode-label").textContent = actualStatus.mode_description;
    const homeHint = hardwareMode && !actualStatus.homed ? "Home required · " : "";
    document.querySelector("#limits").textContent =
      `${homeHint}X ${actualStatus.limits.x[0]}–${actualStatus.limits.x[1]} mm · ` +
      `Y ${actualStatus.limits.y[0]}–${actualStatus.limits.y[1]} mm · Growth ${actualStatus.growth_seconds} s`;
    if (!starterInitialized) {
      if (!editor.value) editor.value = makeStarterCode(actualStatus.growth_seconds);
      updateCodeHighlight();
      starterInitialized = true;
    }
    showPosition(actualStatus.position);
    renderField(status);
    confirmWrap.hidden = !hardwareMode;
    runButton.disabled = running || (hardwareMode && !confirmCheckbox.checked);
  } catch (error) {
    const dot = document.querySelector("#status-dot");
    dot.className = "status-dot error";
    document.querySelector("#mode-label").textContent = "Controller unavailable";
    if (!running) runButton.disabled = true;
    addOutput(error.message, "console-error");
  }
}

async function pollStatus() {
  await refreshStatus();
  window.setTimeout(pollStatus, running ? 250 : 1000);
}

async function loadDocs() {
  try {
    const { functions } = await request("/api/docs");
    const list = document.querySelector("#api-list");
    list.replaceChildren();
    for (const fn of functions) {
      const item = document.createElement("li");
      const signature = document.createElement("code");
      const description = document.createElement("p");
      signature.textContent = fn.name;
      description.textContent = fn.description;
      item.append(signature, description);
      list.append(item);
    }
  } catch (error) {
    document.querySelector("#api-list").textContent = error.message;
  }
}

confirmCheckbox.addEventListener("change", () => {
  runButton.disabled = running || (hardwareMode && !confirmCheckbox.checked);
});

runButton.addEventListener("click", async () => {
  if (running) return;
  previewMode = false;
  previewPlayback = null;
  setRunning(true);
  addOutput("> Running program…");
  try {
    const result = await request("/api/run", {
      method: "POST",
      body: JSON.stringify({
        code: editor.value,
        confirm_hardware: confirmCheckbox.checked,
      }),
    });
    for (const line of result.logs) addOutput(line);
    showPosition(result.position);
    addOutput("Program finished.");
  } catch (error) {
    for (const line of error.body?.logs || []) addOutput(line);
    if (error.body?.position) showPosition(error.body.position);
    addOutput(error.message, "console-error");
  } finally {
    setRunning(false);
    await refreshStatus();
  }
});

testButton.addEventListener("click", async () => {
  if (running) return;
  previewMode = true;
  previewPlayback = null;
  setRunning(true);
  addOutput("> Simulating program; Arduino is not connected to this run.");
  try {
    const result = await request("/api/test", {
      method: "POST",
      body: JSON.stringify({ code: editor.value }),
    });
    for (const line of result.logs) addOutput(line);
    startPreviewPlayback(result.status);
    addOutput("Simulation finished; no Arduino commands were sent.");
  } catch (error) {
    for (const line of error.body?.logs || []) addOutput(line);
    if (error.body?.status) startPreviewPlayback(error.body.status);
    addOutput(error.message, "console-error");
  } finally {
    setRunning(false);
    await refreshStatus();
  }
});

stopButton.addEventListener("click", async () => {
  try {
    const path = previewMode ? "/api/test/stop" : "/api/stop";
    const result = await request(path, { method: "POST", body: "{}" });
    addOutput(result.message);
  } catch (error) {
    addOutput(error.message, "console-error");
  }
});

loadDocs();
pollStatus();
