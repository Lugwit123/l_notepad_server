/* L Notepad 脑图：零依赖渲染器 + 交互编辑器。
 *
 * LNMM.render(container, src)      —— 只读渲染（md 内嵌 ```mindmap 块预览）
 * LNMM.edit(container, src, opts)  —— 交互编辑器（.mmd 全文档）
 *   返回 { serialize(), focus() }
 *   opts.onChange(text) 任何修改后回调
 *
 * 大纲语法（两种可混用，深度即层级）：
 *   - 标题：`# 根`、`## 分支`（列表行挂在最近标题之下）
 *   - 缩进列表：`- 条目`，每 2 空格一层；序列化固定用此格式
 *
 * 编辑器交互：
 *   单击选中 · 双击改名 · 拖到目标节点上松手=移动子树（不可拖进自己后代）
 *   Tab=加子节点 · Enter=加同级 · Delete=删除子树 · F2=改名
 *   拖到画布空白处松手=自由定位（整个子树按像素平移，坐标持久化）
 *   位置以 `<!--mm-pos {json}-->` 注释形式附加在大纲末尾，随笔记内容一起保存。
 */
(function () {
  "use strict";

  var PALETTE = ["#5b9cff", "#38d4cc", "#a78bfa", "#fb923c", "#4ade80", "#f472b6"];
  var TEXT_FILL = "#dce8ff";
  var FONT_ROOT = "600 14px -apple-system, 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
  var FONT_NODE = "13px -apple-system, 'Segoe UI', 'Microsoft YaHei UI', sans-serif";
  var NODE_H = 26, ROOT_H = 30, PAD_X = 12, COL_GAP = 52, ROW_GAP = 10;
  var NS = "http://www.w3.org/2000/svg";
  var META_OPEN = "<!--mm-pos ";
  var META_CLOSE = "-->";
  var META_RE = /\s*<!--mm-pos\s+(\{[\s\S]*?\})\s*-->\s*$/;

  /* ── 位置持久化（坐标偏移）────────────────────────────── */

  function splitMeta(src) {
    var m = String(src || "").match(META_RE);
    if (!m) return { outline: src || "", posMap: null };
    var posMap = null;
    try { posMap = JSON.parse(m[1]); } catch (e) { posMap = null; }
    return { outline: String(src).slice(0, m.index), posMap: posMap };
  }

  function pathKeyStr(n) {
    var parts = [];
    for (var cur = n; cur; cur = cur.parent) parts.unshift(cur.label);
    return parts.join("\u0001");
  }

  function applyStoredPositions(tree, map) {
    if (!map) return;
    (function walk(n) {
      var k = pathKeyStr(n);
      if (Object.prototype.hasOwnProperty.call(map, k)) {
        n._dx = map[k][0] || 0;
        n._dy = map[k][1] || 0;
      }
      n.children.forEach(walk);
    })(tree);
  }

  function buildMeta(root) {
    var map = {};
    (function walk(n) {
      if ((n._dx || 0) || (n._dy || 0)) map[pathKeyStr(n)] = [Math.round(n._dx), Math.round(n._dy)];
      n.children.forEach(walk);
    })(root);
    var keys = Object.keys(map);
    if (!keys.length) return "";
    return META_OPEN + JSON.stringify(map) + META_CLOSE + "\n";
  }

  function clearSubtreeOffsets(n) {
    (function walk(x) {
      x._dx = 0; x._dy = 0;
      x.children.forEach(walk);
    })(n);
  }

  function clearDescendantOffsets(n) {
    n.children.forEach(function (c) {
      (function walk(x) {
        x._dx = 0; x._dy = 0;
        x.children.forEach(walk);
      })(c);
    });
  }

  /* 拖拽时快照/恢复整棵子树的自由定位偏移（用于撤销拖拽）。 */
  function snapshotSubtreeOffsets(n) {
    var map = {};
    (function walk(x) {
      map[x.id] = [x._dx || 0, x._dy || 0];
      x.children.forEach(walk);
    })(n);
    return map;
  }

  function restoreSubtreeOffsets(n, map) {
    if (!map) return;
    (function walk(x) {
      if (Object.prototype.hasOwnProperty.call(map, x.id)) {
        x._dx = map[x.id][0]; x._dy = map[x.id][1];
      }
      x.children.forEach(walk);
    })(n);
  }

  /* 布局后把「子树偏移」累加到节点坐标（祖先+自身偏移逐层求和）。 */
  function applyOffsets(n, ax, ay) {
    var tx = ax + (n._dx || 0);
    var ty = ay + (n._dy || 0);
    n.x += tx;
    n.y += ty;
    n.children.forEach(function (c) { applyOffsets(c, tx, ty); });
  }

  function bbox(root) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    (function walk(n) {
      x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y - n.h / 2);
      x1 = Math.max(x1, n.x + n.w); y1 = Math.max(y1, n.y + n.h / 2);
      n.children.forEach(walk);
    })(root);
    return { x0: x0, y0: y0, x1: x1, y1: y1 };
  }

  /* ── 大纲解析 / 序列化（渲染器与编辑器共用）────────────── */

  function parseTree(src) {
    var lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
    var lastHeadingDepth = -1;
    var flat = [];
    lines.forEach(function (line) {
      var raw = line.replace(/\s+$/, "");
      if (!raw.trim()) return;
      var m = raw.match(/^(#{1,6})\s+(.+)$/);
      var depth, label;
      if (m) {
        depth = m[1].length - 1;
        lastHeadingDepth = depth;
        label = m[2];
      } else {
        m = raw.match(/^(\s*)(?:[-*+•]\s+)?(.*)$/);
        var indent = m[1].replace(/\t/g, "  ");
        depth = lastHeadingDepth + 1 + Math.floor(indent.length / 2);
        label = m[2];
      }
      if (!label.trim()) return;
      flat.push({ _depth: depth, label: label.trim(), children: [] });
    });
    if (!flat.length) return { label: "中心主题", children: [], parent: null };
    var root = { label: "脑图", children: [], parent: null };
    var stack = [root];
    flat.forEach(function (n) {
      while (stack.length > 1 && stack[stack.length - 1]._depth >= n._depth) stack.pop();
      n.parent = stack[stack.length - 1];
      n.parent.children.push(n);
      stack.push(n);
    });
    if (root.children.length === 1) {
      var only = root.children[0];
      only.parent = null;
      return only;
    }
    return root;
  }

  function serializeTree(root) {
    var out = [];
    (function walk(n, depth) {
      out.push(new Array(depth + 1).join("  ") + "- " + n.label);
      n.children.forEach(function (c) { walk(c, depth + 1); });
    })(root, 0);
    return out.join("\n") + "\n";
  }

  function parseNodes(src) {
    var tree = parseTree(src);
    var out = [];
    (function walk(n) {
      out.push({ depth: n.depth || 0, label: n.label });
      n.children.forEach(walk);
    })(tree);
    return out;
  }

  /* ── 布局 / 绘制（共用）───────────────────────────────── */

  function measureText(text, font) {
    var c = measureText._ctx;
    if (!c) c = measureText._ctx = document.createElement("canvas").getContext("2d");
    c.font = font;
    return Math.ceil(c.measureText(text).width);
  }

  function layout(node, depth) {
    node.depth = depth;
    node.isRoot = depth === 0;
    node.h = node.isRoot ? ROOT_H : NODE_H;
    node.w = Math.max(measureText(node.label, node.isRoot ? FONT_ROOT : FONT_NODE) + PAD_X * 2, 24);
    if (!node.children.length) {
      node.treeH = node.h;
      return node;
    }
    var total = 0;
    node.children.forEach(function (c) {
      layout(c, depth + 1);
      total += c.treeH;
    });
    total += ROW_GAP * (node.children.length - 1);
    node.treeH = Math.max(node.h, total);
    return node;
  }

  function assign(node, x, yTop) {
    node.x = x;
    node.y = yTop + node.treeH / 2;
    if (!node.children.length) return;
    var block = node.children.reduce(function (s, c) { return s + c.treeH; }, 0) + ROW_GAP * (node.children.length - 1);
    var cy = yTop + (node.treeH - block) / 2;
    node.children.forEach(function (c) {
      assign(c, x + node.w + COL_GAP, cy);
      cy += c.treeH + ROW_GAP;
    });
  }

  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) e.setAttribute(k, attrs[k]);
    }
    if (parent) parent.appendChild(e);
    return e;
  }

  function drawEdges(svg, node) {
    node.children.forEach(function (c) {
      var color = PALETTE[c.depth % PALETTE.length];
      var x1 = node.x + node.w, y1 = node.y, x2 = c.x, y2 = c.y, mx = (x1 + x2) / 2;
      el("path", {
        d: "M " + x1 + " " + y1 + " C " + mx + " " + y1 + ", " + mx + " " + y2 + ", " + x2 + " " + y2,
        fill: "none", stroke: color, "stroke-width": 1.4, opacity: 0.55,
      }, svg);
      drawEdges(svg, c);
    });
  }

  function drawNodeG(svg, node, editor) {
    var color = PALETTE[node.depth % PALETTE.length];
    var g = el("g", { "data-id": node.id || "", class: "mm-node" }, svg);
    node._g = g;
    var rect = el("rect", {
      x: node.x, y: node.y - node.h / 2,
      width: node.w, height: node.h,
      rx: 7, ry: 7,
      fill: node.isRoot ? "rgba(16,26,48,0.95)" : "rgba(13,19,36,0.92)",
      stroke: color, "stroke-width": node.isRoot ? 1.6 : 1.1,
      class: "mm-node-rect",
    }, g);
    node._rect = rect;
    var t = el("text", {
      x: node.x + PAD_X, y: node.y + (node.isRoot ? 5 : 4.5),
      fill: TEXT_FILL,
      "font-size": node.isRoot ? "14" : "13",
      "font-weight": node.isRoot ? "600" : "400",
      "pointer-events": "none",
    }, g);
    t.textContent = node.label;
    if (editor) editor._bindNode(node, g, rect);
    return g;
  }

  /* 构建自适应 SVG。cam 为空=自然适配（内嵌预览/普通编辑）；有 cam 则按相机平移缩放铺满画布。 */
  function makeSvg(root, cam) {
    var bb = bbox(root);
    var pad = 8;
    var x0 = bb.x0 - pad, y0 = bb.y0 - pad;
    var x1 = bb.x1 + pad, y1 = bb.y1 + pad;
    var natW = x1 - x0, natH = y1 - y0;
    if (!cam) {
      return el("svg", {
        width: Math.ceil(natW), height: Math.ceil(natH),
        viewBox: x0 + " " + y0 + " " + Math.ceil(natW) + " " + Math.ceil(natH),
      });
    }
    var vpW = cam.vpW || 800, vpH = cam.vpH || 600;
    var zoom = cam.zoom || 1, cx = cam.cx || 0, cy = cam.cy || 0;
    var vbx = cx - vpW / (2 * zoom), vby = cy - vpH / (2 * zoom);
    return el("svg", {
      width: vpW, height: vpH,
      viewBox: vbx + " " + vby + " " + (vpW / zoom) + " " + (vpH / zoom),
    });
  }

  function drawAll(svg, n, editor) {
    drawNodeG(svg, n, editor);
    n.children.forEach(function (c) { drawAll(svg, c, editor); });
  }

  /* ── 只读渲染器（md 内嵌预览）────────────────────────── */

  function render(container, src) {
    container.innerHTML = "";
    container.classList.add("mm-block");
    var split = splitMeta(src);
    var root = parseTree(split.outline);
    applyStoredPositions(root, split.posMap);
    if (!root.children.length && root.label === "中心主题") {
      var tip = document.createElement("div");
      tip.className = "mm-fallback";
      tip.textContent = "（空脑图：用 # 标题或缩进列表描述结构）";
      container.appendChild(tip);
      return;
    }
    layout(root, 0);
    assign(root, 0, 0);
    applyOffsets(root, 0, 0);
    var svg = makeSvg(root);
    drawEdges(svg, root);
    drawAll(svg, root, null);
    container.appendChild(svg);
  }

  /* ── 交互编辑器（.mmd 全文档）────────────────────────── */

  var _idSeq = 0;
  function tagIds(node) {
    (function walk(n) {
      n.id = "n" + (++_idSeq);
      if (n._dx === undefined) n._dx = 0;
      if (n._dy === undefined) n._dy = 0;
      n.children.forEach(walk);
    })(node);
    return node;
  }

  function toSvgPoint(svg, clientX, clientY) {
    var p = svg.createSVGPoint();
    p.x = clientX;
    p.y = clientY;
    var ctm = svg.getScreenCTM();
    if (ctm) p = p.matrixTransform(ctm.inverse());
    return p;
  }

  function edit(container, src, opts) {
    opts = opts || {};
    container.innerHTML = "";
    container.classList.add("mm-block", "mm-editor");
    container.setAttribute("tabindex", "0");

    var split = splitMeta(src);
    var root = tagIds(parseTree(split.outline));
    applyStoredPositions(root, split.posMap);
    var selected = null;
    var dragState = null;
    var readonly = !!opts.readonly;
    var onChange = opts.onChange || function () {};
    var svg = null;
    var cam = null;        // 全屏相机：{zoom, cx, cy, vpW, vpH}
    var panState = null;   // 中键平移：{pointerId, startX, startY, cx0, cy0}

    var bar = document.createElement("div");
    bar.className = "mm-toolbar";
    bar.innerHTML =
      (readonly ? "" :
        '<button type="button" data-act="child" title="给选中节点加子节点（Tab）">＋子节点</button>' +
        '<button type="button" data-act="sibling" title="加同级节点（Enter）">＋同级</button>' +
        '<button type="button" data-act="rename" title="重命名（双击节点 / F2）">✏ 改名</button>' +
        '<button type="button" data-act="del" title="删除节点及子树（Delete）">🗑 删除</button>') +
      '<button type="button" class="mm-max" data-act="max" title="最大化脑图进行编辑">⛶ 全屏</button>' +
      '<span class="mm-hint">' + (readonly
        ? "滚轮缩放 · 中键拖动画布 · 单击节点选择"
        : "拖到节点=重排 · 拖到空白=自由定位 · 双击改名 · Tab/Enter 加节点 · Delete 删除") + '</span>';
    container.appendChild(bar);

    var svgHolder = document.createElement("div");
    svgHolder.className = "mm-canvas";
    container.appendChild(svgHolder);

    /* 节点拖拽 + 中键平移/滚轮缩放，事件挂到 svgHolder（持久的容器），重渲染整树不丢 pointer capture。 */
    svgHolder.addEventListener("pointerdown", onPanDown);
    svgHolder.addEventListener("pointermove", function (e) { onPanMove(e); onDragMove(e); });
    svgHolder.addEventListener("pointerup", function (e) { onPanUp(); onDragUp(e); });
    svgHolder.addEventListener("pointercancel", function (e) { onPanUp(); onDragCancel(); });
    svgHolder.addEventListener("wheel", onWheel, { passive: false });

    function serialize() {
      return serializeTree(root) + buildMeta(root);
    }

    function changed() {
      onChange(serialize());
    }

    function findById(id) {
      var found = null;
      (function walk(n) {
        if (found) return;
        if (n.id === id) { found = n; return; }
        n.children.forEach(walk);
      })(root);
      return found;
    }

    function isDescendant(node, maybeAncestor) {
      var p = node.parent;
      while (p) {
        if (p === maybeAncestor) return true;
        p = p.parent;
      }
      return false;
    }

    function select(node) {
      svgHolder.querySelectorAll(".mm-node-rect.sel").forEach(function (r) {
        r.classList.remove("sel");
        r.setAttribute("stroke-width", r.getAttribute("data-sw") || "1.1");
      });
      selected = node;
      if (node && node._rect) {
        node._rect.classList.add("sel");
        node._rect.setAttribute("data-sw", node.isRoot ? "1.6" : "1.1");
        node._rect.setAttribute("stroke-width", "2.4");
      }
    }

    function canvasSize() {
      var w = svgHolder.clientWidth || svgHolder.offsetWidth || 800;
      var h = svgHolder.clientHeight || svgHolder.offsetHeight || (window.innerHeight - 70);
      return { w: w, h: h };
    }

    function redraw(keepId) {
      svgHolder.innerHTML = "";
      layout(root, 0);
      assign(root, 0, 0);
      applyOffsets(root, 0, 0);
      if (cam) {
        var c = canvasSize();
        cam.vpW = c.w; cam.vpH = c.h;
        svg = makeSvg(root, cam);
      } else {
        svg = makeSvg(root, null);
      }
      drawEdges(svg, root);
      drawAll(svg, root, api);
      svgHolder.appendChild(svg);
      if (keepId) {
        var n = findById(keepId);
        if (n) select(n);
      }
    }

    /* 拖拽实时跟随：拖动过程重渲染整树（节点+连线同步移动），松手才提交偏移/结构。 */
    var rafPending = false;

    function requestRender() {
      if (rafPending) return;
      rafPending = true;
      requestAnimationFrame(function () {
        rafPending = false;
        redraw(dragState ? dragState.node.id : (selected ? selected.id : null));
        if (dragState) {
          var t = dragState.hitTarget;
          if (t && t !== dragState.node && !isDescendant(t, dragState.node) && t._rect) {
            t._rect.classList.add("mm-drop");
          }
        }
      });
    }

    function onDragMove(e) {
      if (!dragState) return;
      if (readonly) return;   // 只读视图不移动节点
      var node = dragState.node;
      var dxc = e.clientX - dragState.startX, dyc = e.clientY - dragState.startY;
      if (!dragState.moved && Math.abs(dxc) + Math.abs(dyc) < 4) return;
      if (!dragState.moved) {
        dragState.moved = true;
        dragState.subSnapshot = snapshotSubtreeOffsets(node);
        clearDescendantOffsets(node);
      }
      var cur = toSvgPoint(svg, e.clientX, e.clientY);
      var sp = toSvgPoint(svg, dragState.startX, dragState.startY);
      var dx = cur.x - sp.x, dy = cur.y - sp.y;
      dragState.dx = dx; dragState.dy = dy;
      node._dx = dragState.origDx + dx;
      node._dy = dragState.origDy + dy;
      dragState.hitTarget = nodeAt(e.clientX, e.clientY);
      requestRender();
    }

    function onDragUp(e) {
      if (!dragState) return;
      var node = dragState.node;
      var wasMoved = dragState.moved;
      var dx = dragState.dx || 0, dy = dragState.dy || 0;
      var origDx = dragState.origDx, origDy = dragState.origDy;
      var snap = dragState.subSnapshot;
      dragState = null;
      clearDropHint();
      if (wasMoved) {
        var target = nodeAt(e.clientX, e.clientY);
        if (target && target !== node && !isDescendant(target, node)) {
          // 拖到节点上 = 重排：结构变化，重置该子树自由定位
          if (node.parent) {
            var i = node.parent.children.indexOf(node);
            if (i >= 0) node.parent.children.splice(i, 1);
          }
          target.children.push(node);
          node.parent = target;
          clearSubtreeOffsets(node);
          changed();
          redraw(node.id);
        } else if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) {
          // 拖到空白 = 自由定位：node._dx/_dy 已 = origDx+dx（后代在拖动时已清空），提交
          changed();
          redraw(node.id);
        } else {
          // 撤销本次移动：恢复坐标与后代偏移
          node._dx = origDx; node._dy = origDy;
          restoreSubtreeOffsets(node, snap);
          redraw(selected ? selected.id : null);
        }
      } else {
        select(node);
        container.focus();
      }
    }

    function onDragCancel() {
      if (!dragState) return;
      var node = dragState.node;
      var origDx = dragState.origDx, origDy = dragState.origDy;
      var snap = dragState.subSnapshot;
      dragState = null;
      node._dx = origDx; node._dy = origDy;
      restoreSubtreeOffsets(node, snap);
      redraw(selected ? selected.id : null);
    }

    /* 拖放目标检测：命中哪个节点矩形（+4px 容差） */
    function nodeAt(clientX, clientY) {
      var found = null;
      (function walk(n) {
        if (found) return;
        var r = n._rect;
        if (r) {
          var b = r.getBoundingClientRect();
          if (clientX >= b.left - 4 && clientX <= b.right + 4 && clientY >= b.top - 4 && clientY <= b.bottom + 4) {
            found = n;
            return;
          }
        }
        n.children.forEach(walk);
      })(root);
      return found;
    }

    function clearDropHint() {
      svgHolder.querySelectorAll(".mm-node-rect.mm-drop").forEach(function (r) { r.classList.remove("mm-drop"); });
    }

    /* 节点事件绑定（drawNodeG 回调进 api） */
    function bindNode(node, g) {
      g.addEventListener("pointerdown", function (e) {
        if (e.button !== 0) return;
        dragState = {
          node: node,
          startX: e.clientX,
          startY: e.clientY,
          origDx: node._dx || 0,
          origDy: node._dy || 0,
          subSnapshot: null,
          moved: false,
          dx: 0, dy: 0,
          hitTarget: null,
        };
        try { svgHolder.setPointerCapture(e.pointerId); } catch (err) {}
        e.stopPropagation();
      });

      g.addEventListener("dblclick", function (e) {
        e.stopPropagation();
        if (readonly) return;
        renameNode(node);
      });
    }

    /* 改名：HTML 输入框覆盖在节点上 */
    function renameNode(node) {
      if (!node._rect) return;
      var b = node._rect.getBoundingClientRect();
      var cb = container.getBoundingClientRect();
      var input = document.createElement("input");
      input.className = "mm-rename";
      input.value = node.label;
      input.style.left = (b.left - cb.left) + "px";
      input.style.top = (b.top - cb.top) + "px";
      input.style.width = Math.max(b.width, 90) + "px";
      container.appendChild(input);
      select(node);
      input.focus();
      input.select();
      function commit() {
        var v = input.value.trim();
        input.remove();
        if (v && v !== node.label) {
          node.label = v;
          changed();
          redraw(node.id);
        }
      }
      input.addEventListener("keydown", function (ev) {
        ev.stopPropagation();
        if (ev.key === "Enter") input.blur();
        if (ev.key === "Escape") { input.value = node.label; input.blur(); }
      });
      input.addEventListener("blur", function () {
        if (input.parentNode) commit();
      });
    }

    /* 增删节点 */
    function addChild(node) {
      var n = { label: "新节点", children: [], parent: node };
      n.id = "n" + (++_idSeq);
      n._dx = 0; n._dy = 0;
      node.children.push(n);
      changed();
      redraw(n.id);
      renameNode(n);
    }

    function addSibling(node) {
      if (!node.parent) { addChild(node); return; }
      var n = { label: "新节点", children: [], parent: node.parent };
      n.id = "n" + (++_idSeq);
      n._dx = 0; n._dy = 0;
      var i = node.parent.children.indexOf(node);
      node.parent.children.splice(i + 1, 0, n);
      changed();
      redraw(n.id);
      renameNode(n);
    }

    function delNode(node) {
      if (!node.parent) return; // 根不可删
      var i = node.parent.children.indexOf(node);
      if (i >= 0) node.parent.children.splice(i, 1);
      var keep = node.parent;
      changed();
      redraw(keep.id);
    }

    /* 工具栏 */
    bar.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-act]");
      if (!btn) return;
      var act = btn.getAttribute("data-act");
      var n = selected || root;
      if (act === "child") addChild(n);
      else if (act === "sibling") addSibling(n);
      else if (act === "rename") renameNode(n);
      else if (act === "del" && selected && selected.parent) delNode(selected);
      else if (act === "max") toggleFullscreen();
      // 注意：不要在此 container.focus()——会抢走改名输入框焦点导致其立即提交消失
    });

    /* 最大化/还原：编辑器铺满视口，便于大图编辑 */
    function toggleFullscreen() {
      var on = container.classList.toggle("mm-fullscreen");
      var btn = bar.querySelector(".mm-max");
      if (btn) {
        btn.textContent = on ? "✕ 退出" : "⛶ 全屏";
        btn.setAttribute("title", on ? "退出最大化" : "最大化脑图进行编辑");
      }
      if (on) {
        // 进入全屏：整幅脑图铺满画布并居中（fit+center），随后可用中键平移/滚轮缩放
        var bb = bbox(root);
        var pad = 8;
        var x0 = bb.x0 - pad, y0 = bb.y0 - pad;
        var x1 = bb.x1 + pad, y1 = bb.y1 + pad;
        var natW = x1 - x0, natH = y1 - y0;
        var c = canvasSize();
        var zoom = Math.min(c.w / Math.max(natW, 1), c.h / Math.max(natH, 1));
        zoom = Math.min(2, Math.max(0.15, zoom));
        cam = { zoom: zoom, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, vpW: c.w, vpH: c.h };
      } else {
        cam = null;
      }
      redraw(selected ? selected.id : null);
    }

    /* ── 中键平移 / 滚轮缩放（仅全屏相机模式生效）── */
    function onPanDown(e) {
      if (e.button !== 1) return;          // 仅中键
      if (!cam) return;
      e.preventDefault();
      panState = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        cx0: cam.cx,
        cy0: cam.cy,
      };
      try { svgHolder.setPointerCapture(e.pointerId); } catch (err) {}
      e.stopPropagation();
    }
    function onPanMove(e) {
      if (!panState || !cam) return;
      var dx = e.clientX - panState.startX, dy = e.clientY - panState.startY;
      var z = cam.zoom || 1;
      cam.cx = panState.cx0 - dx / z;
      cam.cy = panState.cy0 - dy / z;
      requestRender();
    }
    function onPanUp() {
      panState = null;
    }
    function onWheel(e) {
      if (!cam) return;
      e.preventDefault();
      var z = cam.zoom;
      var factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      var nz = Math.min(8, Math.max(0.2, z * factor));
      var sr = svg.getBoundingClientRect();
      var px = e.clientX - sr.left, py = e.clientY - sr.top;
      var zw = sr.width || 800, zh = sr.height || 600;
      // 以鼠标为锚点：缩放前后指针下的布局点保持不动
      var lx = cam.cx + (px - zw / 2) / z;
      var ly = cam.cy + (py - zh / 2) / z;
      cam.zoom = nz;
      cam.cx = lx - (px - zw / 2) / nz;
      cam.cy = ly - (py - zh / 2) / nz;
      requestRender();
    }

    /* 键盘 */
    container.addEventListener("keydown", function (e) {
      if (container.querySelector(".mm-rename")) return;
      if (readonly) return;
      var n = selected || root;
      if (e.key === "Tab") {
        e.preventDefault();
        addChild(n);
      } else if (e.key === "Enter") {
        e.preventDefault();
        addSibling(n);
      } else if ((e.key === "Delete" || e.key === "Backspace") && selected && selected.parent) {
        e.preventDefault();
        delNode(selected);
      } else if (e.key === "F2" && selected) {
        e.preventDefault();
        renameNode(selected);
      }
    });

    /* 空白处点选取消 */
    container.addEventListener("pointerdown", function (e) {
      if (e.target === container || e.target === svgHolder || e.target === svg) {
        select(null);
      }
    });

    var api = {
      _bindNode: bindNode,
      serialize: serialize,
      focus: function () { container.focus(); },
    };

    redraw(null);
    select(root);
    return api;
  }

  var util = {
    setPointerCaptureSafe: function (g, pointerId) {
      try { g.setPointerCapture(pointerId); } catch (err) {}
    },
  };

  window.LNMM = {
    render: render,
    edit: edit,
  };
})();
