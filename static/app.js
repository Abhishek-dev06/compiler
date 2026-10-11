/* global require, monaco */
const languageSelect = document.getElementById("language");
const runButton = document.getElementById("run-button");
const stdin = document.getElementById("stdin");
const output = document.getElementById("output");
const outputTitle = document.getElementById("output-title");
const statusBadge = document.getElementById("status-badge");
const metrics = document.getElementById("metrics");
const historyList = document.getElementById("history-list");
const historyEmpty = document.getElementById("history-empty");
const historyCount = document.getElementById("history-count");
const historyPanel = document.getElementById("history-panel");
const toast = document.getElementById("toast");
const javaFileNote = document.getElementById("java-file-note");

const starterCode = {
  python: 'print("Hello, World!")\n',
  c: '#include <stdio.h>\n\nint main(void) {\n    printf("Hello, World!\\n");\n    return 0;\n}\n',
  cpp: '#include <iostream>\n\nint main() {\n    std::cout << "Hello, World!\\n";\n    return 0;\n}\n',
  java: 'public class Main {\n    public static void main(String[] args) {\n        System.out.println("Hello, World!");\n    }\n}\n'
};
let editor;
let activeLanguage = languageSelect.value;
let toastTimer;

function notify(message) { clearTimeout(toastTimer); toast.textContent = message; toast.classList.add("show"); toastTimer = setTimeout(() => toast.classList.remove("show"), 2800); }
function setStatus(label, kind = "neutral") { statusBadge.textContent = label; statusBadge.className = `status ${kind}`; }
function outputFrom(run) { return run.compile_output || run.stderr || run.stdout || run.message || "No output returned by Judge0."; }
function successful(status) { return status === "Accepted"; }
function statusKind(status) { if (successful(status)) return "success"; if (status === "API Error") return "error"; return "error"; }
function formatDate(value) { try { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)); } catch { return value; } }
function updateJavaFileNote() { javaFileNote.hidden = languageSelect.value !== "java"; }

function showResult(run) {
  outputTitle.textContent = run.status === "Compilation Error" ? "Compiler output" : "Output";
  output.textContent = outputFrom(run);
  setStatus(run.status || "Unknown", statusKind(run.status));
  const items = [];
  if (run.execution_time !== null && run.execution_time !== undefined) items.push(`time ${run.execution_time}s`);
  if (run.memory !== null && run.memory !== undefined) items.push(`memory ${run.memory} KB`);
  metrics.innerHTML = items.map(item => `<span class="metric"></span>`).join("");
  [...metrics.children].forEach((element, index) => { element.textContent = items[index]; });
  metrics.hidden = !items.length;
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function historyItem(run) {
  const article = document.createElement("article");
  article.className = "history-item";
  article.tabIndex = 0;
  article.title = "Restore this execution";
  article.innerHTML = `<div class="history-meta"><strong>${run.language_name}</strong><button class="delete-run" aria-label="Delete this execution" title="Delete">×</button></div><div class="history-status"></div><div class="history-time"></div>`;
  article.querySelector(".history-status").textContent = run.status;
  article.querySelector(".history-time").textContent = formatDate(run.created_at);
  const restore = async () => {
    try {
      const item = await request(`/api/history/${run.id}`);
      activeLanguage = item.language;
      languageSelect.value = item.language;
      editor.setValue(item.source_code);
      monaco.editor.setModelLanguage(editor.getModel(), window.CODERUN_LANGUAGES[item.language].monaco);
      stdin.value = item.stdin || "";
      showResult(item);
      historyPanel.classList.remove("open");
      notify("Execution restored.");
    } catch (error) { notify(error.message); }
  };
  article.addEventListener("click", event => { if (!event.target.closest(".delete-run")) restore(); });
  article.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); restore(); } });
  article.querySelector(".delete-run").addEventListener("click", async event => {
    event.stopPropagation();
    try { await request(`/api/history/${run.id}`, { method: "DELETE" }); await loadHistory(); notify("Execution deleted."); } catch (error) { notify(error.message); }
  });
  return article;
}

async function loadHistory() {
  try {
    const runs = await request("/api/history");
    historyList.replaceChildren(...runs.map(historyItem));
    historyCount.textContent = runs.length;
    historyEmpty.hidden = runs.length > 0;
  } catch (error) { historyEmpty.textContent = `Could not load history: ${error.message}`; }
}

async function runCode() {
  if (!editor || runButton.disabled) return;
  runButton.disabled = true;
  runButton.innerHTML = "<span>◌</span> Running…";
  setStatus("Submitting", "pending");
  outputTitle.textContent = "Output";
  output.textContent = "Waiting for Judge0…";
  metrics.hidden = true;
  try {
    const run = await request("/api/execute", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source_code: editor.getValue(), language: languageSelect.value, stdin: stdin.value }) });
    showResult(run);
    await loadHistory();
  } catch (error) {
    output.textContent = error.message;
    setStatus("Request Error", "error");
  } finally {
    runButton.disabled = false;
    runButton.innerHTML = "<span>▶</span> Run code";
  }
}

require.config({ paths: { vs: window.MONACO_VS_PATH } });
require(["vs/editor/editor.main"], () => {
  editor = monaco.editor.create(document.getElementById("editor"), {
    value: starterCode[activeLanguage], language: window.CODERUN_LANGUAGES[activeLanguage].monaco,
    theme: "vs-dark", automaticLayout: true, minimap: { enabled: false }, fontSize: 14,
    fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace", padding: { top: 16 }, scrollBeyondLastLine: false
  });
  editor.addAction({ id: "run-code", label: "Run Code", keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter], run: runCode });
  runButton.addEventListener("click", runCode);
  languageSelect.addEventListener("change", () => {
    const previous = activeLanguage; activeLanguage = languageSelect.value;
    monaco.editor.setModelLanguage(editor.getModel(), window.CODERUN_LANGUAGES[activeLanguage].monaco);
    if (editor.getValue() === starterCode[previous]) editor.setValue(starterCode[activeLanguage]);
    updateJavaFileNote();
  });
  updateJavaFileNote();
  loadHistory();
});
document.getElementById("history-toggle").addEventListener("click", () => historyPanel.classList.toggle("open"));
