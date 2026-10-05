// Model Context Protocol (MCP) Tools View Controller
// Handles fetching, rendering, schema copying, and test execution for MCP notification tools.

let mcpToolsData = [];
let currentTestingTool = null;

async function loadMcpTools() {
  const container = $("#mcp-tools-grid");
  if (!container) return;

  try {
    container.innerHTML = `
      <div style="grid-column: 1 / -1; padding: 40px; text-align: center; color: var(--muted);">
        <span class="spinner" style="display:inline-block;margin-right:8px;"></span> Loading Model Context Protocol (MCP) tools...
      </div>
    `;

    const res = await fetch("/api/agent/mcp_tools");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    mcpToolsData = data.tools || [];

    renderMcpTools(mcpToolsData);
  } catch (err) {
    console.error("Failed to load MCP tools:", err);
    if (container) {
      container.innerHTML = `
        <div style="grid-column: 1 / -1; padding: 30px; text-align: center; color: var(--down);">
          ❌ Failed to load MCP tools: ${esc(err.message)}
        </div>
      `;
    }
  }
}

function renderMcpTools(tools) {
  const container = $("#mcp-tools-grid");
  if (!container) return;

  if (!tools.length) {
    container.innerHTML = `<p style="grid-column: 1 / -1; text-align: center; color: var(--muted);">No MCP tools registered.</p>`;
    return;
  }

  const isHi = typeof getLanguage === "function" && getLanguage() === "hi";

  container.innerHTML = tools.map(t => {
    const props = t.parameters?.properties || {};
    const propKeys = Object.keys(props);
    const requiredKeys = t.parameters?.required || [];

    const paramsListHtml = propKeys.map(k => {
      const p = props[k];
      const isReq = requiredKeys.includes(k);
      return `
        <div class="mcp-param-row">
          <span class="mcp-param-name"><code>${esc(k)}</code>${isReq ? ' <span class="req-star" title="Required">*</span>' : ''}</span>
          <span class="mcp-param-type">${esc(p.type || 'any')}</span>
          <span class="mcp-param-desc">${esc(p.description || '')}</span>
        </div>
      `;
    }).join("");

    return `
      <div class="tool-card mcp-tool-card" id="mcp-card-${esc(t.name)}">
        <div>
          <div class="tool-header">
            <div class="tool-title-wrap">
              <span class="tool-icon" style="font-size:24px;">${t.icon || '⚡'}</span>
              <div>
                <h3 class="tool-title">${esc(t.title || t.name)}</h3>
                <div style="display:flex;align-items:center;gap:6px;margin-top:2px;">
                  <code style="font-size:11px;color:var(--accent);font-weight:600;">${esc(t.name)}</code>
                  <span class="badge" style="font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(255,255,255,0.06);">${esc(t.provider || 'Open-Source Driver')}</span>
                </div>
              </div>
            </div>
            <span class="tool-tag" style="background:color-mix(in srgb, var(--accent) 15%, transparent);color:var(--accent);border-color:rgba(255,109,90,0.3);">${esc(t.category || 'MCP')}</span>
          </div>
          <p class="tool-desc">${esc(t.description)}</p>

          <details class="mcp-schema-details">
            <summary class="mcp-schema-summary">
              <span>📋 ${isHi ? "Input Parameters & JSON Schema" : "Input Parameters & JSON Schema"} (${propKeys.length})</span>
            </summary>
            <div class="mcp-params-wrap">
              ${paramsListHtml}
            </div>
            <div style="margin-top:8px;text-align:right;">
              <button type="button" class="btn small text" onclick="copyToolMcpSchema('${esc(t.name)}')">📋 ${isHi ? "Copy MCP Tool Spec" : "Copy Tool Spec"}</button>
            </div>
          </details>
        </div>

        <div class="tool-actions" style="margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap;">
          <button type="button" class="btn primary small" onclick="openMcpTester('${esc(t.name)}')">
            🧪 ${isHi ? "Test Dispatch" : "Test Dispatch"}
          </button>
          <button type="button" class="btn small" onclick="askAgentMcp('${esc(t.name)}')">
            🤖 ${isHi ? "Ask Agent" : "Ask Agent"}
          </button>
        </div>
      </div>
    `;
  }).join("");
}

function copyToolMcpSchema(toolName) {
  const tool = mcpToolsData.find(t => t.name === toolName);
  if (!tool) return;
  const spec = {
    name: tool.name,
    description: tool.description,
    inputSchema: tool.parameters,
  };
  navigator.clipboard.writeText(JSON.stringify(spec, null, 2)).then(() => {
    if (typeof toast === "function") toast("ok", "Copied", `MCP schema for ${toolName} copied to clipboard.`);
  });
}

function copyAllMcpSpec() {
  if (!mcpToolsData.length) return;
  const payload = {
    protocol: "Model Context Protocol (MCP) v1.0",
    tools: mcpToolsData.map(t => ({
      name: t.name,
      description: t.description,
      inputSchema: t.parameters,
    })),
  };
  navigator.clipboard.writeText(JSON.stringify(payload, null, 2)).then(() => {
    if (typeof toast === "function") toast("ok", "Copied", "Full MCP Server Tools specification copied.");
  });
}

function askAgentMcp(toolName) {
  const tool = mcpToolsData.find(t => t.name === toolName);
  const prompt = tool ? `Please invoke MCP tool ${tool.name} with sample parameters.` : `Use ${toolName}`;
  showView("agent");
  const input = $("#chat-input");
  if (input) {
    input.value = prompt;
    input.focus();
  }
}

// -----------------------------------------------------------------------------
// Interactive MCP Tool Test Modal
// -----------------------------------------------------------------------------

function openMcpTester(toolName) {
  const tool = mcpToolsData.find(t => t.name === toolName);
  if (!tool) return;
  currentTestingTool = tool;

  const modal = $("#mcp-test-modal");
  if (!modal) return;

  $("#mcp-modal-title").textContent = `🧪 Test MCP Tool: ${tool.name}`;
  $("#mcp-modal-subtitle").textContent = `${tool.title} · ${tool.provider}`;
  $("#mcp-test-payload").value = JSON.stringify(tool.sample_args || {}, null, 2);
  $("#mcp-test-output").textContent = "Click 'Send MCP Request' to execute this tool.";
  $("#mcp-test-status").textContent = "Ready";
  $("#mcp-test-status").className = "badge";

  modal.hidden = false;
}

function closeMcpTester() {
  const modal = $("#mcp-test-modal");
  if (modal) modal.hidden = true;
  currentTestingTool = null;
}

async function runMcpTestExecution() {
  if (!currentTestingTool) return;

  const payloadText = $("#mcp-test-payload").value.trim();
  let payload = {};
  try {
    payload = payloadText ? JSON.parse(payloadText) : {};
  } catch (err) {
    alert("Invalid JSON payload: " + err.message);
    return;
  }

  const runBtn = $("#mcp-run-btn");
  const outputBox = $("#mcp-test-output");
  const statusBadge = $("#mcp-test-status");

  if (runBtn) runBtn.disabled = true;
  statusBadge.textContent = "Executing...";
  statusBadge.className = "badge status-warn";
  outputBox.textContent = "Dispatching MCP tool request to server...";

  const startTime = performance.now();
  try {
    const res = await fetch(`/api/agent/mcp_tools/${currentTestingTool.name}/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const elapsed = Math.round(performance.now() - startTime);
    const data = await res.json();

    outputBox.textContent = JSON.stringify(data, null, 2);
    if (data.isError) {
      statusBadge.textContent = `Failed (${elapsed}ms)`;
      statusBadge.className = "badge status-down";
    } else {
      statusBadge.textContent = `Success (${elapsed}ms)`;
      statusBadge.className = "badge status-up";
    }
  } catch (err) {
    statusBadge.textContent = "Error";
    statusBadge.className = "badge status-down";
    outputBox.textContent = `Network / Execution Error: ${err.message}`;
  } finally {
    if (runBtn) runBtn.disabled = false;
  }
}

// Modal event listeners
document.addEventListener("DOMContentLoaded", () => {
  const closeBtn = $("#mcp-test-modal-close");
  if (closeBtn) closeBtn.addEventListener("click", closeMcpTester);

  const modal = $("#mcp-test-modal");
  if (modal) {
    modal.addEventListener("click", e => {
      if (e.target === modal) closeMcpTester();
    });
  }

  const runBtn = $("#mcp-run-btn");
  if (runBtn) runBtn.addEventListener("click", runMcpTestExecution);
});
