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
