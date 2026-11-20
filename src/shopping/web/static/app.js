// Progressive enhancement: forms submit in the background and the page updates in place, and items
// being added to the cart in the background update their status as they land. Without JS everything
// still works as plain forms and redirects.
(() => {
  "use strict";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function toast(message, ok = true) {
    const box = document.getElementById("toasts");
    if (!box || !message) return;
    const el = document.createElement("div");
    el.className = "toast " + (ok ? "ok" : "err");
    el.textContent = message;
    el.addEventListener("click", () => el.remove());
    box.appendChild(el);
    setTimeout(() => el.remove(), ok ? 5000 : 12000);
  }

  // URL without the one-off flash parameters, so reloading doesn't repeat the message.
  function cleanUrl(url) {
    const u = new URL(url, location.href);
    u.searchParams.delete("msg");
    u.searchParams.delete("err");
    return u.pathname + (u.search || "") + u.hash;
  }

  // Something typed or ticked since the last update: don't refresh the page under the user.
  let dirty = false;
  document.addEventListener("input", () => (dirty = true));

  async function refreshPage() {
    if (dirty) return;
    const y = window.scrollY;
    const resp = await fetch(location.href, { credentials: "same-origin" });
    if (!resp.ok || dirty) return;
    swapFrom(new DOMParser().parseFromString(await resp.text(), "text/html"));
    window.scrollTo(0, y);
  }

  function swapFrom(doc) {
    dirty = false;
    for (const sel of ["main", "header.top"]) {
      const fresh = doc.querySelector(sel);
      const old = document.querySelector(sel);
      if (fresh && old) old.replaceWith(fresh);
    }
    if (doc.title) document.title = doc.title;
  }

  async function submitInPlace(form, submitter) {
    const data = new FormData(form);
    if (submitter && submitter.name) data.append(submitter.name, submitter.value);
    const body = new URLSearchParams();
    for (const [k, v] of data) body.append(k, v);
    const buttons = form.querySelectorAll("button");
    buttons.forEach((b) => (b.disabled = true));
    form.classList.add("busy");
    const fromPath = location.pathname;
    const y = window.scrollY;
    try {
      const resp = await fetch(form.action, { method: "POST", body, credentials: "same-origin" });
      if (!resp.ok) throw new Error(`The server returned ${resp.status}.`);
      const doc = new DOMParser().parseFromString(await resp.text(), "text/html");
      if (!doc.querySelector("main")) { location.href = resp.url; return; }
      swapFrom(doc);
      const target = new URL(resp.url);
      if (target.pathname === fromPath) {
        history.replaceState(null, "", cleanUrl(resp.url));
        window.scrollTo(0, y);
      } else {
        history.pushState(null, "", cleanUrl(resp.url));
        window.scrollTo(0, 0);
      }
      watchCart();
    } catch (exc) {
      toast(`That didn't go through: ${exc.message} Try again.`, false);
      buttons.forEach((b) => (b.disabled = false));
      form.classList.remove("busy");
    }
  }

  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post" || "sync" in form.dataset) return;
    ev.preventDefault();
    submitInPlace(form, ev.submitter);
  });

  // Back/forward after in-place navigation: load the page normally.
  window.addEventListener("popstate", () => location.reload());

  // -- background cart updates -------------------------------------------------------------
  let watching = false;

  async function watchCart() {
    const pill = document.getElementById("cart-activity");
    if (watching || !pill || pill.dataset.pending === "0") return;
    watching = true;
    let last = Number(pill.dataset.lastEvent || 0);
    let landed = false;
    try {
      for (;;) {
        const ids = [...document.querySelectorAll("[data-adding]")].map((el) => el.dataset.adding);
        const resp = await fetch(`/cart/queue.json?after=${last}&ids=${encodeURIComponent(ids.join(","))}`);
        if (!resp.ok) break;
        const q = await resp.json();
        q.events.forEach((e) => toast(e.message, e.ok));
        last = q.last_event_id;
        const pending = new Set(q.pending_ids);
        for (const el of document.querySelectorAll("[data-adding]")) {
          const id = el.dataset.adding;
          const st = q.statuses[id];
          if (pending.has(id) || !st) continue;
          landed = true;
          el.textContent = st.label;
          el.className = "status " + st.status;
          el.removeAttribute("data-adding");
          if (st.status === "failed" && st.error) el.title = st.error;
        }
        const p = document.getElementById("cart-activity");
        if (p) {
          p.dataset.pending = String(q.pending);
          p.dataset.lastEvent = String(last);
          p.textContent = `🛒 Adding ${q.pending}…`;
          p.hidden = q.pending === 0;
        }
        if (q.pending === 0) {
          // Totals and counts on a run page are now out of date: refresh them (not mid-triage).
          if (landed && /^\/runs\/[^/]+$/.test(location.pathname)) await refreshPage();
          break;
        }
        await sleep(1500);
      }
    } catch (_) {
      // network hiccup: the labels will be right on the next page load
    } finally {
      watching = false;
    }
  }

  document.addEventListener("DOMContentLoaded", watchCart);
})();
