async function refreshCollection() {
    const btn = document.getElementById('refresh-btn');
    if (!btn) return;

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = 'Refreshing... <span class="loading-spinner"></span>';

    try {
        const response = await fetch('/api/refresh-collection');
        const data = await response.json();
        if (data.status === 'ok') {
            window.location.reload();
        } else {
            btn.textContent = 'Error - try again';
            btn.disabled = false;
        }
    } catch (err) {
        btn.textContent = 'Error - try again';
        btn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// System Alerts — dismissible banners for missing API keys / services
// ---------------------------------------------------------------------------

(async function checkSystemStatus() {
    const container = document.getElementById('system-alerts');
    if (!container) return;

    try {
        const resp = await fetch('/api/system/status');
        if (!resp.ok) return;
        const status = await resp.json();

        const alerts = [];

        if (!status.discogs_configured) {
            alerts.push({
                id: 'alert-discogs',
                type: 'info',
                text: 'No Discogs account connected. Add DISCOGS_TOKEN and DISCOGS_USERNAME to your .env file for collection-based features.',
            });
        }

        if (!status.anthropic_configured && !status.ollama_available) {
            alerts.push({
                id: 'alert-no-ai',
                type: 'warning',
                text: 'No AI service available. Install <a href="https://ollama.com" target="_blank">Ollama</a> (free, local) or add ANTHROPIC_API_KEY to .env for AI-powered recommendations. You can still use "Play Playlist" mode without AI.',
            });
        } else if (!status.anthropic_configured) {
            alerts.push({
                id: 'alert-anthropic',
                type: 'info',
                text: 'Using Ollama for AI recommendations (free, local). Add ANTHROPIC_API_KEY to .env for Claude AI.',
            });
        }

        if (!status.ollama_available && !status.ollama_installed) {
            alerts.push({
                id: 'alert-ollama',
                type: 'info',
                text: 'Ollama not installed. <a href="https://ollama.com" target="_blank">Install Ollama</a> for free local AI recommendations.',
            });
        } else if (!status.ollama_available && status.ollama_installed) {
            alerts.push({
                id: 'alert-ollama-not-running',
                type: 'info',
                text: 'Ollama is installed but not running. Start it with <code>ollama serve</code> for local AI.',
            });
        }

        // Check hardware warnings
        try {
            const hwResp = await fetch('/api/system/hardware');
            if (hwResp.ok) {
                const hw = await hwResp.json();
                if (hw.warnings && hw.warnings.length > 0 && status.ollama_available) {
                    for (const w of hw.warnings) {
                        if (w.includes('Ollama is not running')) continue; // Already shown
                        alerts.push({
                            id: 'alert-hw-' + w.substring(0, 20).replace(/\W/g, ''),
                            type: 'info',
                            text: w,
                        });
                    }
                }
            }
        } catch (e) { /* hardware check is optional */ }

        // Render alerts (skip dismissed ones)
        for (const alert of alerts) {
            if (localStorage.getItem('dismissed_' + alert.id)) continue;

            const div = document.createElement('div');
            div.className = 'system-alert system-alert-' + alert.type;
            div.innerHTML = `
                <span class="system-alert-text">${alert.text}</span>
                <button class="system-alert-dismiss" title="Dismiss" data-alert-id="${alert.id}">&times;</button>
            `;
            container.appendChild(div);
        }

        // Dismiss handlers
        container.addEventListener('click', (e) => {
            const btn = e.target.closest('.system-alert-dismiss');
            if (btn) {
                const id = btn.dataset.alertId;
                localStorage.setItem('dismissed_' + id, '1');
                btn.closest('.system-alert').remove();
            }
        });

    } catch (e) {
        // System status check is optional — don't break the app
    }
})();
