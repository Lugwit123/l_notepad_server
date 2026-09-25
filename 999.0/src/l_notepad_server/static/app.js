/* L Notepad 共享脚本：登出、HTML 转义、时间格式化、Toast。 */
(function () {
  "use strict";

  var ROOT = (document.body.getAttribute("data-root") || "").replace(/\/$/, "");

  var LN = {
    root: ROOT,

    /* HTML 转义：所有 innerHTML 拼接用户数据前必须调用（防 XSS） */
    esc: function (s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    },

    /* 相对时间 */
    relTime: function (ts) {
      if (!ts) return ts;
      var d = new Date(String(ts).trim().replace(" ", "T"));
      if (isNaN(d)) return ts;
      var diff = (Date.now() - d) / 1000;
      if (diff < 60) return "刚刚";
      if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
      if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
      if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
      return ts;
    },

    /* ISO 时间 → 紧凑可读（当年省略年份） */
    fmtTs: function (ts) {
      if (!ts) return ts;
      var d = new Date(String(ts).trim());
      if (isNaN(d)) return ts;
      var now = new Date();
      var y = d.getFullYear();
      var mo = String(d.getMonth() + 1).padStart(2, "0");
      var dd = String(d.getDate()).padStart(2, "0");
      var hh = String(d.getHours()).padStart(2, "0");
      var mi = String(d.getMinutes()).padStart(2, "0");
      return y === now.getFullYear()
        ? mo + "-" + dd + " " + hh + ":" + mi
        : y + "-" + mo + "-" + dd;
    },

    /* 批量格式化 [data-ts] 元素 */
    fmtAll: function (rootEl) {
      (rootEl || document).querySelectorAll("[data-ts]").forEach(function (el) {
        el.textContent = LN.fmtTs(el.getAttribute("data-ts"));
      });
    },

    /* Toast：页面放 <div id="toast"></div> 即可用 */
    toast: function (msg, type) {
      var el = document.getElementById("toast");
      if (!el) return;
      el.textContent = msg;
      el.className = "show " + (type || "success");
      clearTimeout(el._timer);
      el._timer = setTimeout(function () { el.className = ""; }, 2400);
    },

    /* 字节数人性化 */
    fmtSize: function (n) {
      if (n == null) return "";
      if (n < 1024) return n + " B";
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
      if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
      return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
    }
  };

  window.LN = LN;

  /* 登出：服务端清除 HttpOnly cookie 后跳登录页 */
  window.doLogout = function () {
    fetch(ROOT + "/api/auth/logout", { method: "POST" })
      .catch(function () {})
      .finally(function () {
        window.location.href = ROOT + "/login";
      });
  };

  /* 全站外观偏好（base.html 顶栏「外观」菜单控制，localStorage 持久化） */
  var PREF_THEME = "l_notepad_markdown_layout_theme";
  var PREF_FONT = "l_notepad_ui_font";
  var PREF_ACCENT = "l_notepad_ui_accent";

  function prefGet(key, dflt) {
    try {
      return window.localStorage.getItem(key) || dflt;
    } catch (e) {
      return dflt;
    }
  }

  function prefSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {}
  }

  var prefs = {
    editorTheme: function () {
      return prefGet(PREF_THEME, "windterm") === "classic" ? "classic" : "windterm";
    },
    font: function () {
      var v = prefGet(PREF_FONT, "normal");
      return v === "small" || v === "large" ? v : "normal";
    },
    accent: function () {
      return prefGet(PREF_ACCENT, "blue");
    },
    setEditorTheme: function (v) {
      prefSet(PREF_THEME, v === "classic" ? "classic" : "windterm");
      this.apply();
    },
    setFont: function (v) {
      prefSet(PREF_FONT, v === "small" || v === "large" ? v : "normal");
      this.apply();
    },
    setAccent: function (v) {
      prefSet(PREF_ACCENT, v || "blue");
      this.apply();
    },
    /* 偏好 → <html> 属性（CSS 主题由属性选择器接管） */
    apply: function () {
      var html = document.documentElement;
      html.setAttribute("data-editor-theme", prefs.editorTheme());
      var font = prefs.font();
      if (font === "normal") html.removeAttribute("data-ln-font");
      else html.setAttribute("data-ln-font", font);
      var accent = prefs.accent();
      if (accent === "blue") html.removeAttribute("data-ln-accent");
      else html.setAttribute("data-ln-accent", accent);
      document.dispatchEvent(new CustomEvent("ln:prefs", { detail: { theme: prefs.editorTheme() } }));
    }
  };
  LN.prefs = prefs;

  /* 顶栏「外观」菜单内容：分段选择 + 强调色。
     菜单自身的开合由 base.html 内联脚本负责（不依赖本文件的加载与 hidden 属性）。 */
  (function () {
    var segTheme = document.getElementById("seg-editor-theme");
    var segFont = document.getElementById("seg-font");
    var dotsAccent = document.getElementById("dots-accent");

    /* 分段选择态 */
    function markGroup(container, value) {
      if (!container) return;
      var buttons = container.getElementsByTagName("button");
      for (var i = 0; i < buttons.length; i++) {
        var b = buttons[i];
        if (b.getAttribute("data-value") === value) b.className = "active";
        else b.className = String(b.className).replace(/\bactive\b/g, "");
      }
    }

    function syncMenuState() {
      markGroup(segTheme, prefs.editorTheme());
      markGroup(segFont, prefs.font());
      markGroup(dotsAccent, prefs.accent());
    }

    /* 找最近的 button 祖先（Element.closest 在老 IE 内核不存在） */
    function closestButton(el) {
      while (el && el !== document) {
        if (String(el.tagName).toLowerCase() === "button") return el;
        el = el.parentNode;
      }
      return null;
    }

    function bindGroup(container, apply) {
      if (!container) return;
      container.addEventListener("click", function (e) {
        var b = closestButton(e.target || e.srcElement);
        if (!b) return;
        apply(b.getAttribute("data-value"));
        syncMenuState();
      });
    }

    bindGroup(segTheme, prefs.setEditorTheme.bind(prefs));
    bindGroup(segFont, prefs.setFont.bind(prefs));
    bindGroup(dotsAccent, prefs.setAccent.bind(prefs));

    if (document.getElementById("app-theme-menu")) syncMenuState();
    prefs.apply();
  })();
})();

/* ══ 搜索结果渲染（顶栏弹窗 与「搜索索引」页 共用同一份，保证样式一致）══
   用法：container.innerHTML = LN.renderSearchHits(d.hits)（或拿返回值自行包裹） */
(function () {
  window.LN = window.LN || {};

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function num(v, d) { return (v === null || v === undefined) ? "—" : Number(v).toFixed(d); }
  function pct(v) { return (v === null || v === undefined) ? 0 : Math.round(v * 100); }

  /* 命中词高亮：先转义，再按位置区间套 <mark>（相邻/重叠合并） */
  function hl(text, terms) {
    var safe = esc(text), low = safe.toLowerCase(), spans = [];
    (terms || []).slice().sort(function (a, b) { return b.length - a.length; })
      .forEach(function (t) {
        var needle = esc(t).toLowerCase();
        if (!needle) return;
        var from = 0;
        while (spans.length < 200) {
          var p = low.indexOf(needle, from);
          if (p < 0) break;
          spans.push([p, p + needle.length]);
          from = p + 1;
        }
      });
    if (!spans.length) return safe;
    spans.sort(function (a, b) { return a[0] - b[0]; });
    var merged = [];
    spans.forEach(function (s) {
      var last = merged[merged.length - 1];
      if (last && s[0] <= last[1]) last[1] = Math.max(last[1], s[1]);
      else merged.push([s[0], s[1]]);
    });
    var out = "", at = 0;
    merged.forEach(function (s) {
      out += safe.slice(at, s[0]) + "<mark>" + safe.slice(s[0], s[1]) + "</mark>";
      at = s[1];
    });
    return out + safe.slice(at);
  }

  function sig(label, value, tip, extra) {
    return '<span class="sig' + (extra || "") + '" title="' + esc(tip || "") + '">' +
      label + " <b>" + value + "</b></span>";
  }
  function bar(v, cls) {
    return '<i class="mini' + (cls || "") + '"><s style="width:' + pct(v) + '%"></s></i>';
  }

  /* 单条命中 → .hit 片段 */
  var hitSeq = 0;
  var hitStore = {};

  function renderHit(h) {
    var hot = (h.phrase_hits || 0) > 0;
    var icon = h.source === "kb" ? "📚 " : (h.source === "code" ? "🧩 " : "📝 ");
    var libBadge = h.source === "code"
      ? '<span class="badge lib" title="本机库（代码库根 / 知识库工作区），只读查看">' + esc(h.kb_name || "code") + "</span>"
      : "";
    var id = "";
    if (h.explain) {
      id = "lnhit" + (++hitSeq);
      hitStore[id] = h;
    }
    return '<div class="hit' + (hot ? " hot" : "") + '">' +
      '<div class="hit-head"><a href="' + esc(h.open_url) + '">' +
      icon + libBadge + esc(h.rel) + "</a>" +
      (id ? '<button class="why" type="button" data-why="' + id + '" title="为什么它排在这里：展开打分明细">判断依据</button>' : "") +
      '<span class="badge-score" title="综合相关度：3×短语 + 2×覆盖率 + 词频 + 近邻 − 1.5×bm25">' +
      num(h.score, 2) + "</span></div>" +
      '<div class="snip">' + hl(h.snippet, h.matches) + "</div>" +
      '<div class="signals">' +
      (hot ? '<span class="sig hot-sig" title="引号短语精确命中次数">短语 <b>' + h.phrase_hits + "</b></span>" : "") +
      '<span class="sig main" title="查询词块有多少出现在本文（越高越好）">覆盖 <b>' + pct(h.coverage) + "%</b>" +
      bar(h.coverage, " cov") + "</span>" +
      '<span class="sig main" title="命中词块相近程度：1.0 最集中（200 字符内）">近邻 <b>' + num(h.proximity, 2) + "</b>" +
      bar(h.proximity, " prox") + "</span>" +
      (h.vec ? '<span class="sig main" title="向量语义相似度（余弦）">语义 <b>' + num(h.vec, 3) + "</b></span>" : "") +
      (h.rerank ? '<span class="sig main" title="交叉编码重排分（排序主序；0 = 本轮未参与重排）">重排 <b>' + num(h.rerank, 3) + "</b></span>" : "") +
      (h.chunk ? '<span class="sig dim" title="命中的分块（第 ' + (h.chunk_no || 0) + " 块，正文偏移 " + (h.chunk_offset || 0) + '）">块</span>' : "") +
      sig("词频", num(h.tf, 2), "词块出现次数加权（单块封顶 5 次）", " dim") +
      sig("bm25", num(h.bm25, 2), "倒排引擎原始分（越负越相关，取负后计入总分）", " dim") +
      '<span class="sig dim when">' + esc((h.updated_at || "").replace("T", " ").slice(0, 16)) + "</span>" +
      "</div></div>";
  }

  /* ── 「判断依据」对话框：把这个命中项的打分拆开讲清楚 ── */
  var PART_LABEL = {
    phrase: "短语命中", coverage: "覆盖率", tf: "词频",
    proximity: "近邻度", bm25: "bm25", vec: "语义相似度",

  };
  var PART_TIP = {
    phrase: "引号短语精确命中次数 × 权重 3",
    coverage: "命中的词块权重和 ÷ 全部词块权重和（泛词权重记 0，不进分母）× 权重 2",
    tf: "Σ(词块权重 × 出现次数，单块封顶 " + 5 + " 次) ÷ (封顶 × 权重和) × 权重 1",
    proximity: "命中词块首现位置的集中程度：1/(1+跨度/200)，命中短语直接记 1 × 权重 1",
    bm25: "倒排引擎原始分（负值，越负越相关），取负后 × 权重 1.5 加分",
    vec: "向量余弦相似度 × 权重 1.2（纯语义模式下总分就等于它）",
  };

  function whyHtml(h) {
    var ex = h.explain || {};
    var out = [];
    out.push(
      '<div class="why-head">' +
      (h.source === "code" ? "🧩 " : h.source === "kb" ? "📚 " : "📝 ") +
      (h.kb_name ? esc(h.kb_name) + " / " : "") + esc(h.rel || "") +
      "</div>"
    );
    var meta = [];
    if (ex.rank) meta.push("第 <b>" + ex.rank + "</b> / " + ex.of + " 名");
    if (ex.order_by) meta.push("排序主序：<b>" + (ex.order_by === "rerank" ? "重排分" : "综合分") + "</b>");
    meta.push("综合分 <b>" + num(h.score, 2) + "</b>");
    meta.push("实际模式 <b>" + esc(ex.mode || "") + "</b>");
    out.push('<div class="why-meta">' + meta.join(" · ") + "</div>");
    out.push('<div class="why-sum">' + esc(ex.summary || "") + "</div>");

    if (ex.parts) {
      out.push('<table class="why-table"><thead><tr><th>分项</th><th>权重</th><th>取值</th><th>得分</th></tr></thead><tbody>');
      Object.keys(ex.parts).forEach(function (k) {
        var p = ex.parts[k];
        if (k === "vec" && !p.value) return;
        out.push('<tr title="' + esc(PART_TIP[k] || "") + '"><td>' + esc(PART_LABEL[k] || k) + "</td><td>" +
          p.weight + "</td><td>" + num(p.value, 3) + "</td><td><b>" + num(p.score, 3) + "</b></td></tr>");
      });
      out.push("</tbody></table>");
    }

    if (ex.terms && ex.terms.length) {
      out.push('<div class="why-sec">命中词块（权重 = IDF，越稀有权重越高；次数为该词在文中的出现数）</div>');
      out.push('<div class="why-chips">' + ex.terms.map(function (t) {
        return '<span class="chip-term" title="' + esc(t.type || "") + '">' + esc(t.text) +
          " ×" + t.hits + " <i>w" + t.weight + "</i></span>";
      }).join("") + "</div>");
    }
    if (ex.terms_generic && ex.terms_generic.length) {
      out.push('<div class="why-sec">被判为 repo 泛词（覆盖 ≥ ' +
        Math.round((ex.weights && ex.weights.generic_ratio ? ex.weights.generic_ratio : 0.3) * 100) +
        "% 的文档都有），权重记 0、不参与打分</div>");
      out.push('<div class="why-chips">' + ex.terms_generic.map(function (t) {
        return '<span class="chip-term off">' + esc(t) + "</span>";
      }).join("") + "</div>");
    }
    if (ex.terms_missed && ex.terms_missed.length) {
      out.push('<div class="why-sec">未命中的词块</div>');
      out.push('<div class="why-chips">' + ex.terms_missed.map(function (t) {
        return '<span class="chip-term miss">' + esc(t) + "</span>";
      }).join("") + "</div>");
    }
    if (ex.vs_next) {
      out.push('<div class="why-sec">为什么排在它前面：' + esc(ex.vs_next.path) + "</div>");
      out.push('<div class="why-sum">综合分差 <b>' + num(ex.vs_next.score_gap, 2) +
        "</b>，主要来自 " + esc(ex.vs_next.main_reason) + "。</div>");
    }
    return out.join("");
  }

  function ensureWhyDialog() {
    var dlg = document.getElementById("ln-why");
    if (dlg) return dlg;
    dlg = document.createElement("div");
    dlg.id = "ln-why";
    dlg.className = "ln-why";
    dlg.innerHTML = '<div class="ln-why-back"></div><div class="ln-why-box" role="dialog" aria-modal="true">' +
      '<div class="ln-why-bar"><span class="ln-why-title">判断依据：这条为什么排在这里</span>' +
      '<button type="button" class="ln-why-close" title="关闭">✕</button></div>' +
      '<div class="ln-why-body"></div></div>';
    document.body.appendChild(dlg);
    dlg.querySelector(".ln-why-back").addEventListener("click", closeWhy);
    dlg.querySelector(".ln-why-close").addEventListener("click", closeWhy);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeWhy();
    });
    return dlg;
  }
  function closeWhy() {
    var dlg = document.getElementById("ln-why");
    if (dlg) dlg.classList.remove("open");
  }
  function openWhy(id) {
    var h = hitStore[id];
    if (!h) return;
    var dlg = ensureWhyDialog();
    dlg.querySelector(".ln-why-body").innerHTML = whyHtml(h);
    dlg.classList.add("open");
  }
  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest("[data-why]") : null;
    if (btn) {
      e.preventDefault();
      openWhy(btn.getAttribute("data-why"));
    }
  });

  LN.renderSearchHits = function (hits) {
    hits = hits || [];
    /* 不重置 hitStore：顶栏弹窗与页面各渲染一次，重置会让先渲染那批的「判断依据」点不动 */
    if (!hits.length) return '<div class="muted">无命中</div>';
    return hits.map(renderHit).join("");
  };
  LN.searchHitEsc = esc;
  LN.openHitWhy = openWhy;
})();
