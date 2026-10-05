function toast(msg, ok) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'fixed top-4 right-4 z-50 max-w-sm rounded-lg px-4 py-3 text-sm font-medium shadow-lg ' +
    (ok ? 'bg-emerald-600 text-white' : 'bg-red-600 text-white');
  el.classList.remove('hidden');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.add('hidden'), 3500);
}

async function postJSON(url, data) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {}),
  });
  let body = {};
  try { body = await res.json(); } catch (e) { /* noop */ }
  if (!res.ok) {
    toast(body.error || 'Something went wrong', false);
    throw new Error(body.error || 'request failed');
  }
  return body;
}

function toggleRow(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('hidden');
}

// Generic click-to-sort for plain data tables: mark a <th> with
// data-sortable, and (optionally) give its column's <td> a data-sort="raw
// value" attribute when the visible text isn't directly sortable (currency
// symbols, commas, arrows). Rows are assumed one <tr> per record - not used
// on tables with paired detail rows (e.g. Pending Actions' review panels).
function initSortableTable(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.querySelector('tbody');
  const headers = Array.from(table.querySelectorAll('thead th'));

  const cellValue = (row, idx) => {
    const cell = row.children[idx];
    if (!cell) return '';
    return cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent.trim();
  };

  headers.forEach((th, idx) => {
    if (!th.hasAttribute('data-sortable')) return;
    th.style.cursor = 'pointer';
    th.classList.add('select-none');
    const label = document.createElement('span');
    label.className = 'sort-arrow ml-1 inline-block';
    label.style.opacity = '.35';
    label.textContent = '↕';
    th.appendChild(label);

    th.addEventListener('click', () => {
      const asc = th.dataset.dir !== 'asc';
      th.dataset.dir = asc ? 'asc' : 'desc';
      headers.forEach(h => { if (h !== th) delete h.dataset.dir; });
      table.querySelectorAll('.sort-arrow').forEach(a => { a.textContent = '↕'; a.style.opacity = '.35'; });
      label.textContent = asc ? '↑' : '↓';
      label.style.opacity = '1';

      const rows = Array.from(tbody.querySelectorAll(':scope > tr'));
      rows.sort((a, b) => {
        const v1 = cellValue(asc ? a : b, idx);
        const v2 = cellValue(asc ? b : a, idx);
        const n1 = parseFloat(v1.replace(/[^0-9.\-]/g, ''));
        const n2 = parseFloat(v2.replace(/[^0-9.\-]/g, ''));
        if (v1 !== '' && v2 !== '' && !isNaN(n1) && !isNaN(n2)) return n1 - n2;
        return v1.localeCompare(v2);
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
}

// --- Soft navigation -------------------------------------------------
// Swaps #app-shell's HTML for a fetched page's #app-shell instead of doing
// a real browser navigation, so filter changes (day/week/month, cutover
// date, verdict, lookback, action date, ...) feel instant: no white flash,
// no re-fetching fonts/Tailwind, sidebar stays put. Any <a data-soft> or
// <form data-soft> is intercepted automatically, including ones added by a
// later page swap - the listeners are delegated on `document`, not bound
// per-element. Links/forms that must cause a real navigation (CSV
// downloads, sign out, login) simply don't carry `data-soft`.
let __softNavAbort = null;

function setSoftLoading(on) {
  const bar = document.getElementById('soft-progress');
  if (!bar) return;
  if (on) {
    bar.style.transition = 'none';
    bar.style.width = '0%';
    bar.style.opacity = '1';
    requestAnimationFrame(() => {
      bar.style.transition = 'width 1.2s ease';
      bar.style.width = '80%';
    });
  } else {
    bar.style.transition = 'width .2s ease';
    bar.style.width = '100%';
    setTimeout(() => { bar.style.opacity = '0'; bar.style.width = '0%'; }, 250);
  }
}

function runPageScripts(container) {
  if (!container) return;
  container.querySelectorAll('script').forEach(old => {
    const s = document.createElement('script');
    Array.from(old.attributes).forEach(a => s.setAttribute(a.name, a.value));
    if (!old.src) s.textContent = old.textContent;
    document.body.appendChild(s);
  });
}

async function softNavigate(url, { pushState = true } = {}) {
  if (__softNavAbort) __softNavAbort.abort();
  const controller = new AbortController();
  __softNavAbort = controller;
  setSoftLoading(true);
  try {
    const res = await fetch(url, { signal: controller.signal, credentials: 'same-origin' });
    if (!res.ok) { window.location.href = url; return; }
    const html = await res.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const newShell = doc.getElementById('app-shell');
    if (!newShell) { window.location.href = url; return; } // e.g. bounced to /login
    document.getElementById('app-shell').replaceWith(newShell);
    document.title = doc.title;
    runPageScripts(doc.getElementById('page-scripts'));
    if (pushState) history.pushState({ soft: true }, '', url);
    window.scrollTo(0, 0);
  } catch (e) {
    if (e.name !== 'AbortError') window.location.href = url;
    return;
  } finally {
    setSoftLoading(false);
  }
}

document.addEventListener('click', (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest('a[data-soft]');
  if (!a) return;
  e.preventDefault();
  softNavigate(a.getAttribute('href'));
});

document.addEventListener('submit', (e) => {
  const form = e.target.closest('form[data-soft]');
  if (!form) return;
  e.preventDefault();
  // e.submitter carries the name/value of whichever <button type="submit">
  // was actually clicked (e.g. the Day/Week/Month granularity buttons) -
  // plain `new FormData(form)` silently drops that unless the submitter is
  // passed in explicitly.
  const formData = e.submitter ? new FormData(form, e.submitter) : new FormData(form);
  const params = new URLSearchParams(formData).toString();
  const action = form.getAttribute('action') || location.pathname;
  softNavigate(action + (params ? '?' + params : ''));
});

window.addEventListener('popstate', () => softNavigate(location.href, { pushState: false }));
