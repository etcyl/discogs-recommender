// ---------------------------------------------------------------------------
// Theme toggle
// ---------------------------------------------------------------------------

(function initTheme() {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', () => {
        const root = document.documentElement;
        const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
        root.setAttribute('data-theme', next);
        try { localStorage.setItem('theme', next); } catch (e) { /* private mode */ }
    });
})();

// ---------------------------------------------------------------------------
// Collection refresh
// ---------------------------------------------------------------------------

async function refreshCollection() {
    const btn = document.getElementById('refresh-btn');
    if (!btn) return;

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = 'Refreshing…';

    try {
        const response = await fetch('/api/refresh-collection');
        const data = await response.json();
        if (data.status === 'ok') {
            window.location.reload();
            return;
        }
    } catch (err) { /* fall through to the error state */ }

    btn.removeAttribute('aria-busy');
    btn.textContent = 'Error — try again';
    btn.disabled = false;
    setTimeout(() => { btn.textContent = originalText; }, 4000);
}

// ---------------------------------------------------------------------------
// Setup notices
//
// These are advisory ("no Discogs token", "Ollama isn't running"). They used
// to render as full-width banners injected at the top of <main> after the page
// had painted, which shoved the real content down the screen on every load.
// Now they collapse into one compact bar that expands on click.
// ---------------------------------------------------------------------------

(async function checkSystemStatus() {
    const container = document.getElementById('system-alerts');
    if (!container) return;

    let dismissed;
    try {
        dismissed = (id) => Boolean(localStorage.getItem('dismissed_' + id));
    } catch (e) {
        dismissed = () => false;   // private mode — show everything
    }

    let bar = null;
    let listInner = null;

    function ensureShell() {
        if (bar) return;

        bar = document.createElement('button');
        bar.type = 'button';
        bar.className = 'setup-notice__bar';
        bar.setAttribute('aria-expanded', 'false');
        bar.innerHTML =
            '<span class="setup-notice__dot"></span>' +
            '<span class="setup-notice__label">Setup suggestions</span>' +
            '<span class="setup-notice__count">0</span>' +
            '<svg class="setup-notice__chevron" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">' +
            '<path fill="currentColor" d="M7.4 8.6 12 13.2l4.6-4.6L18 10l-6 6-6-6z"/></svg>';

        const list = document.createElement('div');
        list.className = 'setup-notice__list';
        listInner = document.createElement('div');
        list.appendChild(listInner);

        bar.addEventListener('click', () => {
            const open = container.getAttribute('data-open') === 'true';
            container.setAttribute('data-open', String(!open));
            bar.setAttribute('aria-expanded', String(!open));
        });

        container.appendChild(bar);
        container.appendChild(list);
    }

    function refreshShell() {
        if (!bar) return;
        const items = listInner.querySelectorAll('.setup-notice__item');
        if (!items.length) {
            container.replaceChildren();
            container.removeAttribute('data-open');
            bar = null;
            listInner = null;
            return;
        }
        bar.querySelector('.setup-notice__count').textContent = String(items.length);
        bar.querySelector('.setup-notice__label').textContent =
            items.length === 1 ? 'Setup suggestion' : 'Setup suggestions';
        const warning = listInner.querySelector('.setup-notice__item--warning');
        bar.querySelector('.setup-notice__dot').style.background =
            warning ? 'var(--warn)' : 'var(--info)';
    }

    // Renders one notice straight away. Safe to call late (the hardware probe
    // resolves seconds after the status call) — the shell is created on demand.
    function addRendered(id, type, html) {
        if (dismissed(id)) return;
        ensureShell();
        const item = document.createElement('div');
        item.className = 'setup-notice__item setup-notice__item--' + type;
        item.innerHTML =
            '<p>' + html + '</p>' +
            '<button class="setup-notice__dismiss" type="button" aria-label="Dismiss" ' +
            'data-alert-id="' + id + '">&times;</button>';
        listInner.appendChild(item);
        refreshShell();
    }

    container.addEventListener('click', (e) => {
        const btn = e.target.closest('.setup-notice__dismiss');
        if (!btn) return;
        e.stopPropagation();
        try { localStorage.setItem('dismissed_' + btn.dataset.alertId, '1'); } catch (err) { /* ignore */ }
        btn.closest('.setup-notice__item').remove();
        refreshShell();
    });

    let status;
    try {
        const resp = await fetch('/api/system/status');
        if (!resp.ok) return;
        status = await resp.json();
    } catch (e) {
        return; // status check is optional — never break the page over it
    }

    if (!status.discogs_configured) {
        addRendered('alert-discogs', 'info',
            'No Discogs account connected. Add <code>DISCOGS_TOKEN</code> and ' +
            '<code>DISCOGS_USERNAME</code> to your <code>.env</code> for ' +
            'collection-based features.');
    }

    if (!status.anthropic_configured && !status.ollama_available) {
        addRendered('alert-no-ai', 'warning',
            'No AI service available. Install <a href="https://ollama.com" target="_blank" rel="noopener">Ollama</a> ' +
            '(free, local) or add <code>ANTHROPIC_API_KEY</code> to <code>.env</code>. ' +
            '"Play Playlist" mode still works without AI.');
    } else if (!status.anthropic_configured) {
        addRendered('alert-anthropic', 'info',
            'Using Ollama for AI recommendations (free, local). Add ' +
            '<code>ANTHROPIC_API_KEY</code> to <code>.env</code> for Claude.');
    }

    if (!status.ollama_available && !status.ollama_installed) {
        addRendered('alert-ollama', 'info',
            'Ollama not installed. <a href="https://ollama.com" target="_blank" rel="noopener">Install Ollama</a> ' +
            'for free local AI recommendations.');
    } else if (!status.ollama_available && status.ollama_installed) {
        addRendered('alert-ollama-not-running', 'info',
            'Ollama is installed but not running. Start it with <code>ollama serve</code>.');
    }

    // Hardware detection shells out to probe the GPU and can take several
    // seconds. Deliberately not awaited — the notices above must not wait on it.
    if (status.ollama_available) {
        fetch('/api/system/hardware')
            .then((r) => (r.ok ? r.json() : null))
            .then((hw) => {
                if (!hw || !Array.isArray(hw.warnings)) return;
                for (const w of hw.warnings) {
                    if (w.includes('Ollama is not running')) continue; // already covered
                    addRendered('alert-hw-' + w.substring(0, 20).replace(/\W/g, ''), 'info', w);
                }
            })
            .catch(() => { /* hardware check is optional */ });
    }
})();
