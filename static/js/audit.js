// Audit log — lazily loads the songs for a run when asked, so the page itself
// stays light even with a hundred runs recorded.

const STATUS_LABEL = {
    verified: 'Confirmed',
    corrected: 'Confirmed (name differs)',
    unverified: 'Not found',
    skipped: 'Not checked',
};

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function renderItems(container, items) {
    container.replaceChildren();

    if (!items.length) {
        container.appendChild(el('p', 'audit-empty', 'No songs recorded for this run.'));
        return;
    }

    const table = el('table', 'audit-table audit-items-table');
    const thead = el('thead');
    const hrow = el('tr');
    for (const [label, cls] of [['#', 'num'], ['Song', ''], ['Year', 'num'],
                                ['Check', ''], ['Source', ''], ['Outcome', '']]) {
        hrow.appendChild(el('th', cls, label));
    }
    thead.appendChild(hrow);
    table.appendChild(thead);

    const tbody = el('tbody');
    items.forEach((it, i) => {
        const tr = el('tr', it.kept ? '' : 'is-dropped');
        tr.appendChild(el('td', 'num', String(i + 1)));

        const song = el('td');
        song.appendChild(el('div', 'audit-song', `${it.artist} — ${it.title}`));
        if (it.reason) {
            // Labelled as a claim, not a fact: this text is model output and
            // nothing has checked it.
            const reason = el('div', 'audit-reason');
            reason.appendChild(el('span', 'audit-claim-tag', 'model claim'));
            reason.appendChild(document.createTextNode(' ' + it.reason));
            song.appendChild(reason);
        }
        if (it.matched_as && it.verify_status === 'corrected') {
            song.appendChild(el('div', 'audit-matched', `catalogue has: ${it.matched_as}`));
        }
        tr.appendChild(song);

        tr.appendChild(el('td', 'num', it.year || '—'));

        const status = el('td');
        const badge = el('span', 'audit-badge is-' + (it.verify_status || 'skipped'),
                         STATUS_LABEL[it.verify_status] || it.verify_status);
        status.appendChild(badge);
        tr.appendChild(status);

        tr.appendChild(el('td', 'audit-src',
                          it.verify_source
                              ? `${it.verify_source}${it.verify_conf ? ' · ' + it.verify_conf : ''}`
                              : '—'));
        tr.appendChild(el('td', 'audit-outcome',
                          it.kept ? 'played'
                          : it.drop_reason === 'no-playable-match'
                              ? 'no playable match'
                              : it.drop_reason === 'unverified'
                                  ? 'dropped — unconfirmed'
                                  : 'dropped'));
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    const wrap = el('div', 'audit-table-wrap');
    wrap.appendChild(table);
    container.appendChild(wrap);
}

document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.audit-load-btn');
    if (!btn) return;

    const runId = btn.dataset.runId;
    const container = document.getElementById('audit-items-' + runId);
    if (!container) return;

    if (container.dataset.loaded === '1') {
        container.hidden = !container.hidden;
        btn.textContent = container.hidden ? 'Show songs' : 'Hide songs';
        return;
    }

    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = 'Loading…';
    try {
        const resp = await fetch(`/api/audit/runs/${runId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const run = await resp.json();
        renderItems(container, run.items || []);
        container.dataset.loaded = '1';
        container.hidden = false;
        btn.textContent = 'Hide songs';
    } catch (err) {
        container.replaceChildren(
            el('p', 'audit-empty', 'Could not load this run — it may have been pruned.'));
        btn.textContent = 'Show songs';
    } finally {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
    }
});
