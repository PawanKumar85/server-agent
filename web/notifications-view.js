"use strict";

// ---------- Simple Notifications & Node Management ----------
let notifData = { counts: { total: 0, email: 0, whatsapp: 0, sms: 0, active: 0 }, channels: { email: [], whatsapp: [], sms: [] }, nodes: [] };
let notifFilter = "all"; // 'all' | 'email' | 'whatsapp' | 'sms'

async function loadNotifications() {
  const container = $("#notifications-content");
  if (!container) return;

  try {
    const res = await fetch("/api/notifications");
    if (!res.ok) throw new Error(res.statusText);
    notifData = await res.json();
    updateNotifBadges();
    renderNotificationsView();
  } catch (err) {
    if (container) {
      container.innerHTML = `<div class="card-block error" style="padding:16px;text-align:center;color:var(--down);">
        Failed to load notifications: ${esc(err.message)}
      </div>`;
    }
  }
}

function updateNotifBadges() {
  const c = (notifData && notifData.counts) || { total: 0, email: 0, whatsapp: 0, sms: 0, active: 0 };
  
  const totalBadge = $("#sidebar-notif-pill");
  if (totalBadge) totalBadge.textContent = c.total || 0;

  const emailBadge = $("#sub-count-email");
  if (emailBadge) emailBadge.textContent = c.email || 0;

  const waBadge = $("#sub-count-whatsapp");
  if (waBadge) waBadge.textContent = c.whatsapp || 0;

  const smsBadge = $("#sub-count-sms");
  if (smsBadge) smsBadge.textContent = c.sms || 0;

  const topTotal = $("#notif-summary-total");
  if (topTotal) topTotal.textContent = `${c.total || 0} Nodes`;
  const topActive = $("#notif-summary-active");
  if (topActive) topActive.textContent = `${c.active || 0} Active`;
}

function setNotifFilter(ch) {
  notifFilter = ch;
  $$(".notif-tab").forEach(tab => {
    tab.classList.toggle("active", tab.dataset.channel === ch);
  });
  $$(".nav-subitem").forEach(sub => {
    sub.classList.toggle("active", sub.dataset.notifFilter === ch);
  });
  renderNotificationsView();
}

function renderNotificationsView() {
  const container = $("#notifications-content");
  if (!container || !notifData) return;

  const channels = notifData.channels || { email: [], whatsapp: [], sms: [] };
  const showEmail = notifFilter === "all" || notifFilter === "email";
  const showWA = notifFilter === "all" || notifFilter === "whatsapp";
  const showSMS = notifFilter === "all" || notifFilter === "sms";

  let html = "";

  // 1. Email Channel
  if (showEmail) {
    html += renderChannelSection("email", "📧", "Email", channels.email || []);
  }

  // 2. WhatsApp Channel
  if (showWA) {
    html += renderChannelSection("whatsapp", "💬", "WhatsApp", channels.whatsapp || []);
  }

  // 3. SMS Channel
  if (showSMS) {
    html += renderChannelSection("sms", "📱", "SMS", channels.sms || []);
  }

  container.innerHTML = html;
  bindNotifCardEvents();
}

function renderChannelSection(channelKey, icon, title, nodes) {
  return `
    <div class="notif-channel-section" id="channel-sec-${channelKey}">
      <div class="notif-channel-header">
        <div class="notif-channel-title-wrap">
          <span class="notif-channel-icon">${icon}</span>
          <div>
            <h3 class="notif-channel-title">${title} <span class="notif-nodes-count-tag">${nodes.length}</span></h3>
          </div>
        </div>
        <button class="btn small primary btn-add-node" data-channel="${channelKey}">+ Add ${title}</button>
      </div>

      <div class="notif-nodes-grid">
        ${nodes.length === 0 ? `
          <div class="notif-empty-card">
            No ${title} destinations added yet.
            <button class="btn small btn-add-node" data-channel="${channelKey}" style="margin-top:8px;">+ Add ${title} Destination</button>
          </div>
        ` : nodes.map(n => renderNodeCard(n)).join("")}
      </div>
    </div>
  `;
}

function renderNodeCard(node) {
  const isActive = node.status === "active";
  const icon = node.channel === "email" ? "✉️" : node.channel === "whatsapp" ? "💬" : "📱";

  return `
    <div class="notif-node-card ${isActive ? 'active-node' : 'muted-node'}" data-id="${node.id}">
      <div class="notif-node-top">
        <div class="notif-node-ident">
          <span class="notif-node-avatar">${icon}</span>
          <div style="min-width:0">
            <h4 class="notif-node-label">${esc(node.label || node.target)}</h4>
            <div class="notif-node-target"><code>${esc(node.target)}</code></div>
          </div>
        </div>
        <label class="notif-toggle-switch" title="Toggle Active / Muted">
          <input type="checkbox" class="notif-toggle-input" data-id="${node.id}" ${isActive ? "checked" : ""}>
          <span class="notif-slider"></span>
        </label>
      </div>

      <div class="notif-node-footer">
        <span class="notif-status-indicator ${isActive ? 'ok' : 'muted'}">${isActive ? 'Active' : 'Muted'}</span>
        <div class="notif-node-actions">
          <button class="btn xs btn-test-node" data-id="${node.id}" title="Send test ping">⚡ Test</button>
          <button class="btn xs danger btn-del-node" data-id="${node.id}" title="Delete node">✕</button>
        </div>
      </div>
    </div>
  `;
}

function bindNotifCardEvents() {
  // Toggle Active switch
  $$(".notif-toggle-input").forEach(chk => {
    chk.onchange = async () => {
      const id = chk.dataset.id;
      const newStatus = chk.checked ? "active" : "muted";
      try {
        const res = await fetch(`/api/notifications/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: newStatus })
        });
        if (!res.ok) throw new Error("Failed to update status");
        toast("ok", "Updated", `Node ${newStatus}`);
        loadNotifications();
      } catch (e) {
        toast("error", "Error", e.message);
        chk.checked = !chk.checked;
      }
    };
  });

  // Test Node Button
  $$(".btn-test-node").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.dataset.id;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const res = await fetch(`/api/notifications/${id}/test`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Test failed");
        toast("ok", "Test Alert Sent", data.message || "Test alert delivered!");
      } catch (e) {
        toast("error", "Failed", e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = "⚡ Test";
      }
    };
  });

  // Delete Node Button
  $$(".btn-del-node").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.dataset.id;
      if (!confirm("Remove this destination?")) return;
      try {
        const res = await fetch(`/api/notifications/${id}`, { method: "DELETE" });
        if (!res.ok) throw new Error("Delete failed");
        toast("ok", "Removed", "Notification destination removed");
        loadNotifications();
      } catch (e) {
        toast("error", "Delete Failed", e.message);
      }
    };
  });

  // Add Node Buttons
  $$(".btn-add-node").forEach(btn => {
    btn.onclick = () => {
      openAddNodeModal(btn.dataset.channel || "email");
    };
  });
}

// ---------- Add Node Modal Logic ----------
function openAddNodeModal(defaultChannel = "email") {
  const modal = $("#notif-modal");
  if (!modal) return;
  $("#notif-modal-channel").value = defaultChannel;
  $("#notif-modal-target").value = "";
  $("#notif-modal-label").value = "";
  updateTargetPlaceholder();
  modal.hidden = false;
  $("#notif-modal-target")?.focus();
}

function closeAddNodeModal() {
  const modal = $("#notif-modal");
  if (modal) modal.hidden = true;
}

function updateTargetPlaceholder() {
  const ch = $("#notif-modal-channel")?.value;
  const input = $("#notif-modal-target");
  if (!input) return;
  if (ch === "email") {
    input.type = "email";
    input.placeholder = "e.g. alerts@company.com";
  } else if (ch === "whatsapp") {
    input.type = "text";
    input.placeholder = "e.g. +91 9876543210";
  } else {
    input.type = "tel";
    input.placeholder = "e.g. +91 9876543210";
  }
}

// Setup Event Listeners on DOM Ready
document.addEventListener("DOMContentLoaded", () => {
  // Sidebar subitems click
  $$(".nav-subitem").forEach(sub => {
    sub.onclick = (e) => {
      e.stopPropagation();
      const ch = sub.dataset.notifFilter;
      showView("notifications");
      setNotifFilter(ch);
    };
  });

  // Notifications tabs in header
  $$(".notif-tab").forEach(tab => {
    tab.onclick = () => {
      setNotifFilter(tab.dataset.channel);
    };
  });

  // Header Add Node Button
  const btnHeaderAdd = $("#btn-notif-add-header");
  if (btnHeaderAdd) {
    btnHeaderAdd.onclick = () => openAddNodeModal(notifFilter === "all" ? "email" : notifFilter);
  }

  // Modal Channel dropdown change
  const modalChannelSelect = $("#notif-modal-channel");
  if (modalChannelSelect) {
    modalChannelSelect.onchange = updateTargetPlaceholder;
  }

  // Modal Close buttons
  $$(".notif-modal-close").forEach(btn => {
    btn.onclick = closeAddNodeModal;
  });

  // Modal Form Submit
  const form = $("#notif-modal-form");
  if (form) {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const channel = $("#notif-modal-channel").value;
      const target = $("#notif-modal-target").value.trim();
      const label = $("#notif-modal-label").value.trim();

      if (!target) {
        toast("error", "Required", "Destination is required");
        return;
      }

      try {
        const res = await fetch("/api/notifications", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            channel,
            target,
            label: label || target,
            status: "active"
          })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed to add destination");
        toast("ok", "Added", `Added ${channel.toUpperCase()} destination`);
        closeAddNodeModal();
        loadNotifications();
      } catch (err) {
        toast("error", "Error", err.message);
      }
    };
  }
});
