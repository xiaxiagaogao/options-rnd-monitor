/* RND 仪表盘前端。Vue3 全局构建 + ECharts（本地 vendor，无构建步骤）。 */
const { createApp } = Vue;

const PCT_INDICATORS = new Set(["atm_iv", "tail_p_down", "tail_p_up"]);

// ---- 紧凑 Markdown → HTML（先转义再变换，v-html 安全；覆盖报告用到的构件）----
function mdEscape(s) {
  return s.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function mdInline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
}
function mdRow(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
}
function renderMarkdown(md) {
  if (!md) return "";
  const L = mdEscape(md).split("\n");
  const isSep = s => /\|/.test(s) && /^[\s|:-]*-[\s|:-]*$/.test(s.trim());
  const out = [];
  let i = 0;
  while (i < L.length) {
    const line = L[i];
    // 表格：本行含 | 且下一行是分隔行
    if (/\|/.test(line) && i + 1 < L.length && isSep(L[i + 1])) {
      const head = mdRow(line);
      i += 2;
      const rows = [];
      while (i < L.length && /\|/.test(L[i]) && L[i].trim() !== "") { rows.push(mdRow(L[i])); i++; }
      out.push("<table><thead><tr>" + head.map(h => "<th>" + mdInline(h) + "</th>").join("") +
        "</tr></thead><tbody>" +
        rows.map(r => "<tr>" + r.map(c => "<td>" + mdInline(c) + "</td>").join("") + "</tr>").join("") +
        "</tbody></table>");
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { out.push(`<h${h[1].length}>` + mdInline(h[2]) + `</h${h[1].length}>`); i++; continue; }
    if (/^\s*---+\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^\s*&gt;\s?/.test(line)) {  // > 已被 mdEscape 转成 &gt;
      const buf = [];
      while (i < L.length && /^\s*&gt;\s?/.test(L[i])) { buf.push(mdInline(L[i].replace(/^\s*&gt;\s?/, ""))); i++; }
      out.push("<blockquote>" + buf.join("<br>") + "</blockquote>"); continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < L.length && /^\s*([-*]|\d+\.)\s+/.test(L[i])) {
        items.push(mdInline(L[i].replace(/^\s*([-*]|\d+\.)\s+/, ""))); i++;
      }
      out.push((ordered ? "<ol>" : "<ul>") + items.map(t => "<li>" + t + "</li>").join("") +
        (ordered ? "</ol>" : "</ul>")); continue;
    }
    if (line.trim() === "") { i++; continue; }
    const buf = [];
    while (i < L.length && L[i].trim() !== "" && !/^(#{1,4}\s|\s*&gt;|\s*---+\s*$|\s*([-*]|\d+\.)\s)/.test(L[i]) &&
      !(/\|/.test(L[i]) && i + 1 < L.length && isSep(L[i + 1]))) { buf.push(L[i]); i++; }
    out.push("<p>" + mdInline(buf.join(" ")) + "</p>");
  }
  return out.join("\n");
}

async function api(path, opts = {}) {
  // 会话只走 HttpOnly cookie（credentials: same-origin 自动带上）。
  // 不再把 token 存 localStorage：那样任意 XSS 都能偷走 30 天会话。
  const headers = { "Content-Type": "application/json" };
  // cache: no-store 避免切换标的时命中浏览器 GET 缓存造成错位
  const r = await fetch(path, { cache: "no-store", headers, credentials: "same-origin", ...opts });
  if (r.status === 401 && path !== "/api/login") throw { auth: true };
  if (!r.ok) throw await r.json().catch(() => ({ detail: r.statusText }));
  return r.json();
}

createApp({
  data: () => ({
    view: "boot",
    password: "", loginErr: "",
    symbols: [], overview: [], events: [],
    active: "SPY", detail: null, fanData: null, fanDays: 120,
    densityData: { ok: false }, heatData: null,
    dcdfData: { ok: false }, pitData: { ok: false },
    switchSeq: 0, loadingSym: false, uiFading: false,
    admSymbol: "", admBusy: false, admResult: null, admPool: [], admErr: "",
    poolStatus: { holdings: [], count: 0 }, poolTimer: null,
    cmpMode: "prev", cmpLabel: "",
    diagMeta: null,
    form: { direction: "long", identity: "speculative", entry_price: "", risk_budget: "",
            target_price: "", target_rationale: "" },
    journalErr: "",
    charts: {},
    // 研究助手
    asSymbol: "SPY", asQuestion: "", asBusy: false, asErr: "",
    asElapsed: 0, asTimer: null, qa: [], asExpanded: {},
    // 全量市场展望
    outlook: null, outlookBusy: false, outlookErr: "",
    outlookElapsed: 0, outlookTimer: null, outlookAsof: null, outlookSecs: null,
    // 止盈止损（spec 2026-09-25-exit-curves）
    exitList: { holdings: [], others: [], loaded: false },
    exitActive: null, exitData: null, exitErr: "", exitLoading: false, exitSeq: 0,
    presets: ["现在偏度和尾部在什么水平？", "我这笔持仓要注意什么？",
              "和指数并排有什么异常？", "今天闸门/拟合质量可信吗？"],
  }),
  computed: {
    isApp() { return ["today", "symbol", "exits", "assistant"].includes(this.view); },
    dataDate() {
      if (this.detail && this.detail.date) return this.detail.date;
      const r = this.overview.find(o => o.ready && o.date);
      return r ? r.date : null;
    },
    positionedSymbols() {
      return this.overview.filter(o => o.positions);
    },
    ind() { return this.detail ? this.detail.indicators : null; },
    myPosition() {
      return this.detail && this.detail.positions.length ? this.detail.positions[0] : null;
    },
    // 币安实际敞口（持仓同步派生，非 journal 手动轨）。两轨并存时 journal 优先展示。
    binanceEntry() { return (this.detail && this.detail.binance_entry) || null; },
    // 入场日分位 → 今日分位的位移，按今日 σ1 归一（口径与 journal 的 offsetSigma 一致）
    beOffset() {
      const be = this.binanceEntry, i = this.ind;
      if (!be || !be.frozen || !i || !i.sigma1_abs) return null;
      const f = q => {
        const off = (i[q] - be.frozen[q]) / i.sigma1_abs;
        return (off >= 0 ? "+" : "") + off.toFixed(2) + "σ";
      };
      return { q05: f("q05"), q25: f("q25") };
    },
    // 事实陈述：今日收盘是否已跌破入场日 Q05（多头口径；净仓为负则反向）。
    // 注意与 journal 的语义差别——这里没有"你承诺过的失效线"，只是描述位置。
    beBreached() {
      const be = this.binanceEntry;
      if (!be || !be.frozen || !this.fanData) return false;
      const dates = this.fanData.dates;
      const close = this.fanData.close[dates[dates.length - 1]];
      if (close == null) return false;
      return be.qty >= 0 ? close < be.frozen.q05 : close > be.frozen.q05;
    },
    frozenStop() {
      const p = this.myPosition;
      return p ? p["frozen_" + p.stop_q] : null;
    },
    offsetSigma() {
      const p = this.myPosition;
      if (!p || !this.ind) return "—";
      const cur = this.ind[p.stop_q];
      const off = (cur - this.frozenStop) / this.ind.sigma1_abs;
      return (off >= 0 ? "+" : "") + off.toFixed(2) + "σ";
    },
    stopBreached() {
      // 收盘确认制：多头收盘 < 冻结线（空头相反）
      const p = this.myPosition;
      if (!p || !this.fanData) return false;
      const dates = this.fanData.dates;
      const close = this.fanData.close[dates[dates.length - 1]];
      if (close == null) return false;
      return p.direction === "long" ? close < this.frozenStop : close > this.frozenStop;
    },
    termSlopeText() {
      const s = this.detail?.states.find(x => x.indicator === "term_slope");
      if (!s || s.value == null) return "—";
      const pct = s.pct != null ? ` · P${Math.round(s.pct)}` : " · 静默";
      return (s.value * 100).toFixed(1) + " pt" + pct;
    },
    gateSummary() {
      return this.overview.filter(o => o.ready)
        .map(o => `${o.symbol} ${o.gate_pass ? "✓" : "✗"}`).join(" · ");
    },
    gateDetailText() {
      const g = this.detail?.gate_detail || {};
      const zh = { no_arb: "无套利", rr_skew_agree: "RR-偏度", n_strikes_ok: "行权价数", fit_rmse_ok: "拟合误差" };
      return Object.entries(g).map(([k, v]) => `${zh[k] || k}${v ? "✓" : "✗"}`).join(" ");
    },
    outOfRangeCount() {
      if (!this.ind) return 0;
      return ["q05", "q25", "q50", "q75", "q95"]
        .filter(k => !this.ind[k + "_in_range"]).length;
    },
    lastBimodal() {
      const b = this.fanData?.bimodal_dates;
      return b && b.length ? b[b.length - 1] : null;
    },
    tailRatio() {
      const t = this.fanData?.tails;
      if (!t || !t.down.length) return null;
      const d = t.down[t.down.length - 1], u = t.up[t.up.length - 1];
      return u > 0 ? (d / u).toFixed(2) : null;
    },
    asAsof() {
      const o = this.overview.find(x => x.symbol === this.asSymbol);
      return o && o.ready ? o.date : null;
    },
  },
  methods: {
    fmt(v, n = 2) { return v == null ? "—" : Number(v).toFixed(n); },
    // 价位：四位数以上一位小数，否则两位
    fmtPx(v) { return v == null ? "—" : Number(v).toFixed(Math.abs(v) >= 1000 ? 1 : 2); },
    fmtQty(q) { return q == null ? "—" : String(+Math.abs(q).toFixed(6)); },
    fmtSignedPct(x, n = 1) {
      if (x == null || !isFinite(x)) return "—";
      return (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(n) + "%";
    },
    // 侧栏浮动盈亏：按方向算收盘相对成本（多头涨为正、空头跌为正）
    exitPnl(h) {
      if (!h.entry_price || h.close == null) return null;
      return (h.close / h.entry_price - 1) * (h.qty >= 0 ? 1 : -1);
    },
    md(text) { return renderMarkdown(text); },
    // ---------- 研究助手 ----------
    // ---------- 侧栏三入口：今日 / 标的 / 助手 ----------
    pathFor(view, sym) {
      if (view === "assistant") return "/assistant";
      if (view === "exits") return sym ? `/exits/${encodeURIComponent(sym)}` : "/exits";
      if (view === "symbol") return `/sym/${encodeURIComponent(sym || this.active || "SPY")}`;
      return "/";
    },
    sleep(ms) { return new Promise(r => setTimeout(r, ms)); },
    /** 侧栏切页：先淡出再换视图再淡入，避免硬切 */
    async withPaneTransition(changeFn, { animate = true } = {}) {
      if (!animate || this.uiFading) {
        await changeFn();
        return;
      }
      this.uiFading = true;
      await this.sleep(150);
      try {
        await changeFn();
        await this.$nextTick();
      } finally {
        await this.sleep(20);
        this.uiFading = false;
        await this.$nextTick();
        if (this.view === "symbol" || this.view === "exits") {
          Object.values(this.charts).forEach(c => c && c.resize());
        }
      }
    },
    goToday() {
      return this.withPaneTransition(async () => {
        if (this.view !== "today") history.pushState({ view: "today" }, "", "/");
        this.view = "today";
      }, { animate: this.view !== "today" });
    },
    async goSymbol(sym) {
      const s = (sym || this.active || (this.symbols[0] || "SPY")).toUpperCase();
      const path = this.pathFor("symbol", s);
      const leaving = this.view !== "symbol";
      const needLoad = s !== this.active || !this.detail;
      // 切页只过渡壳；数据加载放在淡入后，避免长时间停在空白淡出态
      await this.withPaneTransition(async () => {
        if (location.pathname !== path || leaving) {
          history.pushState({ view: "symbol", sym: s }, "", path);
        }
        this.view = "symbol";
      }, { animate: leaving });
      if (needLoad) await this.switchSymbol(s);
      else this.$nextTick(() => Object.values(this.charts).forEach(c => c && c.resize()));
    },
    async goExits(sym) {
      const leaving = this.view !== "exits";
      await this.withPaneTransition(async () => {
        this.view = "exits";
      }, { animate: leaving });
      if (!this.exitList.loaded) await this.loadExitList();
      const target = (sym || this.exitActive || this.exitList.holdings[0]?.symbol
                      || this.exitList.others[0]?.symbol || "").toUpperCase();
      const path = this.pathFor("exits", target);
      if (location.pathname !== path) {
        if (leaving) history.pushState({ view: "exits", sym: target }, "", path);
        else history.replaceState({ view: "exits", sym: target }, "", path);
      }
      if (target && (target !== this.exitActive || !this.exitData)) await this.switchExit(target);
      else this.$nextTick(() => Object.values(this.charts).forEach(c => c && c.resize()));
    },
    goAssistant() {
      this.asSymbol = this.active || this.asSymbol;
      return this.withPaneTransition(async () => {
        if (this.view !== "assistant") {
          history.pushState({ view: "assistant" }, "", "/assistant");
        }
        this.view = "assistant";
      }, { animate: this.view !== "assistant" });
    },
    backToDash() { return this.goSymbol(this.active); },
    async routeFromLocation() {
      // 首屏 / 浏览器前进后退：瞬时切换，不叠过渡
      const path = location.pathname || "/";
      if (path.startsWith("/assistant")) {
        this.asSymbol = this.active || this.asSymbol;
        this.view = "assistant";
        return;
      }
      const xm = path.match(/^\/exits(?:\/([^/]+))?\/?$/);
      if (xm) {
        this.view = "exits";
        if (!this.exitList.loaded) await this.loadExitList();
        const s = (xm[1] ? decodeURIComponent(xm[1]) : (this.exitActive || this.exitList.holdings[0]?.symbol
                   || this.exitList.others[0]?.symbol || "")).toUpperCase();
        if (s && (s !== this.exitActive || !this.exitData)) await this.switchExit(s);
        return;
      }
      const m = path.match(/^\/sym(?:\/([^/]+))?\/?$/);
      if (m) {
        const s = decodeURIComponent(m[1] || this.active || this.symbols[0] || "SPY").toUpperCase();
        this.view = "symbol";
        if (s !== this.active || !this.detail) await this.switchSymbol(s);
        else this.$nextTick(() => Object.values(this.charts).forEach(c => c && c.resize()));
        return;
      }
      this.view = "today";
    },
    presetAsk(q) { this.asQuestion = q; this.askAssistant(); },
    async askAssistant() {
      const q = this.asQuestion.trim();
      if (!q || this.asBusy) return;
      this.asErr = "";
      this.asBusy = true;
      this.asElapsed = 0;
      this.asTimer = setInterval(() => { this.asElapsed += 1; }, 1000);
      const entry = { symbol: this.asSymbol, question: q, answer: null,
                      asof: this.asAsof, context: null, secs: null };
      this.qa.push(entry);
      this.asQuestion = "";
      this.$nextTick(() => this.scrollAsk());
      try {
        const r = await api("/api/assistant", { method: "POST",
          body: JSON.stringify({ symbol: this.asSymbol, question: q }) });
        entry.answer = r.answer;
        entry.asof = r.asof;
        entry.context = r.context;
      } catch (e) {
        if (e.auth) { this.view = "login"; }
        else { entry.error = e.detail || "生成失败"; this.asErr = entry.error; }
      } finally {
        clearInterval(this.asTimer);
        entry.secs = this.asElapsed;
        this.asBusy = false;
        this.$nextTick(() => this.scrollAsk());
      }
    },
    scrollAsk() {
      const el = this.$refs.askThread;
      if (el) el.scrollTop = el.scrollHeight;
    },
    toggleContext(i) { this.asExpanded[i] = !this.asExpanded[i]; },
    async runOutlook() {
      if (this.outlookBusy) return;
      this.outlookErr = "";
      this.outlookBusy = true;
      this.outlookElapsed = 0;
      this.outlookTimer = setInterval(() => { this.outlookElapsed += 1; }, 1000);
      try {
        const r = await api("/api/assistant/outlook", { method: "POST" });
        this.outlook = r.answer;
        this.outlookAsof = r.asof;
        this.outlookSecs = this.outlookElapsed;
      } catch (e) {
        if (e.auth) this.view = "login";
        else this.outlookErr = e.detail || "生成失败";
      } finally {
        clearInterval(this.outlookTimer);
        this.outlookBusy = false;
      }
    },
    fmtIndicator(name, v) {
      if (v == null) return "—";
      if (PCT_INDICATORS.has(name)) return (v * 100).toFixed(1) + "%";
      if (name === "term_slope") return (v * 100).toFixed(1) + "pt";
      if (name === "bf25" || name === "rr25") return Number(v).toFixed(4);
      return Number(v).toFixed(2);
    },
    async doLogin() {
      this.loginErr = "";
      try {
        await api("/api/login", { method: "POST",
          body: JSON.stringify({ password: this.password }) });
        this.password = "";
        await this.boot();
      } catch (e) { this.loginErr = e.detail || "登录失败"; }
    },
    async doLogout() {
      localStorage.removeItem("rnd_token");   // 清掉旧版本残留，防止老会话继续躺在盘上
      await api("/api/logout", { method: "POST" });
      this.view = "login";
    },
    async boot() {
      try {
        const ov = await api("/api/overview");
        this.overview = ov.symbols;
        this.symbols = ov.symbols.map(o => o.symbol);
        const ready = ov.symbols.find(o => o.ready);
        this.active = (ready ? ready.symbol : this.symbols[0]) || "SPY";
        this.events = (await api("/api/events")).events.slice(0, 12);
        this.refreshPool();
        // 按 URL 分流：/ → 今日总览；/sym/:code → 深页；/assistant → 助手
        await this.routeFromLocation();
      } catch (e) {
        if (e.auth) this.view = "login"; else throw e;
      }
    },
    async switchSymbol(sym) {
      if (!sym) return;
      sym = String(sym).toUpperCase();
      // 序号令牌：丢弃过期的并发切换结果，防止 active=NVDA 却写入 QQQ 数据
      const seq = ++this.switchSeq;
      this.active = sym;
      if (this.view === "symbol") {
        const path = this.pathFor("symbol", sym);
        if (location.pathname !== path) history.replaceState({ view: "symbol", sym }, "", path);
      }
      this.loadingSym = true;
      this.diagMeta = null;
      // 立刻清空旧标的数据，避免标签已切走但数值/图仍显示上一个标的
      this.detail = null;
      this.fanData = null;
      this.densityData = { ok: false };
      this.heatData = null;
      this.dcdfData = { ok: false };
      this.pitData = { ok: false };
      this.cmpLabel = "";
      Object.values(this.charts).forEach(c => c && c.clear());
      try {
        const detail = await api(`/api/symbol/${sym}`);
        if (seq !== this.switchSeq) return;
        this.detail = detail;
        await this.$nextTick();   // 等 v-if 区块挂载，避免图表在零宽容器上初始化
        if (seq !== this.switchSeq) return;
        // 显式传 sym/seq，不依赖 this.active（并发切换时 active 会变）
        await Promise.all([
          this.loadFan(sym, seq), this.loadDensity(sym, seq), this.loadHeatmap(sym, seq),
          this.loadDcdf(sym, seq), this.loadPit(sym, seq),
        ]);
        if (seq !== this.switchSeq) return;
        await this.loadCompare(sym, seq);
        if (seq !== this.switchSeq) return;
        this.renderTails();
        if (this.charts.diagChart) { this.charts.diagChart.clear(); this.diagMeta = null; }
        // 兜底：容器尺寸迟到时（慢渲染环境）延迟重排一次
        setTimeout(() => {
          if (seq !== this.switchSeq) return;
          Object.values(this.charts).forEach(c => c && c.resize());
        }, 600);
      } finally {
        if (seq === this.switchSeq) this.loadingSym = false;
      }
    },
    _stale(seq) { return seq != null && seq !== this.switchSeq; },
    chart(refName) {
      const el = this.$refs[refName];
      if (!el) return null;
      if (this.charts[refName] && this.charts[refName].getDom() !== el) {
        this.charts[refName].dispose();
        delete this.charts[refName];
      }
      if (!this.charts[refName]) {
        // markRaw：ECharts 实例不能被 Vue 响应式 Proxy 包裹，否则 setOption 在内部
        // 取 series/visual 时读到代理对象，报 "Cannot read properties of undefined (reading 'type')"
        const c = Vue.markRaw(echarts.init(el));
        this.charts[refName] = c;
        // 容器尺寸任何时刻变化（面板缩放、布局迟到、侧栏伸缩）都自动重排
        new ResizeObserver(() => c.resize()).observe(el);
      } else {
        this.charts[refName].resize();
      }
      return this.charts[refName];
    },
    // ---------- 扇形带主图 ----------
    async loadFan(sym = this.active, seq = this.switchSeq) {
      const fanData = await api(`/api/symbol/${sym}/fan?days=${this.fanDays}`);
      if (this._stale(seq)) return;
      this.fanData = fanData;
      const f = this.fanData, dates = f.dates;
      const closes = dates.map(d => f.close[d] ?? null);
      const diff = (a, b) => a.map((v, i) => v == null || b[i] == null ? null : v - b[i]);
      const band = (name, data, color) => ({
        name, type: "line", stack: "band", data, symbol: "none",
        lineStyle: { width: 0 }, areaStyle: { color, opacity: 1 }, emphasis: { disabled: true },
      });
      const be = this.detail && this.detail.binance_entry;
      const marks = [
        ...f.roll_dates.map(d => ({ xAxis: d, label: { formatter: "●", color: "#6E6A60" } })),
        ...f.gate_fail_dates.map(d => ({ xAxis: d, label: { formatter: "▮", color: "#A33B2E" } })),
        ...f.bimodal_dates.map(d => ({ xAxis: d, label: { formatter: "◆", color: "#8F5C22" } })),
        // 币安开仓竖线（binance-entry-anchor §3.4）：rnd_date 不在窗口内则 ECharts 不渲染，无害
        ...(be && be.rnd_date ? [{ xAxis: be.rnd_date,
              label: { formatter: "▲入场", color: "#2F6B8F", fontSize: 10 } }] : []),
      ];
      const frozenLines = f.frozen.map(fr => ({
        yAxis: fr.level,
        label: { formatter: `冻结 ${fr.stop_q.toUpperCase()} ${fr.level.toFixed(1)}`, position: "insideEndTop", fontSize: 10 },
        lineStyle: { color: "#26241F", type: "solid", width: 2 },
      }));
      // 币安持仓：把入场日那天的五档分位钉成细横线，看清"我建仓时的分布"与之后的漂移。
      // 细线 + 低对比度，不与扇形带抢视觉；Q50 略深以便一眼定位中枢。
      const beFrozen = (be && be.frozen) ? [
        ["q95", "#B3AFA4"], ["q75", "#A8A396"], ["q50", "#6E6A60"],
        ["q25", "#A8A396"], ["q05", "#B3AFA4"],
      ].filter(([q]) => be.frozen[q] != null).map(([q, color]) => ({
        yAxis: be.frozen[q],
        label: { formatter: `入场 ${q.toUpperCase()} ${be.frozen[q].toFixed(1)}`,
                 position: "insideStartTop", fontSize: 9, color },
        lineStyle: { color, type: "solid", width: 1 },
      })) : [];
      const ys = [...closes.filter(v => v != null), ...f.q.q05, ...f.q.q95];
      this.chart("fanChart")?.setOption({
        animation: false,
        grid: { left: 46, right: 84, top: 18, bottom: 24 },
        xAxis: { type: "category", data: dates, axisLabel: { fontSize: 10 } },
        yAxis: { type: "value", min: Math.floor(Math.min(...ys) * 0.99),
                 max: Math.ceil(Math.max(...ys) * 1.01), axisLabel: { fontSize: 10 } },
        tooltip: { trigger: "axis", confine: true },
        series: [
          { name: "Q05", type: "line", stack: "band", data: f.q.q05, symbol: "none",
            lineStyle: { width: 1, type: "dashed", color: "#B3AFA4" } },
          band("Q05-Q25", diff(f.q.q25, f.q.q05), "#EDE9E0"),
          band("Q25-Q50", diff(f.q.q50, f.q.q25), "#E2DDD0"),
          band("Q50-Q75", diff(f.q.q75, f.q.q50), "#E2DDD0"),
          band("Q75-Q95", diff(f.q.q95, f.q.q75), "#EDE9E0"),
          { name: "收盘", type: "line", data: closes, symbol: "none",
            lineStyle: { width: 1.8, color: "#2F6B8F" },
            markLine: { symbol: "none", silent: true,
                        data: [...beFrozen, ...frozenLines, ...marks],
                        lineStyle: { color: "#B3AFA4", type: "dotted" } } },
        ],
      }, true);
    },
    // ---------- 当日密度 ----------
    async loadDensity(sym = this.active, seq = this.switchSeq) {
      const densityData = await api(`/api/symbol/${sym}/density`);
      if (this._stale(seq)) return;
      this.densityData = densityData;
      const d = this.densityData;
      if (!d.ok) { this.chart("densityChart")?.clear(); return; }
      const posts = Object.entries(d.quantiles).map(([k, v]) => ({
        xAxis: v,
        label: { formatter: k.toUpperCase(), fontSize: 9, color: d.in_range[k] ? "#6E6A60" : "#B0783A" },
        lineStyle: { type: d.in_range[k] ? "dashed" : "dotted", color: "#8B877C" },
      }));
      this.chart("densityChart")?.setOption({
        animation: false,
        grid: { left: 8, right: 8, top: 16, bottom: 20 },
        xAxis: { type: "value", min: d.strikes[0], max: d.strikes[d.strikes.length - 1],
                 axisLabel: { fontSize: 9 } },
        yAxis: { type: "value", show: false },
        tooltip: { trigger: "axis", confine: true,
                   formatter: p => `K=${p[0].value[0].toFixed(0)}` },
        series: [{
          type: "line", data: d.strikes.map((k, i) => [k, d.density[i]]),
          symbol: "none", lineStyle: { color: "#2F6B8F", width: 1.8 },
          areaStyle: { color: "#2F6B8F", opacity: 0.06 },
          markLine: { symbol: "none", silent: true,
                      data: [...posts, { xAxis: d.forward, lineStyle: { color: "#26241F" },
                                         label: { formatter: "F", fontSize: 9 } }] },
          markArea: d.k_quoted ? { silent: true, itemStyle: { color: "#8B877C", opacity: 0.08 },
            data: [[{ xAxis: d.strikes[0] }, { xAxis: d.k_quoted[0] }],
                   [{ xAxis: d.k_quoted[1] }, { xAxis: d.strikes[d.strikes.length - 1] }]] } : undefined,
        }],
      }, true);
    },
    // ---------- 热力图 ----------
    async loadHeatmap(sym = this.active, seq = this.switchSeq) {
      const heatData = await api(`/api/symbol/${sym}/heatmap`);
      if (this._stale(seq)) return;
      this.heatData = heatData;
      const h = this.heatData;
      const closeIdx = h.dates.map(d => {
        const c = h.close[d];
        if (c == null) return null;
        let best = 0, bd = Infinity;
        h.prices.forEach((p, i) => { const dd = Math.abs(p - c); if (dd < bd) { bd = dd; best = i; } });
        return best;
      });
      // 拆股日竖线（数据已复权到现股口径，线只作事件锚点）
      const splitMarks = (h.splits || []).map(s => ({
        xAxis: s.date,
        label: { formatter: s.factor_label || "拆股", fontSize: 9, color: "#8F5C22" },
        lineStyle: { color: "#B0783A", type: "dashed", width: 1.2 },
      }));
      this.chart("heatChart")?.setOption({
        animation: false,
        grid: { left: 44, right: 6, top: 6, bottom: 20 },
        xAxis: { type: "category", data: h.dates, axisLabel: { fontSize: 9, interval: Math.floor(h.dates.length / 6) } },
        yAxis: { type: "category", data: h.prices, axisLabel: { fontSize: 9, interval: Math.floor(h.prices.length / 5) } },
        visualMap: { show: false, min: 0, max: 1,
          inRange: { color: ["#F7F6F3", "#E8E0CE", "#CBA96E", "#8F5C22", "#3B2508"] } },
        tooltip: { show: false },
        series: [
          { type: "heatmap", data: h.cells, progressive: 4000, emphasis: { disabled: true } },
          { type: "line", data: closeIdx, symbol: "none", lineStyle: { color: "#2F8F8F", width: 1.4 },
            markLine: splitMarks.length ? { symbol: "none", silent: true, data: splitMarks } : undefined },
        ],
      }, true);
    },
    // ---------- 双日对比 ----------
    async loadCompare(sym = this.active, seq = this.switchSeq) {
      const d = this.detail;
      if (!d) return;
      let otherDate = null, label = "";
      const be = d.binance_entry;
      if (this.cmpMode === "entry" && this.myPosition) {
        // journal 持仓优先（有意识下注，权威）
        otherDate = this.myPosition.event_date;
        label = `入场日 ${otherDate}（虚）vs 今日（实）`;
      } else if (this.cmpMode === "entry" && be && be.rnd_date) {
        // 无 journal 条目但币安有持仓：用币安开仓日兜底
        otherDate = be.rnd_date;
        label = `币安开仓 ${be.open_date}（虚）vs 今日（实）`;
      } else {
        const f = this.fanData;
        otherDate = f && f.dates.length > 1 ? f.dates[f.dates.length - 2] : null;
        label = otherDate ? `${otherDate}（虚）vs 今日（实）` : "";
        if (this.cmpMode === "entry")
          label = (be ? "币安开仓早于数据窗口 · " : "无持仓 · ") + "退回 昨 vs 今";
      }
      if (this._stale(seq)) return;
      this.cmpLabel = label;
      if (!otherDate) { this.chart("cmpChart")?.clear(); return; }
      const [a, b] = await Promise.all([
        api(`/api/symbol/${sym}/density?date=${otherDate}`),
        Promise.resolve(this.densityData),
      ]);
      if (this._stale(seq)) return;
      if (!a.ok || !b.ok) { this.cmpLabel = "对比日无曲线"; this.chart("cmpChart")?.clear(); return; }
      this.chart("cmpChart")?.setOption({
        animation: false,
        grid: { left: 8, right: 8, top: 10, bottom: 20 },
        xAxis: { type: "value", min: Math.min(a.strikes[0], b.strikes[0]),
                 max: Math.max(a.strikes.at(-1), b.strikes.at(-1)), axisLabel: { fontSize: 9 } },
        yAxis: { type: "value", show: false },
        tooltip: { show: false },
        series: [
          { type: "line", data: a.strikes.map((k, i) => [k, a.density[i]]), symbol: "none",
            lineStyle: { color: "#8B877C", width: 1.4, type: "dashed" } },
          { type: "line", data: b.strikes.map((k, i) => [k, b.density[i]]), symbol: "none",
            lineStyle: { color: "#2F6B8F", width: 1.8 } },
        ],
      }, true);
    },
    // ---------- ΔCDF 迁移 ----------
    async loadDcdf(sym = this.active, seq = this.switchSeq) {
      const dcdfData = await api(`/api/symbol/${sym}/dcdf`);
      if (this._stale(seq)) return;
      this.dcdfData = dcdfData;
      const d = this.dcdfData;
      if (!d.ok) { this.chart("dcdfChart")?.clear(); return; }
      this.chart("dcdfChart")?.setOption({
        animation: false,
        grid: { left: 8, right: 8, top: 8, bottom: 18 },
        xAxis: { type: "value", min: d.strikes[0], max: d.strikes.at(-1),
                 axisLabel: { fontSize: 9 } },
        yAxis: { type: "value", show: false },
        tooltip: { show: false },
        series: [{
          type: "line", data: d.strikes.map((k, i) => [k, d.dcdf[i]]),
          symbol: "none", lineStyle: { color: d.comparable ? "#2F6B8F" : "#B0783A", width: 1.6 },
          areaStyle: { color: d.comparable ? "#2F6B8F" : "#B0783A", opacity: 0.10 },
          markLine: { symbol: "none", silent: true,
                      data: [{ yAxis: 0, lineStyle: { color: "#C9C5BB" } }] },
        }],
      }, true);
    },
    // ---------- PIT 校准 ----------
    async loadPit(sym = this.active, seq = this.switchSeq) {
      const pitData = await api(`/api/symbol/${sym}/pit`);
      if (this._stale(seq)) return;
      this.pitData = pitData;
      const p = this.pitData;
      if (!p.ok) { this.chart("pitChart")?.clear(); return; }
      const uniform = p.n_samples / 10;
      this.chart("pitChart")?.setOption({
        animation: false,
        grid: { left: 30, right: 8, top: 8, bottom: 18 },
        xAxis: { type: "category",
                 data: p.bin_edges.slice(0, -1).map((e, i) => `${e}–${p.bin_edges[i + 1]}`),
                 axisLabel: { fontSize: 8, interval: 1 } },
        yAxis: { type: "value", axisLabel: { fontSize: 9 } },
        tooltip: { show: false },
        series: [{
          type: "bar", data: p.hist, barWidth: "70%",
          itemStyle: { color: "#8FA8B8" },
          markLine: { symbol: "none", silent: true,
                      data: [{ yAxis: uniform, label: { formatter: "均匀", fontSize: 9 },
                               lineStyle: { color: "#8B877C", type: "dashed" } }] },
        }],
      }, true);
    },
    // ---------- 尾部不对称 ----------
    renderTails() {
      const f = this.fanData;
      if (!f || !f.tails) return;
      this.chart("tailChart")?.setOption({
        animation: false,
        grid: { left: 34, right: 8, top: 8, bottom: 18 },
        legend: { show: true, top: 0, right: 0, itemWidth: 12,
                  textStyle: { fontSize: 9 }, data: ["P(下尾)", "P(上尾)"] },
        xAxis: { type: "category", data: f.dates,
                 axisLabel: { fontSize: 9, interval: Math.floor(f.dates.length / 4) } },
        yAxis: { type: "value", axisLabel: { fontSize: 9, formatter: v => (v * 100).toFixed(0) + "%" } },
        tooltip: { show: false },
        series: [
          { name: "P(下尾)", type: "line", data: f.tails.down, symbol: "none",
            lineStyle: { color: "#A33B2E", width: 1.5 } },
          { name: "P(上尾)", type: "line", data: f.tails.up, symbol: "none",
            lineStyle: { color: "#3E7A52", width: 1.5 } },
        ],
      }, true);
    },
    // ---------- 诊断 ----------
    async openDiagnostics() {
      const d = await api(`/api/diagnostics/${this.active}/${this.detail.date}/${this.ind.expiry}`);
      if (!d.ok) { this.diagMeta = null; return; }
      this.diagMeta = d.meta;
      this.chart("diagChart")?.setOption({
        animation: false,
        grid: { left: 40, right: 10, top: 10, bottom: 22 },
        xAxis: { type: "value", name: "ln(K/F)", nameTextStyle: { fontSize: 9 },
                 axisLabel: { fontSize: 9 } },
        yAxis: { type: "value", axisLabel: { fontSize: 9, formatter: v => (v * 100).toFixed(0) + "%" } },
        tooltip: { show: false },
        series: [
          { type: "scatter", data: d.points.x.map((x, i) => [x, d.points.iv[i]]),
            symbolSize: 4, itemStyle: { color: "#2F6B8F", opacity: 0.55 } },
          { type: "line", data: d.curve.x.map((x, i) => [x, d.curve.iv[i]]), symbol: "none",
            lineStyle: { color: "#A33B2E", width: 1.8 },
            markArea: { silent: true, itemStyle: { color: "#8B877C", opacity: 0.08 },
              data: [[{ xAxis: d.curve.x[0] }, { xAxis: d.x_quoted[0] }],
                     [{ xAxis: d.x_quoted[1] }, { xAxis: d.curve.x.at(-1) }]] } },
        ],
      }, true);
    },
    // ---------- 止盈止损 ----------
    async loadExitList() {
      try {
        const r = await api("/api/exit-curves");
        this.exitList = { holdings: r.holdings, others: r.others, loaded: true };
      } catch (e) {
        if (e.auth) { this.view = "login"; return; }
        this.exitErr = e.detail || "持仓列表加载失败";
        this.exitList = { holdings: [], others: [], loaded: true };
      }
    },
    async switchExit(sym) {
      if (!sym) return;
      const seq = ++this.exitSeq;
      this.exitActive = sym;
      this.exitErr = "";
      this.exitLoading = true;
      this.exitData = null;
      if (this.view === "exits") {
        const path = this.pathFor("exits", sym);
        if (location.pathname !== path) history.replaceState({ view: "exits", sym }, "", path);
      }
      try {
        const r = await api(`/api/symbol/${sym}/exit-curves`);
        if (seq !== this.exitSeq) return;
        if (!r.ok) { this.exitErr = r.error || `${sym} 没有可用的分布`; return; }
        this.exitData = r;
        await this.$nextTick();
        if (seq !== this.exitSeq) return;
        this.renderExits();
      } catch (e) {
        if (e.auth) { this.view = "login"; return; }
        if (seq === this.exitSeq) this.exitErr = e.detail || "曲线加载失败";
      } finally {
        if (seq === this.exitSeq) this.exitLoading = false;
      }
    },
    renderExits() {
      const d = this.exitData;
      if (!d || !d.ok) return;
      const qty = d.position ? d.position.qty : null;
      const linked = [];
      if (d.curves.cost) {
        const c = this.renderExitChart("exitCostChart", d.curves.cost, "cost", d, qty);
        if (c) linked.push(c);
      }
      const t = this.renderExitChart("exitTodayChart", d.curves.today, "today", d, qty);
      if (t) linked.push(t);
      // 签名交互：两张图共用价位轴，十字线与读数联动
      linked.forEach(c => { c.group = "exits"; });
      if (linked.length > 1) echarts.connect("exits");
    },
    // 盈亏底色 + 减亏/回吐子区 + 外推区（spec §3：底色以锚点为界，概率方向以 S0 为界）
    exitZones(curve, qty, axis) {
      const [x0, x1] = axis, a = curve.anchor, s0 = curve.s0, span = x1 - x0;
      const WIN = "#E4EDE6", LOSS = "#F2E4DD";
      const lab = (t, row) => ({ show: !!t, formatter: t || "", position: "insideTop",
                                 fontSize: 10, color: "#6E6A60", distance: row ? 18 : 4 });
      const area = (from, to, color, text, opacity = 0.75, row = 0) => {
        const lo = Math.max(x0, Math.min(from, to)), hi = Math.min(x1, Math.max(from, to));
        if (hi <= lo) return null;
        return [{ xAxis: lo, itemStyle: { color, opacity },
                  label: lab((hi - lo) / span > 0.04 ? text : "", row) }, { xAxis: hi }];
      };
      const zones = [];
      if (qty != null && qty !== 0) {
        const long = qty > 0;
        const lowColor = long ? LOSS : WIN, highColor = long ? WIN : LOSS;
        const lowText = long ? "亏" : "盈", highText = long ? "盈" : "亏";
        const lo = Math.min(s0, a), hi = Math.max(s0, a);
        if ((hi - lo) / span > 0.03) {
          // [x0,lo] 必在锚点下侧、[hi,x1] 必在上侧；中间段随 S0 落在哪侧。
          // 中间段在亏的一侧 = 减亏区（朝 S0 反向走才碰得到），在盈的一侧 = 利润回吐区
          const midLow = s0 < a;
          const midLoss = midLow === long;
          zones.push(area(x0, lo, lowColor, lowText));
          zones.push(area(lo, hi, midLow ? lowColor : highColor, midLoss ? "减亏" : "回吐", 0.5));
          zones.push(area(hi, x1, highColor, highText));
        } else {
          zones.push(area(x0, a, lowColor, lowText));
          zones.push(area(a, x1, highColor, highText));
        }
      }
      const [g0, g1] = curve.grid;
      if (curve.k_quoted) {
        zones.push(area(x0, curve.k_quoted[0], "#8B877C", g0 > x0 ? "" : "外推", 0.10, 1));
        zones.push(area(curve.k_quoted[1], x1, "#8B877C", g1 < x1 ? "" : "外推", 0.10, 1));
      }
      // 网格外：分布没有质量，两条读数都按 0 计——再叠一层更深的灰，明说
      zones.push(area(x0, g0, "#8B877C", "网格外", 0.14, 1));
      zones.push(area(g1, x1, "#8B877C", "网格外", 0.14, 1));
      return zones.filter(Boolean);
    },
    exitMarks(curve, kind, d) {
      const line = (x, text, color, type, width, position) => ({
        xAxis: x, lineStyle: { color, type, width },
        label: { formatter: text, position, fontSize: 10, color: "#44413C" },
      });
      // 竖线标签：insideEndTop 贴线左侧、insideEndBottom 贴线右侧。成本与收盘靠得近时，
      // 让左边那条的标签朝左、右边那条朝右，两段文字背向而不相撞
      const cost = d.position ? d.position.entry_price : null;
      const costLeft = cost != null && cost < d.close;
      const marks = [];
      if (cost != null) marks.push(line(cost, `成本 ${this.fmtPx(cost)}`, "#26241F", "solid", 1.2,
                                        costLeft ? "insideEndTop" : "insideEndBottom"));
      marks.push(line(d.close, `收盘 ${this.fmtPx(d.close)}`, "#2F6B8F", "solid", 1.2,
                      cost != null && !costLeft ? "insideEndTop" : "insideEndBottom"));
      marks.push(line(curve.s0, `起点 F ${this.fmtPx(curve.s0)}`, "#26241F", "dotted", 1, "insideStartTop"));
      if (kind === "cost") {
        marks.push(line(curve.q25, `Q25 投机线 ${this.fmtPx(curve.q25)}`, "#C9C5BB", "solid", 1, "insideStartTop"));
        marks.push(line(curve.q05, `Q05 信念线 ${this.fmtPx(curve.q05)}`, "#C9C5BB", "solid", 1, "insideStartTop"));
      }
      return marks.filter(m => m.xAxis != null);
    },
    // 悬停读数：按价位在对应分支里找最近点（概率方向按 S0 分支）
    exitReadout(curve, kind, qty, x) {
      const side = x >= curve.s0 ? "up" : "down";
      const b = curve[side], P = b.prices;
      let lo = 0, hi = P.length - 1;
      while (hi - lo > 1) { const m = (lo + hi) >> 1; if (P[m] < x) lo = m; else hi = m; }
      const i = Math.abs(P[lo] - x) <= Math.abs(P[hi] - x) ? lo : hi;
      const pc = v => v < 0.001 ? "<0.1%" : (v * 100).toFixed(1) + "%";
      const up = side === "up";
      const rows = [
        `<div style="font-weight:700;font-size:13px">${this.fmtPx(P[i])}` +
          ` <span style="color:#8B877C;font-weight:400">较${kind === "cost" ? "成本" : "收盘"} ${this.fmtSignedPct(b.pct[i])}</span></div>`,
        `<div>到期收在其${up ? "上" : "下"} <b>${pc(b.expiry_prob[i])}</b></div>`,
        `<div>路上${up ? "涨" : "跌"}到过 <b>${pc(b.expiry_prob[i])} – ${pc(b.touch_hi[i])}</b></div>`,
      ];
      if (qty != null && b.locked) {
        const v = b.locked[i];
        const amt = Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2 });
        rows.push(`<div>全仓锁定 <b style="color:${v >= 0 ? "#3E7A52" : "#A33B2E"}">${v >= 0 ? "+" : "−"}${amt}</b></div>`);
      }
      return rows.join("");
    },
    renderExitChart(ref, curve, kind, d, qty) {
      const ch = this.chart(ref);
      if (!ch) return null;
      const BLUE = "#2F6B8F";
      const pts = (b, key) => b.prices.map((k, i) => [k, b[key][i]]);
      // 阴影带：[价位, 上沿] 正向 + [价位, 下沿] 反向拼成闭合多边形
      const band = (b) => ({
        type: "custom", silent: true, z: 2, clip: true, data: [0],
        tooltip: { show: false },
        renderItem: (params, api) => ({
          type: "polygon",
          shape: { points: [...b.prices.map((k, i) => api.coord([k, b.touch_hi[i]])),
                            ...b.prices.map((k, i) => api.coord([k, b.expiry_prob[i]])).reverse()] },
          style: { fill: "rgba(47,107,143,0.16)" },
        }),
      });
      const line = (b, extra = {}) => ({
        type: "line", data: pts(b, "expiry_prob"), symbol: "none", z: 3,
        lineStyle: { color: BLUE, width: 2, cap: "round", join: "round" },
        emphasis: { disabled: true }, ...extra,
      });
      ch.setOption({
        animation: false,
        grid: { left: 44, right: 14, top: 16, bottom: 26 },
        xAxis: { type: "value", min: d.axis[0], max: d.axis[1], scale: true,
                 // 轴两端是算出来的非整数，不标；刻度取整数或两位小数
                 axisLabel: { fontSize: 10, color: "#8B877C", showMinLabel: false, showMaxLabel: false,
                              formatter: v => (Number.isInteger(v) ? String(v) : String(+v.toFixed(2))) },
                 axisLine: { lineStyle: { color: "#C9C5BB" } }, splitLine: { show: false } },
        yAxis: { type: "value", min: 0, max: 1, interval: 0.25,
                 axisLabel: { fontSize: 10, color: "#8B877C", formatter: v => Math.round(v * 100) + "%" },
                 splitLine: { lineStyle: { color: "#EFEDE8" } } },
        tooltip: {
          trigger: "axis", confine: true, backgroundColor: "#FFFFFF", borderColor: "#DDDAD2",
          borderWidth: 1, padding: [8, 10], textStyle: { color: "#26241F", fontSize: 12 },
          extraCssText: "box-shadow:0 4px 14px rgba(38,36,31,.10);border-radius:6px;line-height:1.7",
          axisPointer: { type: "line", snap: false, lineStyle: { color: "#8B877C", width: 1 },
                         label: { show: false } },
          formatter: (ps) => {
            const x = Array.isArray(ps) && ps.length ? ps[0].axisValue : null;
            return x == null ? "" : this.exitReadout(curve, kind, qty, Number(x));
          },
        },
        series: [
          band(curve.down), band(curve.up),
          line(curve.down, {
            markArea: { silent: true, data: this.exitZones(curve, qty, d.axis) },
            markLine: { silent: true, symbol: "none", data: this.exitMarks(curve, kind, d) },
          }),
          line(curve.up),
        ],
      }, true);
      return ch;
    },
    // ---------- 准入与池管理 ----------
    async runAdmission() {
      if (!this.admSymbol.trim()) return;
      this.admBusy = true; this.admErr = ""; this.admResult = null;
      try {
        const r = await api(`/api/admission/${this.admSymbol.trim().toUpperCase()}`);
        this.admResult = r.candidate;
        this.admPool = r.pool;
      } catch (e) { this.admErr = e.detail || "检查失败"; }
      this.admBusy = false;
    },
    async poolAdd() {
      this.admBusy = true; this.admErr = "";
      try {
        const dyn = (this.poolStatus.holdings || []).map(d => d.symbol);
        let replace = null;
        if (dyn.length >= 2) {
          replace = prompt(`持仓组已满，换出哪一个？（${dyn.join(" / ")}）\n换出不删数据，换回零成本`);
          if (!replace) { this.admBusy = false; return; }
        }
        const r = await api("/api/pool/add", { method: "POST",
          body: JSON.stringify({ symbol: this.admResult.symbol, replace }) });
        if (!r.ok) { this.admErr = r.error; this.admBusy = false; return; }
        this.admResult = null; this.admSymbol = "";
        await this.refreshPool();
        this.overview = (await api("/api/overview")).symbols;
        this.symbols = this.overview.map(o => o.symbol);
      } catch (e) { this.admErr = e.detail || "换入失败"; }
      this.admBusy = false;
    },
    async refreshPool() {
      this.poolStatus = await api("/api/pool/status");
      const anyRunning = (this.poolStatus.holdings || []).some(d => d.running);
      clearTimeout(this.poolTimer);
      if (anyRunning) this.poolTimer = setTimeout(() => this.refreshPool(), 30000);
    },
    // ---------- 交易日志 ----------
    async openPosition() {
      this.journalErr = "";
      try {
        const r = await api("/api/journal/open", {
          method: "POST",
          body: JSON.stringify({ symbol: this.active, ...this.form }),
        });
        if (!r.ok) { this.journalErr = r.error; return; }
        await this.switchSymbol(this.active);
        this.overview = (await api("/api/overview")).symbols;
      } catch (e) { this.journalErr = e.detail || e.error || "录入失败"; }
    },
    async closePosition() {
      const price = prompt("平仓价（收盘确认制，可留空）");
      if (price === null) return;
      const reason = prompt("平仓理由（失效线触发 / 止盈 / 主观）") || "";
      await api("/api/journal/close", {
        method: "POST",
        body: JSON.stringify({ position_id: this.myPosition.position_id,
                               close_price: price, close_reason: reason }),
      });
      await this.switchSymbol(this.active);
      this.overview = (await api("/api/overview")).symbols;
    },
  },
  mounted() {
    this.boot();
    window.addEventListener("resize", () => {
      Object.values(this.charts).forEach(c => c && c.resize());
    });
    window.addEventListener("popstate", () => {
      if (this.view === "login" || this.view === "boot") return;
      this.routeFromLocation();
    });
  },
}).mount("#app");
