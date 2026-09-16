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
