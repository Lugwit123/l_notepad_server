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
})();
