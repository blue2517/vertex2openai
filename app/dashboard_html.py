"""Dark modern dashboard HTML/CSS/JS inspired by agy-cli-bridge (zero external CDN dependencies)."""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>vertex2openai 控制台</title>
<style>
  :root {
    --bg: #0b1020;
    --panel: #121a2e;
    --panel-2: #18223b;
    --panel-3: #101729;
    --border: #273454;
    --border-soft: #1f2b47;
    --text: #e6ecf8;
    --muted: #8f9bb8;
    --primary: #4f7cff;
    --primary-hover: #3d69eb;
    --primary-soft: rgba(79, 124, 255, 0.14);
    --success: #22c55e;
    --success-bg: rgba(34, 197, 94, 0.14);
    --warn: #f59e0b;
    --warn-bg: rgba(245, 158, 11, 0.14);
    --danger: #ef4444;
    --danger-bg: rgba(239, 68, 68, 0.14);
    --info: #38bdf8;
    --info-bg: rgba(56, 189, 248, 0.14);
    --purple: #a78bfa;
    --purple-bg: rgba(167, 139, 250, 0.14);
    --radius: 14px;
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }
  * { box-sizing: border-box; -webkit-font-smoothing: antialiased; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  .hidden { display: none !important; }
  .container { max-width: 1240px; margin: 0 auto; padding: 20px 16px 56px; }

  /* Header */
  header {
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
    gap: 14px; padding: 14px 18px;
    background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
    margin-bottom: 16px;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.01em; }
  .brand p { margin: 2px 0 0; font-size: 12px; color: var(--muted); }
  .header-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }

  /* Inputs & Buttons */
  input, textarea, select {
    background: #0c1222; color: var(--text); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 11px; font-size: 13px; outline: none; transition: border-color .15s;
  }
  input:focus, textarea:focus, select:focus { border-color: var(--primary); }
  input:disabled, textarea:disabled, select:disabled { opacity: 0.45; cursor: not-allowed; }
  .inp { width: 100%; }
  .mono, .log { font-family: var(--mono); }
  button {
    border: 1px solid var(--border); background: var(--panel-2); color: var(--text);
    border-radius: 8px; padding: 7px 12px; font-size: 12.5px; font-weight: 500;
    cursor: pointer; transition: all .15s ease;
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  }
  button:hover:not(:disabled) { border-color: #415385; background: #202b4b; }
  button:disabled { opacity: 0.48; cursor: not-allowed; }
  button.primary, .btn {
    background: var(--primary); border-color: var(--primary); color: #fff;
  }
  button.primary:hover:not(:disabled), .btn:hover:not(:disabled) {
    background: var(--primary-hover); border-color: var(--primary-hover);
  }
  button.danger {
    background: var(--danger-bg); border-color: rgba(239,68,68,.36); color: #fca5a5;
  }
  button.danger:hover:not(:disabled) {
    background: rgba(239,68,68,.24); border-color: rgba(239,68,68,.6);
  }
  button.tiny-btn { padding: 4px 9px; font-size: 12px; border-radius: 6px; }

  /* Switch */
  .switch { position: relative; display: inline-block; width: 40px; height: 22px; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; cursor: pointer; inset: 0; background: #273454;
    border-radius: 999px; transition: .2s; border: 1px solid #35466f;
  }
  .slider:before {
    position: absolute; content: ""; height: 16px; width: 16px; left: 2px; top: 2px;
    background: #dce4f7; border-radius: 50%; transition: .2s;
  }
  .switch input:checked + .slider { background: var(--primary); border-color: var(--primary); }
  .switch input:checked + .slider:before { transform: translateX(18px); background: #fff; }

  /* Badges / Pills */
  .badge, .pill {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 600;
    white-space: nowrap; background: rgba(143,155,184,.12); color: var(--muted);
    border: 1px solid rgba(143,155,184,.25);
  }
  .badge-ok { background: var(--success-bg); color: #86efac; border-color: rgba(34,197,94,.3); }
  .badge-warn { background: var(--warn-bg); color: #fcd34d; border-color: rgba(245,158,11,.3); }
  .badge-err { background: var(--danger-bg); color: #fca5a5; border-color: rgba(239,68,68,.3); }
  .badge-info { background: var(--info-bg); color: #7dd3fc; border-color: rgba(56,189,248,.3); }
  .badge-primary, .pill-accent { background: var(--primary-soft); color: #93c5fd; border-color: rgba(79,124,255,.38); }
  .badge-purple { background: var(--purple-bg); color: #ddd6fe; border-color: rgba(167,139,250,.38); }
  .live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); box-shadow: 0 0 8px var(--success); }

  /* Nav Tabs */
  nav.main-tabs {
    display: flex; gap: 6px; margin-bottom: 18px;
    padding: 4px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; width: fit-content; max-width: 100%; overflow-x: auto;
  }
  .tab {
    padding: 7px 16px; font-size: 13px; font-weight: 600; color: var(--muted);
    border-radius: 9px; cursor: pointer; white-space: nowrap; transition: all .15s;
    border: 1px solid transparent;
  }
  .tab:hover:not(.active) { color: var(--text); background: rgba(255,255,255,.04); }
  .tab.active { background: var(--primary-soft); border-color: rgba(79,124,255,.42); color: #dbeafe; }

  /* Cards & Layout */
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 16px 18px;
  }
  .card-title {
    font-size: 14px; font-weight: 700; margin: 0 0 12px;
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
  }
  .grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
  @media (max-width: 820px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }
  .lbl { font-size: 12px; color: var(--muted); font-weight: 500; display: block; margin-bottom: 5px; }
  .muted { color: var(--muted); font-size: 12px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .row-wrap { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .stack { display: flex; flex-direction: column; gap: 11px; }
  .divider { border-top: 1px solid var(--border-soft); margin: 12px 0; }
  .mb-12 { margin-bottom: 12px; }
  .mb-16 { margin-bottom: 16px; }
  .mt-12 { margin-top: 12px; }
  .mt-16 { margin-top: 16px; }

  /* Summary KPI Bar (agy-cli-bridge style) */
  .summary-bar {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(195px, 1fr));
    gap: 12px; margin-bottom: 16px;
  }
  .summary-card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 13px 16px;
  }
  .summary-card .k { font-size: 12px; color: var(--muted); display: flex; align-items: center; justify-content: space-between; }
  .summary-card .v { font-size: 21px; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .summary-card .sub { font-size: 11.5px; color: var(--muted); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* Credential Cards Grid (agy-cli-bridge style) */
  .accounts-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(368px, 1fr));
    gap: 14px;
  }
  .acc-card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 16px; padding: 15px 16px;
    display: flex; flex-direction: column; gap: 12px;
    transition: border-color .15s, box-shadow .15s;
  }
  .acc-card.active-ch {
    border-color: rgba(79,124,255,.55);
    box-shadow: 0 0 0 1px rgba(79,124,255,.15), 0 10px 26px rgba(10,16,35,.4);
  }
  .acc-card.disabled-card { opacity: 0.62; }
  .acc-card-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
  .acc-identity { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
  .acc-logo {
    width: 34px; height: 34px; border-radius: 9px; background: #17203a;
    border: 1px solid var(--border); display: grid; place-items: center;
    font-size: 11px; font-weight: 800; font-family: var(--mono); flex-shrink: 0;
  }
  .acc-logo.exp { color: #93c5fd; border-color: rgba(79,124,255,.4); background: rgba(79,124,255,.12); }
  .acc-logo.ck { color: #ddd6fe; border-color: rgba(167,139,250,.4); background: rgba(167,139,250,.12); }
  .acc-id-text { min-width: 0; flex: 1; }
  .acc-title { font-size: 14.5px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .acc-subtitle { font-family: var(--mono); font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 1px; }
  .acc-meta-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .meta-pill {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 8px; border-radius: 6px; font-size: 11px;
    background: var(--panel-2); border: 1px solid var(--border);
    color: #c8d4ee; font-family: var(--mono); max-width: 100%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  /* Historical Usage 4-tile grid */
  .section-block {
    background: var(--panel-3); border: 1px solid var(--border-soft);
    border-radius: 11px; padding: 10px 11px;
  }
  .section-title {
    font-size: 11.5px; font-weight: 600; color: var(--muted); margin-bottom: 8px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .history-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 7px; }
  .history-tile {
    background: var(--panel); border: 1px solid var(--border-soft);
    border-radius: 8px; padding: 7px 8px; min-width: 0;
  }
  .history-tile-k { font-size: 11px; color: var(--muted); white-space: nowrap; }
  .history-tile-v { font-size: 14px; font-weight: 700; margin-top: 2px; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .history-tile-v.ok { color: #4ade80; }
  .history-tile-v.warn { color: #fbbf24; }
  .history-tile-v.err { color: #f87171; }

  /* 200-min Recent Status Strip */
  .recent-header { display: flex; align-items: center; justify-content: space-between; font-size: 11.5px; color: var(--muted); margin-bottom: 7px; }
  .recent-counts { display: inline-flex; align-items: center; gap: 10px; font-family: var(--mono); font-size: 11.5px; }
  .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px; }
  .dot-green { background: #22c55e; }
  .dot-red { background: #ef4444; }
  .recent-strip-row { display: flex; align-items: center; gap: 8px; }
  .recent-bars { display: grid; grid-template-columns: repeat(20, 1fr); gap: 3px; flex: 1; align-items: center; }
  .recent-bar {
    height: 11px; border-radius: 999px; background: #232f4d;
    transition: transform .12s ease; cursor: default;
  }
  .recent-bar:hover { transform: scaleY(1.25); }
  .recent-bar.s-ok { background: #22c55e; }
  .recent-bar.s-mix { background: #f59e0b; }
  .recent-bar.s-err { background: #ef4444; }
  .recent-rate-pill {
    font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 999px;
    white-space: nowrap; background: var(--success-bg); color: #86efac;
    border: 1px solid rgba(34,197,94,.3);
  }
  .recent-rate-pill.mid { background: var(--warn-bg); color: #fcd34d; border-color: rgba(245,158,11,.3); }
  .recent-rate-pill.low { background: var(--danger-bg); color: #fca5a5; border-color: rgba(239,68,68,.3); }
  .recent-rate-pill.idle { background: rgba(143,155,184,.1); color: var(--muted); border-color: rgba(143,155,184,.22); }
  .err-banner {
    background: var(--danger-bg); border: 1px solid rgba(239,68,68,.32); color: #fecaca;
    border-radius: 8px; padding: 6px 9px; font-size: 11.5px; word-break: break-word;
  }
  .acc-footer {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding-top: 8px; border-top: 1px solid var(--border-soft); margin-top: auto;
  }

  /* Channel Radio Cards */
  .mode-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  @media (max-width: 640px) { .mode-grid { grid-template-columns: 1fr; } }
  .mode-opt {
    border: 1px solid var(--border); background: var(--panel-3); border-radius: 11px;
    padding: 12px 14px; cursor: pointer; display: flex; align-items: flex-start; gap: 10px;
    transition: border-color .15s, background .15s;
  }
  .mode-opt:hover { border-color: #415385; }
  .mode-opt.selected { border-color: var(--primary); background: var(--primary-soft); }
  .mode-opt input[type=radio] { width: auto; margin-top: 3px; accent-color: var(--primary); }

  /* Edit Item Box */
  .item-box {
    background: var(--panel-3); border: 1px solid var(--border-soft);
    border-radius: 10px; padding: 12px;
  }
  .item-box.active-item { border-color: rgba(34,197,94,.45); }

  /* Data Table */
  .table-wrap {
    max-height: 380px; overflow: auto;
    border: 1px solid var(--border-soft); border-radius: 10px; background: var(--panel-3);
  }
  .data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .data-table th, .data-table td {
    text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border-soft); vertical-align: middle;
  }
  .data-table th {
    color: var(--muted); font-weight: 600; background: var(--panel); position: sticky; top: 0; z-index: 1;
  }

  /* Logs Terminal */
  .term-head {
    padding: 10px 14px; border-bottom: 1px solid var(--border-soft);
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    background: var(--panel-3);
  }
  .term-body {
    font-family: var(--mono); font-size: 12.5px; line-height: 1.55;
    padding: 12px 14px; height: 64vh; overflow-y: auto;
    background: #090e1c; word-break: break-word;
  }

  /* Modal */
  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(5,9,20,.78);
    display: none; align-items: center; justify-content: center; padding: 16px; z-index: 100;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    width: 100%; max-width: 960px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 16px; padding: 20px; max-height: 90vh; overflow-y: auto;
    box-shadow: 0 24px 60px rgba(0,0,0,.55);
  }
  .modal h2 {
    margin: 0 0 14px; font-size: 16.5px; display: flex; align-items: center;
    justify-content: space-between; gap: 12px;
  }

  /* Toast */
  .toast {
    position: fixed; right: 20px; bottom: 20px; background: #17213a;
    border: 1px solid var(--border); color: var(--text); padding: 10px 16px;
    border-radius: 10px; font-size: 13px; z-index: 200;
    box-shadow: 0 8px 28px rgba(0,0,0,.45); opacity: 0; pointer-events: none; transition: .2s;
  }
  .toast.show { opacity: 1; }
  .status-banner {
    background: var(--warn-bg); border: 1px solid rgba(245,158,11,.38); color: #fcd34d;
    border-radius: 10px; padding: 9px 14px; font-size: 12.5px; margin-bottom: 14px;
  }
  .text-ok { color: #86efac; }
  .text-err { color: #fca5a5; }
  .empty {
    background: var(--panel); border: 1px dashed var(--border); border-radius: var(--radius);
    padding: 28px; text-align: center; color: var(--muted); grid-column: 1 / -1; font-size: 13px;
  }
  ::-webkit-scrollbar { width: 7px; height: 7px; }
  ::-webkit-scrollbar-thumb { background: #273454; border-radius: 8px; }
  ::-webkit-scrollbar-thumb:hover { background: #3b4f7d; }
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="brand">
      <svg width="34" height="34" viewBox="0 0 40 40" fill="none" aria-hidden="true">
        <rect width="40" height="40" rx="10" fill="#4f7cff"/>
        <path d="M11 16 H27" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/>
        <path d="M23 12 L28.5 16 L23 20" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M29 24 H13" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-opacity=".9"/>
        <path d="M17 20 L11.5 24 L17 28" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" stroke-opacity=".9"/>
      </svg>
      <div>
        <h1>vertex2openai 控制台</h1>
        <p>Express API / Cookie 双通道 · 独立代理隔离 · 凭证历史与竞速调度</p>
      </div>
    </div>
    <div class="header-actions">
      <span class="badge badge-primary" id="mode-pill">通道 —</span>
      <span class="badge"><span class="live-dot"></span><span id="uptime">运行中</span></span>
      <button onclick="refreshAllOverview()" title="刷新统计与凭证状态">刷新</button>
      <button id="restart-service" onclick="restartService()" title="重启服务">重启</button>
      <button onclick="logout()">退出</button>
    </div>
  </header>

  <div id="restart-status" class="hidden status-banner" role="status" aria-live="polite"></div>

  <nav class="main-tabs">
    <div class="tab active" data-tab="overview" onclick="switchTab('overview')">概览与凭证</div>
    <div class="tab" data-tab="channel" onclick="switchTab('channel')">通道与代理</div>
    <div class="tab" data-tab="params" onclick="switchTab('params')">参数与调度</div>
    <div class="tab" data-tab="logs" onclick="switchTab('logs')">运行日志</div>
  </nav>

  <!-- Overview -->
  <section id="view-overview">
    <div class="summary-bar">
      <div class="summary-card">
        <div class="k"><span>当前通道</span><span id="kpi-channel-badge" class="badge badge-primary">—</span></div>
        <div class="v" id="kpi-creds">0 / 0</div>
        <div class="sub" id="kpi-creds-sub">Express / Cookie 凭证</div>
      </div>
      <div class="summary-card">
        <div class="k"><span>累计请求</span><span id="kpi-rate-badge" class="badge">—</span></div>
        <div class="v" id="s-total">0</div>
        <div class="sub">成功 <span id="s-success" class="text-ok">0</span> · 失败 <span id="s-error" class="text-err">0</span> · 重试 <span id="s-retries">0</span></div>
      </div>
      <div class="summary-card">
        <div class="k"><span>累计 Token</span></div>
        <div class="v" id="t-total">0</div>
        <div class="sub">输入 <span id="t-prompt">0</span> · 输出 <span id="t-comp">0</span></div>
      </div>
      <div class="summary-card">
        <div class="k"><span>平均耗时</span></div>
        <div class="v" id="kpi-latency">—</div>
        <div class="sub" id="kpi-ttft">首字均耗 —</div>
      </div>
      <div class="summary-card">
        <div class="k"><span>调度策略</span></div>
        <div class="v" id="kpi-dispatch" style="font-size:15px;margin-top:7px;">—</div>
        <div class="sub" id="kpi-dispatch-sub">—</div>
      </div>
    </div>

    <div class="card mb-16">
      <div class="row-wrap" style="justify-content:space-between;margin-bottom:10px;">
        <div class="row-wrap">
          <span style="font-size:13.5px;font-weight:700;">凭证健康度与历史</span>
          <button class="tiny-btn" id="flt-all" onclick="setCredFilter('all')">全部</button>
          <button class="tiny-btn" id="flt-express" onclick="setCredFilter('express')">Express</button>
          <button class="tiny-btn" id="flt-cookie" onclick="setCredFilter('cookie')">Cookie</button>
        </div>
        <div class="row-wrap">
          <button class="tiny-btn" onclick="switchTab('channel')">管理凭证</button>
          <button class="tiny-btn danger" onclick="resetAllStats()">清空统计</button>
        </div>
      </div>
      <div id="global-recent-strip" class="section-block mb-12"></div>
      <div id="cred-cards-grid" class="accounts-grid"></div>
    </div>

    <div class="card">
      <div class="card-title">
        <span>最近调用记录</span>
        <span class="muted" id="recent-events-count">0 条</span>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>凭证</th>
              <th>代理</th>
              <th>模型</th>
              <th>状态</th>
              <th>首字 / 总耗</th>
              <th>Token</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody id="recent-events-tbody">
            <tr><td colspan="8" style="text-align:center;color:var(--muted);">暂无调用记录</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- Channel -->
  <section id="view-channel" class="hidden">
    <div class="card mb-16">
      <div class="card-title"><span>上游通道</span></div>
      <div class="mode-grid">
        <label class="mode-opt" id="opt-api">
          <input type="radio" name="mode" value="api_key" onchange="updateMode('api_key')">
          <div>
            <div style="font-weight:600;font-size:13.5px;">Express API Key</div>
            <div class="muted">Vertex Express 官方 SDK</div>
          </div>
        </label>
        <label class="mode-opt" id="opt-web">
          <input type="radio" name="mode" value="web_proxy" onchange="updateMode('web_proxy')">
          <div>
            <div style="font-weight:600;font-size:13.5px;">Cookie 直连</div>
            <div class="muted">Studio batchGraphql</div>
          </div>
        </label>
      </div>
    </div>

    <!-- 出网代理 -->
    <div class="card mb-16">
      <div class="card-title">
        <span>出网代理</span>
        <span id="proxy-status" class="badge">读取中</span>
      </div>
      <div class="grid-2 mb-12">
        <div>
          <label class="lbl" for="proxy-mode">全局代理模式</label>
          <select id="proxy-mode" class="inp" onchange="proxyModeChanged()" disabled>
            <option value="environment">跟随环境变量（PROXY_URL）</option>
            <option value="custom">自定义代理</option>
            <option value="pool">代理池</option>
            <option value="direct">直接连接</option>
          </select>
        </div>
        <div id="proxy-pool-choice" class="hidden">
          <label class="lbl" for="proxy-pool-select">全局代理池节点</label>
          <select id="proxy-pool-select" class="inp" onchange="$('proxy-result').textContent=''" disabled></select>
        </div>
        <div id="proxy-custom" class="hidden">
          <label class="lbl" for="proxy-url">自定义代理地址</label>
          <div class="row-wrap">
            <input id="proxy-url" type="password" autocomplete="new-password" spellcheck="false" class="inp log" style="flex:1;min-width:0;" placeholder="socks5://user:pass@host:port" oninput="$('proxy-result').textContent=''" disabled>
            <label class="muted" style="white-space:nowrap;"><input id="proxy-show" type="checkbox" onchange="$('proxy-url').type=this.checked?'text':'password'" disabled> 显示</label>
          </div>
        </div>
      </div>
      <div class="row-wrap" style="justify-content:space-between;">
        <span id="proxy-effective" class="muted mono" role="status">读取中…</span>
        <div class="row-wrap">
          <button id="proxy-test" onclick="testNetworkProxy()" disabled>测试连接</button>
          <button id="proxy-save" class="primary" onclick="saveNetworkProxy()" disabled>保存代理</button>
        </div>
      </div>
      <p id="proxy-result" class="muted mt-12" style="margin-bottom:0;" role="status" aria-live="polite"></p>

      <div class="divider"></div>
      <div class="row-wrap mb-12" style="justify-content:space-between;">
        <div class="row-wrap">
          <span style="font-size:13px;font-weight:700;">代理池</span>
          <span id="proxy-pool-count" class="badge">0 个</span>
        </div>
        <button id="proxy-pool-current" class="tiny-btn" onclick="importCurrentProxy()" disabled>导入当前代理</button>
      </div>
      <div id="proxy-pool-list" class="stack mb-12"></div>
      <div class="item-box">
        <div id="proxy-entry-heading" style="font-size:12.5px;font-weight:600;margin-bottom:8px;">添加代理节点</div>
        <div class="grid-2 mb-12">
          <div>
            <label class="lbl" for="proxy-entry-name">名称</label>
            <input id="proxy-entry-name" maxlength="80" class="inp" placeholder="如：美国 A" disabled>
          </div>
          <div>
            <label class="lbl" for="proxy-entry-url">地址</label>
            <input id="proxy-entry-url" type="password" autocomplete="new-password" spellcheck="false" class="inp log" placeholder="socks5://user:pass@host:port" disabled>
          </div>
        </div>
        <div class="row-wrap" style="justify-content:space-between;">
          <label class="muted"><input id="proxy-entry-show" type="checkbox" onchange="$('proxy-entry-url').type=this.checked?'text':'password'" disabled> 显示输入</label>
          <div class="row-wrap">
            <button id="proxy-entry-cancel" class="hidden" onclick="cancelProxyEdit()" disabled>取消编辑</button>
            <button id="proxy-entry-save" class="primary" onclick="saveProxyEntry()" disabled>添加到代理池</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Express API Key 列表 -->
    <div id="keys-box" class="card mb-16">
      <div class="card-title">
        <div class="row-wrap">
          <span>Express API Key</span>
          <span id="keys-count" class="badge">0 个</span>
          <span id="keys-active-badge" class="badge badge-ok hidden">当前通道</span>
        </div>
        <div class="row-wrap">
          <button class="tiny-btn" onclick="probeAllKeys()">探测项目</button>
          <button class="tiny-btn" onclick="addKeyRow()">+ 添加 Key</button>
          <button class="primary tiny-btn" onclick="saveExpressKeys()">保存 Key 列表</button>
        </div>
      </div>
      <div id="keys-list" class="stack"></div>
      <p id="keys-empty" class="muted hidden">暂无 Express Key。</p>
    </div>

    <!-- Cookie 凭证列表 -->
    <div id="cookie-box" class="card">
      <div class="card-title">
        <div class="row-wrap">
          <span>Cookie 凭证</span>
          <span id="cookies-count" class="badge">0 个</span>
          <span id="cookies-active-badge" class="badge badge-ok hidden">当前通道</span>
        </div>
        <div class="row-wrap">
          <button class="tiny-btn" onclick="addCookieRow()">+ 添加 Cookie</button>
          <button id="cookies-save" class="primary tiny-btn" onclick="saveCookieCredentials()">保存 Cookie 列表</button>
        </div>
      </div>
      <div id="cookies-list" class="stack"></div>
      <p id="cookies-empty" class="muted hidden">暂无 Cookie 凭证。</p>
      <p id="cookies-result" class="muted mt-12" style="margin-bottom:0;" role="status" aria-live="polite"></p>
    </div>
  </section>

  <!-- Params -->
  <section id="view-params" class="hidden">
    <div class="card mb-16">
      <div class="row-wrap" style="justify-content:space-between;">
        <div class="row-wrap" style="flex:1;min-width:240px;">
          <select id="model-sel" class="inp" style="max-width:300px;" onchange="renderCaps()"></select>
          <span id="ov-badge" class="badge badge-primary hidden">模型专属</span>
          <div id="caps-summary" class="row-wrap"></div>
        </div>
        <div class="row-wrap">
          <button id="btn-scope-save" class="primary" onclick="saveModelOverride()">保存为全局默认</button>
          <button id="btn-scope-clear" class="hidden" onclick="clearModelOverride()">清除专属</button>
        </div>
      </div>
      <div id="ov-hint" class="muted mt-12"></div>
    </div>

    <div class="grid-2 mb-16">
      <div class="card">
        <div class="card-title"><span>思考与采样</span><span class="badge badge-primary">按模型</span></div>
        <div class="stack">
          <div class="grid-2">
            <div id="wrap-g3level">
              <label class="lbl" for="thinking_g3_level">思考档位 (thinking_level)</label>
              <select id="thinking_g3_level" class="inp">
                <option value="">自动</option>
                <option value="minimal">minimal</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
              </select>
            </div>
            <div>
              <label class="lbl" for="native_thinking_mode">原生思考模式</label>
              <select id="native_thinking_mode" class="inp">
                <option value="request">跟随请求</option>
                <option value="off">关闭原生思考</option>
                <option value="console">强制使用上方档位</option>
              </select>
            </div>
          </div>
          <div id="think-note" class="muted"></div>
          <div class="divider"></div>
          <div class="grid-3">
            <div><label class="lbl" for="default_temperature">temperature</label><input id="default_temperature" type="number" step="0.1" class="inp" placeholder="默认"></div>
            <div><label class="lbl" for="default_top_p">top_p</label><input id="default_top_p" type="number" step="0.05" class="inp" placeholder="默认"></div>
            <div><label class="lbl" for="default_max_tokens">max_tokens</label><input id="default_max_tokens" type="number" class="inp" placeholder="默认"></div>
          </div>
          <div class="row">
            <span style="font-size:13px;">采样参数策略</span>
            <select id="sampling_policy" class="inp" style="max-width:170px;">
              <option value="auto">自动判定</option>
              <option value="deprecated">强制剥离</option>
              <option value="allowed">强制保留</option>
            </select>
          </div>
          <div id="sampling-note" class="muted"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>生图参数</span><span class="badge badge-primary">按模型</span></div>
        <div class="stack">
          <div class="grid-2">
            <div>
              <label class="lbl" for="image_size">分辨率 (image_size)</label>
              <select id="image_size" class="inp">
                <option value="512">512</option>
                <option value="1K">1K</option>
                <option value="2K">2K</option>
                <option value="4K">4K</option>
              </select>
            </div>
            <div>
              <label class="lbl" for="image_aspect_ratio">宽高比 (aspect_ratio)</label>
              <select id="image_aspect_ratio" class="inp"></select>
            </div>
          </div>
          <div class="row">
            <span style="font-size:13px;">生图传递 system 指令</span>
            <label class="switch"><input type="checkbox" id="image_system_instruction"><span class="slider"></span></label>
          </div>
          <div id="image-note" class="muted"></div>
        </div>
      </div>
    </div>

    <div class="grid-2 mb-16">
      <div class="card">
        <div class="card-title"><span>超时、重试与并发竞速</span><span class="badge">全局</span></div>
        <div class="stack">
          <div class="grid-3">
            <div>
              <label class="lbl" for="studio_read_timeout_seconds">Studio 读取超时(s)</label>
              <input id="studio_read_timeout_seconds" type="number" min="1" max="3600" step="1" class="inp" placeholder="180">
            </div>
            <div>
              <label class="lbl" for="express_stream_read_timeout_seconds">Express 流式超时(s)</label>
              <input id="express_stream_read_timeout_seconds" type="number" min="1" max="3600" step="1" class="inp" placeholder="180">
            </div>
            <div>
              <label class="lbl" for="express_nonstream_read_timeout_seconds">Express 非流超时(s)</label>
              <input id="express_nonstream_read_timeout_seconds" type="number" min="1" max="3600" step="1" class="inp" placeholder="180">
            </div>
          </div>
          <div class="grid-2">
            <div><label class="lbl" for="retry_max">单凭证重试次数</label><input id="retry_max" type="number" class="inp"></div>
            <div><label class="lbl" for="retry_backoff_seconds">退避间隔(s)</label><input id="retry_backoff_seconds" type="number" step="0.5" class="inp"></div>
          </div>
          <div class="divider"></div>
          <div class="row">
            <span style="font-size:13px;font-weight:600;">跨凭证自动重试 / 失败补位</span>
            <label class="switch"><input type="checkbox" id="credential_failover_enabled"><span class="slider"></span></label>
          </div>
          <div class="row">
            <label for="credential_failover_max" style="font-size:13px;color:var(--muted);">跨凭证重试次数 (n)</label>
            <input id="credential_failover_max" type="number" min="0" max="50" step="1" class="inp" style="width:110px;" placeholder="3">
          </div>
          <div class="divider"></div>
          <div class="row">
            <span style="font-size:13px;font-weight:600;">多凭证并发竞争</span>
            <label class="switch"><input type="checkbox" id="credential_race_enabled"><span class="slider"></span></label>
          </div>
          <div class="grid-2">
            <div>
              <label class="lbl" for="credential_race_concurrency">并发凭证数</label>
              <input id="credential_race_concurrency" type="number" min="2" max="20" step="1" class="inp" placeholder="2">
            </div>
            <div>
              <label class="lbl" for="credential_race_delay_seconds">随机延迟窗口(s, t)</label>
              <input id="credential_race_delay_seconds" type="number" min="0" max="60" step="0.5" class="inp" placeholder="2">
            </div>
          </div>
          <div>
            <label class="lbl" for="credential_race_win_mode">胜出条件</label>
            <select id="credential_race_win_mode" class="inp">
              <option value="first_output">首字（首个有效内容/思考/工具）</option>
              <option value="first_packet">安全首包（HTTP 200 非错误首包）</option>
            </select>
          </div>
          <div class="row">
            <span style="font-size:13px;">并发竞争仅对真流式生效</span>
            <label class="switch"><input type="checkbox" id="credential_race_stream_only"><span class="slider"></span></label>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>路由、传输与图片压缩</span><span class="badge">全局</span></div>
        <div class="stack">
          <div class="row">
            <span style="font-size:13px;">Express 区域 (location)</span>
            <select id="express_location" class="inp" style="max-width:210px;">
              <option value="global">global（默认）</option>
              <option value="">自动路由</option>
              <optgroup label="美国">
                <option value="us-central1">us-central1</option>
                <option value="us-east1">us-east1</option>
                <option value="us-east4">us-east4</option>
                <option value="us-east5">us-east5</option>
                <option value="us-south1">us-south1</option>
                <option value="us-west1">us-west1</option>
                <option value="us-west4">us-west4</option>
              </optgroup>
              <optgroup label="欧洲">
                <option value="europe-west1">europe-west1</option>
                <option value="europe-west2">europe-west2</option>
                <option value="europe-west3">europe-west3</option>
                <option value="europe-west4">europe-west4</option>
                <option value="europe-west8">europe-west8</option>
                <option value="europe-west9">europe-west9</option>
                <option value="europe-southwest1">europe-southwest1</option>
                <option value="europe-central2">europe-central2</option>
                <option value="europe-north1">europe-north1</option>
              </optgroup>
              <optgroup label="亚太">
                <option value="asia-east1">asia-east1</option>
                <option value="asia-east2">asia-east2</option>
                <option value="asia-northeast1">asia-northeast1</option>
                <option value="asia-northeast2">asia-northeast2</option>
                <option value="asia-northeast3">asia-northeast3</option>
                <option value="asia-south1">asia-south1</option>
                <option value="asia-southeast1">asia-southeast1</option>
                <option value="asia-southeast2">asia-southeast2</option>
                <option value="australia-southeast1">australia-southeast1</option>
              </optgroup>
              <optgroup label="其它">
                <option value="northamerica-northeast1">northamerica-northeast1</option>
                <option value="southamerica-east1">southamerica-east1</option>
                <option value="me-central1">me-central1</option>
                <option value="me-west1">me-west1</option>
              </optgroup>
            </select>
          </div>
          <div class="row">
            <span style="font-size:13px;">多凭证轮询（关闭为随机）</span>
            <label class="switch"><input type="checkbox" id="roundrobin"><span class="slider"></span></label>
          </div>
          <div class="row">
            <span style="font-size:13px;">假流式 (fake streaming)</span>
            <label class="switch"><input type="checkbox" id="fake_streaming"><span class="slider"></span></label>
          </div>
          <div class="row">
            <span style="font-size:13px;color:var(--muted);">假流式心跳间隔(s)</span>
            <input id="fake_streaming_interval" type="number" step="0.5" class="inp" style="width:100px;">
          </div>
          <div class="divider"></div>
          <div class="row">
            <span style="font-size:13px;">弱网续传缓冲</span>
            <label class="switch"><input type="checkbox" id="resumable_enabled"><span class="slider"></span></label>
          </div>
          <div class="row">
            <span style="font-size:13px;">断连后等待重连</span>
            <label class="switch"><input type="checkbox" id="continue_on_disconnect"><span class="slider"></span></label>
          </div>
          <div class="grid-3">
            <div><label class="lbl" for="disconnect_grace_seconds">重连宽限(s)</label><input id="disconnect_grace_seconds" type="number" min="0" max="300" step="1" class="inp" placeholder="5"></div>
            <div><label class="lbl" for="resumable_ttl_seconds">缓冲保留(s)</label><input id="resumable_ttl_seconds" type="number" step="60" class="inp"></div>
            <div><label class="lbl" for="resumable_max_streams">缓冲上限</label><input id="resumable_max_streams" type="number" step="10" class="inp"></div>
          </div>
          <div class="divider"></div>
          <div class="row">
            <span style="font-size:13px;">输入图片压缩</span>
            <label class="switch"><input type="checkbox" id="img_compress_enabled"><span class="slider"></span></label>
          </div>
          <div class="grid-3">
            <div><label class="lbl" for="img_compress_max_dim">最长边(px)</label><input id="img_compress_max_dim" type="number" class="inp"></div>
            <div><label class="lbl" for="img_compress_max_mb">阈值(MB)</label><input id="img_compress_max_mb" type="number" step="0.1" class="inp"></div>
            <div><label class="lbl" for="img_compress_quality">JPEG 质量</label><input id="img_compress_quality" type="number" class="inp"></div>
          </div>
          <div class="divider"></div>
          <div class="row">
            <span style="font-size:13px;">出站参数调试日志</span>
            <label class="switch"><input type="checkbox" id="debug_outbound"><span class="slider"></span></label>
          </div>
          <div class="row">
            <span style="font-size:13px;">Cookie 额外诊断</span>
            <label class="switch"><input type="checkbox" id="cookie_debug"><span class="slider"></span></label>
          </div>
        </div>
      </div>
    </div>

    <div class="row-wrap" style="justify-content:flex-end;">
      <button class="primary" onclick="saveSettings()">保存全局设置</button>
    </div>
  </section>

  <!-- Logs -->
  <section id="view-logs" class="hidden">
    <div class="card" style="padding:0;overflow:hidden;">
      <div class="term-head">
        <span style="font-size:12.5px;font-weight:600;color:var(--muted);">实时运行日志</span>
        <input id="log-filter" class="inp" style="max-width:260px;padding:5px 10px;font-size:12px;" placeholder="过滤凭证 / 模型 / 状态…" oninput="applyLogFilter()">
        <div class="row-wrap" style="margin-left:auto;">
          <button class="tiny-btn" onclick="clearLogView()">清屏</button>
          <button class="primary tiny-btn" onclick="scrollLogsToLatest()">回到最新</button>
        </div>
      </div>
      <div id="logwin" class="term-body"></div>
    </div>
  </section>
</div>

<!-- Credential Detail Modal -->
<div id="detailModal" class="modal-backdrop">
  <div class="modal">
    <h2>
      <span id="detailTitle">凭证详情</span>
      <button class="tiny-btn" onclick="closeDetailModal()">✕</button>
    </h2>
    <div id="detailBody"></div>
    <div class="row-wrap mt-16" style="justify-content:flex-end;">
      <button id="detailResetBtn" class="danger" onclick="resetCurrentCredStats()">清空此凭证统计</button>
      <button onclick="closeDetailModal()">关闭</button>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
const $ = id => document.getElementById(id);
let CAPS = {}, curAR = "";
let GLOBAL_SETTINGS = {}, OVERRIDES = {};
let OVERVIEW_DATA = null, CRED_FILTER = 'all', DETAIL_CRED_ID = '';
const PER_MODEL_KEYS = ['native_thinking_mode','thinking_g3_level','image_size','image_aspect_ratio','default_temperature','default_top_p','default_max_tokens','image_system_instruction','sampling_policy'];
const COMMON_ARS = ["1:1","3:2","2:3","3:4","4:3","4:5","5:4","9:16","16:9","21:9","1:4","4:1","1:8","8:1","9:21"];

function toast(m){ const t=$('toast'); t.textContent=m; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),2000); }
function fmt(n){ return (n||0).toLocaleString('en-US'); }
function fmtCompact(n){
  n = Number(n || 0);
  if(n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if(n >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return String(n);
}
function fmtMs(ms){
  if(ms === null || ms === undefined) return '—';
  return ms >= 1000 ? (ms/1000).toFixed(1) + 's' : ms + 'ms';
}
function fmtTime(ms){
  if(!ms) return '—';
  const d = new Date(ms);
  const pad = v => String(v).padStart(2,'0');
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function rateClass(r){
  if(r === null || r === undefined) return '';
  return r >= 90 ? 'ok' : (r >= 60 ? 'warn' : 'err');
}
async function logout(){ try{ await fetch('/api/logout',{method:'POST'}); }catch(e){} location.href='/'; }

/* ---------- Service restart ---------- */
let restartBusy=false;
async function serviceFetch(url, options={}){
  const controller=new AbortController();
  const timeout=setTimeout(()=>controller.abort(),5000);
  try{
    const response=await fetch(url,{...options,cache:'no-store',signal:controller.signal});
    const data=await response.json();
    return {ok:response.ok,status:response.status,data};
  }
  finally{ clearTimeout(timeout); }
}
async function restartService(){
  if(restartBusy) return;
  if(!confirm('确认重启服务？\n进行中的请求可能中断，未保存的修改会丢失。已保存的配置会保留，恢复后需要重新登录。')) return;
  restartBusy=true;
  const button=$('restart-service'), status=$('restart-status');
  button.disabled=true; button.textContent='重启中…';
  status.classList.remove('hidden'); status.textContent='正在提交重启请求…';
  try{
    const before=await serviceFetch('/api/service/status');
    if(!before.ok) throw new Error('服务状态读取失败，请稍后重试。');
    const previous=before.data.instance_id;
    if(!previous) throw new Error('服务实例标识缺失，请刷新页面后重试。');
    let response;
    try{
      response=await serviceFetch('/api/service/restart',{
        method:'POST',headers:{'X-Requested-With':'service-restart'}
      });
    }catch(e){}
    if(response && !response.ok){
      if(response.status===401) throw new Error('登录已过期，请刷新页面重新登录。');
      throw new Error(response.data.detail||'重启请求提交失败。');
    }
    status.textContent='正在等待服务重新启动（最多约 3 分钟），恢复后将自动刷新并返回登录页…';
    const deadline=Date.now()+180000;
    while(Date.now()<deadline){
      await new Promise(resolve=>setTimeout(resolve,1500));
      let state;
      try{
        const result=await serviceFetch('/api/service/status');
        if(result.ok) state=result.data;
      }catch(e){}
      if(state?.instance_id && state.instance_id!==previous){
        status.textContent='服务已重新启动，正在刷新…';
        location.reload();
        return;
      }
      if(state?.error) throw new Error(state.error);
    }
    throw new Error('等待超时，尚未确认服务恢复。请稍后刷新，或检查 systemd 服务状态与日志。');
  }catch(e){
    status.textContent=e.message||'重启操作异常，请检查服务状态。';
  }finally{
    restartBusy=false; button.disabled=false; button.textContent='重启';
  }
}
/* ---------- End service restart ---------- */

function switchTab(t){
  document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('active', e.dataset.tab===t));
  ['overview','channel','params','logs'].forEach(v=>$('view-'+v).classList.toggle('hidden', v!==t));
  if(t==='logs') scrollLogsToLatest();
}

/* ---------- Overview ---------- */
function renderRecentStripHTML(rs, title){
  const blocks = (rs && rs.blocks) || [];
  const succ = rs ? (rs.total_success || 0) : 0;
  const fail = rs ? (rs.total_failure || 0) : 0;
  const has = !!(rs && rs.has_data);
  const rate = has ? rs.success_rate : null;
  const pillCls = !has ? 'idle' : (rate >= 90 ? '' : (rate >= 60 ? 'mid' : 'low'));
  const bars = blocks.map(b => {
    const cls = b.state === 'success' ? 's-ok' : (b.state === 'mixed' ? 's-mix' : (b.state === 'failure' ? 's-err' : ''));
    const tip = `${fmtTime(b.start_ms)} ~ ${fmtTime(b.end_ms)} | 成功 ${b.success} / 失败 ${b.failure}`;
    return `<div class="recent-bar ${cls}" title="${escapeProxyHTML(tip)}"></div>`;
  }).join('');
  return `
    <div class="recent-header">
      <span>${escapeProxyHTML(title || '最近状态 (200分钟)')}</span>
      <span class="recent-counts">
        <span><span class="dot dot-green"></span>${succ}</span>
        <span><span class="dot dot-red"></span>${fail}</span>
      </span>
    </div>
    <div class="recent-strip-row">
      <div class="recent-bars">${bars}</div>
      <span class="recent-rate-pill ${pillCls}">${has ? (rate + '%') : '无数据'}</span>
    </div>`;
}

function setCredFilter(f){
  CRED_FILTER = f;
  ['all','express','cookie'].forEach(k => {
    const el = $('flt-'+k);
    if(el) el.className = 'tiny-btn' + (k===f ? ' primary' : '');
  });
  renderCredentialCards();
}

function renderCredentialCards(){
  const grid = $('cred-cards-grid');
  if(!grid || !OVERVIEW_DATA) return;
  const activeChannel = OVERVIEW_DATA.use_web_proxy ? 'cookie' : 'express';
  let items = OVERVIEW_DATA.credentials || [];
  if(CRED_FILTER !== 'all') items = items.filter(c => c.channel === CRED_FILTER);
  if(!items.length){
    grid.innerHTML = '<div class="empty">暂无凭证，请前往「通道与代理」添加。</div>';
    return;
  }
  grid.innerHTML = items.map(c => {
    const h = c.history || {};
    const reqs = h.total_requests || 0;
    const rate = h.success_rate;
    const rateText = rate === null || rate === undefined ? '—' : (rate + '%');
    const isExp = c.channel === 'express';
    const isActiveCh = c.channel === activeChannel;
    const cid = escapeProxyHTML(c.cred_id);
    return `
      <div class="acc-card ${isActiveCh && c.enabled ? 'active-ch' : ''} ${c.enabled ? '' : 'disabled-card'}">
        <div class="acc-card-header">
          <div class="acc-identity">
            <div class="acc-logo ${isExp ? 'exp' : 'ck'}">${isExp ? 'EXP' : 'CK'}</div>
            <div class="acc-id-text">
              <div class="acc-title">${escapeProxyHTML(c.label)}</div>
              <div class="acc-subtitle">${escapeProxyHTML(c.subtitle || '—')}</div>
            </div>
          </div>
          <span class="badge ${c.enabled ? 'badge-ok' : ''}">${c.enabled ? '已启用' : '已禁用'}</span>
        </div>
        <div class="acc-meta-row">
          <span class="badge ${isExp ? 'badge-primary' : 'badge-purple'}">${isExp ? 'Express' : 'Cookie'}</span>
          <span class="meta-pill" title="${escapeProxyHTML(c.proxy_label || '直连')}">${escapeProxyHTML(c.proxy_label || '直连')}</span>
          ${c.project_id ? `<span class="meta-pill">proj:${escapeProxyHTML(c.project_id)}</span>` : ''}
        </div>
        <div class="section-block">
          <div class="section-title"><span>历史用量</span><span>${h.last_seen_ms ? ('最近 ' + fmtTime(h.last_seen_ms)) : '尚无调用'}</span></div>
          <div class="history-grid">
            <div class="history-tile"><div class="history-tile-k">请求</div><div class="history-tile-v">${fmt(reqs)}</div></div>
            <div class="history-tile"><div class="history-tile-k">Token</div><div class="history-tile-v">${fmtCompact(h.total_tokens || 0)}</div></div>
            <div class="history-tile"><div class="history-tile-k">均耗</div><div class="history-tile-v">${fmtMs(h.avg_latency_ms)}</div></div>
            <div class="history-tile"><div class="history-tile-k">成功率</div><div class="history-tile-v ${rateClass(rate)}">${rateText}</div></div>
          </div>
        </div>
        <div class="section-block">${renderRecentStripHTML(c.recent_status, '最近状态 (200分钟)')}</div>
        ${h.last_error ? `<div class="err-banner">HTTP ${h.last_status || '?'} · ${escapeProxyHTML(h.last_error)}</div>` : ''}
        <div class="acc-footer">
          <span class="muted mono">${h.last_model ? escapeProxyHTML(h.last_model) : '—'}</span>
          <div class="row-wrap">
            <button class="tiny-btn" onclick="openCredDetail('${cid}')">详情</button>
          </div>
        </div>
      </div>`;
  }).join('');
}

function renderRecentEventsTable(events, tbodyId){
  const tbody = $(tbodyId);
  if(!tbody) return;
  if(!events || !events.length){
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--muted);">暂无调用记录</td></tr>';
    return;
  }
  tbody.innerHTML = events.map(ev => {
    const modelTxt = ev.request_model && ev.upstream_model && ev.request_model !== ev.upstream_model
      ? `${ev.request_model} → ${ev.upstream_model}` : (ev.upstream_model || ev.request_model || '—');
    const stCls = ev.success ? 'badge-ok' : (ev.status_code === 429 ? 'badge-warn' : 'badge-err');
    return `<tr>
      <td class="mono" style="white-space:nowrap;">${fmtTime(ev.ts_ms)}</td>
      <td>${escapeProxyHTML(ev.cred_label || ev.cred_id)}</td>
      <td class="mono">${escapeProxyHTML(ev.proxy_label || '—')}</td>
      <td class="mono">${escapeProxyHTML(modelTxt)}</td>
      <td><span class="badge ${stCls}">${ev.success ? '200' : ('HTTP ' + ev.status_code)}</span></td>
      <td class="mono" style="white-space:nowrap;">${fmtMs(ev.ttft_ms)} / ${fmtMs(ev.latency_ms)}</td>
      <td class="mono">${fmtCompact(ev.total_tokens || 0)}</td>
      <td style="color:${ev.error ? '#fca5a5' : 'var(--muted)'};max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeProxyHTML(ev.error || ev.mode || '')}">${escapeProxyHTML(ev.error || ev.mode || '—')}</td>
    </tr>`;
  }).join('');
}

async function fetchStats(){
  try{
    const d = await (await fetch('/api/credentials/overview',{cache:'no-store'})).json();
    OVERVIEW_DATA = d;
    const g = d.global || {};
    const sess = d.session || {};
    const modeLabel = d.use_web_proxy ? 'Cookie 直连' : 'Express API';
    $('mode-pill').textContent = modeLabel;
    $('kpi-channel-badge').textContent = d.use_web_proxy ? 'Cookie' : 'Express';
    const creds = d.credentials || [];
    const activeChCreds = creds.filter(c => c.channel === (d.use_web_proxy ? 'cookie' : 'express'));
    const enabledCount = activeChCreds.filter(c => c.enabled).length;
    $('kpi-creds').textContent = `${enabledCount} / ${activeChCreds.length} 已启用`;
    const expTotal = creds.filter(c => c.channel === 'express').length;
    const ckTotal = creds.filter(c => c.channel === 'cookie').length;
    $('kpi-creds-sub').textContent = `Express ${expTotal} · Cookie ${ckTotal}`;

    $('s-total').textContent = fmt(g.total_requests || sess.total || 0);
    $('s-success').textContent = fmt(g.success_calls || sess.success || 0);
    $('s-error').textContent = fmt(g.failure_calls || sess.error || 0);
    $('s-retries').textContent = fmt(sess.retries || 0);
    const rateBadge = $('kpi-rate-badge');
    if(g.success_rate !== null && g.success_rate !== undefined){
      rateBadge.textContent = g.success_rate + '%';
      rateBadge.className = 'badge ' + (g.success_rate >= 90 ? 'badge-ok' : (g.success_rate >= 60 ? 'badge-warn' : 'badge-err'));
    } else {
      rateBadge.textContent = '—';
      rateBadge.className = 'badge';
    }

    $('t-prompt').textContent = fmtCompact(g.prompt_tokens || 0);
    $('t-comp').textContent = fmtCompact(g.completion_tokens || 0);
    $('t-total').textContent = fmtCompact(g.total_tokens || 0);
    $('kpi-latency').textContent = fmtMs(g.avg_latency_ms);
    $('kpi-ttft').textContent = '首字均耗 ' + fmtMs(g.avg_ttft_ms);
    $('uptime').textContent = '运行 ' + ((d.uptime || 0)/3600).toFixed(1) + 'h';

    const disp = d.dispatch || {};
    const parts = [];
    if(disp.credential_race_enabled) parts.push(`竞速×${disp.credential_race_concurrency}`);
    if(disp.credential_failover_enabled) parts.push(`补位×${disp.credential_failover_max}`);
    $('kpi-dispatch').textContent = parts.length ? parts.join(' + ') : '单凭证直调';
    $('kpi-dispatch-sub').textContent = (d.roundrobin ? '轮询' : '随机') + (disp.credential_race_enabled ? (` · 窗口 ${disp.credential_race_delay_seconds}s · ${disp.credential_race_win_mode==='first_packet'?'首包':'首字'}`) : '');

    $('global-recent-strip').innerHTML = renderRecentStripHTML(d.recent_status, '全局最近状态 (200分钟)');
    renderCredentialCards();
    const events = d.recent_events || [];
    $('recent-events-count').textContent = events.length + ' 条';
    renderRecentEventsTable(events, 'recent-events-tbody');
  }catch(e){}
}

function refreshAllOverview(){ fetchStats(); loadRuntime(); loadExpressKeys(); loadCookieCredentials(); loadNetworkProxy(); toast('已刷新'); }

async function openCredDetail(credId){
  DETAIL_CRED_ID = credId;
  $('detailTitle').textContent = '凭证详情';
  $('detailBody').innerHTML = '<div class="muted">加载中…</div>';
  $('detailModal').classList.add('open');
  try{
    const d = await (await fetch('/api/credentials/' + encodeURIComponent(credId) + '/history',{cache:'no-store'})).json();
    const h = d.history || {};
    const card = (OVERVIEW_DATA?.credentials || []).find(c => c.cred_id === credId);
    $('detailTitle').textContent = `${card ? card.label : credId} · 调用历史`;
    const rate = h.success_rate;
    $('detailBody').innerHTML = `
      <div class="history-grid mb-12">
        <div class="history-tile"><div class="history-tile-k">总请求</div><div class="history-tile-v">${fmt(h.total_requests || 0)}</div></div>
        <div class="history-tile"><div class="history-tile-k">成功率</div><div class="history-tile-v ${rateClass(rate)}">${rate===null||rate===undefined?'—':(rate+'%')}</div></div>
        <div class="history-tile"><div class="history-tile-k">累计 Token</div><div class="history-tile-v">${fmtCompact(h.total_tokens || 0)}</div></div>
        <div class="history-tile"><div class="history-tile-k">首字 / 总耗</div><div class="history-tile-v">${fmtMs(h.avg_ttft_ms)} / ${fmtMs(h.avg_latency_ms)}</div></div>
      </div>
      <div class="section-block mb-12">${renderRecentStripHTML(d.recent_status, '最近状态 (200分钟)')}</div>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>时间</th><th>凭证</th><th>代理</th><th>模型</th><th>状态</th><th>首字 / 总耗</th><th>Token</th><th>备注</th></tr></thead>
          <tbody id="detail-events-tbody"></tbody>
        </table>
      </div>`;
    renderRecentEventsTable(d.recent_events || [], 'detail-events-tbody');
  }catch(e){
    $('detailBody').innerHTML = '<div class="err-banner">加载失败</div>';
  }
}
function closeDetailModal(){ $('detailModal').classList.remove('open'); DETAIL_CRED_ID = ''; }
async function resetCurrentCredStats(){
  if(!DETAIL_CRED_ID || !confirm('确认清空该凭证的历史统计？')) return;
  await fetch('/api/credentials/' + encodeURIComponent(DETAIL_CRED_ID) + '/stats/reset', {method:'POST'});
  toast('已清空该凭证统计');
  closeDetailModal();
  fetchStats();
}
async function resetAllStats(){
  if(!confirm('确认清空所有凭证的历史统计与调用记录？')) return;
  await fetch('/api/stats/reset', {method:'POST'});
  toast('已清空全部统计');
  fetchStats();
}

/* ---------- Channel ---------- */
function applyChannelModeUI(m){
  const isCookie = m === 'web_proxy';
  $('mode-pill').textContent = isCookie ? 'Cookie 直连' : 'Express API';
  if($('opt-api')) $('opt-api').classList.toggle('selected', !isCookie);
  if($('opt-web')) $('opt-web').classList.toggle('selected', isCookie);
  if($('keys-active-badge')) $('keys-active-badge').classList.toggle('hidden', isCookie);
  if($('cookies-active-badge')) $('cookies-active-badge').classList.toggle('hidden', !isCookie);
}
async function updateMode(m){
  applyChannelModeUI(m);
  await fetch('/api/settings/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})});
}
function parseCookies(str){
  str=(str||'').trim();
  if(str.startsWith('[')&&str.endsWith(']')){ try{ const a=JSON.parse(str); if(Array.isArray(a)) return a.map(c=>{const n=c.name||c.key,v=c.value; return (n&&v)?`${n}=${v}`:'';}).filter(Boolean).join('; ');}catch(e){} }
  return str;
}
let COOKIES=[], COOKIES_READY=false;
function credentialProxyValue(rec){ return rec.proxy_mode==='pool'?'pool:'+(rec.proxy_id||''):(rec.proxy_mode||'inherit'); }
function credentialProxyOptions(value){
  const pool=PROXY_STATE?.pool||[];
  const choices=[['inherit','跟随全局代理'],['direct','直接连接'],...pool.map(p=>['pool:'+p.id,p.name+' · '+p.url_masked])];
  if(value.startsWith('pool:')&&!choices.some(p=>p[0]===value)) choices.push([value,'绑定的代理已失效，请重新选择']);
  return choices.map(([v,label])=>`<option value="${escapeProxyHTML(v)}" ${v===value?'selected':''}>${escapeProxyHTML(label)}</option>`).join('');
}
function readCredentialProxy(value){ return value.startsWith('pool:')?{proxy_mode:'pool',proxy_id:value.slice(5)}:{proxy_mode:value,proxy_id:''}; }
function refreshCredentialProxySelectors(){
  document.querySelectorAll('.credential-proxy').forEach(el=>{const v=el.value; el.innerHTML=credentialProxyOptions(v); el.value=v;});
}
function renderCookies(){
  $('cookies-list').innerHTML=COOKIES.map((rec,i)=>`<div class="item-box" data-cookie-i="${i}">
    <div class="row-wrap mb-12"><span class="badge">#${i+1}</span>
      <input class="inp c-name" style="flex:1;min-width:140px;" maxlength="80" aria-label="凭证名称" placeholder="凭证名称" value="${escapeProxyHTML(rec.name)}">
      <label class="muted" style="white-space:nowrap;"><input type="checkbox" class="c-enabled" ${rec.enabled===false?'':'checked'}> 启用</label>
      <button class="danger tiny-btn" onclick="removeCookieRow(${i})">删除</button></div>
    <label class="lbl">Google Cookie</label>
    <textarea class="inp log c-cookie mb-12" rows="2" autocomplete="off" spellcheck="false" placeholder="${escapeProxyHTML(rec.cookie_configured?rec.cookie_masked+'；留空保留':'粘贴完整 Cookie 或 Cookie-Editor JSON')}">${escapeProxyHTML(rec.cookie||'')}</textarea>
    <div class="grid-2">
      <div><label class="lbl">Project ID</label><input class="inp log c-project" value="${escapeProxyHTML(rec.project_id)}" placeholder="项目 ID 或 Studio URL"></div>
      <div><label class="lbl">出网代理</label><select class="inp credential-proxy c-proxy">${credentialProxyOptions(credentialProxyValue(rec))}</select></div>
    </div>
  </div>`).join('');
  $('cookies-count').textContent=COOKIES.length+' 个';
  $('cookies-empty').classList.toggle('hidden',COOKIES.length>0);
}
function collectCookies(){
  document.querySelectorAll('[data-cookie-i]').forEach(row=>{
    const rec=COOKIES[Number(row.dataset.cookieI)]; if(!rec) return;
    rec.name=row.querySelector('.c-name').value.trim();
    rec.cookie=parseCookies(row.querySelector('.c-cookie').value);
    rec.project_id=row.querySelector('.c-project').value.trim();
    const m=rec.project_id.match(/[?&]project=([^&]+)/); if(m) rec.project_id=decodeURIComponent(m[1]);
    rec.enabled=row.querySelector('.c-enabled').checked;
    Object.assign(rec,readCredentialProxy(row.querySelector('.c-proxy').value));
  });
  return COOKIES.map(({id,name,cookie,project_id,enabled,proxy_mode,proxy_id})=>({id,name,cookie:cookie||'',project_id,enabled,proxy_mode,proxy_id}));
}
function addCookieRow(){if(!COOKIES_READY){toast('请先完成 Cookie 列表读取。');return;} collectCookies(); COOKIES.push({name:'Cookie '+(COOKIES.length+1),cookie:'',project_id:'',enabled:true,proxy_mode:'inherit'}); renderCookies();}
function removeCookieRow(i){collectCookies(); COOKIES.splice(i,1); renderCookies();}
async function loadCookieCredentials(){
  try{const r=await fetch('/api/cookie-credentials',{cache:'no-store'}); const d=await r.json(); if(!r.ok) throw new Error(d.error||'读取失败'); COOKIES=d.credentials||[]; COOKIES_READY=true; renderCookies();}
  catch(e){$('cookies-result').textContent=e.message||'Cookie 列表读取失败，请刷新后重试。';}
}
async function saveCookieCredentials(){
  if(!COOKIES_READY){$('cookies-result').textContent='请先完成 Cookie 列表读取，刷新后重试。'; return;}
  const credentials=collectCookies();
  if(!credentials.length&&!confirm('保存空 Cookie 列表后将停止 Cookie 调用，确定保存？')) return;
  const button=$('cookies-save'); button.disabled=true;
  try{
    const r=await fetch('/api/cookie-credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({credentials})});
    const d=await r.json(); if(!r.ok) throw new Error(d.error||'保存失败');
    COOKIES=d.credentials||[]; renderCookies();
    $('cookies-result').textContent='已保存 '+d.total+' 条凭证（启用 '+d.enabled+' 条）';
    await loadNetworkProxy();
  }catch(e){$('cookies-result').textContent=e.message||'网络请求失败';}
  finally{button.disabled=false;}
}
async function loadRuntime(){
  try{
    const s=await (await fetch('/api/settings/runtime')).json();
    const m=s.use_web_proxy?'web_proxy':'api_key';
    const radio = document.querySelector(`input[name=mode][value=${m}]`);
    if(radio) radio.checked=true;
    applyChannelModeUI(m);
  }catch(e){}
}

/* ---------- 出网代理（仅回显掩码，空输入保留原认证信息） ---------- */
let PROXY_STATE = null;
let PROXY_BUSY = false, PROXY_EDIT_ID = '';
function escapeProxyHTML(value){
  return String(value||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function setProxyBusy(busy){
  PROXY_BUSY=busy;
  const locked=busy||!PROXY_STATE;
  ['proxy-test','proxy-save','proxy-mode','proxy-entry-name','proxy-entry-url','proxy-entry-show','proxy-entry-save','proxy-entry-cancel'].forEach(id=>{$(id).disabled=locked;});
  const mode=$('proxy-mode').value;
  $('proxy-url').disabled=locked||mode!=='custom'; $('proxy-show').disabled=locked||mode!=='custom';
  $('proxy-pool-select').disabled=locked||mode!=='pool'||!(PROXY_STATE?.pool||[]).length;
  $('proxy-pool-current').disabled=locked||!PROXY_STATE?.enabled||!!PROXY_STATE?.current_in_pool;
  $('proxy-pool-list').querySelectorAll('button').forEach(b=>{b.disabled=locked||b.dataset.locked==='1';});
}
function proxyModeChanged(){
  const mode=$('proxy-mode').value, custom=mode==='custom';
  $('proxy-custom').classList.toggle('hidden',!custom);
  $('proxy-pool-choice').classList.toggle('hidden',mode!=='pool');
  setProxyBusy(PROXY_BUSY);
  $('proxy-result').textContent='';
}
function renderProxyPool(s){
  const pool=s.pool||[], selected=pool.some(p=>p.id===s.active_id)?s.active_id:(pool[0]?.id||'');
  $('proxy-pool-count').textContent=pool.length+' 个';
  $('proxy-pool-select').innerHTML=pool.length?pool.map(p=>`<option value="${escapeProxyHTML(p.id)}">${escapeProxyHTML(p.name)} · ${escapeProxyHTML(p.url_masked)}</option>`).join(''):'<option value="">请先添加代理</option>';
  $('proxy-pool-select').value=selected;
  $('proxy-pool-list').innerHTML=pool.length?pool.map(p=>{
    const id=escapeProxyHTML(p.id), locked=p.active?'1':'0';
    const bound=(s.bound_credentials||{})[p.id]||0, deleteLocked=p.active||bound>0;
    return `<div class="item-box ${p.active?'active-item':''}">
      <div class="row-wrap" style="justify-content:space-between;">
        <div style="min-width:0;flex:1;">
          <div style="font-size:13px;font-weight:600;word-break:break-all;">${escapeProxyHTML(p.name)}${p.active?' <span class="badge badge-ok" style="margin-left:6px;">当前使用</span>':''}</div>
          <div class="muted mono" style="margin-top:3px;word-break:break-all;">${escapeProxyHTML(p.url_masked)}</div>
        </div>
        <div class="row-wrap">
          <button data-proxy-action="use" data-proxy-id="${id}" data-locked="${locked}" ${p.active?'disabled':''} class="primary tiny-btn">${p.active?'已启用':'使用'}</button>
          <button data-proxy-action="test" data-proxy-id="${id}" class="tiny-btn">测试</button>
          <button data-proxy-action="edit" data-proxy-id="${id}" class="tiny-btn">编辑</button>
          <button data-proxy-action="delete" data-proxy-id="${id}" data-locked="${deleteLocked?'1':'0'}" ${deleteLocked?'disabled title="先切换全局代理并解除凭证绑定"':''} class="danger tiny-btn">${bound?'已绑定 '+bound+' 条凭证':'删除'}</button>
        </div>
      </div>
      <p id="proxy-pool-result-${id}" class="muted" style="margin:6px 0 0;word-break:break-all;" role="status" aria-live="polite"></p>
    </div>`;
  }).join(''):'<p class="muted" style="margin:0;">代理池为空。</p>';
  $('proxy-pool-list').onclick=event=>{
    const button=event.target.closest('button[data-proxy-action]');
    if(!button||button.disabled||PROXY_BUSY) return;
    const id=button.dataset.proxyId, action=button.dataset.proxyAction;
    if(action==='use') activateProxyEntry(id);
    if(action==='test') testProxyEntry(id);
    if(action==='edit') editProxyEntry(id);
    if(action==='delete') deleteProxyEntry(id);
  };
}
function renderNetworkProxy(s){
  PROXY_STATE=s;
  $('proxy-mode').value=s.mode;
  $('proxy-url').value='';
  $('proxy-url').type='password';
  $('proxy-show').checked=false;
  const saved=s.custom_url_masked||s.environment_url_masked;
  $('proxy-url').placeholder=saved ? ('留空沿用 '+saved) : 'socks5://user:pass@host:port';
  $('proxy-status').textContent={environment:'跟随环境',custom:'自定义代理',direct:'直接连接',pool:'代理池'}[s.mode]||s.mode;
  const active=(s.pool||[]).find(p=>p.active);
  $('proxy-effective').textContent='当前出口：'+(active?(active.name+' · '):'')+(s.effective_url_masked||(s.mode==='environment'?'跟随环境变量':'直接连接'));
  renderProxyPool(s);
  proxyModeChanged();
  if(typeof refreshCredentialProxySelectors==='function') refreshCredentialProxySelectors();
}
async function loadNetworkProxy(){
  try{
    const r=await fetch('/api/proxy',{cache:'no-store'});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'代理配置读取失败');
    renderNetworkProxy(d);
  }catch(e){ $('proxy-status').textContent='读取失败'; $('proxy-effective').textContent='代理配置读取失败，请刷新页面重试。'; }
}
function networkProxyPayload(){
  const mode=$('proxy-mode').value;
  return {mode,proxy_url:mode==='custom'?($('proxy-url').value.trim()||null):null,proxy_id:mode==='pool'?($('proxy-pool-select').value||null):null};
}
async function proxyFetch(url,method,payload){
  const options={method,headers:{'Content-Type':'application/json'}};
  if(payload!==undefined) options.body=JSON.stringify(payload);
  const r=await fetch(url,options), d=await r.json();
  if(!r.ok) throw new Error(typeof d.detail==='string'?d.detail:'代理操作失败');
  return d;
}
function proxyResult(message,ok=true,id=''){
  const target=(id?$('proxy-pool-result-'+id):null)||$('proxy-result');
  target.className='muted '+(ok?'text-ok':'text-err');
  target.textContent=message;
}
async function submitNetworkProxy(test){
  if(!PROXY_STATE||PROXY_BUSY) return;
  const button=$(test?'proxy-test':'proxy-save'), label=button.textContent;
  const payload=networkProxyPayload();
  setProxyBusy(true);
  button.textContent=test?'测试中…':'保存中…';
  $('proxy-result').textContent='';
  try{
    const d=await proxyFetch(test?'/api/proxy/test':'/api/proxy','POST',payload);
    if(test){
      proxyResult(d.message+' '+d.elapsed_ms+' ms'+(d.proxy_url_masked?' · '+d.proxy_url_masked:''),d.ok);
    }else{
      renderNetworkProxy(d);
      proxyResult('代理已保存，立即生效');
      toast('代理设置已保存');
    }
  }catch(e){ proxyResult(e.message||'网络请求失败',false); }
  finally{
    button.textContent=label;
    setProxyBusy(false);
  }
}
function testNetworkProxy(){ return submitNetworkProxy(true); }
function saveNetworkProxy(){ return submitNetworkProxy(false); }
function cancelProxyEdit(){
  PROXY_EDIT_ID=''; $('proxy-entry-name').value=''; $('proxy-entry-url').value='';
  $('proxy-entry-url').type='password'; $('proxy-entry-show').checked=false;
  $('proxy-entry-url').placeholder='socks5://user:pass@host:port';
  $('proxy-entry-heading').textContent='添加代理节点'; $('proxy-entry-save').textContent='添加到代理池';
  $('proxy-entry-cancel').classList.toggle('hidden',true);
}
function editProxyEntry(id){
  if(PROXY_BUSY) return;
  const entry=(PROXY_STATE?.pool||[]).find(p=>p.id===id); if(!entry) return;
  PROXY_EDIT_ID=id; $('proxy-entry-name').value=entry.name; $('proxy-entry-url').value='';
  $('proxy-entry-url').type='password'; $('proxy-entry-show').checked=false;
  $('proxy-entry-url').placeholder='留空保留 '+entry.url_masked;
  $('proxy-entry-heading').textContent='编辑代理（地址留空保留原值）';
  $('proxy-entry-save').textContent='保存修改'; $('proxy-entry-cancel').classList.toggle('hidden',false);
  $('proxy-entry-name').focus();
}
async function proxyPoolAction(url,method,payload,message,id='',test=false){
  if(!PROXY_STATE||PROXY_BUSY) return false;
  setProxyBusy(true);
  if(test) proxyResult('测试中…',true,id);
  try{
    const d=await proxyFetch(url,method,payload);
    if(test){ proxyResult(d.message+' '+d.elapsed_ms+' ms',d.ok,id); }
    else{ renderNetworkProxy(d); proxyResult(message); toast(message); }
    return true;
  }catch(e){ proxyResult(e.message||'网络请求失败',false,id); return false; }
  finally{ setProxyBusy(false); }
}
async function saveProxyEntry(){
  const editing=PROXY_EDIT_ID;
  const payload={name:$('proxy-entry-name').value.trim(),proxy_url:$('proxy-entry-url').value.trim()||null};
  if(!payload.name){ proxyResult('请填写代理名称。',false); return; }
  if(await proxyPoolAction('/api/proxy/pool'+(editing?'/'+encodeURIComponent(editing):''),editing?'PUT':'POST',payload,editing?'代理已更新':'代理已加入列表')) cancelProxyEdit();
}
async function importCurrentProxy(){
  await proxyPoolAction('/api/proxy/pool/current','POST',{name:$('proxy-entry-name').value.trim()||'当前代理'},'当前代理已加入列表');
}
function activateProxyEntry(id){
  return proxyPoolAction('/api/proxy','POST',{mode:'pool',proxy_id:id,proxy_url:null},'已切换代理');
}
function testProxyEntry(id){
  return proxyPoolAction('/api/proxy/test','POST',{mode:'pool',proxy_id:id,proxy_url:null},'',id,true);
}
async function deleteProxyEntry(id){
  const entry=(PROXY_STATE?.pool||[]).find(p=>p.id===id);
  if(!entry||PROXY_BUSY||!confirm('删除代理“'+entry.name+'”？')) return;
  if(await proxyPoolAction('/api/proxy/pool/'+encodeURIComponent(id),'DELETE',undefined,'代理已删除')){
    if(PROXY_EDIT_ID===id) cancelProxyEdit();
  }
}

/* ---------- Express Key 列表（多账号，热编辑） ---------- */
let KEYS = [];
function keySrcPill(rec){
  if(rec.project_source==='auto') return '<span class="badge badge-info">auto</span>';
  if(rec.project_source==='manual') return '<span class="badge badge-primary">manual</span>';
  return '<span class="badge badge-warn">未探测</span>';
}
function renderKeys(){
  const box=$('keys-list');
  box.innerHTML = KEYS.map((rec,i)=>`
    <div class="item-box" data-i="${i}">
      <div class="row-wrap mb-12">
        <span class="badge">#${i+1}</span>
        <input class="inp k-note" style="max-width:160px;" maxlength="60" placeholder="备注名（可选）" value="${escapeProxyHTML(rec.note)}">
        <input class="inp log k-key" style="flex:1;min-width:180px;" placeholder="Express API Key" value="${escapeProxyHTML(rec.key)}">
        <label class="muted" style="white-space:nowrap;"><input type="checkbox" class="k-enabled" ${rec.enabled===false?'':'checked'}> 启用</label>
        <button class="tiny-btn" onclick="probeOneKey(${i})">探测</button>
        <button class="danger tiny-btn" onclick="removeKeyRow(${i})">删除</button>
      </div>
      <div class="grid-2">
        <div>
          <div class="row-wrap" style="justify-content:space-between;margin-bottom:5px;">
            <span class="lbl" style="margin:0;">Project ID</span>
            ${keySrcPill(rec)}
          </div>
          <input class="inp log k-project" placeholder="自动探测或手填" value="${escapeProxyHTML(rec.project_id)}">
        </div>
        <div>
          <label class="lbl">出网代理</label>
          <select class="inp credential-proxy k-proxy">${credentialProxyOptions(credentialProxyValue(rec))}</select>
        </div>
      </div>
      ${rec._msg ? `<div class="muted ${rec._msg_ok===false?'text-err':'text-ok'}" style="margin-top:6px;">${escapeProxyHTML(rec._msg)}</div>` : ''}
    </div>`).join('');
  $('keys-count').textContent = KEYS.length + ' 个';
  $('keys-empty').classList.toggle('hidden', KEYS.length>0);
}
function collectKeys(){
  [...document.querySelectorAll('#keys-list [data-i]')].forEach(row=>{
    const i=Number(row.dataset.i);
    if(!KEYS[i]) return;
    const noteEl = row.querySelector('.k-note');
    if(noteEl) KEYS[i].note = noteEl.value.trim();
    KEYS[i].key = row.querySelector('.k-key').value.trim();
    const pid = row.querySelector('.k-project').value.trim();
    if(pid !== (KEYS[i].project_id||'')){ KEYS[i].project_id = pid; KEYS[i].project_source = pid?'manual':''; }
    KEYS[i].enabled = row.querySelector('.k-enabled').checked;
    Object.assign(KEYS[i],readCredentialProxy(row.querySelector('.k-proxy').value));
  });
  return KEYS.filter(r=>r.key);
}
function addKeyRow(){ collectKeys(); KEYS.push({key:'',note:'',project_id:'',project_source:'',enabled:true}); renderKeys(); }
function removeKeyRow(i){ collectKeys(); KEYS.splice(i,1); renderKeys(); }
async function loadExpressKeys(){
  try{
    const d=await (await fetch('/api/express-keys')).json();
    KEYS=(d.keys||[]).map(r=>Object.assign({},r));
    renderKeys();
  }catch(e){}
}
async function saveExpressKeys(){
  const payload=collectKeys();
  if(payload.length===0 && !confirm('确定保存为空 Key 列表？')) { renderKeys(); return; }
  try{
    const r=await fetch('/api/express-keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keys:payload})});
    const d=await r.json();
    if(r.ok){ KEYS=(d.keys||[]).map(x=>Object.assign({},x)); renderKeys(); toast('已保存 '+KEYS.length+' 个 Key'); await loadNetworkProxy(); }
    else toast(d.error||'保存失败');
  }catch(e){ toast('网络请求失败'); }
}
async function probeOneKey(i){
  collectKeys();
  const rec=KEYS[i]; if(!rec||!rec.key){ toast('请先填入 Key'); return; }
  toast('正在探测…');
  try{
    const r=await fetch('/api/express-keys/probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:rec.key,force:true})});
    const d=await r.json();
    if(!r.ok){ toast(d.error||'探测失败'); return; }
    const res=(d.results||[])[0]||{};
    await loadExpressKeys();
    const hit=KEYS.find(x=>x.key===rec.key);
    if(hit){ hit._msg=res.message||''; hit._msg_ok=!!res.ok; renderKeys(); }
    toast(res.ok?('项目：'+(res.project_id||'—')):(res.message||'探测失败'));
  }catch(e){ toast('网络请求失败'); }
}
async function probeAllKeys(){
  collectKeys();
  toast('正在探测全部 Key…');
  try{
    const r=await fetch('/api/express-keys/probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true,force:true})});
    const d=await r.json();
    if(!r.ok){ toast(d.error||'探测失败'); return; }
    const results=d.results||[];
    await loadExpressKeys();
    results.forEach(res=>{ const hit=KEYS.find(x=>x.key===res.key); if(hit){ hit._msg=res.message||''; hit._msg_ok=!!res.ok; } });
    renderKeys();
    toast('探测完成：'+results.filter(x=>x.ok).length+' / '+results.length);
  }catch(e){ toast('网络请求失败'); }
}

/* ---------- Params ---------- */
function setV(id,v){ const el=$(id); if(!el) return; if(el.type==='checkbox') el.checked=!!v; else el.value=(v===null||v===undefined)?'':v; }
async function loadParams(){
  try{
    const s=await (await fetch('/api/settings')).json();
    GLOBAL_SETTINGS=s;
    curAR = s.image_aspect_ratio || "";
    ['native_thinking_mode','thinking_g3_level','image_size','default_temperature','default_top_p','default_max_tokens','img_compress_max_dim','img_compress_max_mb','img_compress_quality','retry_max','retry_backoff_seconds','credential_failover_max','credential_race_concurrency','credential_race_delay_seconds','credential_race_win_mode','studio_read_timeout_seconds','express_stream_read_timeout_seconds','express_nonstream_read_timeout_seconds','disconnect_grace_seconds','fake_streaming_interval','sampling_policy','express_location','resumable_ttl_seconds','resumable_max_streams'].forEach(k=>setV(k,s[k]));
    ['img_compress_enabled','fake_streaming','roundrobin','cookie_debug','debug_outbound','image_system_instruction','resumable_enabled','continue_on_disconnect','credential_failover_enabled','credential_race_enabled','credential_race_stream_only'].forEach(k=>setV(k,s[k]));
    if((!s.native_thinking_mode || s.native_thinking_mode==='request')){
      if(s.hide_thoughts) setV('native_thinking_mode','off');
      else if(s.thinking_force_console) setV('native_thinking_mode','console');
    }
  }catch(e){}
  try{
    const c=await (await fetch('/api/capabilities')).json();
    CAPS=c.capabilities||{};
    OVERRIDES=c.overrides||{};
    $('model-sel').innerHTML='<option value="__global__">＊ 全局默认</option>'
      + (c.models||[]).map(m=>{const star=OVERRIDES[m]?' ★':''; return `<option value="${m}">${m}${star}</option>`;}).join('');
    renderCaps();
  }catch(e){}
}
function applyModelParamFields(model){
  const ov = (model==='__global__') ? {} : (OVERRIDES[model] || {});
  PER_MODEL_KEYS.forEach(k=>{
    const v = (k in ov) ? ov[k] : GLOBAL_SETTINGS[k];
    if(k==='image_aspect_ratio'){ curAR = v || ""; }
    else setV(k, v);
  });
  const isGlobal = (model==='__global__');
  const has = !isGlobal && !!OVERRIDES[model] && Object.keys(OVERRIDES[model]).length>0;
  $('ov-badge').classList.toggle('hidden', !has);
  $('btn-scope-save').textContent = isGlobal ? '保存为全局默认' : '保存为该模型专属';
  $('btn-scope-clear').classList.toggle('hidden', isGlobal);
  $('ov-hint').textContent = isGlobal ? '当前编辑：全局默认值' : (has ? '当前编辑：模型专属值（★）' : '当前显示全局默认，保存后将创建该模型专属配置');
}
async function saveModelOverride(){
  const m=$('model-sel').value; if(!m) return;
  const patch={
    native_thinking_mode:$('native_thinking_mode').value,
    thinking_g3_level:$('thinking_g3_level').value,
    image_size:$('image_size').value,
    image_aspect_ratio:$('image_aspect_ratio').value,
    default_temperature:numOrNull('default_temperature'),
    default_top_p:numOrNull('default_top_p'),
    default_max_tokens:numOrNull('default_max_tokens'),
    image_system_instruction:$('image_system_instruction').checked,
    sampling_policy:$('sampling_policy').value,
  };
  if(m==='__global__'){
    try{
      const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});
      if(r.ok){ Object.assign(GLOBAL_SETTINGS,patch); toast('已保存为全局默认'); }
      else toast('保存失败');
    }catch(e){ toast('保存失败'); }
    return;
  }
  try{
    const r=await fetch('/api/model-overrides/'+encodeURIComponent(m),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});
    const d=await r.json();
    if(r.ok){ OVERRIDES[m]=d.override||patch; toast('已保存 '+m+' 专属参数'); refreshModelSelStars(); applyModelParamFields(m); }
    else toast('保存失败');
  }catch(e){ toast('网络请求失败'); }
}
async function clearModelOverride(){
  const m=$('model-sel').value; if(!m) return;
  if(!OVERRIDES[m]){ toast('该模型没有专属参数'); return; }
  try{
    const r=await fetch('/api/model-overrides/'+encodeURIComponent(m),{method:'DELETE'});
    if(r.ok){ delete OVERRIDES[m]; toast('已清除专属参数'); refreshModelSelStars(); applyModelParamFields(m); }
    else toast('清除失败');
  }catch(e){ toast('网络请求失败'); }
}
function refreshModelSelStars(){
  const sel=$('model-sel'); const cur=sel.value;
  [...sel.options].forEach(o=>{ if(o.value==='__global__') return; o.textContent = o.value + (OVERRIDES[o.value]?' ★':''); });
  sel.value=cur;
}
function chip(t, accent){ return `<span class="badge${accent?' badge-primary':''}">${t}</span>`; }
function fillARFor(cap){
  const sel=$('image_aspect_ratio');
  const list = (cap && cap.is_image && cap.image_aspect_ratios.length) ? cap.image_aspect_ratios : COMMON_ARS;
  sel.innerHTML='<option value="">自动</option>' + list.map(a=>`<option value="${a}">${a}</option>`).join('');
  sel.value = (curAR && list.includes(curAR)) ? curAR : "";
}
function renderCaps(){
  const m=$('model-sel').value;
  if(m==='__global__'){
    applyModelParamFields(m);
    $('caps-summary').innerHTML='';
    ['thinking_g3_level','image_size','image_aspect_ratio'].forEach(id=>{const el=$(id); if(el) el.disabled=false;});
    $('wrap-g3level').style.opacity='1';
    $('think-note').textContent='';
    $('image-note').textContent='';
    $('sampling-note').textContent='';
    return;
  }
  const cap=CAPS[m]; if(!cap) return;
  applyModelParamFields(m);
  const th=cap.thinking||{};
  let sum=[chip(cap.family, true)];
  if(th.kind==='level') sum.push(chip('思考 '+(th.levels||[]).join('/')));
  if(cap.is_image){ sum.push(chip('生图 '+cap.image_sizes.join('/'))); }
  else { sum.push(chip(cap.sampling_advice==='deprecated'?'采样已废弃':'采样可调')); }
  if(cap.supports_search) sum.push(chip('搜索'));
  $('caps-summary').innerHTML=sum.join('');

  const isLevel=th.kind==='level';
  $('thinking_g3_level').disabled=!isLevel;
  $('wrap-g3level').style.opacity=isLevel?'1':'.4';
  $('think-note').textContent = isLevel ? ('支持档位：' + (th.levels||[]).join(' / ')) : '不支持思考调节';

  const imgOn=cap.is_image;
  $('image_size').disabled=!imgOn; $('image_aspect_ratio').disabled=!imgOn;
  fillARFor(cap);
  $('image-note').textContent = imgOn ? ('支持分辨率：' + cap.image_sizes.join('/')) : '非生图模型';
  $('sampling-note').textContent = cap.is_image ? '生图模型自动剥离采样' : (cap.sampling_advice==='deprecated' ? '该模型已废弃采样参数，默认自动剥离' : '');
}
function numOrNull(id){ const v=$(id).value.trim(); return v===''?null:Number(v); }
function numOr(id,d){ const v=$(id).value.trim(); return v===''?d:Number(v); }
async function saveSettings(){
  const readTimeoutFields=[
    ['studio_read_timeout_seconds','Studio 读取超时'],
    ['express_stream_read_timeout_seconds','Express 流式读取超时'],
    ['express_nonstream_read_timeout_seconds','Express 非流式读取超时'],
  ];
  const timeoutValues={};
  for(const [id,label] of readTimeoutFields){
    const val=numOr(id,180);
    if(!Number.isInteger(val)||val<1||val>3600){
      toast(`${label}须为 1–3600 的整数（秒）。`);
      $(id).focus();
      return;
    }
    timeoutValues[id]=val;
  }
  const disconnectGrace=numOr('disconnect_grace_seconds',5);
  if(!Number.isInteger(disconnectGrace)||disconnectGrace<0||disconnectGrace>300){
    toast('重连宽限须为 0–300 的整数（秒）。');
    $('disconnect_grace_seconds').focus();
    return;
  }
  const failoverMax=numOr('credential_failover_max',3);
  if(!Number.isInteger(failoverMax)||failoverMax<0||failoverMax>50){
    toast('跨凭证重试次数须为 0–50 的整数。');
    $('credential_failover_max').focus();
    return;
  }
  const raceConcurrency=numOr('credential_race_concurrency',2);
  if(!Number.isInteger(raceConcurrency)||raceConcurrency<2||raceConcurrency>20){
    toast('并发凭证数须为 2–20 的整数。');
    $('credential_race_concurrency').focus();
    return;
  }
  const raceDelay=numOr('credential_race_delay_seconds',2);
  if(!Number.isFinite(raceDelay)||raceDelay<0||raceDelay>60){
    toast('随机延迟窗口须为 0–60 之间的数字（秒）。');
    $('credential_race_delay_seconds').focus();
    return;
  }
  const patch={
    img_compress_enabled:$('img_compress_enabled').checked,
    img_compress_max_dim:numOr('img_compress_max_dim',1536),
    img_compress_max_mb:numOr('img_compress_max_mb',1.5),
    img_compress_quality:numOr('img_compress_quality',85),
    retry_max:numOr('retry_max',10),
    retry_backoff_seconds:numOr('retry_backoff_seconds',5),
    credential_failover_enabled:$('credential_failover_enabled').checked,
    credential_failover_max:failoverMax,
    credential_race_enabled:$('credential_race_enabled').checked,
    credential_race_concurrency:raceConcurrency,
    credential_race_delay_seconds:raceDelay,
    credential_race_win_mode:$('credential_race_win_mode').value,
    credential_race_stream_only:$('credential_race_stream_only').checked,
    studio_read_timeout_seconds:timeoutValues.studio_read_timeout_seconds,
    express_stream_read_timeout_seconds:timeoutValues.express_stream_read_timeout_seconds,
    express_nonstream_read_timeout_seconds:timeoutValues.express_nonstream_read_timeout_seconds,
    fake_streaming:$('fake_streaming').checked,
    fake_streaming_interval:numOr('fake_streaming_interval',1),
    roundrobin:$('roundrobin').checked,
    cookie_debug:$('cookie_debug').checked,
    debug_outbound:$('debug_outbound').checked,
    express_location:$('express_location').value,
    resumable_enabled:$('resumable_enabled').checked,
    continue_on_disconnect:$('continue_on_disconnect').checked,
    disconnect_grace_seconds:disconnectGrace,
    resumable_ttl_seconds:numOr('resumable_ttl_seconds',1800),
    resumable_max_streams:numOr('resumable_max_streams',200),
  };
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});
    if(r.ok){
      GLOBAL_SETTINGS=await r.json();
      ['studio_read_timeout_seconds','express_stream_read_timeout_seconds','express_nonstream_read_timeout_seconds','credential_failover_enabled','credential_failover_max','credential_race_enabled','credential_race_concurrency','credential_race_delay_seconds','credential_race_win_mode','credential_race_stream_only'].forEach(k=>setV(k,GLOBAL_SETTINGS[k]));
      toast('全局设置已保存');
    } else toast('保存失败');
  }catch(e){ toast('网络请求失败'); }
}

/* ---------- Logs ---------- */
const logwin=$('logwin'); let autoscroll=true;
function scrollLogsToLatest(){
  autoscroll=true;
  logwin.scrollTop=logwin.scrollHeight;
}
logwin.addEventListener('scroll',()=>{
  if(logwin.clientHeight===0) return;
  autoscroll = logwin.scrollHeight-logwin.scrollTop-logwin.clientHeight<50;
});
function clearLogView(){ logwin.innerHTML=''; }
function applyLogFilter(){
  const q = ($('log-filter')?.value || '').trim().toLowerCase();
  logwin.querySelectorAll('.log-row').forEach(el => {
    el.style.display = (!q || el.textContent.toLowerCase().includes(q)) ? '' : 'none';
  });
}
function logLine(t){
  let c='#cbd5e1', bg='transparent', bl='2px solid transparent';
  if(t.includes('✅')||t.includes('🏆')){c='#86efac';bl='2px solid #22c55e';}
  else if(t.includes('⚠️')||t.includes('WARN')||t.includes('🔄')||t.includes('重试')){c='#fcd34d';bg='rgba(245,158,11,0.08)';bl='2px solid #f59e0b';}
  else if(t.includes('❌')||t.includes('ERROR')||t.includes('🔑')){c='#fca5a5';bg='rgba(239,68,68,0.10)';bl='2px solid #ef4444';}
  else if(t.includes('⚡')||t.includes('📦')){c='#7dd3fc';bl='2px solid #38bdf8';}
  else if(t.includes('🏁')||t.includes('🚀')){c='#c4b5fd';bl='2px solid #a78bfa';}
  const q = (typeof $('log-filter') !== 'undefined' && $('log-filter') && $('log-filter').value) ? $('log-filter').value.trim().toLowerCase() : '';
  const hidden = q && !t.toLowerCase().includes(q) ? 'display:none;' : '';
  let s=t.replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/(gemini-[a-zA-Z0-9.\-]+)/g,'<span style="color:#60a5fa;font-weight:600">$1</span>')
    .replace(/(凭证=[^|\s]+)/g,'<span style="color:#c4b5fd;font-weight:600">$1</span>');
  return `<div class="log-row" style="${hidden}color:${c};background:${bg};border-left:${bl};padding:4px 9px;border-radius:4px;margin-bottom:2px;white-space:pre-wrap;word-break:break-word">${s}</div>`;
}
try{
  const es=new EventSource('/stream-logs');
  es.onmessage=e=>{ if(e.data.includes('keep-alive')) return; logwin.insertAdjacentHTML('beforeend',logLine(e.data)); if(autoscroll) logwin.scrollTop=logwin.scrollHeight; };
}catch(e){}

/* Cookie Project IDs are extracted per row when collecting credentials. */
$('image_aspect_ratio').addEventListener('change', e=>{ curAR=e.target.value; });

/* init */
setCredFilter('all');
fetchStats(); setInterval(fetchStats,4000); loadRuntime(); loadParams(); loadExpressKeys(); loadCookieCredentials(); loadNetworkProxy();
</script>
</body>
</html>
"""
