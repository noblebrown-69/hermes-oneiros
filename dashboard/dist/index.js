/* Oneiros Dream Cycle dashboard — plain IIFE, React from SDK. v0.8.7 */
(function () {
  "use strict";
  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var useToast = SDK.hooks.useToast;
  var C = SDK.components;
  var Badge = C.Badge, Button = C.Button, Input = C.Input;
  var Dialog = C.Dialog, DialogContent = C.DialogContent, DialogHeader = C.DialogHeader;
  var DialogTitle = C.DialogTitle, DialogDescription = C.DialogDescription, DialogFooter = C.DialogFooter;
  var Toast = C.Toast;
  var fetchJSON = SDK.fetchJSON;
  var API = "/api/plugins/dream-cycle";

  function pageParams() {
    var params;
    try { params = new URLSearchParams(window.location.search || ""); }
    catch (e) { params = new URLSearchParams(); }
    return {
      profile: (params.get("profile") || "").trim(),
      day: (params.get("day") || "").trim()
    };
  }

  function withPageQuery(path) {
    var p = pageParams();
    var extra = [];
    if (p.profile) extra.push("profile=" + encodeURIComponent(p.profile));
    if (p.day && path.indexOf("day=") < 0) extra.push("day=" + encodeURIComponent(p.day));
    if (!extra.length) return path;
    return path + (path.indexOf("?") >= 0 ? "&" : "?") + extra.join("&");
  }

  function el(type, props) {
    var kids = Array.prototype.slice.call(arguments, 2);
    return React.createElement.apply(React, [type, props || null].concat(kids));
  }

  var STATE_LABEL = {
    pending: "Pending",
    accept: "Accepted",
    applied: "Applied",
    reject: "Rejected",
    defer: "Deferred",
    "rolled-back": "Rolled back",
    reverted: "Reverted"
  };
  var STATE_FEEL = {
    pending: "Still dreaming",
    accept: "Kept in the ledger",
    applied: "Written into memory",
    reject: "Set aside",
    defer: "Left for another night",
    "rolled-back": "Unwound to the snapshot",
    reverted: "Back in the night"
  };
  var GLYPH = {
    pending: "○",
    accept: "◐",
    applied: "●",
    reject: "✕",
    defer: "◌",
    "rolled-back": "↩",
    reverted: "○"
  };

  function stateKey(p) {
    var d = p && p.last_decision;
    if (!d) return "pending";
    return d;
  }

  function DiffBox(changes, markers) {
    var lines = [];
    (changes || []).forEach(function (c) {
      lines.push("- L" + c.line_no + ": " + (c.before || ""));
      lines.push("+ L" + c.line_no + ": " + (c.after || ""));
    });
    (markers || []).forEach(function (m) { lines.push("+ MARKER: " + m); });
    if (!lines.length) lines.push("(no line changes)");
    return el("pre", { className: "oneiros-pre oneiros-mono" }, lines.join("\n"));
  }

  function OneirosPage() {
    var toastApi = useToast();
    var showToast = toastApi.showToast;
    var toast = toastApi.toast;
    var _ov = useState(null); var overview = _ov[0]; var setOverview = _ov[1];
    var _pr = useState(null); var proposals = _pr[0]; var setProposals = _pr[1];
    var _rc = useState(null); var receipts = _rc[0]; var setReceipts = _rc[1];
    var _ld = useState(true); var loading = _ld[0]; var setLoading = _ld[1];
    var _err = useState(null); var err = _err[0]; var setErr = _err[1];
    var _busy = useState(null); var busy = _busy[0]; var setBusy = _busy[1];

    var _acc = useState(null); var acceptFor = _acc[0]; var setAcceptFor = _acc[1];
    var _keep = useState(""); var keep = _keep[0]; var setKeep = _keep[1];
    var _ev = useState(""); var evidence = _ev[0]; var setEvidence = _ev[1];

    var _ap = useState(null); var applyPreview = _ap[0]; var setApplyPreview = _ap[1];
    var _rb = useState(null); var rollbackPreview = _rb[0]; var setRollbackPreview = _rb[1];
    var _ed = useState({}); var editingIds = _ed[0]; var setEditingIds = _ed[1];
    var _ub = useState(null); var undoBatch = _ub[0]; var setUndoBatch = _ub[1];
    var _flt = useState("all"); var viewFilter = _flt[0]; var setViewFilter = _flt[1];
    var _fr = useState({}); var freshIds = _fr[0]; var setFreshIds = _fr[1];
    var _nt = useState(null); var noteFor = _nt[0]; var setNoteFor = _nt[1];
    var _rm = useState(null); var reminder = _rm[0]; var setReminder = _rm[1];
    var _nk = useState("reject"); var noteKind = _nk[0]; var setNoteKind = _nk[1];
    var _nv = useState(""); var noteText = _nv[0]; var setNoteText = _nv[1];

    function isDecided(p) {
      var d = p && p.last_decision;
      return d === "accept" || d === "reject" || d === "defer" || d === "applied" || d === "rolled-back";
    }
    function showDecisionActions(p) {
      return !isDecided(p) || !!editingIds[p.proposal_id];
    }
    function setEditing(pid, on) {
      setEditingIds(function (prev) {
        var next = Object.assign({}, prev);
        if (on) next[pid] = true; else delete next[pid];
        return next;
      });
    }
    function markFresh(ids) {
      setFreshIds(function (prev) {
        var next = Object.assign({}, prev);
        (ids || []).forEach(function (id) { if (id) next[id] = true; });
        return next;
      });
    }

    var refresh = useCallback(function () {
      setLoading(true); setErr(null);
      return Promise.all([
        fetchJSON(withPageQuery(API + "/overview")),
        fetchJSON(withPageQuery(API + "/proposals")),
        fetchJSON(withPageQuery(API + "/receipts")),
        fetchJSON(withPageQuery(API + "/reminder")),
      ]).then(function (arr) {
        setOverview(arr[0]); setProposals(arr[1]); setReceipts(arr[2]); setReminder(arr[3]);
      }).catch(function (e) {
        setErr(String(e && e.message || e));
      }).finally(function () { setLoading(false); });
    }, []);

    useEffect(function () { refresh(); }, [refresh]);

    function post(path, body, okMsg) {
      setBusy(path);
      return fetchJSON(withPageQuery(API + path), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      }).then(function (r) {
        if (okMsg) showToast(okMsg, "success");
        return r;
      }).catch(function (e) {
        showToast(String(e && e.message || e), "error");
        throw e;
      }).finally(function () { setBusy(null); });
    }

    var gates = (overview && overview.gates) || {};
    var st = (overview && overview.status) || {};
    var latest = st.latest || {};
    var scanned = latest.scanned_files;
    var hyg = (overview && overview.hygiene) || {};
    var wak = (overview && overview.waking) || {};
    var propRows = (proposals && proposals.proposals) || [];
    var recRows = (receipts && receipts.receipts) || [];

    function openAccept(p) {
      setAcceptFor(p);
      setKeep((p.values && p.values[0]) || p.keep || "");
      setEvidence("");
    }

    function submitAccept() {
      if (!acceptFor) return;
      var pid = acceptFor.proposal_id;
      post("/review", {
        proposal_id: pid,
        decision: "accept",
        keep: keep,
        evidence: evidence || "",
        day: proposals && proposals.day,
      }, "Accepted. Memory stays as it is until you apply.").then(function () {
        setAcceptFor(null);
        setEditing(pid, false);
        markFresh([pid]);
        return refresh();
      });
    }

    function oneClickAccept(p) {
      var keepVal = (p.values && p.values[0]) || "";
      if (!keepVal) { showToast("no keep value on this proposal", "error"); return; }
      post("/review", {
        proposal_id: p.proposal_id,
        decision: "accept",
        keep: keepVal,
        evidence: "",
        day: proposals && proposals.day,
      }, "Accepted. Memory stays as it is until you apply.").then(function () {
        setEditing(p.proposal_id, false);
        markFresh([p.proposal_id]);
        return refresh();
      });
    }

    function matchesFilter(p) {
      if (viewFilter === "pending") return !isDecided(p);
      if (viewFilter === "accepted") return p.last_decision === "accept";
      if (viewFilter === "applied") return p.last_decision === "applied";
      return true;
    }

    function pendingRows() {
      return propRows.filter(function (p) { return !isDecided(p) && matchesFilter(p); });
    }

    function acceptAllShown() {
      var pending = pendingRows();
      if (!pending.length) { showToast("nothing pending to accept", "error"); return; }
      setBusy("/review/accept-all");
      fetchJSON(withPageQuery(API + "/review/accept-all"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          proposal_ids: pending.map(function (p) { return p.proposal_id; }),
          day: proposals && proposals.day,
        }),
      }).then(function (r) {
        var n = r && r.count || 0;
        if (r && r.batch_id && n) {
          setUndoBatch({ batch_id: r.batch_id, count: n, day: r.day });
        }
        markFresh((r && r.rows || []).map(function (row) { return row.proposal_id; }));
        showToast("Accepted " + n + " proposal" + (n === 1 ? "" : "s") + ". Memory is unchanged.", "success");
        return refresh();
      }).catch(function (e) {
        showToast(String(e && e.message || e), "error");
        throw e;
      }).finally(function () { setBusy(null); });
    }

    function undoAcceptAll() {
      if (!undoBatch) return;
      var batch = undoBatch;
      setBusy("/review/undo-batch");
      fetchJSON(withPageQuery(API + "/review/undo-batch"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batch_id: batch.batch_id, day: batch.day }),
      }).then(function (r) {
        var n = r && r.count || 0;
        setUndoBatch(null);
        showToast("Undid " + n + " accept" + (n === 1 ? "" : "s") + ". The ledger kept both rows.", "success");
        return refresh();
      }).catch(function (e) {
        showToast(String(e && e.message || e), "error");
        throw e;
      }).finally(function () { setBusy(null); });
    }

    function openNote(p, kind) {
      setNoteFor(p);
      setNoteKind(kind);
      setNoteText("");
    }

    function submitNote() {
      if (!noteFor) return;
      if (noteKind === "reject" && !noteText.trim()) {
        showToast("evidence required", "error");
        return;
      }
      var pid = noteFor.proposal_id;
      var kind = noteKind;
      post("/review", {
        proposal_id: pid,
        decision: kind,
        evidence: noteText.trim(),
        note: kind === "defer" ? "deferred from dashboard" : "",
        day: proposals && proposals.day,
      }, kind === "reject"
        ? "Rejected. The trail kept your note."
        : "Deferred. It will still be here in the morning.").then(function () {
        setNoteFor(null);
        setEditing(pid, false);
        markFresh([pid]);
        return refresh();
      });
    }

    function previewApplyAccepted() {
      post("/apply", { accepted: true, dry_run: true }, null).then(function (r) {
        setApplyPreview(r);
      });
    }

    function confirmApply() {
      post("/apply", { accepted: true, dry_run: false }, null).then(function (r) {
        var results = (r && r.results) || [];
        var wrote = results.filter(function (x) { return x && x.ok; }).length;
        if (!wrote) {
          showToast("Nothing applied. " + ((results[0] && results[0].message) || "The night kept the file."), "error");
        } else {
          showToast("Applied " + wrote + ". A receipt is waiting below.", "success");
        }
        setApplyPreview(null);
        return refresh();
      });
    }

    function previewRollback(rec) {
      post("/rollback", { receipt_id: rec.receipt_id, dry_run: true }, null).then(function (r) {
        setRollbackPreview(r);
      });
    }

    function confirmRollback() {
      if (!rollbackPreview || !rollbackPreview.diff) return;
      post("/rollback", { receipt_id: rollbackPreview.diff.receipt_id, dry_run: false }, null).then(function (r) {
        if (r && r.ok) showToast("Rolled back to the snapshot.", "success");
        else showToast((r && r.result && r.result.message) || "Rollback refused.", "error");
        setRollbackPreview(null);
        return refresh();
      });
    }

    function pill(label, value, tone) {
      return el("div", { className: "oneiros-pill" },
        el("span", { className: "oneiros-pill-k" }, label),
        el("span", { className: "oneiros-pill-v" + (tone ? " " + tone : "") }, value));
    }

    if (loading && !overview) {
      return el("div", { className: "oneiros-page oneiros-loading", role: "status" },
        el("span", { className: "oneiros-moon", "aria-hidden": "true" }),
        el("p", null, "The night is settling…"));
    }
    if (err && !overview) {
      return el("div", { className: "oneiros-page" },
        el("section", { className: "oneiros-panel" }, el("p", { className: "oneiros-bad" }, err)));
    }
    if (overview && !overview.installed) {
      return el("div", { className: "oneiros-page" },
        el("section", { className: "oneiros-panel" },
          el("h2", { className: "oneiros-title" }, "Oneiros"),
          el("p", null, overview.message || "Not installed on this profile."),
          el("p", { className: "oneiros-muted" },
            "Oneiros is not installed on this profile yet. Current profile: ",
            el("code", null, overview.profile || "?"))));
    }

    var pendingCount = propRows.filter(function (p) { return !isDecided(p); }).length;
    var acceptedCount = propRows.filter(function (p) { return p.last_decision === "accept"; }).length;
    var appliedCount = propRows.filter(function (p) { return p.last_decision === "applied"; }).length;
    var rejectedCount = propRows.filter(function (p) { return p.last_decision === "reject"; }).length;
    var shown = propRows.filter(matchesFilter);
    var filters = [
      ["all", "All", propRows.length],
      ["pending", "Pending", pendingCount],
      ["accepted", "Accepted", acceptedCount],
      ["applied", "Applied", appliedCount],
    ];

    function snoozeReminders() {
      post("/reminder/snooze", {}, "Reminders snoozed for a week.").then(function () { return refresh(); });
    }

    var snoozedUntil = reminder && reminder.snoozed_until;
    var linked = pageParams();
    return el("div", {
      className: "oneiros-page",
      "data-pending": String(pendingCount),
      "data-profile": linked.profile || (overview && overview.profile) || "",
      "data-day": (proposals && proposals.day) || linked.day || "",
    },
      el("div", { className: "oneiros-live", role: "status", "aria-live": "polite" },
        toast ? toast.message : ""),
      toast ? el(Toast, { message: toast.message, type: toast.type }) : null,

      el("header", { className: "oneiros-header" },
        el("div", { className: "oneiros-brand" },
          el("span", { className: "oneiros-moon", "aria-hidden": "true" }),
          el("div", null,
            el("h2", { className: "oneiros-title" }, "Oneiros"),
            el("p", { className: "oneiros-banner" }, "Your agent sleeps on it."),
            el("p", { className: "oneiros-lede" },
              "While Hermes sleeps, Oneiros sifts the day and lets the stale memories fade like old dreams."),
            el("p", { className: "oneiros-meta" },
              "profile ", el("code", null, overview.profile),
              " · v", overview.version,
              proposals && proposals.day ? " · night of " + proposals.day : "",
              snoozedUntil && reminder.snooze_active ? " · reminders snoozed until " + snoozedUntil : ""))),
        el("div", { className: "oneiros-actions" },
          el(Button, {
            className: "oneiros-btn oneiros-btn-quiet",
            variant: "outline", size: "sm",
            onClick: snoozeReminders,
            disabled: !!busy,
            title: "Snooze proactive review reminders for this profile for a week. Asking in chat still answers.",
          }, "Snooze for a week"),
          el(Button, { className: "oneiros-btn oneiros-btn-quiet", variant: "outline", size: "sm", onClick: refresh, disabled: !!busy }, "Refresh"))),

      el("div", { className: "oneiros-pills" },
        pill("Apply", gates.apply_enabled ? "On" : "Off", gates.apply_enabled ? "good" : ""),
        pill("Kill switch", gates.kill_switch ? "Disabled" : "Off", gates.kill_switch ? "bad" : ""),
        pill("Freshness", st.freshness || "n/a", null),
        pill("Last run", st.last_run_at || "never", null),
        pill("Files scanned", scanned === 0 ? "0 — check paths" : String(scanned == null ? "n/a" : scanned),
          scanned === 0 ? "warn" : ""),
        pill("Undecided", String((overview.undecided || []).length), null),
        pill("Last receipt", (st.last_receipt && st.last_receipt.receipt_id) || "none", null)),

      (gates.block_reasons || []).length
        ? el("p", { className: "oneiros-block" }, "Apply blocked: " + gates.block_reasons.join("; "))
        : null,

      el("section", { className: "oneiros-section", "aria-labelledby": "oneiros-dreams" },
        el("div", { className: "oneiros-section-head" },
          el("div", null,
            el("h3", { id: "oneiros-dreams" },
              "Last night's dreams",
              proposals && proposals.day ? " — " + proposals.day : ""),
            el("p", { className: "oneiros-muted" },
              pendingCount + " pending · " + acceptedCount + " accepted · " + appliedCount + " applied"
              + (rejectedCount ? " · " + rejectedCount + " rejected" : ""))),
          el("div", { className: "oneiros-actions" },
            el(Button, {
              className: "oneiros-btn oneiros-btn-primary",
              size: "sm",
              disabled: !gates.can_review || !!busy || !pendingRows().length,
              onClick: acceptAllShown,
              title: "Accept every pending proposal in this list (this profile, this day, and the filter above). Writes review.jsonl only.",
            }, "Accept all"),
            el(Button, {
              className: "oneiros-btn oneiros-btn-quiet",
              size: "sm",
              variant: "outline",
              disabled: !gates.can_apply || !!busy,
              onClick: previewApplyAccepted,
              title: gates.can_apply
                ? "Separate step. Dry-run, then apply today's accepts. Evidence is optional."
                : (gates.block_reasons || []).join("; "),
            }, "Apply accepted…"))),

        undoBatch ? el("div", { className: "oneiros-undo-bar", role: "status" },
          el("span", null, "Accepted " + undoBatch.count + " proposal" + (undoBatch.count === 1 ? "" : "s") + ". Memory is untouched."),
          el(Button, { className: "oneiros-btn oneiros-btn-quiet", size: "sm", variant: "outline", disabled: !!busy, onClick: undoAcceptAll }, "Undo"),
          el("button", { type: "button", className: "oneiros-link", onClick: function () { setUndoBatch(null); } }, "Dismiss")) : null,

        el("div", { className: "oneiros-filters", role: "group", "aria-label": "Show proposals" },
          filters.map(function (f) {
            var pressed = viewFilter === f[0];
            return el("button", {
              key: f[0],
              type: "button",
              className: "oneiros-filter",
              "aria-pressed": pressed ? "true" : "false",
              onClick: function () { setViewFilter(f[0]); },
            }, f[1] + " " + f[2]);
          })),

        !propRows.length
          ? el("p", { className: "oneiros-empty" }, "Nothing to wake up to — your agent slept soundly.")
          : !shown.length
            ? el("p", { className: "oneiros-empty" }, "Nothing in this view. The other dreams are still there.")
            : el("div", { className: "oneiros-cards" },
                shown.map(function (p) {
                  var key = stateKey(p);
                  var decided = isDecided(p);
                  var showActs = showDecisionActions(p);
                  var acceptedKeep = p.last_decision === "accept" || p.last_decision === "applied";
                  var keepShown = acceptedKeep ? (p.keep || "") : ((p.values || [])[0] || "");
                  return el("article", {
                    key: p.proposal_id,
                    className: "oneiros-card" + (freshIds[p.proposal_id] ? " is-fresh" : ""),
                    "data-state": key,
                    "data-proposal-id": p.proposal_id,
                  },
                    el("div", { className: "oneiros-card-top" },
                      el("div", null,
                        el("div", { className: "oneiros-id" },
                          el("code", { className: "oneiros-mono" }, p.proposal_id),
                          el(Badge, { variant: "secondary" }, p.label || "label"),
                          p.carried_from ? el(Badge, null, "carried from " + p.carried_from) : null),
                        el("p", { className: "oneiros-metaphor" }, STATE_FEEL[key] || "")),
                      el("span", { className: "oneiros-status" },
                        el("span", { className: "oneiros-glyph", "aria-hidden": "true" }, GLYPH[key] || "○"),
                        STATE_LABEL[key] || String(p.last_decision || "Pending"))),
                    el("div", { className: "oneiros-values" },
                      (p.values || []).map(function (v) {
                        var on = String(v) === String(keepShown);
                        return el("span", {
                          key: String(v),
                          className: "oneiros-value oneiros-mono" + (on ? " is-keep" : ""),
                        }, (on ? (acceptedKeep ? "Keep " : "Default ") : "") + v);
                      })),
                    el("p", { className: "oneiros-where oneiros-mono" },
                      (p.file || "").split("/").slice(-2).join("/"),
                      ":", (p.line_nos || []).join(",")),
                    p.note ? el("p", { className: "oneiros-note oneiros-muted" }, p.note) : null,
                    (p.session_evidence && p.session_evidence.length)
                      ? el("blockquote", { className: "oneiros-quote" },
                          el("span", { className: "oneiros-mono" },
                            "from a dream of session ",
                            String((p.session_evidence[0].session_id || "").slice(0, 12)),
                            "…"),
                          el("p", null, p.session_evidence[0].snippet || ""))
                      : null,
                    decided && !showActs
                      ? el("div", { className: "oneiros-actions" },
                          el("span", { className: "oneiros-muted" },
                            (p.last_actor || "unknown"),
                            p.last_ts ? " · " + p.last_ts : "",
                            p.evidence
                              ? " · evidence noted"
                              : ((p.last_decision === "accept" || p.last_decision === "applied")
                                ? " · no evidence — acceptance stands"
                                : " · no evidence")),
                          el("button", {
                            type: "button",
                            className: "oneiros-link",
                            disabled: !gates.can_review || !!busy,
                            onClick: function () { setEditing(p.proposal_id, true); },
                          }, "Change decision"))
                      : el("div", { className: "oneiros-actions" },
                          el(Button, {
                            className: "oneiros-btn oneiros-btn-primary",
                            size: "sm",
                            disabled: !gates.can_review || !!busy,
                            onClick: function () { oneClickAccept(p); },
                            title: "Accept with the first listed value. Writes review.jsonl only. Evidence optional.",
                          }, "Accept"),
                          el(Button, {
                            className: "oneiros-btn oneiros-btn-quiet",
                            size: "sm", variant: "outline",
                            disabled: !gates.can_review || !!busy,
                            onClick: function () { openAccept(p); },
                            title: "Pick a keep value or add evidence",
                          }, "Accept with note…"),
                          el(Button, {
                            className: "oneiros-btn oneiros-btn-quiet",
                            size: "sm", variant: "outline",
                            disabled: !gates.can_review || !!busy,
                            onClick: function () { openNote(p, "reject"); },
                          }, "Reject"),
                          el(Button, {
                            className: "oneiros-btn oneiros-btn-ghost",
                            size: "sm", variant: "ghost",
                            disabled: !gates.can_review || !!busy,
                            onClick: function () { openNote(p, "defer"); },
                          }, "Defer"),
                          decided
                            ? el("button", {
                                type: "button",
                                className: "oneiros-link",
                                onClick: function () { setEditing(p.proposal_id, false); },
                              }, "Cancel")
                            : null));
                }))),

      el("div", { className: "oneiros-grid" },
        el("section", { className: "oneiros-panel" },
          el("h3", null, "Waking check"),
          !hyg.installed
            ? el("p", { className: "oneiros-muted" }, "Not installed on this profile.")
            : el("div", null,
                el("p", null, "Hits ", el("strong", null, String(hyg.hit_count == null ? "—" : hyg.hit_count))),
                el("p", { className: "oneiros-muted" }, "Last run ", hyg.last_run || "—"),
                el("p", { className: "oneiros-muted" }, "Scanned ", String((hyg.scanned || []).length), " files"))),
        el("section", { className: "oneiros-panel" },
          el("h3", null, "Morning briefing"),
          !wak.installed
            ? el("p", { className: "oneiros-muted" }, "Not installed on this profile.")
            : el("div", null,
                el("p", { className: "oneiros-muted" }, "Last recap ", wak.last_recap || "—"),
                el("ul", { className: "oneiros-mono" },
                  (wak.recaps || []).slice(0, 5).map(function (r) {
                    return el("li", { key: r.name }, r.mtime, " · ", r.name);
                  }))))),

      st.text ? el("details", { className: "oneiros-details" },
        el("summary", null, "Night log"),
        el("pre", { className: "oneiros-pre oneiros-mono" }, st.text)) : null,

      el("section", { className: "oneiros-section", "aria-labelledby": "oneiros-receipts" },
        el("div", { className: "oneiros-section-head" },
          el("h3", { id: "oneiros-receipts" }, "A receipt for every change")),
        !recRows.length
          ? el("p", { className: "oneiros-empty" }, "No receipts yet — nothing kept from last night's dreams.")
          : el("div", { className: "oneiros-cards" },
              recRows.map(function (r) {
                return el("article", {
                  key: r.receipt_id,
                  className: "oneiros-card",
                  "data-state": r.rolled_back_at ? "rolled-back" : "applied",
                },
                  el("div", { className: "oneiros-card-top" },
                    el("code", { className: "oneiros-mono" }, r.receipt_id),
                    el("span", { className: "oneiros-status" },
                      r.rolled_back_at ? "Rolled back" : "Applied")),
                  el("p", { className: "oneiros-muted" },
                    r.applied_at || "",
                    " · keep ", el("span", { className: "oneiros-mono" }, String(r.keep)),
                    " · retired ", el("span", { className: "oneiros-mono" }, (r.retired || []).join(", ") || "—"),
                    " · ", r.evidence_status || "n/a",
                    r.evidence ? " · evidence noted" : " · no cited source"),
                  !r.rolled_back_at
                    ? el("div", { className: "oneiros-actions" },
                        el(Button, {
                          className: "oneiros-btn oneiros-btn-quiet",
                          size: "sm", variant: "outline",
                          disabled: gates.kill_switch || !!busy,
                          onClick: function () { previewRollback(r); },
                        }, "Rollback…"))
                    : null);
              }))),

      el("p", { className: "oneiros-footer" },
        "DREAM AT NIGHT · WAKE TO PROPOSALS · KEEP WHAT'S TRUE · A RECEIPT FOR EVERY CHANGE"),

      acceptFor ? el(Dialog, { open: true, onOpenChange: function (o) { if (!o) setAcceptFor(null); } },
        el(DialogContent, { className: "oneiros-dialog" },
          el("div", { className: "oneiros-dialog-body" },
            el(DialogHeader, null,
              el(DialogTitle, null, "Accept ", acceptFor.proposal_id),
              el(DialogDescription, null, "Optionally change the value to keep or add evidence. Evidence is optional. Writes review.jsonl only.")),
            el("div", null,
              el("label", { className: "oneiros-label", htmlFor: "oneiros-keep" }, "Keep"),
              el("select", {
                id: "oneiros-keep",
                className: "oneiros-field",
                value: keep,
                onChange: function (e) { setKeep(e.target.value); },
              }, (acceptFor.values || []).map(function (v) {
                return el("option", { key: v, value: v }, v);
              })),
              el("label", { className: "oneiros-label", htmlFor: "oneiros-evidence" }, "Evidence"),
              el(Input, {
                id: "oneiros-evidence",
                className: "oneiros-field",
                value: evidence,
                onChange: function (e) { setEvidence(e.target.value); },
                placeholder: "optional — real source that shows the current value",
              }),
              el("p", { className: "oneiros-hint" }, "Leave this blank if you like. Acceptance is the decision. Apply stays a separate step.")),
            el(DialogFooter, null,
              el(Button, { className: "oneiros-btn oneiros-btn-ghost", variant: "ghost", onClick: function () { setAcceptFor(null); } }, "Cancel"),
              el(Button, {
                className: "oneiros-btn oneiros-btn-primary",
                disabled: !keep || !!busy,
                onClick: submitAccept,
              }, "Write accept"))))) : null,

      noteFor ? el(Dialog, { open: true, onOpenChange: function (o) { if (!o) setNoteFor(null); } },
        el(DialogContent, { className: "oneiros-dialog" },
          el("div", { className: "oneiros-dialog-body" },
            el(DialogHeader, null,
              el(DialogTitle, null, (noteKind === "reject" ? "Reject " : "Defer ") + noteFor.proposal_id),
              el(DialogDescription, null, noteKind === "reject"
                ? "A short note stays in the ledger. Reject needs a reason so the trail is honest."
                : "Optional note. The proposal stays open for another night.")),
            el("label", { className: "oneiros-label", htmlFor: "oneiros-note" },
              noteKind === "reject" ? "Reason" : "Note"),
            el(Input, {
              id: "oneiros-note",
              className: "oneiros-field",
              value: noteText,
              autoFocus: true,
              onChange: function (e) { setNoteText(e.target.value); },
              placeholder: noteKind === "reject" ? "why this dream should fade" : "optional",
            }),
            el(DialogFooter, null,
              el(Button, { className: "oneiros-btn oneiros-btn-ghost", variant: "ghost", onClick: function () { setNoteFor(null); } }, "Cancel"),
              el(Button, {
                className: "oneiros-btn oneiros-btn-primary",
                disabled: !!busy || (noteKind === "reject" && !noteText.trim()),
                onClick: submitNote,
              }, noteKind === "reject" ? "Write reject" : "Write defer"))))) : null,

      applyPreview ? el(Dialog, { open: true, onOpenChange: function (o) { if (!o) setApplyPreview(null); } },
        el(DialogContent, { className: "oneiros-dialog oneiros-apply-dialog" },
          (function () {
            var results = applyPreview.results || [];
            var ready = results.filter(function (r) { return r && r.ok; });
            var blocked = results.length - ready.length;
            return el("div", {
              className: "oneiros-dialog-body oneiros-apply-shell",
              "data-ready": String(ready.length),
              "data-blocked": String(blocked),
            },
              el("div", { className: "oneiros-apply-head" },
                el(DialogHeader, null,
                  el(DialogTitle, null, "Apply accepted dreams"),
                  el(DialogDescription, null,
                    results.length
                      ? (ready.length + " ready" + (blocked ? " · " + blocked + " blocked" : "")
                        + ". Nothing is written until you confirm.")
                      : "No accepted proposals to apply today."),
                  el("p", { className: "oneiros-hint" },
                    "Evidence is optional. Stale or conflicting lines are refused."))),
              el("div", {
                className: "oneiros-apply-scroll",
                tabIndex: 0,
                role: "region",
                "aria-label": "Dry-run preview",
              },
                !results.length
                  ? el("p", { className: "oneiros-muted" }, "Nothing is staged for this night.")
                  : results.map(function (r) {
                      var n = (r.changes || []).length;
                      var file = (r.file || "").split("/").pop();
                      return el("details", {
                        key: r.proposal_id,
                        className: "oneiros-apply-row" + (r.ok ? "" : " is-blocked"),
                      },
                        el("summary", null,
                          el("span", { className: "oneiros-mono oneiros-apply-id" }, r.proposal_id),
                          file ? el("span", { className: "oneiros-apply-file" }, file) : null,
                          el("span", { className: r.ok ? "oneiros-apply-ready" : "oneiros-diff-refused" },
                            r.ok ? "Ready" : "Blocked"),
                          el("span", { className: "oneiros-apply-brief" },
                            r.ok ? (n + (n === 1 ? " line" : " lines")) : (r.message || r.status || "refused"))),
                        el("p", { className: "oneiros-muted oneiros-apply-msg" }, r.message || r.status || ""),
                        DiffBox(r.changes, r.markers));
                    })),
              el("div", { className: "oneiros-apply-foot" },
                el(Button, { className: "oneiros-btn oneiros-btn-ghost", variant: "ghost", onClick: function () { setApplyPreview(null); } }, "Cancel"),
                el(Button, {
                  className: "oneiros-btn oneiros-btn-primary",
                  disabled: !ready.length || !!busy || !gates.can_apply,
                  onClick: confirmApply,
                }, "Apply now")));
          })())) : null,

      rollbackPreview ? el(Dialog, { open: true, onOpenChange: function (o) { if (!o) setRollbackPreview(null); } },
        el(DialogContent, { className: "oneiros-dialog" },
          el("div", { className: "oneiros-dialog-body" },
            el(DialogHeader, null,
              el(DialogTitle, null, "Unwind this change"),
              el(DialogDescription, null, "Rollback restores the pre-apply snapshot for this receipt. The ledger gains a rolled-back row; nothing is deleted.")),
            el("div", null,
              el("p", { className: "oneiros-mono" }, rollbackPreview.diff && rollbackPreview.diff.receipt_id),
              el("p", { className: "oneiros-muted" },
                "file: ", rollbackPreview.diff && rollbackPreview.diff.file),
              rollbackPreview.diff && rollbackPreview.diff.file_changed_since_apply
                ? el("p", { className: "oneiros-warn" }, "File changed since apply — rollback refuses unless the snapshot is still a safe match.")
                : null,
              el("p", { className: "oneiros-muted" }, "Snapshot"),
              el("pre", { className: "oneiros-pre oneiros-mono" },
                (rollbackPreview.diff && rollbackPreview.diff.snapshot_preview) || ""),
              el("p", { className: "oneiros-muted" }, "Current"),
              el("pre", { className: "oneiros-pre oneiros-mono" },
                (rollbackPreview.diff && rollbackPreview.diff.current_preview) || "")),
            el(DialogFooter, null,
              el(Button, { className: "oneiros-btn oneiros-btn-ghost", variant: "ghost", onClick: function () { setRollbackPreview(null); } }, "Cancel"),
              el(Button, {
                className: "oneiros-btn oneiros-btn-danger",
                variant: "destructive",
                disabled: !!busy || gates.kill_switch,
                onClick: confirmRollback,
              }, "Rollback now"))))) : null
    );
  }

  window.__HERMES_PLUGINS__.register("dream-cycle", OneirosPage);
})();
