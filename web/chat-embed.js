/*
 * GMIC 官网聊天启动器(可嵌入组件)—— gmic.ai 用 <script src=".../chat-embed.js" defer> 引入。
 *
 * 做什么:在页面【右下角】放一个品牌蓝的聊天气泡按钮(Headset bot 图标),点开从右下角弹出一个
 *   聊天面板 = <iframe src=".../widget/">(即我们托管的纯文字聊天 widget)。再点/按 Esc/点 ✕ 关闭。
 *
 * 为什么用 iframe(不像语音那样把 UI 跑在宿主页):聊天没有麦克风、不需要 shadow-DOM 气泡尖角,
 *   iframe 内 origin = 本后端域,widget 的 /config、/chat 都同源、免 CORS;且聊天 UI 独立可随时更新
 *   (改 widget 无需动这文件、更无需重发 WP)。启动器外壳很薄、很少变。
 *
 * 隔离:整个启动器挂在一个 Shadow DOM 里,宿主页(WordPress/Meng)的 CSS 进不来、也污染不到官网。
 * 位置:固定右下角,和底部居中的 fab 工具栏(语音麦克风在那)错开,各司其职。
 * 单一代码源:这文件在 website-bot 仓库;改启动器只动它 + EC2 git pull(静态文件,免重启),不重发 WP。
 */
(function () {
  "use strict";
  if (window.__gmicChatLoaded) return;            // 防重复注入(脚本被引两次也只建一个)
  window.__gmicChatLoaded = true;

  // API base = 本脚本的来源域(https://web-bot.telalive.us),widget iframe 从这里加载。
  var API = "";
  try { API = new URL(document.currentScript.src).origin; } catch (e) { API = "https://web-bot.telalive.us"; }
  var WIDGET_URL = API + "/widget/";

  var ACCENT = "#2563eb", ACCENT_DEEP = "#1e40af";

  // 选定的图标:Headset bot(戴耳机的机器人头;和 fab 的麦克风区分开,偏"助手/找人聊")。
  var BOT_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M5 11a7 7 0 0 1 14 0"/>' +
    '<rect x="3.4" y="10.6" width="2.6" height="4.4" rx="1.3"/>' +
    '<rect x="18" y="10.6" width="2.6" height="4.4" rx="1.3"/>' +
    '<rect x="6.4" y="9" width="11.2" height="9.4" rx="3"/>' +
    '<circle cx="10" cy="12.6" r="1.05" fill="currentColor" stroke="none"/>' +
    '<circle cx="14" cy="12.6" r="1.05" fill="currentColor" stroke="none"/>' +
    '<path d="M10.2 15.4q1.8 1.2 3.6 0"/></svg>';

  // 最小化图标(一根横杠,经典"最小化")——收起面板回气泡,对话【保留】,方便暂时看网站。
  var MIN_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
    'stroke-linecap="round" aria-hidden="true"><path d="M6 12h12"/></svg>';
  // 关闭图标(X)——彻底删除本次对话,下次打开是全新会话。
  var X_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
    'stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  // 宿主锚点 + Shadow DOM(样式与宿主页彻底隔离)
  var host = document.createElement("div");
  host.id = "gmic-chat-embed";
  host.style.cssText = "position:fixed;right:0;bottom:0;z-index:2147483000;";
  (document.body || document.documentElement).appendChild(host);
  var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

  // 宿主页的"返回顶部"键(#backTop,Meng 的)本来也在右下角,会被聊天气泡盖住。往上顶一档,
  // 让它叠在聊天气泡【上方】(两个都靠右、上下错开)。注入到宿主页 head(不在 shadow 里才能作用到它)。
  try {
    var hs = document.createElement("style");
    hs.textContent = "#backTop{bottom:90px !important;}" +
      "@media (max-width:480px){#backTop{bottom:84px !important;}}";
    (document.head || document.documentElement).appendChild(hs);
  } catch (e) {}

  var style = document.createElement("style");
  style.textContent =
    ":host,*{box-sizing:border-box;}" +
    // 启动按钮
    ".launch{position:fixed;right:20px;bottom:20px;width:58px;height:58px;border-radius:50%;" +
    "background:" + ACCENT + ";color:#fff;border:none;cursor:pointer;display:grid;place-items:center;" +
    "box-shadow:0 10px 28px rgba(37,99,235,.42);transition:transform .16s,background .16s;}" +
    ".launch:hover{background:" + ACCENT_DEEP + ";transform:translateY(-2px);}" +
    ".launch:focus-visible{outline:3px solid rgba(37,99,235,.5);outline-offset:2px;}" +
    ".launch svg{width:30px;height:30px;display:block;}" +
    ".launch .x{display:none;} .launch.open .bot{display:none;} .launch.open .x{display:block;}" +
    // 聊天面板(桌面:右下角浮窗)
    ".panel{position:fixed;right:20px;bottom:90px;width:384px;height:600px;max-height:calc(100vh - 110px);" +
    "background:#fff;border-radius:18px;overflow:hidden;box-shadow:0 18px 60px rgba(20,22,30,.28);" +
    "opacity:0;transform:translateY(12px) scale(.98);transform-origin:bottom right;pointer-events:none;" +
    "transition:opacity .18s,transform .18s;}" +
    ".panel.open{opacity:1;transform:none;pointer-events:auto;}" +
    ".panel iframe{width:100%;height:100%;border:0;display:block;}" +
    // 面板右上角一排两个控制键:最小化(横杠)+ 关闭(X)。浮在 widget 头部右侧空白处。
    ".ctrls{position:absolute;top:10px;right:10px;display:flex;gap:6px;}" +
    ".ctrl{width:28px;height:28px;border-radius:50%;border:none;cursor:pointer;" +
    "background:rgba(255,255,255,.85);color:#1f2430;display:grid;place-items:center;" +
    "box-shadow:0 2px 8px rgba(0,0,0,.15);}" +
    ".ctrl:hover{background:#fff;} .ctrl svg{width:15px;height:15px;}" +
    ".ctrl.close:hover{background:#fee2e2;color:#b91c1c;}" +   // X hover 变红,暗示"删除"
    // 手机:面板做成【底部抽屉】(约 72vh,不铺满全屏——顶部留出页面可见,不压迫)
    "@media (max-width:480px){" +
    ".panel{right:8px;left:8px;bottom:82px;top:auto;width:auto;height:72vh;max-height:calc(100vh - 104px);}" +
    ".launch{right:16px;bottom:16px;}}" +
    "@media (prefers-reduced-motion:reduce){.launch,.panel{transition:none;}}";
  root.appendChild(style);

  // 启动按钮
  var launch = document.createElement("button");
  launch.type = "button";
  launch.className = "launch";
  launch.setAttribute("aria-label", "Chat with us");
  launch.innerHTML = '<span class="bot">' + BOT_SVG + '</span><span class="x">' + MIN_SVG + '</span>';
  root.appendChild(launch);

  // 聊天面板(iframe 懒加载:首次打开才设 src,不拖慢首屏)
  var panel = document.createElement("div");
  panel.className = "panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "GMIC AI chat");
  var ctrls = document.createElement("div"); ctrls.className = "ctrls";
  var minBtn = document.createElement("button");     // 最小化:收起、对话保留
  minBtn.type = "button"; minBtn.className = "ctrl min";
  minBtn.setAttribute("aria-label", "Minimize chat"); minBtn.setAttribute("title", "Minimize (keep chat)");
  minBtn.innerHTML = MIN_SVG;
  var closeBtn = document.createElement("button");   // 关闭:删除对话,下次全新
  closeBtn.type = "button"; closeBtn.className = "ctrl close";
  closeBtn.setAttribute("aria-label", "Close and clear chat"); closeBtn.setAttribute("title", "Close (clear chat)");
  closeBtn.innerHTML = X_SVG;
  ctrls.appendChild(minBtn); ctrls.appendChild(closeBtn);
  var frame = document.createElement("iframe");
  frame.title = "GMIC AI chat";
  frame.setAttribute("loading", "lazy");
  frame.allow = "clipboard-write";
  panel.appendChild(frame);
  panel.appendChild(ctrls);
  root.appendChild(panel);

  var open = false, loaded = false;
  function setOpen(v) {
    open = v;
    if (v && !loaded) { frame.src = WIDGET_URL; loaded = true; }   // 懒加载
    launch.classList.toggle("open", v);
    panel.classList.toggle("open", v);
    launch.setAttribute("aria-label", v ? "Minimize chat" : "Chat with us");
  }
  // 删除对话:收起面板 + 把 iframe 重载成【全新会话】(?new= → widget 忽略旧 session_id,开一段新的)。
  function deleteChat() {
    setOpen(false);
    frame.src = WIDGET_URL + "?new=" + Date.now();   // widget 见到 ?new 就起新会话(见 index.html session 初始化)
    loaded = true;
  }
  launch.addEventListener("click", function () { setOpen(!open); });   // 气泡:开/最小化(对话保留)
  minBtn.addEventListener("click", function () { setOpen(false); });   // 最小化:收起,对话保留
  closeBtn.addEventListener("click", deleteChat);                      // X:删除对话,下次全新
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && open) setOpen(false); });
})();
