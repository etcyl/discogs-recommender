// Household view: refresh who's playing what, and copy someone's likes into a
// channel of your own.

const HOUSEHOLD_POLL_MS = 30000;

async function playTheirLikes(button) {
    const userId = button.dataset.playLikes;
    const name = button.dataset.name || 'their';
    const original = button.textContent;

    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = 'Building…';

    try {
        const resp = await fetch(`/household/${encodeURIComponent(userId)}/play-likes`, {
            method: 'POST',
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        button.textContent = `Opening ${data.tracks} songs…`;
        window.location.href = `/radio?channel=${encodeURIComponent(data.channel_id)}`;
    } catch (err) {
        button.textContent = err.message || 'Could not build that playlist';
        setTimeout(() => {
            button.textContent = original;
            button.disabled = false;
        }, 3500);
    } finally {
        button.removeAttribute('aria-busy');
    }
}

document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-play-likes]');
    if (btn) playTheirLikes(btn);
});

// Keep the "listening now" state fresh without a full page reload.
async function refreshHousehold() {
    const grid = document.getElementById('household-grid');
    if (!grid || document.hidden) return;

    let people;
    try {
        const resp = await fetch('/api/household');
        if (!resp.ok) return;
        people = (await resp.json()).people || [];
    } catch (e) {
        return;   // transient; the next tick can try again
    }

    for (const person of people) {
        const card = grid.querySelector(`[data-user-id="${CSS.escape(person.id)}"]`);
        if (!card) continue;

        const head = card.querySelector('.person-head');
        const existing = head.querySelector('.person-live');
        const isLive = !!(person.track && person.track.live);
        if (isLive && !existing) {
            const badge = document.createElement('span');
            badge.className = 'person-live';
            badge.innerHTML = '<span class="live-dot"></span>listening';
            head.appendChild(badge);
        } else if (!isLive && existing) {
            existing.remove();
        }

        if (!person.track) continue;
        const title = card.querySelector('.person-title');
        const artist = card.querySelector('.person-artist');
        const when = card.querySelector('.person-when');
        if (title) title.textContent = person.track.title || '';
        if (artist) artist.textContent = person.track.artist || '';
        if (when) when.textContent = isLive ? 'now playing' : 'last played';
    }
}

if (document.getElementById('household-grid')) {
    setInterval(refreshHousehold, HOUSEHOLD_POLL_MS);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshHousehold();
    });
}
