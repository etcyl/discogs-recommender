// ---------------------------------------------------------------------------
// Radio Mode — YouTube IFrame API + Queue Management + Channels
// ---------------------------------------------------------------------------

let player = null;
let queue = [];
let currentIndex = -1;
let isPlaying = false;
let progressInterval = null;
let isSeeking = false;
let likedSet = new Set();
let dislikedSet = new Set();

// ---- Channel State ----
let activeChannelId = 'my-collection';
let menuTargetChannelId = null;

// ---- YouTube IFrame API ----
const tag = document.createElement('script');
tag.src = 'https://www.youtube.com/iframe_api';
document.head.appendChild(tag);

window.onYouTubeIframeAPIReady = function () {
    player = new YT.Player('yt-player', {
        height: '180',
        width: '320',
        playerVars: {
            autoplay: 0,
            controls: 0,
            disablekb: 1,
            modestbranding: 1,
            rel: 0,
        },
        events: {
            onReady: onPlayerReady,
            onStateChange: onPlayerStateChange,
            onError: onPlayerError,
        },
    });
};

function onPlayerReady() {
    const vol = document.getElementById('volume-slider');
    player.setVolume(parseInt(vol.value));
    loadPlaylist();
}

function onPlayerError(event) {
    const track = queue[currentIndex];
    if (track && track.altVideoIds && track.altVideoIds.length > 0) {
        const nextId = track.altVideoIds.shift();
        console.warn('YouTube error:', event.data, '— trying alt video', nextId);
        track.videoId = nextId;
        player.loadVideoById(nextId);
    } else {
        console.warn('YouTube error:', event.data, '— no alternatives, skipping');
        playNext();
    }
}

function onPlayerStateChange(event) {
    if (event.data === YT.PlayerState.ENDED) {
        playNext();
    } else if (event.data === YT.PlayerState.PLAYING) {
        isPlaying = true;
        showPauseIcon();
        startProgressUpdates();
        startVisualizer();
        startSilentAudio();
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
    } else if (event.data === YT.PlayerState.PAUSED) {
        isPlaying = false;
        showPlayIcon();
        stopProgressUpdates();
        stopSilentAudio();
        if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
    }
}

// ---- Playlist Loading (SSE with progress) ----
function loadPlaylistSSE() {
    showLoading(true);
    const url = `/api/radio/playlist-stream?channel_id=${encodeURIComponent(activeChannelId)}`;
    const es = new EventSource(url);

    es.addEventListener('progress', (e) => {
        const data = JSON.parse(e.data);
        updateLoadingProgress(data.message, data.percent);
    });

    es.addEventListener('complete', (e) => {
        es.close();
        const data = JSON.parse(e.data);
        if (data.playlist && data.playlist.length > 0) {
            queue = data.playlist;
            currentIndex = -1;
            showLoading(false);
            renderQueue();
            playNext();
        } else {
            showError('No songs found. Try a different playlist or add more to your collection.');
        }
    });

    es.addEventListener('error', (e) => {
        try {
            const data = JSON.parse(e.data);
            showError(data.message || 'Failed to load playlist.');
        } catch {
            showError('Connection lost. Try refreshing the page.');
        }
        es.close();
    });
}

function loadPlaylist() {
    loadPlaylistSSE();
}

async function refreshPlaylist() {
    showLoading(true);
    resetLoadingUI();
    await fetch(`/api/radio/refresh-playlist?channel_id=${encodeURIComponent(activeChannelId)}`);
    loadPlaylistSSE();
}

function updateLoadingProgress(message, percent) {
    const msgEl = document.getElementById('loading-message');
    const subEl = document.getElementById('loading-sub');
    const fillEl = document.getElementById('loading-progress-fill');
    const pctEl = document.getElementById('loading-percent');

    if (msgEl) msgEl.textContent = message;
    if (subEl) subEl.textContent = getStepHint(percent);
    if (fillEl) fillEl.style.width = percent + '%';
    if (pctEl) pctEl.textContent = percent + '%';
}

function resetLoadingUI() {
    updateLoadingProgress('Generating your personal radio station...', 0);
}

function getStepHint(percent) {
    if (percent < 15) return 'Connecting to Discogs...';
    if (percent < 25) return 'Building your taste profile...';
    if (percent < 30) return 'AI is picking the perfect songs...';
    if (percent < 95) return 'Matching songs to YouTube...';
    return 'Almost there!';
}

// ---- Channel Switching ----
function switchChannel(channelId) {
    if (channelId === activeChannelId) return;
    activeChannelId = channelId;

    // Update sidebar highlighting
    document.querySelectorAll('.channel-item').forEach(el => {
        el.classList.toggle('channel-active', el.dataset.channelId === channelId);
    });

    // Stop current playback
    if (player && isPlaying) {
        try { player.stopVideo(); } catch (e) {}
    }

    // Reset queue
    queue = [];
    currentIndex = -1;
    renderQueue();

    // Load new channel
    showLoading(true);
    resetLoadingUI();
    loadPlaylistSSE();

    // Close mobile sidebar
    document.getElementById('channel-sidebar')?.classList.remove('sidebar-open');
}

// ---- Channel CRUD ----
function openNewChannelDialog() {
    const dialog = document.getElementById('new-channel-dialog');
    document.getElementById('channel-name-input').value = '';
    const themeInput = document.getElementById('theme-input');
    if (themeInput) themeInput.value = '';
    const spotifyInput = document.getElementById('spotify-url-input');
    if (spotifyInput) spotifyInput.value = '';
    const preview = document.getElementById('playlist-preview');
    if (preview) preview.style.display = 'none';
    const modeRadio = document.querySelector('input[name="channel-mode"][value="similar_songs"]');
    if (modeRadio) modeRadio.checked = true;
    // Default to themed
    const typeRadio = document.querySelector('input[name="channel-type"][value="themed"]');
    if (typeRadio) typeRadio.checked = true;
    toggleChannelTypeFields();
    dialog.showModal();
}

function toggleChannelTypeFields() {
    const selectedType = document.querySelector('input[name="channel-type"]:checked')?.value || 'themed';
    const themedFields = document.getElementById('themed-fields');
    const spotifyFields = document.getElementById('spotify-fields');
    if (themedFields) themedFields.style.display = selectedType === 'themed' ? '' : 'none';
    if (spotifyFields) spotifyFields.style.display = selectedType === 'spotify' ? '' : 'none';
}

async function previewSpotifyPlaylist() {
    const urlInput = document.getElementById('spotify-url-input');
    const url = urlInput.value.trim();
    if (!url || !url.includes('spotify')) return;

    try {
        const resp = await fetch('/api/radio/spotify-preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        if (!resp.ok) return;
        const data = await resp.json();

        document.getElementById('preview-name').textContent = data.name;
        document.getElementById('preview-count').textContent = `${data.track_count} tracks`;
        const img = document.getElementById('preview-image');
        if (data.image_url) {
            img.src = data.image_url;
            img.style.display = '';
        } else {
            img.style.display = 'none';
        }
        document.getElementById('playlist-preview').style.display = 'flex';

        const nameInput = document.getElementById('channel-name-input');
        if (!nameInput.value) {
            nameInput.value = data.name;
        }
    } catch (e) {}
}

async function createChannel(e) {
    e.preventDefault();
    const channelType = document.querySelector('input[name="channel-type"]:checked')?.value || 'themed';
    const name = document.getElementById('channel-name-input').value.trim();
    if (!name) return;

    let body;
    if (channelType === 'themed') {
        const theme = document.getElementById('theme-input')?.value.trim();
        if (!theme) { alert('Please enter a theme or mood.'); return; }
        body = { name, theme, mode: 'themed' };
    } else {
        const url = document.getElementById('spotify-url-input')?.value.trim();
        const mode = document.querySelector('input[name="channel-mode"]:checked')?.value || 'similar_songs';
        if (!url) { alert('Please enter a Spotify URL.'); return; }
        body = { name, spotify_url: url, mode };
    }

    const btn = document.getElementById('btn-create-channel');
    btn.disabled = true;
    btn.textContent = 'Creating...';

    try {
        const resp = await fetch('/api/radio/channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert(data.error || 'Failed to create channel');
            return;
        }

        addChannelToSidebar(data.channel);
        document.getElementById('new-channel-dialog').close();
        switchChannel(data.channel.id);
    } catch (e) {
        alert('Failed to create channel');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Create Channel';
    }
}

function addChannelToSidebar(channel) {
    const list = document.getElementById('channel-list');
    const item = document.createElement('div');
    item.className = 'channel-item';
    item.dataset.channelId = channel.id;
    item.dataset.sourceType = channel.source_type;
    item.innerHTML = `
        <span class="channel-icon">
            <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/></svg>
        </span>
        <span class="channel-name">${channel.name}</span>
        <button class="channel-menu-btn" data-channel-id="${channel.id}" title="Channel options">&hellip;</button>
        <div class="channel-discovery" data-channel-id="${channel.id}">
            <input type="range" class="channel-discovery-slider" min="0" max="100" step="5"
                   value="${channel.discovery || 30}" title="Discovery: ${channel.discovery || 30}%">
            <div class="channel-discovery-labels">
                <span>Familiar</span>
                <span>Adventurous</span>
            </div>
        </div>
        <div class="channel-era" data-channel-id="${channel.id}">
            <select class="channel-era-select" data-era-from="" data-era-to="">
                <option value="" selected>All Eras</option>
                <option value="1960-1969">60s</option>
                <option value="1970-1979">70s</option>
                <option value="1980-1989">80s</option>
                <option value="1990-1999">90s</option>
                <option value="2000-2009">2000s</option>
                <option value="2010-2019">2010s</option>
                <option value="2020-2029">2020s</option>
                <option value="custom">Custom...</option>
            </select>
            <div class="era-custom-range" style="display:none;">
                <input type="number" class="era-from-input" placeholder="From" min="1900" max="2099">
                <span class="era-dash">&ndash;</span>
                <input type="number" class="era-to-input" placeholder="To" min="1900" max="2099">
            </div>
        </div>
    `;
    list.appendChild(item);
}

function openChannelMenu(channelId) {
    menuTargetChannelId = channelId;
    document.getElementById('channel-menu-dialog').showModal();
}

async function renameChannel() {
    if (!menuTargetChannelId) return;
    document.getElementById('channel-menu-dialog').close();
    const newName = prompt('Enter new channel name:');
    if (!newName || !newName.trim()) return;

    try {
        const resp = await fetch(`/api/radio/channels/${encodeURIComponent(menuTargetChannelId)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName.trim() }),
        });
        if (resp.ok) {
            const item = document.querySelector(`.channel-item[data-channel-id="${menuTargetChannelId}"]`);
            if (item) {
                item.querySelector('.channel-name').textContent = newName.trim();
            }
        }
    } catch (e) {}
}

async function deleteChannel() {
    if (!menuTargetChannelId) return;
    document.getElementById('channel-menu-dialog').close();
    if (!confirm('Delete this channel?')) return;

    try {
        const resp = await fetch(`/api/radio/channels/${encodeURIComponent(menuTargetChannelId)}`, {
            method: 'DELETE',
        });
        if (resp.ok) {
            const item = document.querySelector(`.channel-item[data-channel-id="${menuTargetChannelId}"]`);
            if (item) item.remove();
            if (menuTargetChannelId === activeChannelId) {
                switchChannel('my-collection');
            }
        }
    } catch (e) {}
}

// ---- Playback Controls ----
function playNext() {
    if (currentIndex + 1 < queue.length) {
        currentIndex++;
        loadTrack(queue[currentIndex]);
    }
}

function playPrev() {
    if (currentIndex > 0) {
        currentIndex--;
        loadTrack(queue[currentIndex]);
    }
}

function togglePlay() {
    if (!player) return;
    if (isPlaying) {
        player.pauseVideo();
    } else {
        player.playVideo();
    }
}

function loadTrack(track) {
    if (!player || !track.videoId) return;

    player.loadVideoById(track.videoId);
    updateTrackInfo(track);
    updateMediaSession(track);
    renderQueue();
    resetThumbButton(track);
    saveToHistory(track);
}

function updateTrackInfo(track) {
    document.getElementById('track-title').textContent = track.title || '—';
    document.getElementById('track-artist').textContent = track.artist || '—';
    document.getElementById('track-album').textContent = track.album
        ? `${track.album}${track.year ? ' (' + track.year + ')' : ''}`
        : '';
    document.getElementById('track-reason').textContent = track.reason || '';

    const similarSection = document.getElementById('similar-to');
    const similarList = document.getElementById('similar-to-list');
    if (similarSection && similarList) {
        if (track.similar_to && track.similar_to.length > 0) {
            similarList.innerHTML = track.similar_to.map(s =>
                `<div class="similar-to-item">
                    <span class="similar-to-album">${s.artist} — ${s.album}</span>
                    <span class="similar-to-why">${s.why || ''}</span>
                </div>`
            ).join('');
            similarSection.style.display = '';
        } else {
            similarSection.style.display = 'none';
        }
    }

    const artwork = document.getElementById('track-artwork');
    if (track.thumbnail) {
        artwork.src = track.thumbnail;
        artwork.style.display = 'block';
    } else {
        artwork.style.display = 'none';
    }
}

// ---- Progress Bar ----
function startProgressUpdates() {
    stopProgressUpdates();
    progressInterval = setInterval(updateProgress, 500);
}

function stopProgressUpdates() {
    if (progressInterval) {
        clearInterval(progressInterval);
        progressInterval = null;
    }
}

function updateProgress() {
    if (!player || isSeeking) return;
    try {
        const current = player.getCurrentTime() || 0;
        const total = player.getDuration() || 0;
        const pct = total > 0 ? (current / total) * 100 : 0;

        document.getElementById('progress-fill').style.width = pct + '%';
        document.getElementById('progress-handle').style.left = pct + '%';
        document.getElementById('time-current').textContent = formatTime(current);
        document.getElementById('time-total').textContent = formatTime(total);
    } catch (e) {}
}

function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
}

const progressBarEl = document.getElementById('progress-bar');
if (progressBarEl) {
    progressBarEl.addEventListener('click', (e) => {
        if (!player) return;
        const rect = progressBarEl.getBoundingClientRect();
        const pct = (e.clientX - rect.left) / rect.width;
        const duration = player.getDuration() || 0;
        player.seekTo(pct * duration, true);
    });
}

// ---- Thumbs Up ----
async function thumbsUp() {
    if (currentIndex < 0 || currentIndex >= queue.length) return;
    const track = queue[currentIndex];
    const key = `${track.artist}-${track.title}`.toLowerCase();

    if (likedSet.has(key)) return;
    likedSet.add(key);
    dislikedSet.delete(key);

    document.getElementById('btn-thumbs')?.classList.add('liked');
    document.getElementById('btn-dislike')?.classList.remove('disliked');

    try {
        await fetch('/api/radio/thumbs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                artist: track.artist,
                title: track.title,
                album: track.album || '',
                genres: track.genres || [],
                styles: track.styles || [],
            }),
        });
    } catch (e) {}
}

// ---- Thumbs Down ----
async function thumbsDown() {
    if (currentIndex < 0 || currentIndex >= queue.length) return;
    const track = queue[currentIndex];
    const key = `${track.artist}-${track.title}`.toLowerCase();

    if (dislikedSet.has(key)) { playNext(); return; }
    dislikedSet.add(key);
    likedSet.delete(key);

    document.getElementById('btn-dislike')?.classList.add('disliked');
    document.getElementById('btn-thumbs')?.classList.remove('liked');

    try {
        await fetch('/api/radio/dislike', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                artist: track.artist,
                title: track.title,
                album: track.album || '',
                genres: track.genres || [],
                styles: track.styles || [],
            }),
        });
    } catch (e) {}

    setTimeout(() => playNext(), 300);
}

function resetThumbButton(track) {
    const likeBtn = document.getElementById('btn-thumbs');
    const dislikeBtn = document.getElementById('btn-dislike');
    const key = `${track.artist}-${track.title}`.toLowerCase();
    if (likedSet.has(key)) {
        likeBtn?.classList.add('liked');
    } else {
        likeBtn?.classList.remove('liked');
    }
    if (dislikedSet.has(key)) {
        dislikeBtn?.classList.add('disliked');
    } else {
        dislikeBtn?.classList.remove('disliked');
    }
}

// ---- Play History ----
async function saveToHistory(track) {
    try {
        await fetch('/api/radio/history', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                artist: track.artist,
                title: track.title,
                album: track.album || '',
                genres: track.genres || [],
                styles: track.styles || [],
            }),
        });
    } catch (e) {}
}

// ---- Queue Rendering ----
function renderQueue() {
    const list = document.getElementById('queue-list');
    if (!list) return;
    list.innerHTML = '';

    const start = Math.max(currentIndex, 0);
    const upcoming = queue.slice(start, start + 10);

    upcoming.forEach((track, i) => {
        const idx = start + i;
        const item = document.createElement('div');
        item.className = 'queue-item' + (idx === currentIndex ? ' queue-current' : '');
        item.innerHTML = `
            <span class="queue-num">${idx === currentIndex ? '&#9835;' : idx + 1}</span>
            <div class="queue-info">
                <span class="queue-track-title">${track.title || '—'}</span>
                <span class="queue-track-artist">${track.artist || '—'}</span>
            </div>
            <span class="queue-duration">${track.duration || ''}</span>
        `;
        if (idx !== currentIndex) {
            item.style.cursor = 'pointer';
            item.addEventListener('click', () => {
                currentIndex = idx;
                loadTrack(queue[currentIndex]);
            });
        }
        list.appendChild(item);
    });
}

// ---- UI Helpers ----
function showPlayIcon() {
    document.getElementById('icon-play').style.display = '';
    document.getElementById('icon-pause').style.display = 'none';
}

function showPauseIcon() {
    document.getElementById('icon-play').style.display = 'none';
    document.getElementById('icon-pause').style.display = '';
}

function showLoading(show) {
    document.getElementById('radio-loading').style.display = show ? 'flex' : 'none';
    document.getElementById('radio-player').style.display = show ? 'none' : 'block';
}

function showError(msg) {
    document.getElementById('radio-loading').innerHTML =
        `<div class="radio-loading-inner"><p style="color:#ff6b6b;">${msg}</p></div>`;
}

// ---- Volume ----
const volSlider = document.getElementById('volume-slider');
if (volSlider) {
    volSlider.addEventListener('input', (e) => {
        if (player) player.setVolume(parseInt(e.target.value));
    });
}

// ---- Button Bindings ----
document.getElementById('btn-play')?.addEventListener('click', togglePlay);
document.getElementById('btn-next')?.addEventListener('click', playNext);
document.getElementById('btn-prev')?.addEventListener('click', playPrev);
document.getElementById('btn-thumbs')?.addEventListener('click', thumbsUp);
document.getElementById('btn-dislike')?.addEventListener('click', thumbsDown);
document.getElementById('btn-refresh')?.addEventListener('click', refreshPlaylist);

// ---- Channel Bindings ----
document.getElementById('btn-new-channel')?.addEventListener('click', openNewChannelDialog);
document.getElementById('new-channel-form')?.addEventListener('submit', createChannel);
document.getElementById('spotify-url-input')?.addEventListener('blur', previewSpotifyPlaylist);
document.getElementById('btn-cancel-channel')?.addEventListener('click', () => {
    document.getElementById('new-channel-dialog').close();
});
document.querySelectorAll('input[name="channel-type"]').forEach(r => {
    r.addEventListener('change', toggleChannelTypeFields);
});

document.getElementById('channel-list')?.addEventListener('click', (e) => {
    // Ignore clicks on discovery slider or era picker areas
    if (e.target.closest('.channel-discovery')) return;
    if (e.target.closest('.channel-era')) return;
    const menuBtn = e.target.closest('.channel-menu-btn');
    if (menuBtn) {
        e.stopPropagation();
        openChannelMenu(menuBtn.dataset.channelId);
        return;
    }
    const item = e.target.closest('.channel-item');
    if (item) {
        switchChannel(item.dataset.channelId);
    }
});

// ---- Discovery Slider ----
let discoveryDebounce = null;
document.getElementById('channel-list')?.addEventListener('input', (e) => {
    if (!e.target.classList.contains('channel-discovery-slider')) return;
    const wrapper = e.target.closest('.channel-discovery');
    if (!wrapper) return;
    const channelId = wrapper.dataset.channelId;
    const value = parseInt(e.target.value);
    e.target.title = `Discovery: ${value}%`;

    clearTimeout(discoveryDebounce);
    discoveryDebounce = setTimeout(async () => {
        try {
            await fetch(`/api/radio/channels/${encodeURIComponent(channelId)}/discovery`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ discovery: value }),
            });
        } catch (err) {}
    }, 400);
});

// ---- Era Select ----
// Set initial selected value for all era selects on page load
document.querySelectorAll('.channel-era-select').forEach(sel => {
    const eraFrom = sel.dataset.eraFrom;
    const eraTo = sel.dataset.eraTo;
    if (eraFrom && eraTo) {
        const preset = `${eraFrom}-${eraTo}`;
        const option = sel.querySelector(`option[value="${preset}"]`);
        if (option) {
            sel.value = preset;
        } else {
            sel.value = 'custom';
            const customRange = sel.closest('.channel-era')?.querySelector('.era-custom-range');
            if (customRange) customRange.style.display = '';
        }
    }
});

let eraDebounce = null;
async function saveChannelEra(channelId, eraFrom, eraTo) {
    clearTimeout(eraDebounce);
    eraDebounce = setTimeout(async () => {
        try {
            await fetch(`/api/radio/channels/${encodeURIComponent(channelId)}/era`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ era_from: eraFrom, era_to: eraTo }),
            });
            // Auto-refresh if this is the active channel
            if (channelId === activeChannelId) {
                await fetch(`/api/radio/refresh-playlist?channel_id=${encodeURIComponent(channelId)}`);
                loadPlaylistSSE();
                showLoading(true);
                resetLoadingUI();
            }
        } catch (err) {}
    }, 500);
}

document.getElementById('channel-list')?.addEventListener('change', (e) => {
    if (!e.target.classList.contains('channel-era-select')) return;
    const wrapper = e.target.closest('.channel-era');
    if (!wrapper) return;
    const channelId = wrapper.dataset.channelId;
    const customRange = wrapper.querySelector('.era-custom-range');

    if (e.target.value === 'custom') {
        customRange.style.display = '';
        return; // wait for custom inputs
    }
    customRange.style.display = 'none';

    if (!e.target.value) {
        saveChannelEra(channelId, null, null);
    } else {
        const [from, to] = e.target.value.split('-').map(Number);
        saveChannelEra(channelId, from, to);
    }
});

// Handle custom era range inputs
document.getElementById('channel-list')?.addEventListener('change', (e) => {
    if (!e.target.classList.contains('era-from-input') && !e.target.classList.contains('era-to-input')) return;
    const wrapper = e.target.closest('.channel-era');
    if (!wrapper) return;
    const channelId = wrapper.dataset.channelId;
    const fromInput = wrapper.querySelector('.era-from-input');
    const toInput = wrapper.querySelector('.era-to-input');
    const eraFrom = fromInput.value ? parseInt(fromInput.value) : null;
    const eraTo = toInput.value ? parseInt(toInput.value) : null;
    saveChannelEra(channelId, eraFrom, eraTo);
});

document.getElementById('btn-rename-channel')?.addEventListener('click', renameChannel);
document.getElementById('btn-delete-channel')?.addEventListener('click', deleteChannel);
document.getElementById('btn-close-menu')?.addEventListener('click', () => {
    document.getElementById('channel-menu-dialog').close();
});

document.getElementById('sidebar-toggle-open')?.addEventListener('click', () => {
    document.getElementById('channel-sidebar').classList.add('sidebar-open');
});
document.getElementById('sidebar-toggle-close')?.addEventListener('click', () => {
    document.getElementById('channel-sidebar').classList.remove('sidebar-open');
});

// ---- Keyboard Shortcuts ----
document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.target.closest('dialog')) return;
    switch (e.code) {
        case 'Space': e.preventDefault(); togglePlay(); break;
        case 'ArrowRight': playNext(); break;
        case 'ArrowLeft': playPrev(); break;
        case 'KeyD': thumbsDown(); break;
        case 'KeyL': thumbsUp(); break;
    }
});

// ---- Visualizer ----
let vizAnimFrame = null;
const vizCanvas = document.getElementById('visualizer');
let vizCtx = vizCanvas ? vizCanvas.getContext('2d') : null;
let vizBars = [];

function initVisualizerBars() {
    vizBars = [];
    for (let i = 0; i < 32; i++) {
        vizBars.push({ height: 0, target: 0, velocity: 0 });
    }
}
initVisualizerBars();

function startVisualizer() {
    if (vizAnimFrame) return;
    resizeVizCanvas();
    animateVisualizer();
}

function resizeVizCanvas() {
    if (!vizCanvas) return;
    vizCanvas.width = vizCanvas.offsetWidth * window.devicePixelRatio;
    vizCanvas.height = vizCanvas.offsetHeight * window.devicePixelRatio;
    vizCtx.scale(window.devicePixelRatio, window.devicePixelRatio);
}

function animateVisualizer() {
    if (!vizCtx || !vizCanvas) return;
    const w = vizCanvas.offsetWidth;
    const h = vizCanvas.offsetHeight;
    vizCtx.clearRect(0, 0, w, h);

    const barCount = vizBars.length;
    const barWidth = (w / barCount) * 0.7;
    const gap = (w / barCount) * 0.3;

    for (let i = 0; i < barCount; i++) {
        if (isPlaying) {
            if (Math.random() < 0.1) {
                vizBars[i].target = Math.random() * h * 0.85 + h * 0.05;
            }
        } else {
            vizBars[i].target = h * 0.03;
        }
        const bar = vizBars[i];
        const spring = 0.08;
        const damping = 0.75;
        bar.velocity = (bar.velocity + (bar.target - bar.height) * spring) * damping;
        bar.height += bar.velocity;

        const x = i * (barWidth + gap) + gap / 2;
        const barH = Math.max(2, bar.height);

        const hue = 200 + (i / barCount) * 160;
        const gradient = vizCtx.createLinearGradient(x, h, x, h - barH);
        gradient.addColorStop(0, `hsla(${hue}, 80%, 60%, 0.9)`);
        gradient.addColorStop(1, `hsla(${hue + 30}, 90%, 75%, 0.6)`);

        vizCtx.fillStyle = gradient;
        vizCtx.beginPath();
        vizCtx.roundRect(x, h - barH, barWidth, barH, [3, 3, 0, 0]);
        vizCtx.fill();
    }

    vizAnimFrame = requestAnimationFrame(animateVisualizer);
}

window.addEventListener('resize', resizeVizCanvas);

// ---- Media Session API (hardware media keys) ----
// We use the Web Audio API to generate a near-silent tone. This is more reliable
// than a data-URI WAV because it creates a real, ongoing audio context that Chrome
// recognises for Media Session purposes.
let silentAudioCtx = null;
let silentOscillator = null;
let silentGain = null;
let silentMediaDest = null;
let silentAudioEl = null;

function initSilentAudio() {
    if (silentAudioCtx) return;
    try {
        silentAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
        silentMediaDest = silentAudioCtx.createMediaStreamDestination();
        silentOscillator = silentAudioCtx.createOscillator();
        silentGain = silentAudioCtx.createGain();
        silentGain.gain.value = 0.001; // nearly inaudible
        silentOscillator.connect(silentGain);
        silentGain.connect(silentMediaDest);
        silentOscillator.start();

        silentAudioEl = document.createElement('audio');
        silentAudioEl.srcObject = silentMediaDest.stream;
        silentAudioEl.loop = true;
    } catch (e) {
        console.warn('Could not init silent audio for media keys:', e);
    }
}

function startSilentAudio() {
    initSilentAudio();
    if (silentAudioCtx && silentAudioCtx.state === 'suspended') {
        silentAudioCtx.resume().catch(() => {});
    }
    if (silentAudioEl) {
        silentAudioEl.play().catch(() => {});
    }
}

function stopSilentAudio() {
    // Don't pause — keep it alive so media keys stay registered
}

if ('mediaSession' in navigator) {
    navigator.mediaSession.setActionHandler('play', () => { if (player && !isPlaying) player.playVideo(); });
    navigator.mediaSession.setActionHandler('pause', () => { if (player && isPlaying) player.pauseVideo(); });
    navigator.mediaSession.setActionHandler('nexttrack', () => playNext());
    navigator.mediaSession.setActionHandler('previoustrack', () => playPrev());
}

function updateMediaSession(track) {
    if (!('mediaSession' in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
        title: track.title || '',
        artist: track.artist || '',
        album: track.album || '',
        artwork: track.thumbnail ? [{ src: track.thumbnail, sizes: '480x360', type: 'image/jpeg' }] : [],
    });
    navigator.mediaSession.playbackState = 'playing';
}
