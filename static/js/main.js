document.addEventListener('DOMContentLoaded', function() {
    const sidebar = document.getElementById('sidebar');
    const mainContent = document.getElementById('mainContent');
    const hamburger = document.getElementById('hamburger');
    const sidebarClose = document.getElementById('sidebarClose');
    const sidebarOverlay = document.getElementById('sidebarOverlay');
    const globalSearch = document.getElementById('globalSearch');
    const fullscreenButton = document.getElementById('fullscreenButton');

    if (hamburger) {
        hamburger.addEventListener('click', function() {
            sidebar.classList.add('open');
            sidebarOverlay.classList.add('show');
            document.body.style.overflow = 'hidden';
        });
    }

    function closeSidebar() {
        sidebar.classList.remove('open');
        sidebarOverlay.classList.remove('show');
        document.body.style.overflow = '';
    }

    if (sidebarClose) sidebarClose.addEventListener('click', closeSidebar);
    if (sidebarOverlay) sidebarOverlay.addEventListener('click', closeSidebar);

    if (globalSearch) {
        globalSearch.addEventListener('input', function () {
            const table = document.querySelector('.data-table');
            if (table) {
                searchTable(this, table.id);
                return;
            }

            const query = this.value.trim().toLowerCase();
            document.querySelectorAll('.product-card, .tracking-card').forEach(card => {
                card.style.display = card.textContent.toLowerCase().includes(query) ? '' : 'none';
            });
        });
    }

    if (fullscreenButton) {
        fullscreenButton.addEventListener('click', function () {
            if (document.fullscreenElement) {
                document.exitFullscreen();
            } else if (document.documentElement.requestFullscreen) {
                document.documentElement.requestFullscreen();
            }
        });
    }

    document.querySelectorAll('.submenu-item').forEach(item => {
        item.addEventListener('click', closeSidebar);
    });

    document.addEventListener('click', function(e) {
        document.querySelectorAll('.dropdown-menu.show').forEach(menu => {
            if (!menu.parentElement.contains(e.target)) {
                menu.classList.remove('show');
            }
        });
    });
});

function toggleSubmenu(el) {
    const parent = el.closest('.nav-item');
    const submenu = parent.querySelector('.submenu');
    const isOpen = submenu.classList.contains('open');

    document.querySelectorAll('.submenu.open').forEach(s => {
        s.classList.remove('open');
        s.closest('.nav-item').querySelector('.submenu-arrow').style.transform = '';
    });

    if (!isOpen) {
        submenu.classList.add('open');
        el.querySelector('.submenu-arrow').style.transform = 'rotate(180deg)';
    }
}

function toggleDropdown(event, btn) {
    event.stopPropagation();
    const menu = btn.closest('.action-dropdown').querySelector('.dropdown-menu');
    const wasOpen = menu.classList.contains('show');
    document.querySelectorAll('.dropdown-menu.show').forEach(m => m.classList.remove('show'));
    if (!wasOpen) menu.classList.add('show');
}

function toggleSwitch(el) {
    el.classList.toggle('active');
}

/* ------------------------------------------------------------------
 * Shared table search (SaviNutrition)
 *
 * Every list page used to filter with a raw
 * `row.textContent.toLowerCase().includes(query)` test, which meant:
 *   - typing "2000" did NOT find a row whose stock renders as "2,000"
 *     (thousands separators broke every numeric search),
 *   - action button labels ("Edit Stock", "Delete", "View stock") were
 *     part of the haystack, so typing those words matched every row,
 *   - only a single word ever matched (typing "maize bran" found nothing),
 *   - a search with zero hits left a blank area with no explanation.
 *
 * saviFilterRows() fixes all of that and is used by searchTable(),
 * filterMaterials() and the per-page inline search handlers.
 * ------------------------------------------------------------------ */

// Action cells (buttons AND the anchor-based "View stock"/"View details"
// links) are UI, not data, so they must not match a search. Everything the
// user can actually read as data stays in the haystack.
const SAVI_ACTION_SELECTOR = 'button, input, select, textarea, a.btn, a.ac-btn, .action-btn, .action-dropdown, .action-cell, .pagination, .dropdown-menu';

// Lowercase text with thousands separators removed so "2,000" and "2000"
// are the same thing. Separators between words stay as single spaces.
function saviNormalizeText(text) {
    return (text || '')
        .replace(/\u00a0/g, ' ')
        .replace(/(\d)[,](?=\d)/g, '$1')
        .replace(/[,\/\\|]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .toLowerCase();
}

// Second, separator-free view used for number matching: "2,000 kg" becomes
// "2000kg", so typing 2000 or 2,000 finds it regardless of the cell format.
function saviCompactText(text) {
    return (text || '').replace(/[^0-9a-z\u00c0-\u024f]+/gi, '');
}

// Normalised, comparable text for one row.
function saviRowHaystack(row) {
    const clone = row.cloneNode(true);
    clone.querySelectorAll(SAVI_ACTION_SELECTOR).forEach(node => node.remove());
    return saviNormalizeText(clone.textContent);
}

// "maize bran 2,000" -> ["maize","bran","2000"] (every term must match).
function saviSearchTerms(query) {
    return saviNormalizeText(query).split(' ').filter(Boolean);
}

function saviRowMatches(row, terms) {
    if (!terms.length) return true;
    const haystack = saviRowHaystack(row);
    const compact = saviCompactText(haystack);
    return terms.every(term => {
        if (haystack.indexOf(term) !== -1) return true;
        const compactTerm = saviCompactText(term);
        return !!compactTerm && compact.indexOf(compactTerm) !== -1;
    });
}

// When nothing matches, say so instead of showing an unexplained blank table.
function saviNoMatchRow(scope, visible, total) {
    const old = scope.querySelector('.savi-no-match');
    if (visible > 0 || total === 0) {
        if (old) old.remove();
        return;
    }
    if (old) return;

    let row;
    if (scope.matches('table')) {
        row = document.createElement('tr');
        row.className = 'savi-no-match';
        const cell = document.createElement('td');
        cell.colSpan = Math.max(1, scope.querySelectorAll('thead th').length);
        cell.style.cssText = 'text-align:center;padding:18px;color:var(--text-light, #8a938d);font-size:13px;';
        cell.textContent = 'No matching records found.';
        row.appendChild(cell);
        const body = scope.querySelector('tbody');
        if (body) body.appendChild(row);
    } else {
        row = document.createElement('div');
        row.className = 'savi-no-match';
        row.style.cssText = 'padding:18px;text-align:center;color:var(--text-light, #8a938d);font-size:13px;';
        row.textContent = 'No matching records found.';
        scope.appendChild(row);
    }
}

// Live "Showing X of Y" feedback under the search box.
function saviUpdateCount(input, visible, total) {
    const box = input.closest('.filter-input') || input.parentElement;
    if (!box) return;
    let counter = box.parentElement ? box.parentElement.querySelector('.savi-search-count') : null;
    if (!counter) {
        counter = document.createElement('span');
        counter.className = 'savi-search-count';
        counter.style.cssText = 'font-size:11.5px;color:var(--text-medium, #66756c);white-space:nowrap;';
        if (box.parentElement) box.parentElement.appendChild(counter);
    }
    counter.textContent = 'Showing ' + visible + ' of ' + total;
}

// Core filter: hide non-matching rows, show the empty-state row and the count.
function saviFilterRows(rows, query) {
    const list = Array.prototype.slice.call(rows).filter(row => !row.classList.contains('savi-no-match'));
    const terms = saviSearchTerms(query);
    let visible = 0;
    list.forEach(row => {
        const match = saviRowMatches(row, terms);
        row.style.display = match ? '' : 'none';
        if (match) visible++;
    });
    return { visible: visible, total: list.length };
}

function saviApplyFilter(scope, rows, query, input) {
    const result = saviFilterRows(rows, query);
    saviNoMatchRow(scope, result.visible, result.total);
    if (input) saviUpdateCount(input, result.visible, result.total);
    return result;
}

function searchTable(inputOrId, targetId) {
    const input = typeof inputOrId === 'string'
        ? document.getElementById(inputOrId)
        : inputOrId;
    const target = document.getElementById(targetId);
    if (!input || !target) return;

    const rows = target.matches('table')
        ? target.querySelectorAll('tbody tr')
        : target.querySelectorAll('.tracking-card, .data-row, tbody tr');

    saviApplyFilter(target, rows, input.value, input);
}

// Used by the per-page inline handlers (Resource Maintenance, Reports) so
// they behave exactly like the shared search. Both arguments accept an
// element or an id.
function saviSearchRows(targetOrId, inputOrId) {
    const target = (typeof targetOrId === 'string') ? document.getElementById(targetOrId) : targetOrId;
    const input = (typeof inputOrId === 'string') ? document.getElementById(inputOrId) : inputOrId;
    if (!target || !input) return;
    const rows = target.matches('table')
        ? target.querySelectorAll('tbody tr')
        : target.querySelectorAll('.tracking-card, .data-row, tbody tr');
    saviApplyFilter(target, rows, input.value, input);
}

function filterMaterials() {
    const table = document.getElementById('materialsTable');
    if (!table) return;

    const input = document.getElementById('searchMaterials');
    const status = document.getElementById('materialStatusFilter')?.value || 'all';
    const rows = Array.prototype.slice.call(table.querySelectorAll('tbody tr'))
        .filter(row => !row.classList.contains('savi-no-match'));

    const terms = saviSearchTerms(input ? input.value : '');
    let visible = 0;
    rows.forEach(row => {
        const rowStatus = (row.querySelector('.badge')?.textContent || '').trim();
        const matchesStatus = status === 'all' || rowStatus.toLowerCase().indexOf(status.toLowerCase()) !== -1;
        const matchesSearch = saviRowMatches(row, terms);
        const match = matchesStatus && matchesSearch;
        row.style.display = match ? '' : 'none';
        if (match) visible++;
    });

    saviNoMatchRow(table, visible, rows.length);
    if (input) saviUpdateCount(input, visible, rows.length);
}

function filterMaterialStatus(status) {
    const select = document.getElementById('materialStatusFilter');
    if (select) select.value = status;
    filterMaterials();
}

function saviExportActionCols(table) {
    /* Column indexes whose header is an action/operation column. They are
       left out of exports so the downloaded sheet contains only data. */
    const skip = new Set();
    const headerRow = table.querySelector('thead tr') || table.querySelector('tr');
    if (!headerRow) return skip;
    headerRow.querySelectorAll('th').forEach((th, i) => {
        const label = th.textContent.trim().toLowerCase().replace(/[^a-z]/g, '');
        if (label === 'action' || label === 'actions' || label === 'operation'
            || label === 'operations' || label === 'remove') skip.add(i);
    });
    return skip;
}

function exportTable(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) { alert('Nothing to export: table not found (' + tableId + ').'); return; }
    const skipCols = saviExportActionCols(table);
    let csv = [];
    const rows = table.querySelectorAll('tr');
    rows.forEach(row => {
        const cols = row.querySelectorAll('td, th');
        const rowData = [];
        cols.forEach((col, i) => {
            if (skipCols.has(i)) return;
            rowData.push('"' + col.textContent.trim().replace(/"/g, '""') + '"');
        });
        csv.push(rowData.join(','));
    });
    const blob = new Blob(['\uFEFF' + csv.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename + '.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => window.URL.revokeObjectURL(url), 8000);
}

function addProduction() {
    const product = document.getElementById('prodProduct').value;
    const quantity = document.getElementById('prodQuantity').value;
    const batch = document.getElementById('prodBatch').value;
    const notes = document.getElementById('prodNotes').value;

    if (!product || !quantity || !batch) {
        alert('Please fill in all required fields');
        return;
    }

    fetch('/api/production', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ product: product, quantity: parseInt(quantity), batch: batch, notes: notes })
    })
    .then(async res => {
        const data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || 'Unable to save production entry');
        return data;
    })
    .then(data => {
        if (data.success) {
            alert('Production entry added successfully!');
            document.getElementById('prodForm').reset();
            location.reload();
        }
    })
    .catch(err => alert(err.message || 'Error adding production entry'));
}

function addRawMaterial() {
    const name = document.getElementById('matName').value;
    const unit = document.getElementById('matUnit').value;
    const opening = document.getElementById('matOpening').value;
    const minStock = document.getElementById('matMinStock').value;

    if (!name || !opening || !minStock) {
        alert('Please fill in all required fields');
        return;
    }

    fetch('/api/raw-materials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, unit: unit, opening: parseInt(opening), min_stock: parseInt(minStock) })
    })
    .then(async res => {
        const data = await res.json();
        if (!res.ok || !data.success) throw new Error(data.error || 'Unable to add raw material');
        return data;
    })
    .then(data => {
        if (data.success) {
            alert('Raw material added successfully!');
            document.getElementById('addMaterialForm').reset();
            location.reload();
        }
    })
    .catch(err => alert(err.message || 'Error adding raw material'));
}

function openAddMaterialModal() {
    document.getElementById('addMaterialModal').style.display = 'flex';
}

function openModal(id) {
    document.getElementById(id).style.display = 'flex';
    document.body.classList.add('modal-open');
}
function closeModal(id) {
    document.getElementById(id).style.display = 'none';
    if (!document.querySelector('.modal-overlay[style*="display: flex"], .modal-overlay[style*="display:block"]')) {
        document.body.classList.remove('modal-open');
    }
}

document.addEventListener('click', function(e) {
    if (e.target.classList.contains('modal-overlay')) {
        e.target.style.display = 'none';
        if (!document.querySelector('.modal-overlay[style*="display: flex"], .modal-overlay[style*="display:block"]')) {
            document.body.classList.remove('modal-open');
        }
    }
});

let refreshInterval;
function startAutoRefresh() {
    stopAutoRefresh();
    refreshInterval = setInterval(() => location.reload(), 30000);
}

function stopAutoRefresh() {
    clearInterval(refreshInterval);
    refreshInterval = null;
}


/* ---- Change #61: input constraints shared by admin + user portals ----
   data-con="phone"   -> digits only, exactly 10 max
   data-con="name"    -> first letter of each word caps, rest small (blur)
   data-con="invoice" -> uppercase, auto-hyphen every 4 chars (INV1024 -> INV-1024)
   data-con="vehicle" -> uppercase, auto-hyphen after every 2 chars (KA01AB1234 -> KA-01-AB-1234) */
function saviTitleCaseName(str) {
    return String(str || '').toLowerCase().replace(/(^|[\s\-'])([a-z])/g, function (m, p, c) {
        return p + c.toUpperCase();
    });
}
function saviFormatNameInput(el, live) {
    var v = el.value;
    if (live) {
        if (/^[a-z]/.test(v)) {
            var p = el.selectionStart;
            el.value = v.charAt(0).toUpperCase() + v.slice(1);
            try { el.setSelectionRange(p, p); } catch (e) { }
        }
        return;
    }
    var t = saviTitleCaseName(v.replace(/\s+/g, ' ').trim());
    if (t !== v) el.value = t;
}
function saviDigitsOnly(el, max) {
    var v = el.value.replace(/[^0-9]/g, '');
    if (max && v.length > max) v = v.slice(0, max);
    if (v !== el.value) el.value = v;
}
function saviFormatInvoice(el) {
    var raw = el.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 48);
    var m = raw.match(/^([A-Z]+)(.*)$/);
    var groups;
    if (m && m[2]) {
        groups = [m[1]].concat(m[2].match(/.{1,4}/g) || []);
    } else {
        groups = raw.match(/.{1,4}/g) || [];
    }
    var out = groups.join('-');
    if (out !== el.value) el.value = out;
}
function saviFormatVehicle(el) {
    var raw = el.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 10);
    var out = raw;
    if (raw.length > 2) out = raw.slice(0, 2) + '-' + raw.slice(2);
    if (raw.length > 4) out = raw.slice(0, 2) + '-' + raw.slice(2, 4) + '-' + raw.slice(4);
    if (raw.length > 6) out = raw.slice(0, 2) + '-' + raw.slice(2, 4) + '-' + raw.slice(4, 6) + '-' + raw.slice(6);
    if (out !== el.value) el.value = out;
}
function saviBindConstraints(el) {
    if (!el || el.dataset.saviCon) return;
    el.dataset.saviCon = '1';
    var kind = el.dataset.con || '';
    if (kind === 'phone') {
        el.setAttribute('inputmode', 'numeric');
        el.setAttribute('maxlength', '10');
        el.addEventListener('input', function () { saviDigitsOnly(el, 10); });
    } else if (kind === 'name') {
        el.addEventListener('input', function () { saviFormatNameInput(el, true); });
        el.addEventListener('blur', function () { saviFormatNameInput(el, false); });
    } else if (kind === 'invoice') {
        el.addEventListener('input', function () { saviFormatInvoice(el); });
    } else if (kind === 'vehicle') {
        el.addEventListener('input', function () { saviFormatVehicle(el); });
    }
}
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-con]').forEach(saviBindConstraints);
});
