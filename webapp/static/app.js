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
