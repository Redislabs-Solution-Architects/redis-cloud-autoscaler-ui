// Redis Autoscaler Demo — client controller
// ----------------------------------------------------------------------------
// WebSocket to /ws → live state → render. POST /api/load/{start,stop} for memtier.

(() => {
  'use strict';

  const $    = (id) => document.getElementById(id);
  const fmt  = (n)  => Number(n).toLocaleString('en-US');
  const fmtBytes = (b) => {
    if (b >= 1024**3) return (b/1024**3).toFixed(2) + ' GB';
    if (b >= 1024**2) return (b/1024**2).toFixed(1) + ' MB';
    if (b >= 1024)    return (b/1024).toFixed(0) + ' KB';
    return Math.round(b) + ' B';
  };

  // ----------------------------------------------------------- state
  let presets = [];
  let lastSeenEventKey = null;
  let lastDbThroughput = null;
  let lastMemtierRunning = false;
  let isFirstSnapshot = true;

  // ----------------------------------------------------------- chart
  // Draws a vertical marker wherever the CONFIGURED limit changes — that is
  // the moment the autoscaler actually resized the database, and it deserves
  // more than a subtle step in a dashed line.
  const scaleMarkers = {
    id: 'scaleMarkers',
    afterDatasetsDraw(c) {
      const cfg = c.data.datasets[1] && c.data.datasets[1].data;
      if (!cfg || cfg.length < 2) return;
      const { ctx: g, chartArea, scales } = c;
      g.save();
      g.font = "10px 'JetBrains Mono', monospace";
      g.textAlign = 'center';
      for (let i = 1; i < cfg.length; i++) {
        if (cfg[i].y === cfg[i - 1].y) continue;
        const up = cfg[i].y > cfg[i - 1].y;
        const x = scales.x.getPixelForValue(cfg[i].x);
        if (x < chartArea.left || x > chartArea.right) continue;
        g.strokeStyle = up ? 'rgba(255,68,56,0.85)' : 'rgba(128,219,255,0.85)';
        g.setLineDash([2, 3]);
        g.lineWidth = 1.5;
        g.beginPath();
        g.moveTo(x, chartArea.top + 14);
        g.lineTo(x, chartArea.bottom);
        g.stroke();
        g.setLineDash([]);
        g.fillStyle = up ? '#ff6b5b' : '#80dbff';
        g.fillText(up ? '▲ scale-up' : '▼ scale-down', x, chartArea.top + 9);
      }
      g.restore();
    },
  };

  const ctx = $('chart').getContext('2d');
  const chart = new Chart(ctx, {
    plugins: [scaleMarkers],
    type: 'line',
    data: {
      datasets: [
        {
          label: 'live ops/sec',
          data: [],
          borderColor: '#FF4438',
          backgroundColor: 'rgba(255,68,56,0.12)',
          borderWidth: 2.5,
          fill: true,
          tension: 0.35,
          pointRadius: 0,
          pointHoverRadius: 4,
        },
        {
          label: 'configured limit',
          data: [],
          borderColor: '#80DBFF',
          backgroundColor: 'rgba(128,219,255,0.05)',
          borderWidth: 2,
          borderDash: [6, 4],
          stepped: 'before',
          fill: false,
          pointRadius: 0,
        },
      ],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          labels: {
            color: '#9ca3af',
            boxWidth: 14,
            padding: 16,
            font: { size: 11, family: 'Inter, sans-serif' },
          },
        },
        tooltip: {
          mode: 'index',
          intersect: false,
          backgroundColor: '#091A23',
          borderColor: '#1d3a4a',
          borderWidth: 1,
          titleFont: { family: 'JetBrains Mono, monospace', size: 11 },
          bodyFont:  { family: 'Inter, sans-serif', size: 12 },
          callbacks: {
            title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleTimeString('en-US', { hour12: false }) : '',
            label: (item) => `  ${item.dataset.label}: ${fmt(Math.round(item.parsed.y))}`,
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          ticks: {
            color: '#6b7280',
            maxTicksLimit: 8,
            font: { family: 'JetBrains Mono, monospace', size: 10 },
            callback: (v) => new Date(v).toLocaleTimeString('en-US', { hour12: false }),
          },
          grid: { color: 'rgba(29,58,74,0.5)' },
        },
        y: {
          beginAtZero: true,
          type: 'logarithmic',
          min: 1,
          ticks: {
            color: '#6b7280',
            font: { family: 'JetBrains Mono, monospace', size: 10 },
            callback: (v) => v >= 1000 ? (v/1000).toFixed(0) + 'k' : v,
          },
          grid: { color: 'rgba(29,58,74,0.5)' },
        },
      },
    },
  });

  function updateChart(history) {
    const live = [];
    const cfg  = [];
    for (const p of history) {
      const t = p.t * 1000;
      live.push({ x: t, y: Math.max(1, p.live) });
      cfg.push({ x: t, y: Math.max(1, p.configured) });
    }
    chart.data.datasets[0].data = live;
    chart.data.datasets[1].data = cfg;
    chart.update('none');
  }

  // ----------------------------------------------------------- panels
  function setBranding(b) {
    $('tagline').textContent = b.tagline;
    document.title = 'Redis Autoscaler — ' + b.client_name;
  }

  function setDBPanel(db) {
    $('db-name').textContent = `${db.name} · id ${db.id}`;

    const stEl = $('db-status');
    const st = db.status || '…';
    if (st === 'active')           stEl.innerHTML = '<span class="status-active">● active</span>';
    else if (st.startsWith('pending')) stEl.innerHTML = `<span class="status-pending">◐ ${st}</span>`;
    else                            stEl.innerHTML = `<span class="status-error">${st}</span>`;

    const thr = db.throughput || 0;
    const thrEl = $('db-throughput');
    thrEl.innerHTML = `${fmt(thr)} <span class="unit">ops/sec</span>`;

    const badge = $('db-throughput-badge');
    badge.className = 'badge';
    if (thr === 0) {
      badge.textContent = '—';
    } else if (thr === db.baseline_ops) {
      badge.textContent = 'baseline';
      badge.classList.add('b-base');
    } else if (thr >= db.burst_ops) {
      badge.textContent = `scaled · ${(thr/db.baseline_ops).toFixed(1)}× baseline`;
      badge.classList.add('b-scaled');
    } else {
      badge.textContent = `${(thr/db.baseline_ops).toFixed(1)}× baseline`;
      badge.classList.add('b-mid');
    }

    if (lastDbThroughput !== null && lastDbThroughput !== thr) {
      thrEl.classList.remove('flash'); void thrEl.offsetWidth; thrEl.classList.add('flash');
      if (thr > lastDbThroughput) {
        toast('success', '🚀 Scaled UP', `${fmt(lastDbThroughput)} → ${fmt(thr)} ops/sec`);
      } else {
        toast('info', '⬇ Scaled DOWN', `${fmt(lastDbThroughput)} → ${fmt(thr)} ops/sec`);
      }
    }
    lastDbThroughput = thr;

    const memFmt = (v) => Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1);
    // Redis Cloud's API returns memory_limit_gb already × 2 when HA is on.
    // dataset_size_gb is the customer-facing value (what they configured).
    const dataset  = db.dataset_size_gb || db.memory_limit_gb;
    const physical = db.memory_limit_gb;
    $('db-memlim').innerHTML = memFmt(dataset) + ' <span class="unit">GB</span>';
    const physEl = $('db-memlim-physical');
    if (physEl) {
      if (db.replication) {
        physEl.textContent = `with HA: ${memFmt(physical)} GB physical`;
      } else {
        physEl.textContent = 'no replication';
      }
    }
    const shardsEl = $('db-shards');
    shardsEl.textContent = db.shards > 0 ? String(db.shards) : '—';
    $('db-modified').textContent = db.last_modified || '—';

    // reset-baseline label
    const lbl = document.getElementById('reset-baseline-label');
    if (lbl) lbl.textContent = `${fmt(db.baseline_ops)} ops/sec · ${db.baseline_mem_gb} GB`;
  }

  function setLivePanel(live, db, memtier) {
    const ops = live.ops_per_sec || 0;
    const mem = live.memory_bytes || 0;

    // When the load just started but ops/sec hasn't propagated through the
    // Prometheus scrape yet, show "ramping up..." instead of a stark "0".
    // This is the difference between "looks broken" and "looks responsive".
    const ramping = memtier && memtier.running && ops < 100;
    if (ramping) {
      $('live-ops').innerHTML = `<span style="color: var(--c-amber)">ramping up…</span>`;
    } else {
      $('live-ops').innerHTML = `${fmt(Math.round(ops))} <span class="unit">ops/sec</span>`;
    }
    $('live-mem').innerHTML = fmtBytes(mem).replace(/( \w+)$/, ' <span class="unit">$1</span>').replace('<span class="unit"> ', '<span class="unit">');

    if (db.throughput > 0) {
      const pct = Math.min(100, (ops / db.throughput) * 100);
      if (ramping) {
        $('live-ops-pct').innerHTML = '<span style="color: var(--c-amber)">prometheus scrape ≤ 5s</span>';
      } else {
        $('live-ops-pct').textContent = `${pct.toFixed(0)}% of configured (${fmt(db.throughput)})`;
      }
      const bar = $('live-ops-bar');
      bar.style.width = pct + '%';
      bar.className = 'bar-fill ' + (pct < 50 ? 'bar-green' : pct < 80 ? 'bar-amber' : 'bar-redis');
    } else {
      $('live-ops-pct').textContent = 'waiting for DB config…';
    }
    if (db.memory_limit_gb > 0) {
      const pct = Math.min(100, (mem / (db.memory_limit_gb * 1024**3)) * 100);
      $('live-mem-pct').textContent = `${pct.toFixed(1)}% of limit (${db.memory_limit_gb.toFixed(2)} GB)`;
      $('live-mem-bar').style.width = pct + '%';
    } else {
      $('live-mem-pct').textContent = 'waiting…';
    }
  }

  function setAlertsPanel(alerts) {
    const el = $('alerts-list');
    if (!alerts.length) {
      el.innerHTML = '<div class="dim small">(no rules loaded)</div>';
      return;
    }
    el.innerHTML = '';
    for (const a of alerts) {
      const row = document.createElement('div');
      row.className = 'alert-row ' + a.state;

      const name = document.createElement('div');
      name.className = 'alert-name';
      name.textContent = a.name;

      const tag = document.createElement('span');
      tag.className = 'alert-state ' + a.state;
      const icon = a.state === 'firing' ? '⚡' : a.state === 'pending' ? '◐' : '○';
      tag.textContent = `${icon} ${a.state}`;

      row.appendChild(name);
      row.appendChild(tag);
      el.appendChild(row);
    }
  }

  // Honest view of the FULL upstream rule set (all four ruleTypes). Rules
  // this demo keeps off stay VISIBLE, greyed, with the reason: the autoscaler
  // has the capability, the demo config chose not to arm it.
  function setPolicyPanel(features, db) {
    const el = $('policy-list');
    if (!el || !features) return;
    const memOn = features.memory_scaling_enabled;
    const rows = [
      {
        on: true,
        title: 'Throughput scale-up',
        rule: 'IncreaseThroughput',
        detail: `${fmt(db.baseline_ops)} → ${fmt(db.burst_ops)} ops/sec on alert (webhook) · ceiling ${fmt(db.burst_ops)}`,
      },
      {
        on: false,
        title: 'Throughput scale-down',
        rule: 'DecreaseThroughput',
        detail: 'the autoscaler supports it as a SCHEDULED (cron) rule, never reactive: shrinking on a quiet minute would yo-yo against real traffic. This demo returns to baseline with the scheduled reset below instead',
      },
      {
        on: memOn,
        title: 'Memory scale-up',
        rule: 'IncreaseMemory',
        detail: memOn
          ? `+${features.memory_step_gb} GB steps on alert (webhook) · ceiling ${features.memory_ceiling_gb} GB`
          : 'fully supported by the autoscaler; this demo ships it disabled because growing RAM changes billed capacity instantly. One flag turns it on: MEMORY_SCALING_ENABLED=true',
      },
      {
        on: false,
        title: 'Memory scale-down',
        rule: 'DecreaseMemory',
        detail: 'supported as a SCHEDULED (cron) rule, same anti-yo-yo rule as throughput. Off here together with memory scaling',
      },
    ];
    el.innerHTML = '';
    for (const r of rows) {
      const row = document.createElement('div');
      row.className = 'policy-row' + (r.on ? '' : ' policy-off');
      const chip = r.on
        ? '<span class="policy-chip on">enabled</span>'
        : '<span class="policy-chip off">off via config</span>';
      row.innerHTML =
        `<div class="policy-head"><span class="policy-title">${r.title}</span>` +
        `<span class="policy-rule">${r.rule}</span>${chip}</div>` +
        `<div class="policy-detail">${r.detail}</div>`;
      el.appendChild(row);
    }
  }

  function kindIcon(kind) {
    return ({
      scale_up_throughput:   '🚀',
      scale_down_throughput: '⬇',
      scale_memory:          '💾',
      task:                  '📋',
      webhook:               '⚡',
      silence:               '🤫',
    })[kind] || '·';
  }

  function setEventsPanel(events) {
    const el = $('events-list');
    if (!events.length) {
      el.innerHTML = '<div class="dim small" style="padding:10px">waiting for events…</div>';
      return;
    }
    el.innerHTML = '';
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      const row = document.createElement('div');
      row.className = 'event-row kind-' + e.kind;

      const ts = document.createElement('span');
      ts.className = 'event-ts';
      ts.textContent = (e.ts.split(' ')[1] || e.ts).slice(0, 8);

      const msg = document.createElement('span');
      msg.className = 'event-msg';
      msg.textContent = `${kindIcon(e.kind)}  ${e.msg}`;

      row.appendChild(ts);
      row.appendChild(msg);
      el.appendChild(row);
    }

    // toast on new event (skip on initial load)
    const top = events[events.length - 1];
    const key = top.ts + '|' + top.msg;
    if (key !== lastSeenEventKey && !isFirstSnapshot) {
      if (top.kind === 'webhook')                 toast('warning', '⚡ Alert firing', top.msg);
      else if (top.kind === 'task')               toast('info',    '📋 Task queued',  top.msg);
      else if (top.kind === 'scale_up_throughput')   toast('success', '🚀 Scale UP',   top.msg);
      else if (top.kind === 'scale_down_throughput') toast('info',    '⬇ Scale DOWN', top.msg);
    }
    lastSeenEventKey = key;
  }

  function setMemtier(m) {
    const card = $('memtier-card');
    const statusEl = $('memtier-status');
    const startBtn = $('btn-start');
    const stopBtn  = $('btn-stop');
    const startLbl = $('btn-start-label');
    if (m.running) {
      statusEl.innerHTML = `<span style="color: var(--c-redis); font-weight: 600">▶ running</span> · <span class="dim">${m.status || ''}</span>`;
      card.classList.add('memtier-running');
      startBtn.disabled = true;
      startBtn.title = 'A load is already running — stop it first to start a new one';
      startLbl.textContent = 'Load running';
      stopBtn.disabled = false;
      if (!lastMemtierRunning && !isFirstSnapshot) toast('success', '▶ Load started', 'memtier_benchmark is running');
    } else {
      statusEl.textContent = 'idle';
      card.classList.remove('memtier-running');
      startBtn.disabled = false;
      startBtn.title = '';
      startLbl.textContent = 'Start load';
      stopBtn.disabled = true;
      if (lastMemtierRunning && !isFirstSnapshot) toast('info', '■ Load stopped', 'memtier_benchmark terminated');
    }
    lastMemtierRunning = m.running;
  }

  function setAutoReset(r, db, memtier) {
    const card = $('reset-card');
    const status = $('reset-status');
    const cd = $('reset-countdown');
    const prog = $('reset-progress');
    const btnNow = $('btn-reset-now');
    const btnCancel = $('btn-reset-cancel');
    const windowLabel = $('reset-window-label');

    if (r && r.window_seconds) {
      const min = Math.round(r.window_seconds / 60);
      windowLabel.textContent = (min === 1 ? '1 minute' : `${min} minutes`);
    }

    // Decide on REAL state, not on stale last_action.
    // Compare against the HA-normalized dataset size (memory_limit_gb is the
    // raw API value, doubled when replication is on) so this matches the
    // backend's _is_scaled_above_baseline — otherwise an HA DB at true
    // baseline looks "scaled" because 5 GB physical > 2.5 GB dataset.
    const datasetGb = (db && db.dataset_size_gb) || (db && db.memory_limit_gb);
    const atBaseline =
      db && db.throughput <= db.baseline_ops &&
      datasetGb <= (db.baseline_mem_gb + 0.01);

    // Scheduled scale-down suspended (AUTO_RESET_ENABLED=false): the DB holds
    // its scaled capacity until someone resets it manually.
    if (r && r.enabled === false) {
      card.classList.remove('active');
      cd.textContent = '—';
      prog.style.width = '0%';
      btnCancel.disabled = true;
      if (atBaseline) {
        status.innerHTML = '<span style="color: var(--c-emerald)">✓ at baseline</span> <span class="dim">· auto scale-down suspended</span>';
        btnNow.disabled = true;
      } else {
        status.innerHTML = '<span style="color: var(--c-amber)">scaled · auto scale-down suspended</span> <span class="dim">— use Reset now to return to baseline</span>';
        btnNow.disabled = false;
      }
      return;
    }

    if (r && r.in_flight) {
      // Reset submitted; Redis Cloud is applying it. The REST API keeps
      // reporting the scaled size for ~1 min — say so instead of re-arming
      // a phantom countdown.
      card.classList.add('active');
      status.innerHTML = '<span style="color: var(--c-blue)">⏳ scale-down in flight — Redis Cloud is applying the change</span>';
      cd.textContent = '…';
      prog.style.width = '100%';
      prog.classList.add('indeterminate');
      btnNow.disabled = true;
      btnCancel.disabled = true;
      return;
    }
    prog.classList.remove('indeterminate');

    if (r && r.scheduled && r.seconds_remaining !== null && r.seconds_remaining > 0) {
      // Active countdown — auto-reset is armed
      card.classList.add('active');
      status.innerHTML = '<span style="color: var(--c-blue)">⏱ countdown active — will reset to baseline</span>';
      const s = r.seconds_remaining;
      const mm = Math.floor(s / 60).toString().padStart(2, '0');
      const ss = (s % 60).toString().padStart(2, '0');
      cd.textContent = `${mm}:${ss}`;
      const pct = Math.max(0, Math.min(100, (s / r.window_seconds) * 100));
      prog.style.width = pct + '%';
      btnNow.disabled = false;
      btnCancel.disabled = false;
    } else if (atBaseline) {
      // Idle and at baseline — nothing to do
      card.classList.remove('active');
      status.innerHTML = '<span style="color: var(--c-emerald)">✓ at baseline</span>';
      cd.textContent = '—';
      prog.style.width = '0%';
      btnNow.disabled = true;
      btnCancel.disabled = true;
    } else if (memtier && memtier.running) {
      // Above baseline but load is still running — countdown will start when it stops
      card.classList.remove('active');
      status.innerHTML = '<span style="color: var(--c-amber)">load running — auto-reset will arm when load stops</span>';
      cd.textContent = '—';
      prog.style.width = '0%';
      btnNow.disabled = false;   // user can still force-reset
      btnCancel.disabled = true;
    } else {
      // Above baseline, no load, no countdown — user cancelled or just landed here.
      // Offer "Reset now" so the demo can recover.
      card.classList.remove('active');
      status.innerHTML = '<span class="dim">above baseline — auto-reset cancelled, use Reset now</span>';
      cd.textContent = '—';
      prog.style.width = '0%';
      btnNow.disabled = false;
      btnCancel.disabled = true;
    }
  }

  function setDiagnostics(d) {
    const dbEl = $('diag-db');
    const promEl = $('diag-prom');
    if (d.db_fetch_err) { dbEl.textContent = `DB API: ${d.db_fetch_err}`; dbEl.classList.remove('hidden'); }
    else dbEl.classList.add('hidden');
    if (d.prom_fetch_err) { promEl.textContent = `Prom: ${d.prom_fetch_err}`; promEl.classList.remove('hidden'); }
    else promEl.classList.add('hidden');
  }

  // ----------------------------------------------------------- toasts
  function toast(kind, title, msg) {
    const t = document.createElement('div');
    t.className = 'toast ' + kind;
    t.innerHTML = `<div class="toast-title">${title}</div><div class="toast-msg">${msg}</div>`;
    $('toasts').appendChild(t);
    setTimeout(() => t.remove(), 6500);
  }

  // ----------------------------------------------------------- WebSocket
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(proto + '//' + location.host + '/ws');
    ws.onopen = () => {
      $('ws-dot').classList.remove('ws-off');
      $('ws-dot').classList.add('ws-on');
      $('ws-status').textContent = 'live';
    };
    ws.onmessage = (ev) => {
      try {
        const s = JSON.parse(ev.data);
        setBranding(s.branding);
        setDBPanel(s.db);
        setLivePanel(s.live, s.db, s.memtier);
        setAlertsPanel(s.alerts);
        setPolicyPanel(s.features, s.db);
        setEventsPanel(s.events);
        setMemtier(s.memtier);
        setAutoReset(s.auto_reset, s.db, s.memtier);
        setDiagnostics(s.diagnostics);
        updateChart(s.history);
        isFirstSnapshot = false;
      } catch (e) { console.error(e); }
    };
    ws.onclose = () => {
      $('ws-dot').classList.remove('ws-on');
      $('ws-dot').classList.add('ws-off');
      $('ws-status').textContent = 'reconnecting…';
      setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
  }

  // ----------------------------------------------------------- memtier form
  async function loadPresets() {
    const r = await fetch('/api/config');
    const c = await r.json();
    presets = c.presets;
    const wrap = $('preset-buttons');
    wrap.innerHTML = '';
    for (const p of presets) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'preset-btn';
      b.dataset.pid = p.id;
      b.title = p.description;
      b.textContent = p.name;
      b.addEventListener('click', () => applyPreset(p.id));
      wrap.appendChild(b);
    }
    // Default to the second preset (the "makes-the-demo-happen" burst) when
    // present, else the first. Never reference a preset id that may not exist.
    if (presets.length) applyPreset((presets[1] || presets[0]).id);
  }

  function applyPreset(pid) {
    const p = presets.find((x) => x.id === pid);
    if (!p) return;
    document.querySelectorAll('.preset-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.pid === pid));
    $('p-clients').value  = p.params.clients;
    $('p-pipeline').value = p.params.pipeline;
    $('p-threads').value  = p.params.threads;
    $('p-time').value     = p.params.test_time;
    $('p-ratio').value    = p.params.ratio;
    $('p-size').value     = p.params.data_size;
    $('p-keys').value     = p.params.key_maximum;
    refreshForm();
  }

  function readForm() {
    return {
      threads:     +$('p-threads').value,
      clients:     +$('p-clients').value,
      pipeline:    +$('p-pipeline').value,
      ratio:        $('p-ratio').value,
      data_size:   +$('p-size').value,
      key_minimum: 1,
      key_maximum: +$('p-keys').value,
      test_time:   +$('p-time').value,
    };
  }

  function refreshForm() {
    $('p-clients-v').textContent  = $('p-clients').value;
    $('p-pipeline-v').textContent = $('p-pipeline').value;
    $('p-threads-v').textContent  = $('p-threads').value;
    const t = +$('p-time').value;
    $('p-time-v').textContent = t >= 60 ? Math.round(t/60) + ' min' : t + ' s';

    const p = readForm();
    $('cmd-preview').textContent =
      `memtier_benchmark -t ${p.threads} -c ${p.clients} --pipeline=${p.pipeline} ` +
      `--ratio=${p.ratio} --data-size=${p.data_size} ` +
      `--key-pattern=R:R --key-maximum=${p.key_maximum} --test-time=${p.test_time}`;
    $('estimated-load').textContent = fmt(p.clients * p.pipeline * p.threads) + ' inflight';
  }

  document.querySelectorAll('#memtier-form input, #memtier-form select').forEach((el) =>
    el.addEventListener('input', () => {
      document.querySelectorAll('.preset-btn').forEach((b) => b.classList.remove('active'));
      refreshForm();
    }));

  // Optimistic UI: immediately reflect intent in the button + status card.
  // The WS snapshot will confirm a second later. No more "I clicked, nothing
  // happened, then suddenly it started".
  function setLoadButtonsBusy(starting) {
    const start = $('btn-start');
    const stop  = $('btn-stop');
    const label = $('btn-start-label');
    if (starting) {
      start.disabled = true;
      stop.disabled = true;
      if (label) label.textContent = 'starting…';
      $('memtier-status').innerHTML = '<span style="color: var(--c-amber)">▶ starting…</span>';
      $('memtier-card').classList.add('memtier-running');
    } else {
      // Snapshot logic will re-enable correctly based on m.running
      start.disabled = false;
      stop.disabled  = false;
      if (label) label.textContent = 'Start load';
    }
  }

  $('btn-start').addEventListener('click', async () => {
    setLoadButtonsBusy(true);
    const params = readForm();
    try {
      const r = await fetch('/api/load/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });
      const j = await r.json();
      if (!j.ok) {
        toast('error', 'Failed to start', j.message);
        setLoadButtonsBusy(false);
      }
      // On success, the WS snapshot will arrive within <=1s with memtier.running=true
      // (backend flips the flag eagerly). setMemtier() takes over from here.
    } catch (e) {
      toast('error', 'Failed to start', String(e));
      setLoadButtonsBusy(false);
    }
  });

  $('btn-stop').addEventListener('click', async () => {
    const btn = $('btn-stop');
    btn.disabled = true;
    const label = $('btn-start-label');
    if (label) label.textContent = 'Start load';
    $('memtier-status').innerHTML = '<span class="dim">stopping…</span>';
    try {
      const r = await fetch('/api/load/stop', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) toast('error', 'Failed to stop', j.message);
    } catch (e) {
      toast('error', 'Failed to stop', String(e));
    }
    // Snapshot will sync the rest.
  });

  // ----- how-it-works collapse -----
  const hiwToggle = $('howitworks-toggle');
  const hiwCard = hiwToggle ? hiwToggle.closest('.howitworks-card') : null;
  if (hiwToggle && hiwCard) {
    hiwToggle.addEventListener('click', () => {
      const expanded = hiwToggle.getAttribute('aria-expanded') === 'true';
      hiwToggle.setAttribute('aria-expanded', String(!expanded));
      hiwCard.classList.toggle('expanded', !expanded);
    });
  }

  // ----- admin actions -----
  async function adminCall(path, confirmText) {
    if (!confirm(confirmText)) return;
    const btn = event.currentTarget; const oldTxt = btn.textContent;
    btn.disabled = true; btn.textContent = 'working…';
    try {
      const r = await fetch(path, { method: 'POST' });
      const j = await r.json();
      if (j.ok) toast('warning', '⚙ Admin action', j.message || 'OK');
      else      toast('error',   '⚙ Admin failed', j.message || 'error');
    } catch (e) { toast('error', '⚙ Admin failed', String(e)); }
    finally { btn.disabled = false; btn.textContent = oldTxt; }
  }
  $('btn-flushdb').addEventListener('click', (e) => adminCall(
    '/api/admin/flushdb',
    'This wipes the customer keys but PRESERVES the autoscaler rules. Continue?'));
  $('btn-reset').addEventListener('click', (e) => adminCall(
    '/api/admin/reset-baseline',
    'Force-scale the DB back to its configured baseline? This bypasses the autoscaler.'));
  $('btn-reload-rules').addEventListener('click', (e) => adminCall(
    '/api/admin/reload-rules',
    'Re-register all 4 autoscaler scaling rules? (Safe to run anytime — idempotent.)'));

  // Auto-reset controls (no confirm — these are scheduled/expected actions)
  $('btn-reset-now').addEventListener('click', async (e) => {
    const btn = e.currentTarget; const old = btn.textContent;
    btn.disabled = true; btn.textContent = 'resetting…';
    try {
      const r = await fetch('/api/auto-reset/now', { method: 'POST' });
      const j = await r.json();
      if (j.ok) toast('info', '⬇ Reset now', j.message || 'DB scaled back to baseline');
      else      toast('error', 'Reset failed', j.message || 'error');
    } catch (e) { toast('error', 'Reset failed', String(e)); }
    finally { btn.disabled = false; btn.textContent = old; }
  });
  $('btn-reset-cancel').addEventListener('click', async () => {
    const r = await fetch('/api/auto-reset/cancel', { method: 'POST' });
    const j = await r.json();
    if (j.ok) toast('info', '✕ Auto-reset cancelled', j.message || 'countdown stopped');
  });

  // ----------------------------------------------------------- boot
  // The WebSocket must connect even if the presets fetch fails (e.g. a
  // transient 401/network hiccup) — live state is the heart of the page.
  loadPresets().catch((e) => console.error('presets:', e)).finally(connect);
})();
