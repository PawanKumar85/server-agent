"use strict";

// ---------- "Was this the right cause?" — one-tap rating of an outage's root-cause call ----------
// Each outage is keyed by the blamed server and when it started, so the banner and the server panel share one answer.
// Ratings teach the root-cause ranking (learning.py priors) and build a real accuracy figure; 50 is the first target.

const rateCause = {answers: {}, progress: null, picking: null};  // picking: key whose "what was it?" list is open

async function loadCauseRatings() {
  try {
    const res = await fetch("/api/learning/outage-ratings");
    if (!res.ok) return;
    const data = await res.json();
    rateCause.answers = data.answers || {};
    rateCause.progress = data;
    rerenderCauseRatings();
  } catch (_) { /* optional: outages still show without the rating */ }
}

function causeKey(node, onsetAt) {
  return `${node}|${onsetAt || ""}`;
}

function causeProgressText() {
  const p = rateCause.progress;
  if (!p || !p.rated) return `Your answer teaches the ranking. 0 of ${p ? p.target : 50} outages rated so far.`;
  return `${p.rated} of ${p.target} outages rated · the call was right ${Math.round(100 * p.accuracy)}% of the time`;
}

// The rating block. blamed: the server the system blamed; others: the other servers involved (to pick the real one).
function rateCauseHtml(blamed, onsetAt, others = []) {
  const key = causeKey(blamed, onsetAt), short = alertShort(blamed);
  const a = rateCause.answers[key];
  if (a) {
    const what = a.right ? `Yes, ${short}` : a.real ? `No, it was ${a.real === "network" ? "the network" : a.real === "other" ? "something else" : alertShort(a.real)}` : "No";
    return `<div class="rc-rate rc-done" data-rc-key="${esc(key)}">
      <span>✓ Rated: ${esc(what)}</span><button type="button" class="rc-link" data-rc-undo="${esc(key)}">Change</button>
      <span class="rc-progress">${esc(causeProgressText())}</span></div>`;
  }
  if (rateCause.picking === key) {
    const choices = [...new Set(others.filter(o => o && o !== blamed))].slice(0, 6);
    return `<div class="rc-rate" data-rc-key="${esc(key)}">
      <span class="rc-q">What was the real cause?</span>
      <span class="rc-choices">
        ${choices.map(o => `<button type="button" class="rc-btn" data-rc-pick="${esc(o)}" data-rc-blamed="${esc(blamed)}">${esc(alertShort(o))}</button>`).join("")}
        <button type="button" class="rc-btn" data-rc-pick="network" data-rc-blamed="${esc(blamed)}">The network / ISP</button>
        <button type="button" class="rc-btn" data-rc-pick="other" data-rc-blamed="${esc(blamed)}">Something else</button>
        <button type="button" class="rc-link" data-rc-cancel>Cancel</button>
      </span></div>`;
  }
  return `<div class="rc-rate" data-rc-key="${esc(key)}">
    <span class="rc-q">Was <b>${esc(short)}</b> the right cause?</span>
    <button type="button" class="rc-btn rc-yes" data-rc-yes="${esc(blamed)}">👍 Yes</button>
    <button type="button" class="rc-btn rc-no" data-rc-no="${esc(blamed)}">👎 No</button>
    <span class="rc-progress">${esc(causeProgressText())}</span></div>`;
}

function rerenderCauseRatings() {
  if (typeof renderOriginBanner === "function") renderOriginBanner();
  const open = document.querySelector(".node.selected");
  if (open && open.dataset.node && typeof showDetail === "function" && !document.getElementById("detail")?.hidden) {
    showDetail(open.dataset.node);
  }
}

async function sendCauseRating(key, blamed, right, real) {
  rateCause.answers[key] = {right, real: real || null};  // shows at once; undone if the save fails
  rateCause.picking = null;
  rerenderCauseRatings();
  try {
    const res = await fetch("/api/learning/outage-rating", {method: "POST", headers: {"Content-Type": "application/json"},
                                                            body: JSON.stringify({key, blamed, right, real: real || null})});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    rateCause.answers = data.answers || rateCause.answers;
    rateCause.progress = data;
    rerenderCauseRatings();
    if (typeof toast === "function") toast("ok", "Thanks, rating saved", causeProgressText());
  } catch (err) {
    delete rateCause.answers[key];
    rerenderCauseRatings();
    if (typeof toast === "function") toast("bad", "Rating not saved", err.message);
  }
}

document.addEventListener("click", e => {
  const box = e.target.closest(".rc-rate");
  if (!box) return;
  e.stopPropagation();  // the banner and the panel have their own click handlers
  const key = box.dataset.rcKey, t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.rcYes) sendCauseRating(key, t.dataset.rcYes, true);
  else if (t.dataset.rcNo) { rateCause.picking = key; rerenderCauseRatings(); }
  else if (t.dataset.rcPick) sendCauseRating(key, t.dataset.rcBlamed, false, t.dataset.rcPick);
  else if ("rcCancel" in t.dataset) { rateCause.picking = null; rerenderCauseRatings(); }
  else if (t.dataset.rcUndo) { delete rateCause.answers[t.dataset.rcUndo]; rerenderCauseRatings(); }
}, true);

loadCauseRatings();
setInterval(loadCauseRatings, 60000);  // other operators' ratings
