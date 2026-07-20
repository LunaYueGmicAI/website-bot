/* ============================================================
   GMIC 语音留言 —— 可嵌入版(embed.js)。
   给 gmic.ai 官网用:底部 .fab 工具栏那个 #fabVoice 话筒按钮 = "hold to talk" 按钮。
   长按它 → 从按钮【正上方】弹出一个带【向下小尖角】的小气泡(音波 + 联系方式 + 发送),
   像气泡是从这个按钮长出来的。松手=暂停,再长按=续录,点发送=结束+上传。

   为什么用 embed.js(不是 iframe):要让气泡真正"从 fab 按钮弹出、尖角连着它",
   录音 UI 必须跑在宿主页面里、锚定到那个按钮。iframe 里有它自己的话筒、尖角也连不过来。
   组件用 Shadow DOM 隔离,绝不污染 gmic.ai 的全局 CSS。

   数据:跨域 POST 到本脚本所在后端(web-bot.telalive.us)的 /voice/message。
   后端 CORS 已放行 gmic.ai(ALLOWED_ORIGINS)。逻辑与 /voice-widget/ 独立页一致
   (录音/音波/联系方式 pill/体积兜底/发送),只是把"话筒按钮"换成宿主页的 #fabVoice。
   ============================================================ */
(function () {
  "use strict";
  // 本脚本所在后端 origin = 数据要发去的地方(web-bot.telalive.us)。
  var API = "https://web-bot.telalive.us";
  try { API = new URL(document.currentScript.src).origin; } catch (e) {}

  var TRIGGER_ID = "fabVoice";          // 宿主页 fab 工具栏里的话筒按钮 id
  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  var SIZE_BUDGET = 7500000;            // 体积兜底:后端硬上限 8MB,留余量(见独立页同款注释)

  var CT_META = {
    email:    { ph: "you@example.com",     mode: "email", bad: "That doesn't look like a valid email." },
    phone:    { ph: "+1 650 555 0100",     mode: "tel",   bad: "Please enter a valid phone number." },
    whatsapp: { ph: "WhatsApp number",     mode: "tel",   bad: "Please enter your WhatsApp number." },
    wechat:   { ph: "WeChat ID",           mode: "text",  bad: "Please enter your WeChat ID." },
    telegram: { ph: "@username or number", mode: "text",  bad: "Please enter your Telegram handle." }
  };

  var CSS = "\
:host{all:initial;} \
*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;} \
.wrap{--accent:#2563eb;--accent-ink:#fff;--bg:#fff;--panel:#f5f6f9;--text:#1f2430;--muted:#7a8194;--border:#e3e6ef;--danger:#e5484d;} \
@media (prefers-color-scheme:dark){.wrap{--bg:#14161c;--panel:#1b1e26;--text:#eceef4;--muted:#9aa2b4;--border:#2a2f3b;}} \
.scrim{position:fixed;inset:0;z-index:2147483000;display:none;background:rgba(15,17,24,.14);} \
.scrim.show{display:block;} \
.pop{position:fixed;z-index:2147483001;width:296px;max-width:calc(100vw - 16px);background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:16px;box-shadow:0 14px 44px rgba(0,0,0,.24);padding:10px 13px 12px;display:none;} \
.pop.show{display:block;animation:gvpop .16s ease-out;} \
@keyframes gvpop{from{transform:translateY(6px) scale(.97);opacity:.4;}to{transform:none;opacity:1;}} \
.tail{position:absolute;top:100%;width:14px;height:14px;margin-top:-8px;margin-left:-7px;background:var(--bg);border-right:1px solid var(--border);border-bottom:1px solid var(--border);transform:rotate(45deg);} \
.hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:2px;} \
.hd .t{font-size:13px;font-weight:650;color:var(--text);} \
.x{border:none;background:none;color:var(--muted);font-size:16px;cursor:pointer;line-height:1;} \
canvas.wave{display:block;width:70%;height:40px;margin:2px auto 1px;} \
.status{text-align:center;font-size:11.5px;color:var(--muted);min-height:15px;margin-bottom:8px;} \
.status.rec{color:var(--danger);font-weight:600;} \
.status.bad{color:var(--danger);} \
.em-lbl{font-size:11.5px;color:var(--muted);margin:4px 0 5px;} \
.em-lbl b{color:var(--danger);} \
.ct-pills{display:flex;flex-wrap:nowrap;gap:4px;margin-bottom:7px;} \
.ct-pill{flex:1 1 0;text-align:center;font-size:10px;padding:4px 5px;border-radius:999px;border:1px solid var(--border);background:var(--panel);color:var(--muted);cursor:pointer;user-select:none;line-height:1.25;white-space:nowrap;} \
.ct-pill .e{font-size:9px;margin-right:1px;} \
.ct-pill.on{background:var(--accent);color:#fff;border-color:var(--accent);} \
.em-row{display:flex;gap:8px;align-items:stretch;} \
.contact{flex:1;min-width:0;padding:10px 12px;font-size:14px;border:1px solid var(--border);border-radius:11px;background:var(--panel);color:var(--text);outline:none;} \
.contact:focus{border-color:var(--accent);} \
.send{flex:0 0 auto;padding:10px 16px;border:none;border-radius:11px;background:var(--accent);color:var(--accent-ink);font-size:14px;font-weight:650;cursor:pointer;} \
.send:disabled{opacity:.4;cursor:default;} \
.em-hint{font-size:11.5px;min-height:14px;margin-top:4px;color:var(--muted);} \
.em-hint.bad{color:var(--danger);} \
.done{text-align:center;padding:16px 6px;} \
.done .big{font-size:30px;} .done .m2{margin-top:7px;font-size:13px;color:var(--text);}";

  var HTML = '\
<div class="wrap"> \
  <div class="scrim" id="scrim"></div> \
  <div class="pop" id="pop"> \
    <div id="body"> \
      <div class="hd"><span class="t">🎙️ Voice message</span><button class="x" id="close" aria-label="Close">✕</button></div> \
      <div id="formArea"> \
        <canvas class="wave" id="wave"></canvas> \
        <div class="status" id="status">Hold the mic and speak</div> \
        <div class="em-lbl">How can we reach you? <b>*</b></div> \
        <div class="ct-pills" id="ctPills"> \
          <button type="button" class="ct-pill on" data-ct="email"><span class="e">📧</span>Email</button> \
          <button type="button" class="ct-pill" data-ct="phone"><span class="e">📞</span>Phone</button> \
          <button type="button" class="ct-pill" data-ct="whatsapp"><span class="e">💬</span>WhatsApp</button> \
          <button type="button" class="ct-pill" data-ct="wechat"><span class="e">💚</span>WeChat</button> \
          <button type="button" class="ct-pill" data-ct="telegram"><span class="e">✈️</span>Telegram</button> \
        </div> \
        <div class="em-row"> \
          <input class="contact" id="contact" type="text" inputmode="email" autocomplete="off" placeholder="you@example.com" /> \
          <button class="send" id="send" disabled>Send</button> \
        </div> \
        <div class="em-hint" id="emHint"></div> \
      </div> \
      <div id="doneArea" style="display:none"></div> \
    </div> \
    <div class="tail" id="tail"></div> \
  </div> \
</div>';

  function init() {
    var trigger = document.getElementById(TRIGGER_ID);
    if (!trigger) return;                         // 这页没有 fab 话筒按钮 → 不装

    // 宿主页轻量样式:话筒按钮长按不触发滚动/选中 + 录音时变红。
    var lite = document.createElement("style");
    lite.textContent = "#" + TRIGGER_ID + "{touch-action:none;-webkit-user-select:none;user-select:none;}"
      + "#" + TRIGGER_ID + ".gmic-voice-rec{color:#e5484d !important;}";
    document.head.appendChild(lite);

    var host = document.createElement("div");
    host.id = "gmic-voice-embed";
    document.body.appendChild(host);
    var root = host.attachShadow({ mode: "open" });
    root.innerHTML = "<style>" + CSS + "</style>" + HTML;

    var $ = function (id) { return root.getElementById(id); };
    var scrim = $("scrim"), pop = $("pop"), tail = $("tail"),
        formArea = $("formArea"), doneArea = $("doneArea"),
        waveEl = $("wave"), statusEl = $("status"), contactEl = $("contact"),
        emHint = $("emHint"), sendBtn = $("send"), ctPills = $("ctPills");

    var ctype = "email";
    var open = false, recState = "idle", holding = false;
    var recorder = null, stream = null, chunks = [], mime = "audio/webm", audioBlob = null;
    var audioCtx = null, analyser = null, waveOn = false, raf = 0;
    var segStart = 0, elapsedMs = 0, recordedBytes = 0;

    // ---------- 气泡定位:锚到 #fabVoice 正上方,尖角对准按钮中心 ----------
    function positionPop() {
      var r = trigger.getBoundingClientRect();
      var vw = window.innerWidth;
      var popW = Math.min(296, vw - 16);
      pop.style.width = popW + "px";
      var centerX = r.left + r.width / 2;
      var left = Math.max(8, Math.min(centerX - popW / 2, vw - popW - 8));
      pop.style.left = left + "px";
      pop.style.bottom = (window.innerHeight - r.top + 12) + "px";  // 按钮上方留 12px
      var tailX = Math.max(14, Math.min(centerX - left, popW - 14));
      tail.style.left = tailX + "px";
    }
    window.addEventListener("resize", function () { if (open) positionPop(); });

    // ---------- 开/关气泡 ----------
    function openPop() {
      if (open) return;
      open = true; audioBlob = null; recState = "idle"; elapsedMs = 0; recordedBytes = 0; chunks = [];
      // 恢复表单区(上次发送成功后 showDone 把它藏起来了),不销毁元素(引用保持有效)
      formArea.style.display = ""; doneArea.style.display = "none";
      contactEl.value = ""; emHint.textContent = ""; emHint.className = "em-hint";
      selectType("email");
      setStatus("Hold the mic and speak", "");
      positionPop();
      pop.classList.add("show"); scrim.classList.add("show");
      startWaveLoop(); updateSend();
    }
    function closePop() {
      holding = false;
      try { if (recorder && recState !== "idle") recorder.stop(); } catch (e) {}
      teardownStream(); stopWaveLoop();
      recState = "idle"; open = false;
      pop.classList.remove("show"); scrim.classList.remove("show");
      trigger.classList.remove("gmic-voice-rec");
    }
    $("close").addEventListener("click", closePop);
    scrim.addEventListener("click", closePop);

    // ---------- 联系方式:类型选择 + 校验 ----------
    function selectType(t) {
      ctype = t;
      var meta = CT_META[t] || CT_META.email;
      Array.prototype.forEach.call(ctPills.children, function (b) { b.classList.toggle("on", b.dataset.ct === t); });
      contactEl.placeholder = meta.ph;
      contactEl.setAttribute("inputmode", meta.mode);
      emHint.textContent = ""; emHint.className = "em-hint";
      updateSend();
    }
    ctPills.addEventListener("click", function (e) {
      var btn = e.target.closest(".ct-pill");
      if (btn) selectType(btn.dataset.ct);
    });
    function contactValid() {
      var v = contactEl.value.trim();
      if (!v) return false;
      if (ctype === "email") return EMAIL_RE.test(v);
      if (ctype === "phone" || ctype === "whatsapp") {
        var digits = (v.match(/\d/g) || []).length;
        return digits >= 7 && digits <= 15;
      }
      return v.length >= 2;
    }
    function recorded() { return elapsedMs >= 500; }
    function updateSend() { sendBtn.disabled = !(contactValid() && recorded() && recState !== "recording"); }
    contactEl.addEventListener("input", function () {
      if (!contactEl.value.trim() || contactValid()) { emHint.textContent = ""; emHint.className = "em-hint"; }
      else { emHint.textContent = (CT_META[ctype] || CT_META.email).bad; emHint.className = "em-hint bad"; }
      updateSend();
    });

    // ---------- 录音:长按 #fabVoice(录/续录),松手暂停 ----------
    trigger.addEventListener("pointerdown", function (e) { e.preventDefault(); if (!open) openPop(); holdStart(); });
    window.addEventListener("pointerup", function () { holdEnd(); });
    window.addEventListener("pointercancel", function () { holdEnd(); });

    function ensureStream() {
      return new Promise(function (resolve) {
        if (stream) return resolve(true);
        if (!navigator.mediaDevices || !window.MediaRecorder) { setStatus("Voice not supported in this browser.", "bad"); return resolve(false); }
        navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
          stream = s;
          try {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            var src = audioCtx.createMediaStreamSource(stream);
            analyser = audioCtx.createAnalyser(); analyser.fftSize = 128; src.connect(analyser);
          } catch (e) {}
          resolve(true);
        }).catch(function () { setStatus("Please allow microphone access.", "bad"); resolve(false); });
      });
    }

    function holdStart() {
      if (recState === "recording") return;
      holding = true;
      ensureStream().then(function (ok) {
        if (!ok) { holding = false; return; }
        if (!holding) return;                 // 授权弹窗期间已松手 → 等下次长按
        if (recState === "idle") {
          chunks = []; recordedBytes = 0;
          recorder = new MediaRecorder(stream); mime = recorder.mimeType || "audio/webm";
          recorder.ondataavailable = function (e) {
            if (e.data && e.data.size) { chunks.push(e.data); recordedBytes += e.data.size; }
            if (recordedBytes >= SIZE_BUDGET && recState === "recording") { holding = false; holdEnd(true); }
          };
          recorder.onstop = onStopped;
          recorder.start(1000);
        } else if (recState === "paused") {
          try { recorder.resume(); } catch (e) {}
        }
        recState = "recording"; segStart = Date.now();
        trigger.classList.add("gmic-voice-rec");
        tick(); updateSend();
      });
    }
    function holdEnd(maxedOut) {
      holding = false;
      if (recState !== "recording") return;
      try { recorder.pause(); } catch (e) {}
      elapsedMs += Date.now() - segStart;
      recState = "paused";
      trigger.classList.remove("gmic-voice-rec");
      if (maxedOut) setStatus("Reached max length (" + fmt(elapsedMs) + ") · tap Send", "");
      else if (recorded()) setStatus("Paused " + fmt(elapsedMs) + " · hold to add, or Send", "");
      else setStatus("Too short — hold the mic and speak.", "bad");
      updateSend();
    }

    function fmt(ms) { var s = Math.floor(ms / 1000); return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); }
    function setStatus(t, cls) { statusEl.textContent = t; statusEl.className = "status" + (cls ? " " + cls : ""); }
    function tick() {
      if (recState !== "recording") return;
      setStatus("● Recording " + fmt(elapsedMs + (Date.now() - segStart)), "rec");
      setTimeout(tick, 250);
    }

    // ---------- 音条 ----------
    function startWaveLoop() { if (waveOn) return; waveOn = true; drawWave(); }
    function stopWaveLoop() { waveOn = false; if (raf) { cancelAnimationFrame(raf); raf = 0; } }
    function drawWave() {
      if (!waveOn) return;
      var cv = waveEl, ctx = cv.getContext("2d"), dpr = window.devicePixelRatio || 1;
      if (cv.width !== Math.round(cv.clientWidth * dpr)) { cv.width = Math.round(cv.clientWidth * dpr); cv.height = Math.round(cv.clientHeight * dpr); }
      var W = cv.width, H = cv.height, mid = H / 2;
      ctx.clearRect(0, 0, W, H);
      var bars = 32, gap = W / bars, bw = Math.max(2 * dpr, gap * 0.42), minH = 3 * dpr;
      var data = null;
      if (recState === "recording" && analyser) { data = new Uint8Array(analyser.frequencyBinCount); analyser.getByteFrequencyData(data); }
      ctx.fillStyle = "#2563eb";
      for (var i = 0; i < bars; i++) {
        var h = minH;
        if (data) { var v = data[Math.floor(i * data.length / bars)] / 255; h = Math.max(minH, v * (H - 4 * dpr)); }
        var x = i * gap + (gap - bw) / 2, r = bw / 2;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(x, mid - h / 2, bw, h, r); else ctx.rect(x, mid - h / 2, bw, h);
        ctx.fill();
      }
      raf = requestAnimationFrame(drawWave);
    }
    function teardownStream() {
      if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
      analyser = null;
      if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    }

    // ---------- 发送 ----------
    var pendingSend = false;
    function onStopped() { audioBlob = new Blob(chunks, { type: mime }); if (pendingSend) { pendingSend = false; doUpload(); } }
    sendBtn.addEventListener("click", function () {
      if (sendBtn.disabled) return;
      if (!contactValid()) { emHint.textContent = (CT_META[ctype] || CT_META.email).bad; emHint.className = "em-hint bad"; return; }
      if (!recorded()) { setStatus("Record a message first.", "bad"); return; }
      sendBtn.disabled = true; sendBtn.textContent = "Sending…";
      pendingSend = true;
      try { recorder.stop(); } catch (e) { pendingSend = false; doUpload(); }
      recState = "idle";
    });
    function doUpload() {
      var sid = "voice_" + (crypto.randomUUID ? crypto.randomUUID() : (Date.now() + "_" + Math.floor(Math.random() * 1e6)));
      var fd = new FormData();
      fd.append("session_id", sid);
      fd.append("contact_type", ctype);
      fd.append("contact_value", contactEl.value.trim());
      fd.append("audio", audioBlob, "voice." + extFor(mime));
      fd.append("page_url", location.href);
      fetch(API + "/voice/message", { method: "POST", body: fd }).then(function (res) {
        if (!res.ok) throw new Error("/voice/message -> " + res.status);
        showDone(contactEl.value.trim());
      }).catch(function () {
        sendBtn.disabled = false; sendBtn.textContent = "Send";
        setStatus("Couldn't send — please try again.", "bad");
      });
    }
    function extFor(m) {
      if (!m) return "webm";
      if (m.indexOf("mp4") >= 0 || m.indexOf("m4a") >= 0) return "m4a";
      if (m.indexOf("ogg") >= 0) return "ogg";
      if (m.indexOf("wav") >= 0) return "wav";
      return "webm";
    }
    function showDone(who) {
      teardownStream(); stopWaveLoop();
      trigger.classList.remove("gmic-voice-rec");
      // 藏表单、显示"已发送"(不销毁表单 → 下次开还能用,元素引用保持有效)
      formArea.style.display = "none";
      doneArea.innerHTML = '<div class="done"><div class="big">✅</div><div class="m2">Voice message sent!<br>We\'ll reply at <b></b>.</div></div>';
      doneArea.querySelector("b").textContent = who;
      doneArea.style.display = "";
      setTimeout(closePop, 2500);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
