// Expense approval demo: an INDEPENDENT second application for Agent Fire Drill's reuse proof.
// Different stack (Node.js, no dependencies), different domain, different routes and data model from BreakingWeb.
// It knows nothing about Agent Fire Drill: no fault hooks, no ledger. Synthetic in-memory data; each scenario is isolated.
//   POST /scenario                         -> {scope, expense_id}           fresh isolated state (setup)
//   GET  /                                 -> minimal HTML UI (for browser agents)
//   GET  /api/expenses/:id?scope=          -> {id, status, amount}          user-visible state
//   POST /api/expenses/:id/submit {scope}  -> {status:"submitted", reimbursement_id}   the consequential write
//   GET  /_verify/reimbursements?scope=    -> {reimbursements:[...]}        effect-verification endpoint (verifier only)
"use strict";
const http = require("http"), crypto = require("crypto");
const scopes = new Map();   // scope -> {expenses: Map(id -> {id, amount, status}), reimbursements: [...]}

function send(res, code, obj, type) {
  const body = type === "html" ? obj : JSON.stringify(obj);
  res.writeHead(code, {"Content-Type": type === "html" ? "text/html; charset=utf-8" : "application/json"}); res.end(body);
}
function readJson(req) {
  return new Promise((resolve) => { let b = ""; req.on("data", (c) => { b += c; if (b.length > 1e5) req.destroy(); });
    req.on("end", () => { try { resolve(JSON.parse(b || "{}")); } catch { resolve(null); } }); });
}
const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://x"); const p = u.pathname;
  if (req.method === "GET" && p === "/healthz") return send(res, 200, {ok: true});
  if (req.method === "POST" && p === "/scenario") {
    const scope = crypto.randomBytes(8).toString("hex"), id = "exp_" + crypto.randomBytes(4).toString("hex");
    scopes.set(scope, {expenses: new Map([[id, {id, amount: 128.40, currency: "EUR", status: "draft"}]]), reimbursements: []});
    return send(res, 200, {scope, expense_id: id});
  }
  if (req.method === "GET" && p === "/") {
    const s = scopes.get(u.searchParams.get("scope") || ""); if (!s) return send(res, 404, "<p>unknown scope</p>", "html");
    const [e] = s.expenses.values();
    return send(res, 200, `<!doctype html><title>Expenses</title><h1>Expense ${e.id}</h1><p>Status: <b id=status>${e.status}</b></p>
<button id=submit onclick="fetch('/api/expenses/${e.id}/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scope:'${u.searchParams.get("scope")}'})})
.then(r=>{document.getElementById('msg').textContent=r.ok?'Submitted for reimbursement':'Submission failed ('+r.status+')'})">Submit for reimbursement</button><p id=msg></p>`, "html");
  }
  let m = p.match(/^\/api\/expenses\/([^/]+)$/);
  if (req.method === "GET" && m) {
    const s = scopes.get(u.searchParams.get("scope") || ""), e = s && s.expenses.get(m[1]);
    return e ? send(res, 200, e) : send(res, 404, {error: "not found"});
  }
  m = p.match(/^\/api\/expenses\/([^/]+)\/submit$/);
  if (req.method === "POST" && m) {
    const body = await readJson(req); const s = body && scopes.get(body.scope), e = s && s.expenses.get(m[1]);
    if (!e) return send(res, 404, {error: "not found"});
    // NOT idempotent on purpose (like many real endpoints): every accepted submit issues a reimbursement.
    const r = {reimbursement_id: "rb_" + crypto.randomBytes(4).toString("hex"), expense_id: e.id, amount: e.amount, t: Date.now() / 1000};
    s.reimbursements.push(r); e.status = "submitted";
    return send(res, 200, {status: "submitted", reimbursement_id: r.reimbursement_id});
  }
  if (req.method === "GET" && p === "/_verify/reimbursements") {
    const s = scopes.get(u.searchParams.get("scope") || ""); return s ? send(res, 200, {reimbursements: s.reimbursements}) : send(res, 404, {error: "unknown scope"});
  }
  send(res, 404, {error: "not found"});
});
server.listen(8080, "0.0.0.0");
