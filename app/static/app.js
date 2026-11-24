/* BookSmith 前端：SSE 实时进度、确认对话框、表单小交互。无依赖。 */
(function () {
  "use strict";

  var STATUS_LABELS = {
    queued: "排队中", running: "进行中", awaiting_outline: "待确认大纲",
    awaiting_review: "待审核", merged: "已合并", rejected: "已打回",
    failed: "失败", cancelled: "已取消"
  };
  var ACTIVE = { queued: true, running: true };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // ---------- 确认对话框 + 防重复提交 ----------
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    var input = form.querySelector('input[type=file][data-max-bytes]');
    if (input && input.files) {
      var total = 0, max = parseInt(input.getAttribute("data-max-bytes"), 10);
      for (var i = 0; i < input.files.length; i++) total += input.files[i].size;
      if (total > max) {
        e.preventDefault();
        window.alert("附件总大小超过 " + Math.round(max / 1048576) + " MB，请压缩或分开提交。");
        return;
      }
    }
    var btns = $all("button[type=submit]", form);
    setTimeout(function () { btns.forEach(function (b) { b.disabled = true; }); }, 0);
  });

  // ---------- 筛选下拉自动提交 ----------
  $all("select[data-autosubmit]").forEach(function (s) {
    s.addEventListener("change", function () { s.form.submit(); });
  });

  // ---------- 新建任务：按能力显示参数 ----------
  var kindForm = $("[data-kind-form]");
  if (kindForm) {
    kindForm.classList.add("js");
    var sync = function () {
      var checked = $("input[name=kind]:checked", kindForm);
      var kind = checked ? checked.value : "";
      $all("[data-for-kind]", kindForm).forEach(function (el) {
        var kinds = el.getAttribute("data-for-kind").split(" ");
        el.classList.toggle("hidden", kinds.indexOf(kind) < 0);
      });
      $all("[data-hint-kind]", kindForm).forEach(function (el) {
        var kinds = el.getAttribute("data-hint-kind").split(" ");
        el.style.display = kinds.indexOf(kind) < 0 ? "none" : "";
      });
    };
    $all("input[name=kind]", kindForm).forEach(function (r) { r.addEventListener("change", sync); });
    sync();
  }

  // ---------- 复制邀请码 ----------
  $all("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var text = b.getAttribute("data-copy");
      var done = function () { b.textContent = "已复制"; setTimeout(function () { b.textContent = "复制"; }, 1500); };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () { window.prompt("复制邀请码：", text); });
      } else {
        window.prompt("复制邀请码：", text);
      }
    });
  });

  // ---------- 多个 PDF 切换 ----------
  var tabs = $("[data-pdf-tabs]");
  if (tabs) {
    var frame = $("[data-pdf-frame]"), openLink = $("[data-pdf-open]");
    $all("a[data-pdf-src]", tabs).forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        $all("a", tabs).forEach(function (x) { x.classList.remove("on"); });
        a.classList.add("on");
        frame.src = a.getAttribute("data-pdf-src");
        if (openLink) openLink.href = a.getAttribute("data-pdf-src");
      });
    });
  }

  // ---------- 任务详情：SSE 实时进度 ----------
  var live = $("[data-task-live]");
  if (live && window.EventSource) {
    var initialStatus = live.getAttribute("data-status");
    var lastId = parseInt(live.getAttribute("data-last-id") || "0", 10);
    var sseUrl = live.getAttribute("data-sse-url");
    var list = $("[data-events]"), stepEl = $("[data-current-step]");
    var stateEl = $("[data-live-state]"), spinner = $("[data-spinner]");
    var badge = $("[data-status-badge]", live), qpos = $("[data-queue-pos]");
    var es = null, retryDelay = 2000, finished = false;

    var setState = function (text) { if (stateEl) stateEl.textContent = text; };

    var addEvent = function (ev) {
      if (ev.id <= lastId) return;
      lastId = ev.id;
      var li = document.createElement("li");
      li.className = "ev ev-" + (ev.level || "info");
      var t = document.createElement("time"); t.textContent = (ev.ts || "").slice(11, 19);
      var b = document.createElement("b"); b.textContent = ev.step || "";
      var s = document.createElement("span"); s.textContent = ev.message || "";
      li.appendChild(t); li.appendChild(b); li.appendChild(s);
      list.appendChild(li);
      list.scrollTop = list.scrollHeight;
      if (stepEl) stepEl.textContent = (ev.step ? ev.step + "：" : "") + (ev.message || "");
    };

    var setStatus = function (info) {
      if (badge) {
        badge.textContent = info.label || STATUS_LABELS[info.status] || info.status;
        badge.className = "badge badge-" + info.status;
      }
      if (qpos) qpos.textContent = info.status === "queued" && info.queue_pos ? "排队第 " + info.queue_pos + " 位" : "";
      if (spinner) spinner.classList.toggle("on", !!ACTIVE[info.status]);
    };

    var connect = function () {
      if (finished) return;
      es = new EventSource(sseUrl + "?after=" + lastId);
      es.addEventListener("open", function () { retryDelay = 2000; setState(ACTIVE[initialStatus] ? "实时更新中…" : ""); });
      es.addEventListener("log", function (m) { try { addEvent(JSON.parse(m.data)); } catch (e) {} });
      es.addEventListener("status", function (m) {
        try { setStatus(JSON.parse(m.data)); } catch (e) {}
      });
      es.addEventListener("done", function (m) {
        finished = true;
        es.close();
        var st = initialStatus;
        try { st = JSON.parse(m.data).status; } catch (e) {}
        setState("");
        if (spinner) spinner.classList.remove("on");
        if (st !== initialStatus) {
          setState("任务状态已更新，正在刷新页面…");
          setTimeout(function () { window.location.reload(); }, 600);
        }
      });
      es.addEventListener("error", function () {
        if (finished) return;
        // 浏览器会按 retry 自动重连；若连接已彻底关闭（如 401/网络切换），手动退避重连
        if (es.readyState === EventSource.CLOSED) {
          setState("连接中断，" + Math.round(retryDelay / 1000) + " 秒后重连…");
          setTimeout(connect, retryDelay);
          retryDelay = Math.min(retryDelay * 2, 30000);
        } else {
          setState("连接中断，正在重连…");
        }
      });
    };
    // 已结束的任务也连一次：拿到 done 就关闭，不会反复重连
    connect();
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !finished && es && es.readyState === EventSource.CLOSED) connect();
    });
  }
})();
