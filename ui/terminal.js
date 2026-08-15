(function () {
  var params = new URLSearchParams(location.search);
  var server = params.get("server") || "";
  document.body.classList.toggle("embedded", params.get("embedded") === "1");
  var termId = "";
  var csrf = "";
  var out = document.getElementById("output");
  var input = document.getElementById("input");
  var statusEl = document.getElementById("status");
  var dot = document.getElementById("dot");
  var closed = false;
  var timer = null;

  function post(path, body) {
    body.csrf = csrf;
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) { return response.json(); });
  }

  function append(text) {
    if (!text) return;
    out.textContent += text;
    out.scrollTop = out.scrollHeight;
    // Switch the input to hidden mode while a password prompt is on screen.
    if (/assword|验证码|Verification code/i.test(out.textContent.slice(-120))) {
      input.type = "password";
    } else if (input.type === "password" && /\$\s*$/.test(out.textContent.slice(-60))) {
      input.type = "text";
    }
  }

  function poll() {
    if (closed || !termId) return;
    post("/api/term/output", { term_id: termId }).then(function (response) {
      if (!response || response.ok === false) {
        statusEl.textContent = response && response.error ? response.error : "会话已结束";
        if (response && response.alive === false) {
          dot.classList.remove("live");
          clearInterval(timer);
          closed = true;
          input.disabled = true;
        }
        return;
      }
      append(response.data);
      if (response.alive) {
        dot.classList.add("live");
        statusEl.textContent = "已连接";
      } else {
        dot.classList.remove("live");
        statusEl.textContent = "会话已结束（可关闭本窗口）";
      }
    }).catch(function () { /* transient */ });
  }

  input.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    var data = input.value + "\n";
    input.value = "";
    if (!termId || closed) return;
    post("/api/term/input", { term_id: termId, data: data }).then(function (response) {
      if (response && response.ok === false) {
        statusEl.textContent = response.error || "输入失败";
      }
    });
  });

  fetch("/api/health").then(function (response) {
    return response.json();
  }).then(function (health) {
    csrf = health.csrf || "";
    document.getElementById("serverName").textContent = server || "默认";
    return post("/api/terminal", { name: server });
  }).then(function (response) {
    if (!response || response.ok === false) {
      statusEl.textContent = "打开终端失败: " + (response && response.error ? response.error : "未知错误");
      return;
    }
    termId = response.term_id;
    statusEl.textContent = "会话已建立，正在连接 " + server + "…";
    timer = setInterval(poll, 300);
    input.focus();
  }).catch(function (error) {
    statusEl.textContent = "打开终端失败: " + error.message;
  });

  window.addEventListener("beforeunload", function () {
    if (termId) {
      closed = true;
      try {
        navigator.sendBeacon(
          "/api/term/close",
          new Blob([JSON.stringify({ term_id: termId, csrf: csrf })], { type: "application/json" })
        );
      } catch (error) {
        // Closing is best-effort; the server also reaps finished sessions.
      }
    }
  });
})();
