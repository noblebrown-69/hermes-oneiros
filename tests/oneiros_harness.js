/* Test-only stand-in for the Hermes plugin SDK. Not loaded by the dashboard. */
(function () {
  "use strict";
  var hook = { comp: null, index: 0 };

  function flatten(children, out) {
    out = out || [];
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c == null || c === false || c === true) continue;
      if (Array.isArray(c)) flatten(c, out);
      else out.push(c);
    }
    return out;
  }

  function createElement(type, props) {
    var kids = [];
    for (var i = 2; i < arguments.length; i++) kids.push(arguments[i]);
    props = props ? Object.assign({}, props) : {};
    if (props.children != null) {
      var extra = Array.isArray(props.children) ? props.children : [props.children];
      kids = kids.concat(extra);
      delete props.children;
    }
    return { type: type, props: props, children: kids };
  }

  function depsChanged(a, b) {
    if (!a || !b || a.length !== b.length) return true;
    for (var i = 0; i < a.length; i++) if (!Object.is(a[i], b[i])) return true;
    return false;
  }

  function schedule(comp) {
    if (comp.queued) return;
    comp.queued = true;
    queueMicrotask(function () {
      comp.queued = false;
      render(comp);
    });
  }

  function useState(init) {
    var comp = hook.comp;
    var i = hook.index++;
    if (!comp.hooks[i]) {
      comp.hooks[i] = { state: typeof init === "function" ? init() : init };
    }
    var slot = comp.hooks[i];
    function setState(v) {
      var next = typeof v === "function" ? v(slot.state) : v;
      if (Object.is(next, slot.state)) return;
      slot.state = next;
      schedule(comp);
    }
    return [slot.state, setState];
  }

  function useEffect(fn, deps) {
    var comp = hook.comp;
    var i = hook.index++;
    var prev = comp.hooks[i];
    var slot = prev || { cleanup: null };
    slot.fn = fn;
    slot.run = !prev || depsChanged(prev.deps, deps);
    slot.deps = deps ? deps.slice() : null;
    comp.hooks[i] = slot;
  }

  function useCallback(fn, deps) {
    var comp = hook.comp;
    var i = hook.index++;
    var prev = comp.hooks[i];
    if (!prev || depsChanged(prev.deps, deps)) {
      comp.hooks[i] = { fn: fn, deps: deps ? deps.slice() : null };
    }
    return comp.hooks[i].fn;
  }

  function useToast() {
    var pair = useState(null);
    function showToast(message, type) {
      pair[1]({ message: message, type: type || "success" });
    }
    return { showToast: showToast, toast: pair[0] };
  }

  function runEffects(comp) {
    comp.hooks.forEach(function (slot) {
      if (!slot || !slot.run || typeof slot.fn !== "function") return;
      if (typeof slot.cleanup === "function") {
        try { slot.cleanup(); } catch (e) { /* effect cleanup */ }
      }
      var ret = slot.fn();
      slot.cleanup = typeof ret === "function" ? ret : null;
      slot.run = false;
    });
  }

  var SKIP = {
    onClick: 1, onChange: 1, onOpenChange: 1, variant: 1, size: 1, key: 1,
    className: 1, style: 1, htmlFor: 1, autoFocus: 1, value: 1, children: 1
  };

  function materialize(node) {
    if (node == null || node === false || node === true) return null;
    if (Array.isArray(node)) {
      var frag = document.createDocumentFragment();
      flatten(node).forEach(function (n) {
        var d = materialize(n);
        if (d) frag.appendChild(d);
      });
      return frag;
    }
    if (typeof node === "string" || typeof node === "number") {
      return document.createTextNode(String(node));
    }
    if (typeof node.type === "function") {
      var props = Object.assign({}, node.props);
      var flat = flatten(node.children);
      props.children = flat.length <= 1 ? flat[0] : flat;
      var saved = hook.comp;
      var savedI = hook.index;
      hook.comp = null;
      var inner;
      try {
        inner = node.type(props);
      } finally {
        hook.comp = saved;
        hook.index = savedI;
      }
      return materialize(inner);
    }
    var el = document.createElement(node.type);
    var p = node.props || {};
    if (p.className) el.className = p.className;
    if (p.htmlFor) el.htmlFor = p.htmlFor;
    if (p.style && typeof p.style === "object") {
      Object.keys(p.style).forEach(function (k) { el.style[k] = p.style[k]; });
    }
    Object.keys(p).forEach(function (k) {
      if (SKIP[k] || typeof p[k] === "function" || p[k] == null) return;
      if (k === "disabled" || k === "checked") return;
      el.setAttribute(k, p[k] === true ? "" : String(p[k]));
    });
    if (p.disabled) el.disabled = true;
    if (p.onClick) el.addEventListener("click", function (e) { p.onClick(e); });
    if (p.onChange) {
      var fire = function (e) { p.onChange(e); };
      el.addEventListener("input", fire);
      el.addEventListener("change", fire);
    }
    flatten(node.children).forEach(function (c) {
      var d = materialize(c);
      if (d) el.appendChild(d);
    });
    if (p.value != null && (node.type === "input" || node.type === "select" || node.type === "textarea")) {
      el.value = String(p.value);
    }
    if (p.autoFocus) setTimeout(function () { try { el.focus(); } catch (e) {} }, 0);
    return el;
  }

  function Button(props) {
    return createElement("button", {
      type: "button",
      className: props.className || "",
      disabled: !!props.disabled,
      title: props.title || "",
      onClick: function (e) { if (!props.disabled && props.onClick) props.onClick(e); },
    }, props.children);
  }
  function Badge(props) {
    return createElement("span", { className: "sdk-badge" }, props.children);
  }
  function Input(props) {
    return createElement("input", {
      id: props.id,
      className: props.className || "oneiros-field",
      value: props.value || "",
      placeholder: props.placeholder || "",
      autoFocus: !!props.autoFocus,
      onChange: props.onChange,
    });
  }
  function Dialog(props) {
    if (!props.open) return null;
    return createElement("div", { className: "sdk-dialog", role: "presentation" }, props.children);
  }
  function DialogContent(props) {
    return createElement("div", {
      className: "oneiros-dialog " + (props.className || ""),
      role: "dialog",
      "aria-modal": "true",
    }, props.children);
  }
  function DialogHeader(props) { return createElement("div", null, props.children); }
  function DialogTitle(props) { return createElement("h2", null, props.children); }
  function DialogDescription(props) { return createElement("p", null, props.children); }
  function DialogFooter(props) { return createElement("div", { className: "oneiros-actions" }, props.children); }
  function Toast(props) {
    return createElement("div", { className: "sdk-toast", role: "status" }, props.message || "");
  }

  function fetchJSON(url, opts) {
    return fetch(url, opts).then(function (res) {
      return res.text().then(function (text) {
        var body = {};
        if (text) {
          try { body = JSON.parse(text); }
          catch (e) { body = { detail: text }; }
        }
        if (!res.ok) {
          var detail = body && body.detail;
          if (detail && typeof detail !== "string") detail = JSON.stringify(detail);
          throw new Error(detail || res.statusText || "request failed");
        }
        return body;
      });
    });
  }

  function render(comp) {
    hook.comp = comp;
    hook.index = 0;
    var vnode = comp.fn({});
    hook.comp = null;
    hook.index = 0;
    comp.root.textContent = "";
    var dom = materialize(vnode);
    if (dom) comp.root.appendChild(dom);
    runEffects(comp);
  }

  window.__HERMES_PLUGIN_SDK__ = {
    React: { createElement: createElement },
    hooks: { useState: useState, useEffect: useEffect, useCallback: useCallback, useToast: useToast },
    components: {
      Badge: Badge, Button: Button, Input: Input, Toast: Toast,
      Dialog: Dialog, DialogContent: DialogContent, DialogHeader: DialogHeader,
      DialogTitle: DialogTitle, DialogDescription: DialogDescription, DialogFooter: DialogFooter,
    },
    utils: { cn: function () { return Array.prototype.filter.call(arguments, Boolean).join(" "); } },
    fetchJSON: fetchJSON,
  };
  window.__HERMES_PLUGINS__ = {
    register: function (name, comp) { window.__ONEIROS_PAGE__ = comp; },
  };
  window.__oneirosMount = function (root) {
    if (!window.__ONEIROS_PAGE__) throw new Error("Oneiros page did not register");
    render({ fn: window.__ONEIROS_PAGE__, hooks: [], queued: false, root: root });
  };
})();
