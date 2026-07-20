/* RND 仪表盘前端。Vue3 全局构建 + ECharts（本地 vendor，无构建步骤）。 */
const { createApp } = Vue;

const PCT_INDICATORS = new Set(["atm_iv", "tail_p_down", "tail_p_up"]);

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = localStorage.getItem("rnd_token");
  if (token) headers["X-Auth"] = token;
  const r = await fetch(path, { headers, credentials: "same-origin", ...opts });
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
    admSymbol: "", admBusy: false, admResult: null, admPool: [], admErr: "",
    poolStatus: {}, poolTimer: null,
    cmpMode: "prev", cmpLabel: "",
    diagMeta: null,
    form: { direction: "long", identity: "speculative", entry_price: "", risk_budget: "",
            target_price: "", target_rationale: "" },
    journalErr: "",
    charts: {},
  }),
  computed: {
    ind() { return this.detail ? this.detail.indicators : null; },
    myPosition() {
      return this.detail && this.detail.positions.length ? this.detail.positions[0] : null;
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
  },
  methods: {
    fmt(v, n = 2) { return v == null ? "—" : Number(v).toFixed(n); },
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
        const r = await api("/api/login", { method: "POST",
          body: JSON.stringify({ password: this.password }) });
        if (r.token) localStorage.setItem("rnd_token", r.token);
        this.password = "";
        await this.boot();
      } catch (e) { this.loginErr = e.detail || "登录失败"; }
    },
    async doLogout() {
      localStorage.removeItem("rnd_token");
      await api("/api/logout", { method: "POST" });
      this.view = "login";
    },
    async boot() {
      try {
        const ov = await api("/api/overview");
        this.overview = ov.symbols;
        this.symbols = ov.symbols.map(o => o.symbol);
        this.view = "dash";
        const ready = ov.symbols.find(o => o.ready);
        await this.switchSymbol(ready ? ready.symbol : this.symbols[0]);
        this.events = (await api("/api/events")).events.slice(0, 12);
        this.refreshPool();
      } catch (e) {
        if (e.auth) this.view = "login"; else throw e;
      }
    },
    async switchSymbol(sym) {
      this.active = sym;
      this.diagMeta = null;
      this.detail = await api(`/api/symbol/${sym}`);
      await this.$nextTick();   // 等 v-if 区块挂载，避免图表在零宽容器上初始化
      await Promise.all([this.loadFan(), this.loadDensity(), this.loadHeatmap(),
                        this.loadDcdf(), this.loadPit()]);
      await this.loadCompare();
      this.renderTails();
      if (this.charts.diagChart) { this.charts.diagChart.clear(); this.diagMeta = null; }
      // 兜底：容器尺寸迟到时（慢渲染环境）延迟重排一次
      setTimeout(() => Object.values(this.charts).forEach(c => c && c.resize()), 600);
    },
    chart(refName) {
      const el = this.$refs[refName];
      if (!el) return null;
      if (!this.charts[refName]) {
        const c = echarts.init(el);
        this.charts[refName] = c;
        // 容器尺寸任何时刻变化（面板缩放、布局迟到、侧栏伸缩）都自动重排
        new ResizeObserver(() => c.resize()).observe(el);
      } else {
        this.charts[refName].resize();
      }
      return this.charts[refName];
    },
    // ---------- 扇形带主图 ----------
    async loadFan() {
      this.fanData = await api(`/api/symbol/${this.active}/fan?days=${this.fanDays}`);
      const f = this.fanData, dates = f.dates;
      const closes = dates.map(d => f.close[d] ?? null);
      const diff = (a, b) => a.map((v, i) => v == null || b[i] == null ? null : v - b[i]);
      const band = (name, data, color) => ({
        name, type: "line", stack: "band", data, symbol: "none",
        lineStyle: { width: 0 }, areaStyle: { color, opacity: 1 }, emphasis: { disabled: true },
      });
      const marks = [
        ...f.roll_dates.map(d => ({ xAxis: d, label: { formatter: "●", color: "#6E6A60" } })),
        ...f.gate_fail_dates.map(d => ({ xAxis: d, label: { formatter: "▮", color: "#A33B2E" } })),
        ...f.bimodal_dates.map(d => ({ xAxis: d, label: { formatter: "◆", color: "#8F5C22" } })),
      ];
      const frozenLines = f.frozen.map(fr => ({
        yAxis: fr.level,
        label: { formatter: `冻结 ${fr.stop_q.toUpperCase()} ${fr.level.toFixed(1)}`, position: "insideEndTop", fontSize: 10 },
        lineStyle: { color: "#26241F", type: "solid", width: 2 },
      }));
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
                        data: [...frozenLines, ...marks],
                        lineStyle: { color: "#B3AFA4", type: "dotted" } } },
        ],
      }, true);
    },
    // ---------- 当日密度 ----------
    async loadDensity() {
      this.densityData = await api(`/api/symbol/${this.active}/density`);
      const d = this.densityData;
      if (!d.ok) return;
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
    async loadHeatmap() {
      this.heatData = await api(`/api/symbol/${this.active}/heatmap`);
      const h = this.heatData;
      const closeIdx = h.dates.map(d => {
        const c = h.close[d];
        if (c == null) return null;
        let best = 0, bd = Infinity;
        h.prices.forEach((p, i) => { const dd = Math.abs(p - c); if (dd < bd) { bd = dd; best = i; } });
        return best;
      });
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
          { type: "line", data: closeIdx, symbol: "none", lineStyle: { color: "#2F8F8F", width: 1.4 } },
        ],
      }, true);
    },
    // ---------- 双日对比 ----------
    async loadCompare() {
      const d = this.detail;
      if (!d) return;
      let otherDate = null, label = "";
      if (this.cmpMode === "entry" && this.myPosition) {
        otherDate = this.myPosition.event_date;
        label = `入场日 ${otherDate}（虚）vs 今日（实）`;
      } else {
        const f = this.fanData;
        otherDate = f && f.dates.length > 1 ? f.dates[f.dates.length - 2] : null;
        label = otherDate ? `${otherDate}（虚）vs 今日（实）` : "";
        if (this.cmpMode === "entry") label = "无持仓 · 退回 昨 vs 今";
      }
      this.cmpLabel = label;
      if (!otherDate) return;
      const [a, b] = await Promise.all([
        api(`/api/symbol/${this.active}/density?date=${otherDate}`),
        Promise.resolve(this.densityData),
      ]);
      if (!a.ok || !b.ok) { this.cmpLabel = "对比日无曲线"; return; }
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
    async loadDcdf() {
      this.dcdfData = await api(`/api/symbol/${this.active}/dcdf`);
      const d = this.dcdfData;
      if (!d.ok) return;
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
    async loadPit() {
      this.pitData = await api(`/api/symbol/${this.active}/pit`);
      const p = this.pitData;
      if (!p.ok) return;
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
        const dyn = (this.poolStatus.dynamic || []).map(d => d.symbol);
        let replace = null;
        if (dyn.length >= 2) {
          replace = prompt(`动态槽已满，换出哪一个？（${dyn.join(" / ")}）\n换出不删数据，换回零成本`);
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
      const anyRunning = (this.poolStatus.dynamic || []).some(d => d.running);
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
  },
}).mount("#app");
