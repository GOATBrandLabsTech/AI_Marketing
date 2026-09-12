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
