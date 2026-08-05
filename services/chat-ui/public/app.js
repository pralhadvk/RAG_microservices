const messagesEl = document.getElementById("messages");
const form = document.getElementById("chatForm");
const promptEl = document.getElementById("prompt");
const sendBtn = document.getElementById("sendBtn");
const clearBtn = document.getElementById("clearBtn");
const statusEl = document.getElementById("status");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");

const state = {
  busy: false,
};

function showEmpty() {
  messagesEl.innerHTML = `
    <div class="empty">
      <h1>Ask the cluster</h1>
      <p>Queries flow through api-gateway → orchestrator → retrieval → llm-gateway. Citations appear under each answer.</p>
    </div>
  `;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function appendMessage(role, content, extras = {}) {
  const empty = messagesEl.querySelector(".empty");
  if (empty) empty.remove();

  const node = document.createElement("article");
  node.className = `msg ${role}${extras.error ? " error" : ""}`;

  const citationsHtml = (extras.citations || [])
    .map((c) => {
      const source = c.metadata?.source || c.chunk_id;
      const score = typeof c.score === "number" ? c.score.toFixed(3) : "-";
      return `
        <div class="citation">
          <div class="source">${escapeHtml(source)} · score ${escapeHtml(score)}</div>
          <p class="text">${escapeHtml(c.text)}</p>
        </div>
      `;
    })
    .join("");

  const chips = [];
  if (extras.model) chips.push(`<span class="chip">model ${escapeHtml(extras.model)}</span>`);
  if (extras.requestId) chips.push(`<span class="chip">req ${escapeHtml(extras.requestId)}</span>`);
  if (extras.usage?.total_tokens != null) {
    chips.push(`<span class="chip">tokens ${escapeHtml(extras.usage.total_tokens)}</span>`);
  }

  node.innerHTML = `
    <p class="role">${role === "user" ? "You" : "Assistant"}</p>
    <p class="body">${escapeHtml(content)}</p>
    ${chips.length ? `<div class="meta">${chips.join("")}</div>` : ""}
    ${citationsHtml ? `<div class="citations">${citationsHtml}</div>` : ""}
  `;
  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return node;
}

function appendTyping() {
  const node = document.createElement("article");
  node.className = "msg assistant";
  node.id = "typing";
  node.innerHTML = `
    <p class="role">Assistant</p>
    <div class="typing" aria-label="Thinking"><span></span><span></span><span></span></div>
  `;
  messagesEl.appendChild(node);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function removeTyping() {
  document.getElementById("typing")?.remove();
}

function setBusy(busy) {
  state.busy = busy;
  sendBtn.disabled = busy;
  promptEl.disabled = busy;
}

async function checkHealth() {
  try {
    const res = await fetch("/health", { cache: "no-store" });
    if (!res.ok) throw new Error("unhealthy");
    statusEl.classList.add("ok");
    statusEl.classList.remove("bad");
    statusText.textContent = "api online";
  } catch {
    statusEl.classList.remove("ok");
    statusEl.classList.add("bad");
    statusText.textContent = "api offline";
  }
}

async function sendQuery(query) {
  const payload = {
    query,
    collection_id: document.getElementById("collection").value.trim() || "docs-v1",
    top_k: Number(document.getElementById("topK").value) || 5,
    model: document.getElementById("model").value,
    temperature: Number(document.getElementById("temperature").value) || 0.2,
    max_tokens: 1024,
    provider: "openai",
  };

  const requestId = `ui-${crypto.randomUUID().slice(0, 8)}`;
  const res = await fetch("/api/v1/query", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Request-Id": requestId,
    },
    body: JSON.stringify(payload),
  });

  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error(text || `HTTP ${res.status}`);
  }

  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    throw new Error(detail);
  }

  return { ...data, request_id: data.request_id || requestId };
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;

  const query = promptEl.value.trim();
  if (!query) return;

  appendMessage("user", query);
  promptEl.value = "";
  autoResize();
  setBusy(true);
  appendTyping();

  try {
    const data = await sendQuery(query);
    removeTyping();
    appendMessage("assistant", data.answer || "(empty answer)", {
      citations: data.citations || [],
      model: data.model,
      requestId: data.request_id,
      usage: data.usage,
    });
  } catch (err) {
    removeTyping();
    appendMessage("assistant", err.message || "Request failed", { error: true });
  } finally {
    setBusy(false);
    promptEl.focus();
  }
});

clearBtn.addEventListener("click", () => {
  showEmpty();
});

function autoResize() {
  promptEl.style.height = "auto";
  promptEl.style.height = `${Math.min(promptEl.scrollHeight, 128)}px`;
}

promptEl.addEventListener("input", autoResize);
promptEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

showEmpty();
checkHealth();
setInterval(checkHealth, 15000);
