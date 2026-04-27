/* ============================================================
   app.js — OS Performance Dashboard frontend logic
   WebSocket client, Chart.js graphs, process table, controls
   ============================================================ */

(() => {
  "use strict";

  // ── State ──────────────────────────────────────────────────

  let ws = null;
  let paused = false;
  let sortKey = "cpu";
  let sortAsc = false;
  let filterText = "";
  let latestProcesses = [];

  // Latest payload from backend; used with rAF batching
  let latestPayload = null;
  let renderScheduled = false;

  /** Map of PID → <tr> DOM node for diff-based table updates. */
  let rowMap = new Map();

  // ── DOM refs ───────────────────────────────────────────────

  const $ = (id) => document.getElementById(id);

  const statusBadge = $("status-badge");
  const connIndicator = $("connection-indicator");
  const intervalSelect = $("interval-select");
  const pauseBtn = $("pause-btn");
  const processFilter = $("process-filter");
  const processTbody = $("process-tbody");
  const coreBarsContainer = $("core-bars");
  const memBar = $("mem-bar");
  const memText = $("mem-text");
  const alertBanner = $("alert-banner");

  const metricCpu = $("metric-cpu");
  const metricMem = $("metric-mem");
  const metricMemPressure = $("metric-mem-pressure");
  const metricDiskRead = $("metric-disk-read");
  const metricDiskWrite = $("metric-disk-write");
  const metricNetUp = $("metric-net-up");
  const metricNetDown = $("metric-net-down");
  const metricLoadOne = $("metric-load-one");
  const metricLoadFive = $("metric-load-five");
  const metricLoadFifteen = $("metric-load-fifteen");

  // System info value elements
  const infoEls = {
    os: $("info-os"),
    cpu: $("info-cpu"),
    pcores: $("info-pcores"),
    lcores: $("info-lcores"),
    ram: $("info-ram"),
    disk: $("info-disk"),
    uptime: $("info-uptime"),
    procs: $("info-procs"),
  };

  // ── Chart.js setup ────────────────────────────────────────

  const chartDefaults = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    scales: {
      x: {
        display: false,
      },
      y: {
        min: 0,
        max: 100,
        ticks: {
          color: "#555e6c",
          font: { size: 23, family: "Inter" },
          callback: (v) => v + "%",
          stepSize: 25,
        },
        grid: { color: "rgba(255,255,255,0.04)" },
        border: { display: false },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "rgba(17,22,33,0.92)",
        titleFont: { family: "Inter", size: 23 },
        bodyFont: { family: "Inter", size: 23 },
        padding: 10,
        cornerRadius: 8,
        callbacks: {
          label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}%`,
        },
      },
    },
    elements: {
      point: { radius: 0, hoverRadius: 4 },
      line: { tension: 0.35, borderWidth: 2 },
    },
  };

  const HISTORY_POINTS = 60;
  const emptyLabels = Array.from({ length: HISTORY_POINTS }, () => "");

  function makeSlidingDataset(label, color, fillColor) {
    return {
      labels: [...emptyLabels],
      datasets: [
        {
          label,
          data: Array(HISTORY_POINTS).fill(null),
          borderColor: color,
          backgroundColor: fillColor,
          fill: true,
        },
      ],
    };
  }

  const cpuChart = new Chart($("cpu-chart"), {
    type: "line",
    data: makeSlidingDataset("CPU", "#58a6ff", "rgba(88,166,255,0.08)"),
    options: { ...chartDefaults },
  });

  const memChart = new Chart($("memory-chart"), {
    type: "line",
    data: makeSlidingDataset("Memory", "#bc8cff", "rgba(188,140,255,0.08)"),
    options: { ...chartDefaults },
  });

  const memPressureChart = new Chart($("mem-pressure-chart"), {
    type: "line",
    data: makeSlidingDataset("Mem pressure", "#3fb950", "rgba(63,185,80,0.08)"),
    options: { ...chartDefaults },
  });

  const diskChart = new Chart($("disk-chart"), {
    type: "line",
    data: {
      labels: [...emptyLabels],
      datasets: [
        {
          label: "Read MB/s",
          data: Array(HISTORY_POINTS).fill(null),
          borderColor: "#58a6ff",
          backgroundColor: "rgba(88,166,255,0.08)",
          fill: true,
        },
        {
          label: "Write MB/s",
          data: Array(HISTORY_POINTS).fill(null),
          borderColor: "#bc8cff",
          backgroundColor: "rgba(188,140,255,0.08)",
          fill: true,
        },
      ],
    },
    options: { ...chartDefaults, scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, min: 0, suggestedMax: 10, max: undefined } } },
  });

  const netChart = new Chart($("network-chart"), {
    type: "line",
    data: {
      labels: [...emptyLabels],
      datasets: [
        {
          label: "Up MB/s",
          data: Array(HISTORY_POINTS).fill(null),
          borderColor: "#3fb950",
          backgroundColor: "rgba(63,185,80,0.08)",
          fill: true,
        },
        {
          label: "Down MB/s",
          data: Array(HISTORY_POINTS).fill(null),
          borderColor: "#d29922",
          backgroundColor: "rgba(210,153,34,0.08)",
          fill: true,
        },
      ],
    },
    options: { ...chartDefaults, scales: { ...chartDefaults.scales, y: { ...chartDefaults.scales.y, min: 0, suggestedMax: 10, max: undefined } } },
  });

  const loadChart = new Chart($("load-chart"), {
    type: "line",
    data: {
      labels: [...emptyLabels],
      datasets: [
        {
          label: "1m",
          data: Array(HISTORY_POINTS).fill(null),
          borderColor: "#58a6ff",
          backgroundColor: "rgba(88,166,255,0.08)",
          fill: true,
        },
        {
          label: "5m",
          data: Array(HISTORY_POINTS).fill(null),
          borderColor: "#bc8cff",
          backgroundColor: "rgba(188,140,255,0.08)",
          fill: true,
        },
        {
          label: "15m",
          data: Array(HISTORY_POINTS).fill(null),
          borderColor: "#d29922",
          backgroundColor: "rgba(210,153,34,0.08)",
          fill: true,
        },
      ],
    },
    options: {
      ...chartDefaults,
      scales: {
        ...chartDefaults.scales,
        y: {
          ...chartDefaults.scales.y,
          min: 0,
          suggestedMax: 10,
          max: undefined,
          ticks: {
            ...chartDefaults.scales.y.ticks,
            callback: (v) => v.toFixed(1),
          },
        },
      },
    },
  });

  // ── WebSocket ─────────────────────────────────────────────

  let reconnectTimer = null;

  function connect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
      connIndicator.classList.remove("offline");
      connIndicator.classList.add("online");
      // send current interval setting
      ws.send(JSON.stringify({ interval: parseInt(intervalSelect.value, 10) }));
    };

    ws.onclose = () => {
      connIndicator.classList.remove("online");
      connIndicator.classList.add("offline");
      if (!reconnectTimer) {
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          connect();
        }, 2000); // reconnect
      }
    };

    ws.onerror = () => ws.close();

    ws.onmessage = (event) => {
      try {
        latestPayload = JSON.parse(event.data);
        if (!renderScheduled) {
          renderScheduled = true;
          requestAnimationFrame(flushFrame);
        }
      } catch (e) {
        console.error("Failed to parse WebSocket message:", e);
      }
    };
  }

  function flushFrame() {
    renderScheduled = false;
    if (!latestPayload) return;
    handleUpdate(latestPayload);
  }

  // ── Handle incoming data ──────────────────────────────────

  let initializedHistory = false;

  function handleUpdate(data) {
    if (!initializedHistory && data.history) {
      const replaySliding = (chart, series, datasetIdx = 0) => {
        if (!series || !series.length) return;
        let d = series.slice(-HISTORY_POINTS);
        while (d.length < HISTORY_POINTS) d.unshift(null);
        chart.data.datasets[datasetIdx].data = d;
        chart.update();
      };

      replaySliding(cpuChart,  data.history.cpu);
      replaySliding(memChart,  data.history.memory);

      // Disk — two datasets (read + write)
      if (data.history.disk) {
        replaySliding(diskChart, data.history.disk.read,  0);
        replaySliding(diskChart, data.history.disk.write, 1);
      }
      // Network — two datasets (up + down)
      if (data.history.network) {
        replaySliding(netChart, data.history.network.up,   0);
        replaySliding(netChart, data.history.network.down, 1);
      }
      // Load — three datasets (1m, 5m, 15m)
      if (data.history.load) {
        replaySliding(loadChart, data.history.load.one, 0);
        replaySliding(loadChart, data.history.load.five, 1);
        replaySliding(loadChart, data.history.load.fifteen, 2);
      }

      initializedHistory = true;
    }

    // Always update status badge and alerts (critical should show even when paused)
    updateStatusBadge(data.status, data.alerts || []);
    renderAlerts(data.alerts || []);

    // Store latest processes regardless (so unpausing shows fresh data)
    latestProcesses = data.processes || [];

    if (paused) return;

    updateSystemInfo(data.system_info);
    updateCpuChart(data.cpu, data.history);
    updateCpuStats(data.cpu);
    updateMemChart(data.memory, data.history);
    updateMemPressureChart(data.memory);
    updateDiskChart(data.disk);
    updateNetworkChart(data.network);
    updateLoadChart(data.load);
    updateCoreBars(data.cpu.per_core);
    updateMemSummary(data.memory);
    updateSyncPanel(data.sync);
    updatePartitions(data.system_info?.partitions);
    updateHealthGauge(data.health);
    updateHealthBreakdown(data.health);
    renderProcessTable();
  }

  // ── System info ───────────────────────────────────────────

  function updateSystemInfo(info) {
    if (!info) return;
    infoEls.os.textContent = info.os;
    infoEls.cpu.textContent = info.cpu_model;
    infoEls.pcores.textContent = info.physical_cores;
    infoEls.lcores.textContent = info.logical_threads;
    infoEls.ram.textContent = info.total_ram;
    infoEls.disk.textContent = info.total_disk;
    infoEls.uptime.textContent = info.uptime;
    infoEls.procs.textContent = info.running_processes;
  }

  // ── Status badge ──────────────────────────────────────────

  function updateStatusBadge(status, alerts) {
    statusBadge.textContent = status;
    statusBadge.className = "badge";
    if (status === "Critical") statusBadge.classList.add("badge-critical");
    else if (status === "High Load") statusBadge.classList.add("badge-warning");
    else statusBadge.classList.add("badge-normal");

    const header = document.getElementById("header");
    header.classList.remove("header-stress-high", "header-stress-critical");
    const hasCritical = (alerts || []).some((a) => a.level === "critical");
    const hasWarning = (alerts || []).some((a) => a.level === "warning");
    if (hasCritical) header.classList.add("header-stress-critical");
    else if (hasWarning) header.classList.add("header-stress-high");
  }

  // ── Alert rendering (Fix 1) ────────────────────────────────

  const SOURCE_ICONS = {
    cpu: "🖥", memory: "🧠", disk: "💾", network: "🌐", load: "📈"
  };

  function renderAlerts(alerts) {
    if (!alerts || alerts.length === 0) {
      alertBanner.classList.add("hidden");
      alertBanner.innerHTML = "";
      return;
    }
    alertBanner.classList.remove("hidden");
    alertBanner.innerHTML = alerts
      .map(a => {
        const icon = SOURCE_ICONS[a.source] || "⚠";
        return `<span class="alert-chip ${a.level}">${icon} ${a.message}</span>`;
      })
      .join("");
  }

  // ── CPU Stats (Fix 3) ──────────────────────────────────────

  function updateCpuStats(cpu) {
    const ctx  = $("stat-ctx");
    const irq  = $("stat-irq");
    const wait = $("stat-iowait");
    if (ctx)  ctx.textContent  = (cpu.ctx_switches_per_sec || 0).toLocaleString() + "/s";
    if (irq)  irq.textContent  = (cpu.interrupts_per_sec   || 0).toLocaleString() + "/s";
    if (wait) wait.textContent = (cpu.iowait || 0).toFixed(1) + "%";
  }

  // ── Charts ────────────────────────────────────────────────

  function _pushSlidingPoint(dataset, value) {
    const data = dataset.data;
    if (data.length >= HISTORY_POINTS) {
      data.shift();
    }
    data.push(value);
  }

  function updateCpuChart(cpu, history) {
    const series = history && history.cpu && history.cpu.length ? history.cpu : [];
    const latest = series.length ? series[series.length - 1] : cpu.overall;
    _pushSlidingPoint(cpuChart.data.datasets[0], latest);
    cpuChart.update("none");
  }

  function updateMemChart(memory, history) {
    const series = history && history.memory && history.memory.length ? history.memory : [];
    const latest = series.length ? series[series.length - 1] : memory.percent;
    _pushSlidingPoint(memChart.data.datasets[0], latest);
    memChart.update("none");
  }

  function updateMemPressureChart(memory) {
    _pushSlidingPoint(memPressureChart.data.datasets[0], memory.pressure ?? memory.percent);
    memPressureChart.update("none");
  }

  function updateDiskChart(disk) {
    _pushSlidingPoint(diskChart.data.datasets[0], disk.read_mb_s || 0);
    _pushSlidingPoint(diskChart.data.datasets[1], disk.write_mb_s || 0);
    diskChart.update("none");
  }

  function updateNetworkChart(network) {
    _pushSlidingPoint(netChart.data.datasets[0], network.up_mb_s || 0);
    _pushSlidingPoint(netChart.data.datasets[1], network.down_mb_s || 0);
    netChart.update("none");
  }

  function updateLoadChart(load) {
    _pushSlidingPoint(loadChart.data.datasets[0], load.one || 0);
    _pushSlidingPoint(loadChart.data.datasets[1], load.five || 0);
    _pushSlidingPoint(loadChart.data.datasets[2], load.fifteen || 0);
    loadChart.update("none");
  }

  // ── Per-core bars ─────────────────────────────────────────

  function updateCoreBars(perCore) {
    if (!perCore || !perCore.length) return;

    // Build HTML if core count changed
    if (coreBarsContainer.children.length !== perCore.length) {
      coreBarsContainer.innerHTML = perCore
        .map(
          (_, i) => `
        <div class="core-row">
          <span class="core-label">Core ${i}</span>
          <div class="core-track"><div class="core-fill" id="core-fill-${i}"></div></div>
          <span class="core-pct" id="core-pct-${i}">0%</span>
        </div>`
        )
        .join("");
    }

    perCore.forEach((pct, i) => {
      const fill = $(`core-fill-${i}`);
      const pctEl = $(`core-pct-${i}`);
      if (!fill) return;
      fill.style.width = `${pct}%`;
      fill.className = "core-fill";
      if (pct > 90) fill.classList.add("danger");
      else if (pct > 70) fill.classList.add("warn");
      pctEl.textContent = `${pct.toFixed(0)}%`;
    });
  }

  // ── Memory summary bar ───────────────────────────────────

  function updateMemSummary(mem) {
    memBar.style.width = `${mem.percent}%`;

    // Color code the bar
    if (mem.percent > 85) {
      memBar.style.background = `linear-gradient(90deg, var(--red), #da3633)`;
    } else if (mem.percent > 70) {
      memBar.style.background = `linear-gradient(90deg, var(--yellow), #e3b341)`;
    } else {
      memBar.style.background = `linear-gradient(90deg, var(--chart-mem), #8957e5)`;
    }

    memText.textContent = `${mem.used} / ${mem.total} (${mem.percent.toFixed(1)}%)`;

    metricCpu.textContent = `${(latestPayload?.cpu?.overall ?? 0).toFixed(1)}%`;
    metricMem.textContent = `${mem.percent.toFixed(1)}%`;
    const pressure = mem.pressure ?? mem.percent;
    metricMemPressure.textContent = `${pressure.toFixed(1)}%`;

    if (latestPayload?.disk) {
      metricDiskRead.textContent = `${(latestPayload.disk.read_mb_s || 0).toFixed(2)} MB/s`;
      metricDiskWrite.textContent = `${(latestPayload.disk.write_mb_s || 0).toFixed(2)} MB/s`;
    }
    if (latestPayload?.network) {
      metricNetUp.textContent = `${(latestPayload.network.up_mb_s || 0).toFixed(2)} MB/s`;
      metricNetDown.textContent = `${(latestPayload.network.down_mb_s || 0).toFixed(2)} MB/s`;
    }
    if (latestPayload?.load) {
      metricLoadOne.textContent = `${(latestPayload.load.one || 0).toFixed(2)}`;
      metricLoadFive.textContent = `${(latestPayload.load.five || 0).toFixed(2)}`;
      metricLoadFifteen.textContent = `${(latestPayload.load.fifteen || 0).toFixed(2)}`;
    }

    // Memory detail (Fix 7)
    if ($("mem-buffers")) $("mem-buffers").textContent = formatBytes(mem.buffers || 0);
    if ($("mem-cached"))  $("mem-cached").textContent  = formatBytes(mem.cached  || 0);
    if ($("mem-swapin"))  $("mem-swapin").textContent  = (mem.swap_in_rate  || 0).toFixed(1) + " pg/s";
    if ($("mem-swapout")) $("mem-swapout").textContent = (mem.swap_out_rate || 0).toFixed(1) + " pg/s";
  }

  // ── Process table (diff-based) ───────────────────────────

  /**
   * Compute the row CSS class based on CPU usage.
   */
  function _rowClass(cpu) {
    if (cpu > 80) return "row-danger";
    if (cpu > 50) return "row-warn";
    return "";
  }

  /**
   * Create a <tr> element for a process.
   */
  function _createRow(p) {
    const tr = document.createElement("tr");
    tr.dataset.pid = p.pid;
    tr.className = _rowClass(p.cpu);
    const riskyClass = p.risky ? "kill-btn--risky" : "";
    const riskyAttr = p.risky ? 'data-risky="1"' : '';
    const rssStr     = formatBytes(p.rss || 0);
    const stateClass = p.status === "zombie" ? "state-zombie"
                     : p.status === "disk-sleep" ? "state-dstate" : "";
    tr.innerHTML = `
      <td>${p.pid}</td>
      <td>${escapeHtml(p.name)}${p.risky ? ' <span class="risky-badge">🔴 sys</span>' : ''}</td>
      <td class="proc-type">${p.type || "user"}</td>
      <td class="cpu-cell">${p.cpu.toFixed(1)}</td>
      <td class="mem-cell">${p.memory.toFixed(1)}</td>
      <td>${rssStr}</td>
      <td><span class="proc-state ${stateClass}">${p.status || "?"}</span></td>
      <td>${p.threads || 0}</td>
      <td><button class="kill-btn ${riskyClass}" data-pid="${p.pid}" data-name="${escapeHtml(p.name)}" ${riskyAttr}>Kill</button></td>`;
    return tr;
  }

  /**
   * Update an existing <tr> in-place if values changed.
   * Returns true if any cell was updated.
   */
  function _updateRow(tr, p) {
    const cls = _rowClass(p.cpu);
    if (tr.className !== cls) tr.className = cls;

    const cells = tr.children;
    // cells[0] = PID (doesn't change)

    const nameHtml = escapeHtml(p.name) + (p.risky ? ' <span class="risky-badge">🔴 sys</span>' : '');
    if (cells[1].innerHTML !== nameHtml) cells[1].innerHTML = nameHtml;

    const typeStr = p.type || "user";
    if (cells[2].textContent !== typeStr) cells[2].textContent = typeStr;

    const cpuStr = p.cpu.toFixed(1);
    if (cells[3].textContent !== cpuStr) cells[3].textContent = cpuStr;

    const memStr = p.memory.toFixed(1);
    if (cells[4].textContent !== memStr) cells[4].textContent = memStr;

    // RSS
    const rssStr = formatBytes(p.rss || 0);
    if (cells[5].textContent !== rssStr) cells[5].textContent = rssStr;

    // State
    const stateClass = p.status === "zombie" ? "state-zombie"
                     : p.status === "disk-sleep" ? "state-dstate" : "";
    const stateHtml = `<span class="proc-state ${stateClass}">${p.status || "?"}</span>`;
    if (cells[6].innerHTML !== stateHtml) cells[6].innerHTML = stateHtml;

    // Threads
    const threadStr = String(p.threads || 0);
    if (cells[7].textContent !== threadStr) cells[7].textContent = threadStr;
  }

  /**
   * Render the process table using diff updates.
   * Only rows that actually changed are touched in the DOM.
   */
  function renderProcessTable() {
    let procs = [...latestProcesses];

    // Filter
    if (filterText) {
      const ft = filterText.toLowerCase();
      procs = procs.filter(
        (p) => p.name.toLowerCase().includes(ft) || String(p.pid).includes(ft)
      );
    }

    // Sort
    procs.sort((a, b) => {
      let va = a[sortKey];
      let vb = b[sortKey];
      if (typeof va === "string") {
        va = va.toLowerCase();
        vb = vb.toLowerCase();
      }
      if (va < vb) return sortAsc ? -1 : 1;
      if (va > vb) return sortAsc ? 1 : -1;
      return 0;
    });

    // Cap visible rows
    const visibleProcs = procs.slice(0, 80);
    const visiblePids = new Set(visibleProcs.map((p) => p.pid));

    // Track which existing rows are still needed
    const newRowMap = new Map();

    // Build a document fragment for new rows, and collect ordered rows
    const orderedRows = [];

    for (const p of visibleProcs) {
      let tr = rowMap.get(p.pid);
      if (tr) {
        // Update existing row in-place
        _updateRow(tr, p);
      } else {
        // Create new row
        tr = _createRow(p);
      }
      newRowMap.set(p.pid, tr);
      orderedRows.push(tr);
    }

    // Remove rows for PIDs that are no longer visible
    for (const [pid, tr] of rowMap) {
      if (!visiblePids.has(pid)) {
        tr.remove();
      }
    }

    // Reorder DOM to match sorted order (only moves if needed)
    for (let i = 0; i < orderedRows.length; i++) {
      const tr = orderedRows[i];
      const current = processTbody.children[i];
      if (current !== tr) {
        processTbody.insertBefore(tr, current || null);
      }
    }

    rowMap = newRowMap;
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Utility — format bytes to human readable (Fix 4)
  function formatBytes(b) {
    const units = ["B","KB","MB","GB","TB"];
    let i = 0;
    while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
    return b.toFixed(1) + " " + units[i];
  }

  // ── Sync & Deadlocks panel (Fix 5) ────────────────────────

  function updateSyncPanel(sync) {
    if (!sync) return;

    $("sync-total-threads").textContent = sync.total_threads ?? "—";
    $("sync-fds").textContent           = sync.total_fds ?? "—";
    
    let fdLimitText = "N/A";
    if (sync.fd_limit > 0) {
      fdLimitText = sync.fd_limit > 1000000000 ? "Unlimited" : sync.fd_limit.toLocaleString();
    }
    $("sync-fd-limit").textContent = fdLimitText;
    $("sync-dstate").textContent        = sync.d_state_count ?? 0;
    $("sync-invol-ctx").textContent     = (sync.invol_ctx_ratio ?? 0).toFixed(1) + "%";

    // Highlight D-state card red if any processes are stuck
    const dcard = $("sync-dstate-card");
    if (dcard) {
      dcard.classList.toggle("danger", (sync.d_state_count || 0) > 0);
    }

    // Thread state bars
    const states = sync.thread_states || {};
    const STATE_COLORS = {
      running: "#3fb950", sleeping: "#58a6ff",
      "disk-sleep": "#f85149", zombie: "#ff7b72",
      stopped: "#d29922", other: "#8b949e"
    };
    const container = $("thread-state-bars");
    if (container) {
      container.innerHTML = Object.entries(states)
        .filter(([, v]) => v > 0)
        .map(([state, count]) => `
          <div class="ts-row">
            <span class="ts-label">${state}</span>
            <div class="ts-track">
              <div class="ts-fill" style="width:${sync.total_threads > 0 ? Math.min(count / sync.total_threads * 100, 100) : 0}%;
                background:${STATE_COLORS[state] || "#8b949e"}"></div>
            </div>
            <span class="ts-count">${count}</span>
          </div>`).join("");
    }

    // D-state process list
    const dlist = $("dstate-list");
    if (dlist) {
      const procs = sync.d_state_procs || [];
      dlist.innerHTML = procs.length === 0
        ? '<span class="no-dstate">None — system looks healthy</span>'
        : procs.map(p =>
            `<span class="dstate-chip">${escapeHtml(p.name)} <em>(${p.pid})</em></span>`
          ).join("");
    }
  }

  // ── Partitions (Fix 6) ────────────────────────────────────

  function updatePartitions(partitions) {
    const container = $("partition-list");
    if (!container || !partitions) return;
    container.innerHTML = partitions.map(p => `
      <div class="partition-row">
        <div class="part-header">
          <span class="part-mount">${p.mountpoint}</span>
          <span class="part-type">${p.fstype}</span>
          <span class="part-pct ${p.percent > 90 ? "danger" : p.percent > 75 ? "warn" : ""}">
            ${p.percent.toFixed(1)}%
          </span>
        </div>
        <div class="part-bar-track">
          <div class="part-bar-fill" style="width:${p.percent}%"></div>
        </div>
        ${p.inodes_total > 0 ? `
          <div class="inode-row">
            <span class="inode-label">Inodes</span>
            <span class="inode-val ${p.inodes_pct > 80 ? "danger" : ""}">${p.inodes_pct.toFixed(1)}%</span>
          </div>` : ""}
      </div>`).join("");
  }

  // ── Sort header clicks ───────────────────────────────────

  document.querySelectorAll("#process-table th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortKey === key) {
        sortAsc = !sortAsc;
      } else {
        sortKey = key;
        sortAsc = key === "name" || key === "pid"; // default asc for name/pid
      }

      // Update header arrows
      document.querySelectorAll("#process-table th.sortable").forEach((h) => {
        h.classList.remove("active", "asc", "desc");
        h.querySelector(".sort-arrow").textContent = "";
      });
      th.classList.add("active", sortAsc ? "asc" : "desc");
      th.querySelector(".sort-arrow").textContent = sortAsc ? "▲" : "▼";

      renderProcessTable();
    });
  });

  // ── Kill confirmation modal ──────────────────────────────

  const modalOverlay  = $("kill-modal-overlay");
  const modalIcon     = $("modal-icon");
  const modalTitle    = $("modal-title");
  const modalMessage  = $("modal-message");
  const modalWarning  = $("modal-warning");
  const modalCancel   = $("modal-cancel");
  const modalConfirm  = $("modal-confirm");

  let pendingKillPid = null;
  let pendingKillName = "";

  // ── Kill history ────────────────────────────────────────

  const killHistory = [];
  const killHistorySection = $("kill-history-section");
  const killHistoryTbody   = $("kill-history-tbody");
  const killHistoryCount   = $("kill-history-count");

  function addKillEntry(pid, name, success, message) {
    killHistory.unshift({
      time: new Date(),
      pid,
      name,
      success,
      message: message || "",
    });
    renderKillHistory();
  }

  function renderKillHistory() {
    if (killHistory.length === 0) return;
    killHistorySection.classList.remove("hidden");
    killHistoryCount.textContent = `(${killHistory.length})`;

    killHistoryTbody.innerHTML = killHistory
      .map((entry) => {
        const timeStr = entry.time.toLocaleTimeString();
        const statusClass = entry.success ? "status-ok" : "status-fail";
        const statusLabel = entry.success ? "✓ Killed" : `✗ ${entry.message}`;
        return `<tr>
          <td>${timeStr}</td>
          <td>${entry.pid}</td>
          <td>${escapeHtml(entry.name)}</td>
          <td><span class="kill-status ${statusClass}">${statusLabel}</span></td>
        </tr>`;
      })
      .join("");
  }

  // ── Modal logic ─────────────────────────────────────────

  function showKillModal(pid, name) {
    pendingKillPid = pid;
    pendingKillName = name;

    modalIcon.textContent = "🛑";
    modalTitle.textContent = "⚠ Critical System Process";
    modalMessage.textContent = `You are about to terminate "${name}" (PID ${pid}).`;
    modalWarning.classList.remove("hidden");
    modalWarning.style.display = "";
    modalConfirm.classList.add("modal-btn-danger");
    modalConfirm.textContent = "Kill Anyway";

    modalOverlay.style.display = "";
    modalOverlay.classList.remove("hidden");
    // small delay so the CSS transition plays
    requestAnimationFrame(() => modalOverlay.classList.add("visible"));
  }

  function hideKillModal() {
    modalOverlay.classList.remove("visible");
    setTimeout(() => {
      modalOverlay.classList.add("hidden");
      modalOverlay.style.display = "none";
    }, 250);
    pendingKillPid = null;
    pendingKillName = "";
  }

  modalCancel.addEventListener("click", hideKillModal);
  modalOverlay.addEventListener("click", (e) => {
    if (e.target === modalOverlay) hideKillModal();
  });

  modalConfirm.addEventListener("click", async () => {
    if (pendingKillPid === null) return;
    const pid = pendingKillPid;
    const name = pendingKillName;
    hideKillModal();

    try {
      const res = await fetch(`/kill/${pid}`, { method: "POST" });
      const body = await res.json();
      if (body.status === "ok") {
        addKillEntry(pid, name, true);
      } else {
        addKillEntry(pid, name, false, body.message);
      }
    } catch (err) {
      addKillEntry(pid, name, false, err.message);
    }
  });

  // ── Kill button delegation ───────────────────────────────

  processTbody.addEventListener("click", async (e) => {
    const btn = e.target.closest(".kill-btn");
    if (!btn) return;
    const pid  = parseInt(btn.dataset.pid, 10);
    const name = btn.dataset.name || "Unknown";
    const isRisky = btn.hasAttribute("data-risky");

    if (isRisky) {
      showKillModal(pid, name);
    } else {
      // Immediate kill for non-risky processes
      try {
        const res = await fetch(`/kill/${pid}`, { method: "POST" });
        const body = await res.json();
        if (body.status === "ok") {
          addKillEntry(pid, name, true);
        } else {
          addKillEntry(pid, name, false, body.message);
        }
      } catch (err) {
        addKillEntry(pid, name, false, err.message);
      }
    }
  });

  // ── Health Score Gauge ────────────────────────────────────

  function updateHealthGauge(health) {
    if (!health) return;
    const ringFill = $("health-ring-fill");
    const scoreText = $("health-score-text");
    const gradeEl = $("health-grade");

    if (!ringFill || !scoreText || !gradeEl) return;

    const score = health.score ?? 0;
    ringFill.setAttribute("stroke-dasharray", `${score}, 100`);

    // Color based on level
    ringFill.classList.remove("warning", "critical");
    gradeEl.classList.remove("warning", "critical");
    if (health.level === "critical") {
      ringFill.classList.add("critical");
      gradeEl.classList.add("critical");
    } else if (health.level === "warning") {
      ringFill.classList.add("warning");
      gradeEl.classList.add("warning");
    }

    scoreText.textContent = Math.round(score);
    gradeEl.textContent = health.grade || "—";
  }

  // ── Health Breakdown ─────────────────────────────────────

  function updateHealthBreakdown(health) {
    if (!health || !health.breakdown) return;
    const bd = health.breakdown;

    const items = ["cpu", "memory", "swap", "dstate", "load", "zombie"];
    items.forEach(key => {
      const bar = $(`hb-${key}`);
      const val = $(`hb-${key}-val`);
      if (!bar || !val) return;

      const penalty = bd[key] ?? 0;
      bar.style.width = `${Math.min(penalty, 100)}%`;
      bar.classList.remove("warn", "danger");
      if (penalty > 60) bar.classList.add("danger");
      else if (penalty > 30) bar.classList.add("warn");
      val.textContent = Math.round(penalty);
    });
  }

  // ── Process Tree View ───────────────────────────────────

  let treeViewActive = false;
  let treeData = [];
  let treeLoading = false;
  let treeCompact = true;       // default: hide idle system leaf processes

  const viewFlatBtn = $("view-flat-btn");
  const viewTreeBtn = $("view-tree-btn");
  const flatView = $("flat-view");
  const treeView = $("tree-view");
  const treeTbody = $("tree-tbody");
  const treeShowAll = $("tree-show-all");
  const treeCount = $("tree-count");

  if (viewFlatBtn) {
    viewFlatBtn.addEventListener("click", () => {
      treeViewActive = false;
      viewFlatBtn.classList.add("active");
      viewTreeBtn.classList.remove("active");
      flatView.classList.remove("hidden");
      treeView.classList.add("hidden");
    });
  }

  if (viewTreeBtn) {
    viewTreeBtn.addEventListener("click", async () => {
      treeViewActive = true;
      viewTreeBtn.classList.add("active");
      viewFlatBtn.classList.remove("active");
      flatView.classList.add("hidden");
      treeView.classList.remove("hidden");
      await fetchAndRenderTree();
    });
  }

  // "Show All" toggle
  if (treeShowAll) {
    treeShowAll.addEventListener("change", async () => {
      treeCompact = !treeShowAll.checked;
      await fetchAndRenderTree();
    });
  }

  async function fetchAndRenderTree() {
    if (treeLoading) return;
    treeLoading = true;
    try {
      const res = await fetch(`/process-tree?compact=${treeCompact}`);
      treeData = await res.json();
      renderTree();
    } catch (err) {
      console.error("Failed to fetch process tree:", err);
    } finally {
      treeLoading = false;
    }
  }

  function renderTree() {
    if (!treeTbody) return;
    if (!treeData || !treeData.length) {
      treeTbody.innerHTML = '<tr><td colspan="8" style="text-align:center;opacity:.5">Loading process tree…</td></tr>';
      return;
    }

    // Update count
    if (treeCount) {
      treeCount.textContent = `${treeData.length} processes`;
    }

    const visible = treeData.slice(0, 500);
    treeTbody.innerHTML = visible.map(p => {
      // Build visual tree connectors
      const connector = p.depth > 0
        ? "│  ".repeat(p.depth - 1) + "├─ "
        : "";
      // Icon: folder for branch nodes, file for leaf nodes
      const icon = p.children_count > 0 ? "📂 " : "📄 ";

      const stateClass = p.status === "zombie" ? "state-zombie"
                       : p.status === "disk-sleep" ? "state-dstate" : "";
      const rowClass = p.is_user ? "tree-row-user" : "tree-row-system";
      const userBadge = p.is_user
        ? `<span class="user-badge user">${escapeHtml(p.username)}</span>`
        : `<span class="user-badge system">${escapeHtml(p.username || "root")}</span>`;

      return `<tr data-pid="${p.pid}" class="${rowClass}">
        <td>${p.pid}</td>
        <td class="tree-name-cell">
          <span class="tree-indent">${connector}</span>${icon}<span class="tree-name">${escapeHtml(p.name)}</span>
        </td>
        <td>${userBadge}</td>
        <td>${p.cpu.toFixed(1)}</td>
        <td>${p.memory.toFixed(1)}</td>
        <td><span class="proc-state ${stateClass}">${p.status}</span></td>
        <td>${p.threads}</td>
        <td>${p.children_count > 0 ? `<span class="children-badge">${p.children_count}</span>` : "—"}</td>
      </tr>`;
    }).join("");
  }

  // Auto-refresh tree every 5s if tree view is active
  setInterval(() => {
    if (treeViewActive && !paused) fetchAndRenderTree();
  }, 5000);

  // Click on tree row → open detail modal
  if (treeTbody) {
    treeTbody.addEventListener("click", (e) => {
      const tr = e.target.closest("tr");
      if (!tr) return;
      const pid = parseInt(tr.dataset.pid, 10);
      if (pid) openDetailModal(pid);
    });
  }

  // ── Process Detail Modal ────────────────────────────────

  const detailOverlay = $("detail-modal-overlay");
  const detailBody = $("detail-body");
  const detailTitle = $("detail-title");
  const detailClose = $("detail-close");

  function openDetailModal(pid) {
    detailBody.innerHTML = '<div class="detail-loading">Loading process info…</div>';
    detailTitle.textContent = `Process Detail — PID ${pid}`;

    detailOverlay.style.display = "";
    detailOverlay.classList.remove("hidden");
    requestAnimationFrame(() => detailOverlay.classList.add("visible"));

    fetch(`/process/${pid}`)
      .then(r => r.json())
      .then(data => {
        if (data.error) {
          detailBody.innerHTML = `<div class="detail-loading">${escapeHtml(data.error)}</div>`;
          return;
        }
        renderDetailModal(data);
      })
      .catch(err => {
        detailBody.innerHTML = `<div class="detail-loading">Error: ${escapeHtml(err.message)}</div>`;
      });
  }

  function hideDetailModal() {
    detailOverlay.classList.remove("visible");
    setTimeout(() => {
      detailOverlay.classList.add("hidden");
      detailOverlay.style.display = "none";
    }, 250);
  }

  if (detailClose) detailClose.addEventListener("click", hideDetailModal);
  if (detailOverlay) detailOverlay.addEventListener("click", (e) => {
    if (e.target === detailOverlay) hideDetailModal();
  });

  function renderDetailModal(d) {
    const pf = d.page_faults || {};
    const ctx = d.ctx_switches || {};
    const created = d.create_time ? new Date(d.create_time * 1000).toLocaleString() : "—";

    let html = `
      <div class="detail-grid">
        <div class="detail-card"><span class="detail-card-label">PID</span><span class="detail-card-value">${d.pid}</span></div>
        <div class="detail-card"><span class="detail-card-label">PPID</span><span class="detail-card-value">${d.ppid || "—"}</span></div>
        <div class="detail-card"><span class="detail-card-label">User</span><span class="detail-card-value">${escapeHtml(d.username || "?")}</span></div>
        <div class="detail-card"><span class="detail-card-label">State</span><span class="detail-card-value">${d.status}</span></div>
        <div class="detail-card"><span class="detail-card-label">CPU %</span><span class="detail-card-value">${d.cpu}%</span></div>
        <div class="detail-card"><span class="detail-card-label">Memory %</span><span class="detail-card-value">${d.memory}%</span></div>
        <div class="detail-card"><span class="detail-card-label">Nice (Priority)</span><span class="detail-card-value">${d.nice ?? "N/A"}</span></div>
        <div class="detail-card"><span class="detail-card-label">Created</span><span class="detail-card-value" style="font-size:12px">${created}</span></div>
      </div>
    `;

    // Page Faults & Memory
    html += `<div class="detail-section-title">🧠 Virtual Memory & Page Faults</div>
      <div class="detail-grid">
        <div class="detail-card"><span class="detail-card-label">RSS (Physical)</span><span class="detail-card-value">${formatBytes(pf.rss || 0)}</span></div>
        <div class="detail-card"><span class="detail-card-label">VMS (Virtual)</span><span class="detail-card-value">${formatBytes(pf.vms || 0)}</span></div>
        ${pf.total_faults !== undefined ? `<div class="detail-card"><span class="detail-card-label">Total Page Faults</span><span class="detail-card-value">${pf.total_faults.toLocaleString()}</span></div>` : ""}
        ${pf.major_faults !== undefined ? `<div class="detail-card"><span class="detail-card-label">Major Faults (Disk)</span><span class="detail-card-value">${pf.major_faults.toLocaleString()}</span></div>` : ""}
      </div>`;

    // Context Switches
    html += `<div class="detail-section-title">🔄 Context Switches (CPU Scheduling)</div>
      <div class="detail-grid">
        <div class="detail-card"><span class="detail-card-label">Voluntary</span><span class="detail-card-value">${(ctx.voluntary || 0).toLocaleString()}</span></div>
        <div class="detail-card"><span class="detail-card-label">Involuntary</span><span class="detail-card-value">${(ctx.involuntary || 0).toLocaleString()}</span></div>
      </div>`;

    // CPU Affinity
    if (d.cpu_affinity) {
      html += `<div class="detail-section-title">🖥️ CPU Affinity</div>
        <p style="font-size:13px;color:var(--text-secondary);margin-bottom:10px">Cores allowed: <strong>${d.cpu_affinity.join(", ")}</strong></p>`;
    }

    // Threads
    html += `<div class="detail-section-title">🧵 Threads (${d.num_threads || d.threads?.length || 0})</div>`;
    if (d.threads && d.threads.length > 0) {
      html += `<table class="detail-table"><thead><tr><th>Thread ID</th><th>User Time (s)</th><th>System Time (s)</th></tr></thead><tbody>`;
      d.threads.forEach(t => {
        html += `<tr><td>${t.id}</td><td>${t.user_time}</td><td>${t.system_time}</td></tr>`;
      });
      html += `</tbody></table>`;
    } else {
      html += `<div class="detail-empty">No thread info available</div>`;
    }

    // Open Files
    html += `<div class="detail-section-title">📁 Open Files (FDs: ${d.num_fds >= 0 ? d.num_fds : "N/A"})</div>`;
    if (d.open_files && d.open_files.length > 0) {
      html += `<table class="detail-table"><thead><tr><th>FD</th><th>Path</th></tr></thead><tbody>`;
      d.open_files.forEach(f => {
        html += `<tr><td>${f.fd}</td><td>${escapeHtml(f.path)}</td></tr>`;
      });
      html += `</tbody></table>`;
    } else {
      html += `<div class="detail-empty">No open files or access denied</div>`;
    }

    // Network Connections
    html += `<div class="detail-section-title">🌐 Network Connections</div>`;
    if (d.connections && d.connections.length > 0) {
      html += `<table class="detail-table"><thead><tr><th>Proto</th><th>Local</th><th>Remote</th><th>Status</th></tr></thead><tbody>`;
      d.connections.forEach(c => {
        html += `<tr><td>${c.type}/${c.family}</td><td>${escapeHtml(c.local)}</td><td>${escapeHtml(c.remote || "—")}</td><td>${c.status}</td></tr>`;
      });
      html += `</tbody></table>`;
    } else {
      html += `<div class="detail-empty">No active network connections</div>`;
    }

    detailBody.innerHTML = html;
  }

  // Click on flat table row (but not kill button) → open detail
  processTbody.addEventListener("click", (e) => {
    if (e.target.closest(".kill-btn")) return; // don't open detail when clicking kill
    const tr = e.target.closest("tr");
    if (!tr) return;
    const pid = parseInt(tr.dataset.pid, 10);
    if (pid) openDetailModal(pid);
  });

  // ── Controls ─────────────────────────────────────────────

  intervalSelect.addEventListener("change", () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ interval: parseInt(intervalSelect.value, 10) }));
    }
  });

  pauseBtn.addEventListener("click", () => {
    paused = !paused;
    pauseBtn.textContent = paused ? "▶ Resume" : "⏸ Pause";
    pauseBtn.classList.toggle("active", paused);
  });

  processFilter.addEventListener("input", (e) => {
    filterText = e.target.value;
    renderProcessTable();
  });

  // ── Boot ──────────────────────────────────────────────────

  connect();
})();
