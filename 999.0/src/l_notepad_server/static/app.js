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
  function renderHit(h) {
    var hot = (h.phrase_hits || 0) > 0;
    return '<div class="hit' + (hot ? " hot" : "") + '">' +
      '<div class="hit-head"><a href="' + esc(h.open_url) + '">' +
      (h.source === "kb" ? "📚 " : "📝 ") + esc(h.rel) + "</a>" +
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

  LN.renderSearchHits = function (hits) {
    hits = hits || [];
    if (!hits.length) return '<div class="muted">无命中</div>';
    return hits.map(renderHit).join("");
  };
  LN.searchHitEsc = esc;
})();
