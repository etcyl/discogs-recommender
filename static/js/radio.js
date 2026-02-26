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

// ---- Session Feedback State ----
let sessionFeedback = { liked: [], disliked: [], skipped: [] };
let isGeneratingReplacements = false;
let feedbackDebounceTimer = null;
let trackStartTime = null;

// ---- Channel State ----
let activeChannelId = 'my-collection';
let menuTargetChannelId = null;
let activeEventSource = null;

// ---- Shuffle State ----
let shuffleMode = false;
let playedIndices = new Set();
let playHistory = [];

function getActiveAiModel() {
    const sel = document.querySelector(`.channel-ai-model[data-channel-id="${activeChannelId}"] .channel-ai-model-select`);
    return sel ? sel.value : 'claude-sonnet';
}

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

// ---- Inline Progress Bar ----
let isRefreshing = false;

function showInlineProgress(show) {
    const bar = document.getElementById('inline-progress');
    if (bar) bar.style.display = show ? 'flex' : 'none';
    isRefreshing = show;
}

function updateInlineProgress(message, percent) {
    const text = document.getElementById('inline-progress-text');
    const fill = document.getElementById('inline-progress-fill');
    if (text) text.textContent = message;
    if (fill) fill.style.width = percent + '%';
}

// ---- Playlist Loading (SSE with progress) ----
function loadPlaylistSSE(isRefreshMode = false) {
    // Close any existing SSE connection
    if (activeEventSource) {
        activeEventSource.close();
        activeEventSource = null;
    }

    let sseFinished = false;
    let songsReceived = 0;
    const isFirstLoad = queue.length === 0 && !isRefreshMode;

    if (isFirstLoad) {
        // First time: show full-screen overlay
        showLoading(true);
    } else {
        // Refresh: keep player visible, show inline progress
        showInlineProgress(true);
        updateInlineProgress('Starting refresh...', 0);
    }

    // On refresh, save current song and clear upcoming queue
    const savedCurrentTrack = isRefreshMode && currentIndex >= 0 ? queue[currentIndex] : null;
    if (isRefreshMode) {
        // Keep only the currently playing song
        if (savedCurrentTrack) {
            queue = [savedCurrentTrack];
            currentIndex = 0;
        } else {
            queue = [];
            currentIndex = -1;
        }
        renderQueue();
    }

    const url = `/api/radio/playlist-stream?channel_id=${encodeURIComponent(activeChannelId)}`;
    const es = new EventSource(url);
    activeEventSource = es;

    es.addEventListener('progress', (e) => {
        const data = JSON.parse(e.data);
        if (isFirstLoad) {
            updateLoadingProgress(data.message, data.percent);
        } else {
            updateInlineProgress(data.message, data.percent);
        }
    });

    // Progressive song streaming — songs arrive in batches
    es.addEventListener('song', (e) => {
        const data = JSON.parse(e.data);
        if (!data.songs || data.songs.length === 0) return;

        // Append new songs to queue
        queue.push(...data.songs);
        songsReceived += data.songs.length;
        renderQueue();

        // Start playback if this is the first batch
        if (isFirstLoad && currentIndex === -1) {
            showLoading(false);
            // Show first song without auto-playing — let user press Play or Next
            currentIndex = 0;
            loadTrack(queue[0], false);
        } else if (isRefreshMode && currentIndex === -1) {
            currentIndex = -1;
            playNext();
        }
    });

    es.addEventListener('complete', (e) => {
        sseFinished = true;
        es.close();
        activeEventSource = null;
        showInlineProgress(false);

        if (isFirstLoad) showLoading(false);

        const data = JSON.parse(e.data);

        // Show "Curated by" badge
        const badge = document.getElementById('curated-by-badge');
        if (badge && data.ai_model) {
            badge.textContent = `curated by ${data.ai_model}`;
            badge.style.display = '';
        } else if (badge && !data.ai_model) {
            badge.style.display = 'none';
        }

        renderQueue();

        // If no songs at all
        if (queue.length === 0 || (isRefreshMode && songsReceived === 0)) {
            if (isFirstLoad) {
                showError('No songs found. Try a different playlist or add more to your collection.');
            } else {
                showToast('No new songs found');
            }
        }
    });

    // Server-sent "event: error" from our backend
    es.addEventListener('error', (e) => {
        if (!e.data) return;
        sseFinished = true;
        showInlineProgress(false);
        try {
            const data = JSON.parse(e.data);
            if (isFirstLoad) {
                showError(data.message || 'Failed to load playlist.');
            } else {
                showToast(data.message || 'Refresh failed');
            }
        } catch {
            if (isFirstLoad) showError('Failed to load playlist.');
            else showToast('Refresh failed');
        }
        es.close();
        activeEventSource = null;
    });

    // Connection-level error
    es.onerror = () => {
        if (sseFinished) return;
        if (es.readyState === EventSource.CLOSED) {
            showInlineProgress(false);
            if (isFirstLoad && queue.length === 0) {
                showError('Connection lost. Try refreshing the page.');
            } else {
                showToast('Connection lost');
            }
            activeEventSource = null;
        }
    };
}

function stopLoading() {
    if (activeEventSource) {
        activeEventSource.close();
        activeEventSource = null;
    }
    showLoading(false);
    showInlineProgress(false);
    if (queue.length === 0) {
        showError('Generation stopped.');
    }
}

// Brief toast notification (non-blocking)
function showToast(msg, duration = 3000) {
    let toast = document.getElementById('radio-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'radio-toast';
        toast.style.cssText = 'position:fixed;bottom:2rem;left:50%;transform:translateX(-50%);' +
            'background:rgba(0,0,0,0.85);color:#fff;padding:0.6rem 1.2rem;border-radius:8px;' +
            'font-size:0.85rem;z-index:9999;opacity:0;transition:opacity 0.3s;pointer-events:none;';
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.style.opacity = '1';
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toast.style.opacity = '0'; }, duration);
}

function loadPlaylist() {
    loadPlaylistSSE(false);
}

async function refreshPlaylist() {
    sessionFeedback = { liked: [], disliked: [], skipped: [] };
    isGeneratingReplacements = false;
    clearTimeout(feedbackDebounceTimer);
    await fetch(`/api/radio/refresh-playlist?channel_id=${encodeURIComponent(activeChannelId)}`);
    loadPlaylistSSE(true);
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

    // Reset session feedback
    sessionFeedback = { liked: [], disliked: [], skipped: [] };
    isGeneratingReplacements = false;
    clearTimeout(feedbackDebounceTimer);

    // Update sidebar highlighting
    document.querySelectorAll('.channel-item').forEach(el => {
        el.classList.toggle('channel-active', el.dataset.channelId === channelId);
    });

    // Stop current playback
    if (player && isPlaying) {
        try { player.stopVideo(); } catch (e) {}
    }

    // Reset queue and shuffle state
    queue = [];
    currentIndex = -1;
    playedIndices.clear();
    playHistory = [];
    renderQueue();

    // Auto-enable shuffle for Liked Songs channel
    const channelItem = document.querySelector(`.channel-item[data-channel-id="${channelId}"]`);
    const isLiked = channelItem?.dataset.sourceType === 'liked';
    if (isLiked && !shuffleMode) {
        toggleShuffle();
    } else if (!isLiked && shuffleMode) {
        toggleShuffle();
    }

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
    const youtubeInput = document.getElementById('youtube-url-input');
    if (youtubeInput) youtubeInput.value = '';
    const ytPreview = document.getElementById('youtube-preview');
    if (ytPreview) ytPreview.style.display = 'none';
    const modeRadio = document.querySelector('input[name="channel-mode"][value="similar_songs"]');
    if (modeRadio) modeRadio.checked = true;
    const ytModeRadio = document.querySelector('input[name="youtube-mode"][value="similar_songs"]');
    if (ytModeRadio) ytModeRadio.checked = true;
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
    const youtubeFields = document.getElementById('youtube-fields');
    const uploadFields = document.getElementById('upload-fields');
    if (themedFields) themedFields.style.display = selectedType === 'themed' ? '' : 'none';
    if (spotifyFields) spotifyFields.style.display = selectedType === 'spotify' ? '' : 'none';
    if (youtubeFields) youtubeFields.style.display = selectedType === 'youtube' ? '' : 'none';
    if (uploadFields) uploadFields.style.display = selectedType === 'upload' ? '' : 'none';
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

async function previewYouTubePlaylist() {
    const urlInput = document.getElementById('youtube-url-input');
    const url = urlInput.value.trim();
    if (!url || (!url.includes('youtube') && !url.includes('youtu.be'))) return;

    try {
        const resp = await fetch('/api/radio/youtube-preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        if (!resp.ok) return;
        const data = await resp.json();

        document.getElementById('youtube-preview-name').textContent = data.name;
        document.getElementById('youtube-preview-count').textContent = `${data.track_count} tracks`;
        const img = document.getElementById('youtube-preview-image');
        if (data.image_url) {
            img.src = data.image_url;
            img.style.display = '';
        } else {
            img.style.display = 'none';
        }
        document.getElementById('youtube-preview').style.display = 'flex';

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

    const btn = document.getElementById('btn-create-channel');
    btn.disabled = true;
    btn.textContent = 'Creating...';

    try {
        let resp;

        const aiModel = document.getElementById('new-channel-model')?.value || 'claude-sonnet';
        const era = document.getElementById('new-channel-era')?.value || '';
        const numSongs = parseInt(document.getElementById('new-channel-num-songs')?.value || '50', 10);

        if (channelType === 'upload') {
            const fileInput = document.getElementById('upload-file-input');
            const file = fileInput?.files[0];
            if (!file) { alert('Please select a file.'); btn.disabled = false; btn.textContent = 'Create Channel'; return; }
            const mode = document.querySelector('input[name="upload-mode"]:checked')?.value || 'similar_songs';
            const formData = new FormData();
            formData.append('file', file);
            formData.append('name', name);
            formData.append('mode', mode);
            formData.append('ai_model', aiModel);
            formData.append('era', era);
            formData.append('num_songs', numSongs);
            resp = await fetch('/api/radio/upload-channel', { method: 'POST', body: formData });
        } else if (channelType === 'youtube') {
            const url = document.getElementById('youtube-url-input')?.value.trim();
            const mode = document.querySelector('input[name="youtube-mode"]:checked')?.value || 'similar_songs';
            if (!url) { alert('Please enter a YouTube playlist URL.'); btn.disabled = false; btn.textContent = 'Create Channel'; return; }
            resp = await fetch('/api/radio/youtube-channel', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, url, mode, ai_model: aiModel, era, num_songs: numSongs }),
            });
        } else if (channelType === 'themed') {
            const theme = document.getElementById('theme-input')?.value.trim();
            if (!theme) { alert('Please enter a theme or mood.'); btn.disabled = false; btn.textContent = 'Create Channel'; return; }
            resp = await fetch('/api/radio/channels', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, theme, mode: 'themed', ai_model: aiModel, era, num_songs: numSongs }),
            });
        } else {
            const url = document.getElementById('spotify-url-input')?.value.trim();
            const mode = document.querySelector('input[name="channel-mode"]:checked')?.value || 'similar_songs';
            if (!url) { alert('Please enter a Spotify URL.'); btn.disabled = false; btn.textContent = 'Create Channel'; return; }
            resp = await fetch('/api/radio/channels', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, spotify_url: url, mode, ai_model: aiModel, era, num_songs: numSongs }),
            });
        }

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

function _buildModelOptions(selected) {
    const all = [
        { value: 'claude-sonnet', label: 'Claude Sonnet' },
        { value: 'claude-haiku', label: 'Claude Haiku (cheaper)' },
        { value: 'ollama', label: 'Ollama (free, local)' },
    ];
    const allowed = typeof ALLOWED_MODELS !== 'undefined' ? ALLOWED_MODELS : all.map(m => m.value);
    return all.filter(m => allowed.includes(m.value))
        .map(m => `<option value="${m.value}"${selected === m.value ? ' selected' : ''}>${m.label}</option>`)
        .join('');
}

function addChannelToSidebar(channel) {
    const list = document.getElementById('channel-list');
    const item = document.createElement('div');
    item.className = 'channel-item';
    item.dataset.channelId = channel.id;
    item.dataset.sourceType = channel.source_type;
    const iconSvg = channel.source_type === 'spotify'
        ? '<svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/></svg>'
        : channel.source_type === 'youtube'
        ? '<svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M21.582 7.186a2.506 2.506 0 0 0-1.768-1.768C18.254 5 12 5 12 5s-6.254 0-7.814.418A2.506 2.506 0 0 0 2.418 7.186C2 8.746 2 12 2 12s0 3.254.418 4.814a2.506 2.506 0 0 0 1.768 1.768C5.746 19 12 19 12 19s6.254 0 7.814-.418a2.506 2.506 0 0 0 1.768-1.768C22 15.254 22 12 22 12s0-3.254-.418-4.814zM10 15.464V8.536L16 12l-6 3.464z"/></svg>'
        : channel.source_type === 'upload'
        ? '<svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm4 18H6V4h7v5h5v11z"/></svg>'
        : '<svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 14.5c-2.49 0-4.5-2.01-4.5-4.5S9.51 7.5 12 7.5s4.5 2.01 4.5 4.5-2.01 4.5-4.5 4.5zm0-5.5c-.55 0-1 .45-1 1s.45 1 1 1 1-.45 1-1-.45-1-1-1z"/></svg>';
    item.innerHTML = `
        <span class="channel-icon">
            ${iconSvg}
        </span>
        <span class="channel-name">${channel.name}</span>
        <button class="channel-menu-btn" data-channel-id="${channel.id}" title="Channel options">&hellip;</button>
        <div class="channel-discovery" data-channel-id="${channel.id}">
            <input type="range" class="channel-discovery-slider" min="0" max="100" step="5"
                   value="${channel.discovery || 30}" title="Discovery: ${channel.discovery || 30}%">
            <div class="channel-discovery-labels">
                <span class="discovery-tier-name" data-channel-id="${channel.id}"></span>
            </div>
        </div>
        <div class="channel-era" data-channel-id="${channel.id}">
            <select class="channel-era-select" data-era-from="${channel.era_from || ''}" data-era-to="${channel.era_to || ''}">
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
        <div class="channel-ai-model" data-channel-id="${channel.id}">
            <select class="channel-ai-model-select" data-ai-model="${channel.ai_model || 'claude-sonnet'}">
                ${_buildModelOptions(channel.ai_model || 'claude-sonnet')}
            </select>
        </div>
        <div class="channel-num-songs" data-channel-id="${channel.id}">
            <label style="font-size:0.75rem;opacity:0.7;">Songs: <span class="num-songs-label">${channel.num_songs || 50}</span></label>
            <input type="range" class="channel-num-songs-slider" min="5" max="100" step="5"
                   value="${channel.num_songs || 50}" title="Playlist size: ${channel.num_songs || 50}">
        </div>
    `;
    list.appendChild(item);
    // Initialize discovery tier label for the new slider
    const newSlider = item.querySelector('.channel-discovery-slider');
    if (newSlider) updateDiscoveryLabel(newSlider);
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
    // Detect early skip (< 30s) as implicit negative signal
    if (currentIndex >= 0 && currentIndex < queue.length && trackStartTime) {
        const playedMs = Date.now() - trackStartTime;
        const track = queue[currentIndex];
        const key = `${track.artist}-${track.title}`.toLowerCase();
        if (playedMs < 30000 && !dislikedSet.has(key) && !likedSet.has(key)) {
            sessionFeedback.skipped.push({
                artist: track.artist || '',
                title: track.title || '',
                match_attributes: track.match_attributes || [],
                reason: track.reason || '',
            });
            scheduleFeedbackGeneration();
        }
    }
    if (shuffleMode && queue.length > 0) {
        if (currentIndex >= 0) playedIndices.add(currentIndex);

        // Build pool of unplayed indices
        const pool = [];
        for (let i = 0; i < queue.length; i++) {
            if (!playedIndices.has(i)) pool.push(i);
        }

        if (pool.length === 0) {
            // All songs played — reshuffle
            playedIndices.clear();
            if (currentIndex >= 0) playedIndices.add(currentIndex);
            for (let i = 0; i < queue.length; i++) {
                if (i !== currentIndex) pool.push(i);
            }
            showToast('All songs played! Reshuffling...');
        }

        if (pool.length > 0) {
            const pick = pool[Math.floor(Math.random() * pool.length)];
            currentIndex = pick;
            playHistory.push(currentIndex);
            loadTrack(queue[currentIndex]);
        }
    } else if (currentIndex + 1 < queue.length) {
        currentIndex++;
        loadTrack(queue[currentIndex]);
    } else if (isRefreshing) {
        showToast('Loading more songs...');
    }
}

function playPrev() {
    if (shuffleMode && playHistory.length > 1) {
        playHistory.pop(); // remove current
        currentIndex = playHistory[playHistory.length - 1];
        loadTrack(queue[currentIndex]);
    } else if (currentIndex > 0) {
        currentIndex--;
        loadTrack(queue[currentIndex]);
    }
}

function toggleShuffle() {
    shuffleMode = !shuffleMode;
    const btn = document.getElementById('btn-shuffle');
    btn?.classList.toggle('shuffle-active', shuffleMode);

    if (shuffleMode) {
        playedIndices.clear();
        playHistory = [];
        if (currentIndex >= 0) {
            playedIndices.add(currentIndex);
            playHistory.push(currentIndex);
        }
        showToast('Shuffle on');
    } else {
        showToast('Shuffle off');
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

function loadTrack(track, autoplay = true) {
    if (!track) return;

    // Record start time for skip detection
    trackStartTime = Date.now();

    // Always update UI even if YouTube player hasn't loaded yet
    updateTrackInfo(track);
    renderQueue();
    resetThumbButton(track);
    saveToHistory(track);
    if (mindmapVisible) updateMindmapForCurrentTrack();

    // Lyrics/meaning: fetch if panel is visible
    if (lyricsVisible) {
        fetchLyricsForTrack(track);
        activeLyricIndex = -1;
    }

    // Always fetch meaning in background for dynamic theming
    fetchMeaningForTrack(track);

    if (player && track.videoId) {
        if (autoplay) {
            player.loadVideoById(track.videoId);
        } else {
            // Cue without auto-playing — user can press Play when ready
            player.cueVideoById(track.videoId);
            isPlaying = false;
            showPlayIcon();
        }
        updateMediaSession(track);
    }
}

// Discovery tier definitions (mirrors backend RadioService.DISCOVERY_TIERS)
const DISCOVERY_TIERS = [
    { max: 15, name: "Comfort Zone", label: "Deep cuts from artists you already love" },
    { max: 30, name: "Familiar Ground", label: "Same scenes, labels, and close collaborators" },
    { max: 50, name: "Near Orbit", label: "Adjacent genres, shared producers, related movements" },
    { max: 70, name: "Explorer", label: "Cross-genre connections, unexpected bridges" },
    { max: 85, name: "Adventurer", label: "Different eras, countries, and sonic territories" },
    { max: 100, name: "Deep Space", label: "Wildcard picks with only a thin thread back to your taste" },
];

function getDiscoveryTier(value) {
    for (const tier of DISCOVERY_TIERS) {
        if (value <= tier.max) return tier;
    }
    return DISCOVERY_TIERS[DISCOVERY_TIERS.length - 1];
}

function updateDiscoveryLabel(slider) {
    const value = parseInt(slider.value);
    const tier = getDiscoveryTier(value);
    const wrapper = slider.closest('.channel-discovery');
    if (!wrapper) return;
    const label = wrapper.querySelector('.discovery-tier-name');
    if (label) {
        label.textContent = tier.name;
        label.title = tier.label;
    }
}

// Initialize all discovery slider labels on load
document.querySelectorAll('.channel-discovery-slider').forEach(updateDiscoveryLabel);

function getMatchScoreColor(score) {
    if (score >= 90) return '#4ade80';
    if (score >= 70) return '#60a5fa';
    if (score >= 50) return '#facc15';
    if (score >= 30) return '#fb923c';
    return '#f87171';
}

function getMatchScoreLabel(score) {
    if (score >= 90) return 'Near-perfect';
    if (score >= 70) return 'Strong';
    if (score >= 50) return 'Moderate';
    if (score >= 30) return 'Adventurous';
    return 'Wildcard';
}

function updateTrackInfo(track) {
    document.getElementById('track-title').textContent = track.title || '—';
    document.getElementById('track-artist').textContent = track.artist || '—';
    document.getElementById('track-album').textContent = track.album
        ? `${track.album}${track.year ? ' (' + track.year + ')' : ''}`
        : '';
    document.getElementById('track-reason').textContent = track.reason || '';

    // Match score + attributes section
    const matchInfo = document.getElementById('match-info');
    if (matchInfo) {
        const score = track.match_score;
        if (score && typeof score === 'number') {
            const bar = document.getElementById('match-score-bar');
            const val = document.getElementById('match-score-value');
            const color = getMatchScoreColor(score);
            bar.style.width = `${score}%`;
            bar.style.background = color;
            val.textContent = `${score} — ${getMatchScoreLabel(score)}`;
            val.style.color = color;

            const attrsEl = document.getElementById('match-attributes');
            if (track.match_attributes && track.match_attributes.length > 0) {
                attrsEl.innerHTML = track.match_attributes.map(attr =>
                    `<span class="match-attr-tag">${attr}</span>`
                ).join('');
                attrsEl.style.display = '';
            } else {
                attrsEl.style.display = 'none';
            }
            matchInfo.style.display = '';
        } else {
            matchInfo.style.display = 'none';
        }
    }

    const similarSection = document.getElementById('similar-to');
    const similarList = document.getElementById('similar-to-list');
    if (similarSection && similarList) {
        if (track.similar_to && track.similar_to.length > 0) {
            const isStringArray = typeof track.similar_to[0] === 'string';
            if (isStringArray) {
                similarList.innerHTML = track.similar_to.map(name =>
                    `<div class="similar-to-item">
                        <span class="similar-to-album">${name}</span>
                    </div>`
                ).join('');
            } else {
                similarList.innerHTML = track.similar_to.map(s =>
                    `<div class="similar-to-item">
                        <span class="similar-to-album">${s.artist} — ${s.album}</span>
                        <span class="similar-to-why">${s.why || ''}</span>
                    </div>`
                ).join('');
            }
            similarSection.style.display = '';
        } else {
            similarSection.style.display = 'none';
        }
    }

    const artwork = document.getElementById('track-artwork');
    const artSrc = track.albumArt || track.thumbnail;
    if (artSrc) {
        artwork.src = artSrc;
        artwork.style.display = 'block';
    } else {
        artwork.style.display = 'none';
    }

    // Show/hide YouTube copy button based on videoId
    const ytBtn = document.getElementById('btn-copy-youtube');
    if (ytBtn) ytBtn.style.display = track.videoId ? '' : 'none';
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

    // Session feedback: positive reinforcement
    sessionFeedback.liked.push({
        artist: track.artist || '',
        title: track.title || '',
        match_attributes: track.match_attributes || [],
        reason: track.reason || '',
        similar_to: track.similar_to || [],
    });
    boostSimilarSongs(track);
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

    // Session feedback: negative signal + queue adjustment
    sessionFeedback.disliked.push({
        artist: track.artist || '',
        title: track.title || '',
        match_attributes: track.match_attributes || [],
        reason: track.reason || '',
        similar_to: track.similar_to || [],
    });
    const removed = removeSimilarFromQueue(track);
    scheduleFeedbackGeneration(removed);

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

// ---- Feedback: Similarity & Queue Manipulation ----
function getSimilarityScore(refTrack, candidate) {
    let score = 0;
    if ((candidate.artist || '').toLowerCase().trim() === (refTrack.artist || '').toLowerCase().trim()) {
        score += 100;
    }
    const refAttrs = new Set(refTrack.match_attributes || []);
    (candidate.match_attributes || []).forEach(a => { if (refAttrs.has(a)) score += 15; });
    const simArtists = (t) => new Set(
        (t.similar_to || []).map(s => (typeof s === 'string' ? s : (s.artist || '')).toLowerCase())
    );
    const refSim = simArtists(refTrack);
    simArtists(candidate).forEach(a => { if (refSim.has(a)) score += 20; });
    return score;
}

function removeSimilarFromQueue(dislikedTrack) {
    let removed = 0;
    const kept = [];
    for (let i = 0; i < queue.length; i++) {
        if (i <= currentIndex) { kept.push(queue[i]); continue; }
        if (getSimilarityScore(dislikedTrack, queue[i]) >= 50) { removed++; }
        else { kept.push(queue[i]); }
    }
    queue = kept;
    renderQueue();
    return removed;
}

function boostSimilarSongs(likedTrack) {
    const upcoming = [];
    for (let i = currentIndex + 1; i < queue.length; i++) {
        upcoming.push({ idx: i, score: getSimilarityScore(likedTrack, queue[i]) });
    }
    const best = upcoming.filter(s => s.score >= 20 && s.score < 100)
                         .sort((a, b) => b.score - a.score)[0];
    if (!best) return;
    const moveBy = Math.min(3, best.idx - currentIndex - 1);
    if (moveBy > 0) {
        const [song] = queue.splice(best.idx, 1);
        queue.splice(best.idx - moveBy, 0, song);
        renderQueue();
    }
}

function scheduleFeedbackGeneration(removedCount = 0) {
    if (isGeneratingReplacements) return;
    clearTimeout(feedbackDebounceTimer);
    feedbackDebounceTimer = setTimeout(() => {
        const remaining = queue.length - currentIndex - 1;
        const negativeCount = sessionFeedback.disliked.length + sessionFeedback.skipped.length;
        if (removedCount > 0 || remaining <= 5 || negativeCount >= 2) {
            generateReplacements(Math.max(removedCount, 5));
        }
    }, 2000);
}

async function generateReplacements(numReplacements = 8) {
    if (isGeneratingReplacements) return;
    isGeneratingReplacements = true;
    showToast('Adjusting your playlist...', 5000);

    const upcomingQueue = queue.slice(currentIndex + 1).map(t => ({
        artist: t.artist || '', title: t.title || '',
        match_attributes: t.match_attributes || [],
    }));

    try {
        const resp = await fetch('/api/radio/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                channel_id: activeChannelId,
                session_liked: sessionFeedback.liked.slice(-10),
                session_disliked: [...sessionFeedback.disliked, ...sessionFeedback.skipped].slice(-10),
                current_queue: upcomingQueue,
                num_replacements: Math.min(numReplacements, 10),
            }),
        });
        if (!resp.ok) { console.warn('Feedback generation failed:', resp.status); return; }
        const data = await resp.json();
        if (data.songs && data.songs.length > 0) {
            insertReplacements(data.songs);
            showToast(`Added ${data.songs.length} songs based on your feedback`);
        }
    } catch (e) {
        console.warn('Feedback generation error:', e);
    } finally {
        isGeneratingReplacements = false;
    }
}

function insertReplacements(newSongs) {
    const insertStart = Math.min(currentIndex + 3, queue.length);
    let insertIdx = insertStart;
    for (const song of newSongs) {
        song._feedbackAdded = true;
        queue.splice(insertIdx, 0, song);
        insertIdx += 3;
        if (insertIdx > queue.length) insertIdx = queue.length;
    }
    renderQueue();
}

// ---- Queue Rendering ----
function renderQueue() {
    const list = document.getElementById('queue-list');
    if (!list) return;
    list.innerHTML = '';

    const start = Math.max(currentIndex, 0);
    const upcoming = queue.slice(start);

    upcoming.forEach((track, i) => {
        const idx = start + i;
        const item = document.createElement('div');
        item.className = 'queue-item'
            + (idx === currentIndex ? ' queue-current' : '')
            + (track._feedbackAdded ? ' queue-feedback-added' : '');
        const ytBtn = track.videoId
            ? `<button class="queue-share-btn" data-action="youtube" data-idx="${idx}" title="Copy YouTube link">
                <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M21.582 7.186a2.506 2.506 0 0 0-1.768-1.768C18.254 5 12 5 12 5s-6.254 0-7.814.418A2.506 2.506 0 0 0 2.418 7.186C2 8.746 2 12 2 12s0 3.254.418 4.814a2.506 2.506 0 0 0 1.768 1.768C5.746 19 12 19 12 19s6.254 0 7.814-.418a2.506 2.506 0 0 0 1.768-1.768C22 15.254 22 12 22 12s0-3.254-.418-4.814zM10 15.464V8.536L16 12l-6 3.464z"/></svg>
               </button>` : '';
        const spotifyBtn = `<button class="queue-share-btn" data-action="spotify" data-idx="${idx}" title="Copy Spotify link">
                <svg viewBox="0 0 24 24" width="14" height="14"><path fill="currentColor" d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/></svg>
               </button>`;
        const scoreTag = track.match_score ? `<span class="queue-match-score" style="color:${getMatchScoreColor(track.match_score)}">${track.match_score}</span>` : '';
        item.innerHTML = `
            <span class="queue-num">${idx === currentIndex ? '&#9835;' : idx + 1}</span>
            <div class="queue-info">
                <span class="queue-track-title">${track.title || '—'}</span>
                <span class="queue-track-artist">${track.artist || '—'}</span>
            </div>
            ${scoreTag}
            <div class="queue-share-group">${ytBtn}${spotifyBtn}</div>
            <span class="queue-duration">${track.duration || ''}</span>
        `;
        if (idx !== currentIndex) {
            item.style.cursor = 'pointer';
            item.addEventListener('click', (e) => {
                if (e.target.closest('.queue-share-btn')) return;
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
document.getElementById('btn-shuffle')?.addEventListener('click', toggleShuffle);

// ---- Channel Bindings ----
document.getElementById('btn-new-channel')?.addEventListener('click', openNewChannelDialog);
document.getElementById('new-channel-form')?.addEventListener('submit', createChannel);
document.getElementById('spotify-url-input')?.addEventListener('blur', previewSpotifyPlaylist);
document.getElementById('youtube-url-input')?.addEventListener('blur', previewYouTubePlaylist);
document.getElementById('btn-cancel-channel')?.addEventListener('click', () => {
    document.getElementById('new-channel-dialog').close();
});
document.getElementById('btn-stop-loading')?.addEventListener('click', stopLoading);
document.getElementById('btn-cancel-refresh')?.addEventListener('click', stopLoading);
document.querySelectorAll('input[name="channel-type"]').forEach(r => {
    r.addEventListener('change', toggleChannelTypeFields);
});

document.getElementById('channel-list')?.addEventListener('click', (e) => {
    // Ignore clicks on discovery slider, era picker, AI model, or num songs areas
    if (e.target.closest('.channel-discovery')) return;
    if (e.target.closest('.channel-era')) return;
    if (e.target.closest('.channel-ai-model')) return;
    if (e.target.closest('.channel-num-songs')) return;
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
    updateDiscoveryLabel(e.target);

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
                loadPlaylistSSE(true);
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

// ---- AI Model Select ----
// Set initial selected value for all AI model selects on page load
document.querySelectorAll('.channel-ai-model-select').forEach(sel => {
    const model = sel.dataset.aiModel;
    if (model) sel.value = model;
});

document.getElementById('channel-list')?.addEventListener('change', (e) => {
    if (!e.target.classList.contains('channel-ai-model-select')) return;
    const wrapper = e.target.closest('.channel-ai-model');
    if (!wrapper) return;
    const channelId = wrapper.dataset.channelId;
    const aiModel = e.target.value;
    saveChannelAiModel(channelId, aiModel);
});

async function saveChannelAiModel(channelId, aiModel) {
    try {
        await fetch(`/api/radio/channels/${encodeURIComponent(channelId)}/ai-model`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ai_model: aiModel }),
        });
        // Auto-refresh if this is the active channel
        if (channelId === activeChannelId) {
            await fetch(`/api/radio/refresh-playlist?channel_id=${encodeURIComponent(channelId)}`);
            loadPlaylistSSE(true);
        }
    } catch (err) {
        console.error('Failed to save AI model:', err);
    }
}

// ---- Num Songs Slider ----
let numSongsDebounce = null;
document.getElementById('channel-list')?.addEventListener('input', (e) => {
    if (!e.target.classList.contains('channel-num-songs-slider')) return;
    const wrapper = e.target.closest('.channel-num-songs');
    if (!wrapper) return;
    const channelId = wrapper.dataset.channelId;
    const value = parseInt(e.target.value);
    e.target.title = `Playlist size: ${value}`;
    const label = wrapper.querySelector('.num-songs-label');
    if (label) label.textContent = value;

    clearTimeout(numSongsDebounce);
    numSongsDebounce = setTimeout(async () => {
        try {
            await fetch(`/api/radio/channels/${encodeURIComponent(channelId)}/num-songs`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ num_songs: value }),
            });
        } catch (err) {}
    }, 400);
});

// ---- Create Dialog: Num Songs label ----
document.getElementById('new-channel-num-songs')?.addEventListener('input', (e) => {
    const label = document.getElementById('new-channel-num-songs-label');
    if (label) label.textContent = e.target.value;
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
        case 'KeyY': toggleLyrics(); break;
        case 'KeyS': toggleShuffle(); break;
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

        const hue = (typeof vizBaseHue !== 'undefined' ? vizBaseHue : 200) + (i / barCount) * 160;
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
        artwork: (track.albumArt || track.thumbnail) ? [{ src: track.albumArt || track.thumbnail, sizes: '480x360', type: 'image/jpeg' }] : [],
    });
    navigator.mediaSession.playbackState = 'playing';
}

// ---- Copy / Share ----
function showCopyToast(message) {
    const toast = document.getElementById('copy-toast');
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2000);
}

function copyToClipboard(text, label) {
    navigator.clipboard.writeText(text).then(() => {
        showCopyToast(`${label} copied!`);
    }).catch(() => {
        showCopyToast('Copy failed');
    });
}

function getTrackText(track) {
    let text = `${track.artist} — ${track.title}`;
    if (track.album) text += ` (${track.album})`;
    return text;
}

function getYouTubeUrl(track) {
    return track.videoId ? `https://www.youtube.com/watch?v=${track.videoId}` : null;
}

function getSpotifySearchUrl(track) {
    const q = encodeURIComponent(`${track.artist} ${track.title}`);
    return `https://open.spotify.com/search/${q}`;
}

// ---- Mindmap Integration ----
let mindmapGraph = null;
let mindmapVisible = false;

function initMindmap() {
    const canvas = document.getElementById('mindmap-canvas');
    if (!canvas || mindmapGraph) return;
    mindmapGraph = new ForceGraph(canvas, { onNodeClick: onMindmapNodeClick });
}

function toggleMindmap() {
    mindmapVisible = !mindmapVisible;
    const panel = document.getElementById('mindmap-panel');
    const artworkSection = document.querySelector('.player-artwork-section');
    const btn = document.getElementById('btn-mindmap');

    if (mindmapVisible) {
        // Close lyrics if open
        if (lyricsVisible) {
            lyricsVisible = false;
            document.getElementById('lyrics-panel').style.display = 'none';
            document.getElementById('btn-lyrics')?.classList.remove('mindmap-active');
            stopLyricsSync();
        }
        panel.style.display = '';
        artworkSection.style.display = 'none';
        btn.classList.add('mindmap-active');
        initMindmap();
        updateMindmapForCurrentTrack();
        mindmapGraph.start();
    } else {
        panel.style.display = 'none';
        artworkSection.style.display = '';
        btn.classList.remove('mindmap-active');
        if (mindmapGraph) mindmapGraph.stop();
    }
}

function updateMindmapForCurrentTrack() {
    if (!mindmapGraph || currentIndex < 0) return;
    const track = queue[currentIndex];
    if (!track) return;

    mindmapGraph.clear();

    const centerId = 'current';
    mindmapGraph.addNode({
        id: centerId,
        label: track.title,
        sublabel: track.artist,
        imageUrl: track.albumArt || track.thumbnail || '',
        type: 'current',
        radius: 45,
    });
    mindmapGraph.setCenter(centerId);

    // Add similar_to connections from the playlist data (strongest connections)
    const simNodes = [];
    if (track.similar_to && track.similar_to.length > 0) {
        const isStringArr = typeof track.similar_to[0] === 'string';
        track.similar_to.forEach((sim, i) => {
            const nodeId = `sim-${i}`;
            const label = isStringArr ? '' : (sim.album || '');
            const sublabel = isStringArr ? sim : (sim.artist || '');
            const why = isStringArr ? '' : (sim.why || '');
            mindmapGraph.addNode({
                id: nodeId,
                label: label,
                sublabel: sublabel,
                type: 'collection',
                radius: 32,
                depth: 1,
            });
            mindmapGraph.addEdge(centerId, nodeId, why, 0.9);
            simNodes.push(nodeId);
        });
    }

    // Connect nearby queue songs that share similar_to artists
    const _getArtist = (s) => typeof s === 'string' ? s : (s.artist || '');
    const currentSimArtists = new Set(
        (track.similar_to || []).map(s => _getArtist(s).toLowerCase())
    );
    queue.forEach((other, i) => {
        if (i === currentIndex || !other.similar_to) return;
        const shared = other.similar_to.find(s => currentSimArtists.has(_getArtist(s).toLowerCase()));
        if (shared && !mindmapGraph.nodes.has(`queue-${i}`)) {
            mindmapGraph.addNode({
                id: `queue-${i}`,
                label: other.title,
                sublabel: other.artist,
                type: 'playlist',
                radius: 24,
                depth: 1,
            });
            mindmapGraph.addEdge(centerId, `queue-${i}`, `Both relate to ${_getArtist(shared)}`, 0.5);
        }
    });

    mindmapGraph.resize();

    // Auto-expand: fetch related artists for the current track
    autoExpandMindmap(track, centerId, simNodes);
}

async function autoExpandMindmap(track, centerId, simNodes) {
    // Expand the center node (current track) — strong connections
    const _aiModel = encodeURIComponent(getActiveAiModel());
    try {
        const resp = await fetch(
            `/api/mindmap/expand?artist=${encodeURIComponent(track.artist)}&album=${encodeURIComponent(track.album || track.title)}&ai_model=${_aiModel}`
        );
        if (resp.ok) {
            const data = await resp.json();
            (data.related || []).forEach((rel, i) => {
                const childId = `center-c${i}`;
                const isDuplicate = mindmapGraph.nodes && Array.from(mindmapGraph.nodes.values()).some(
                    n => n.sublabel && n.sublabel.toLowerCase() === (rel.artist || '').toLowerCase()
                );
                if (isDuplicate) return;
                mindmapGraph.addNode({
                    id: childId,
                    label: rel.album || '',
                    sublabel: rel.artist,
                    type: rel.in_collection ? 'collection' : 'recommended',
                    radius: 26,
                    depth: 1,
                });
                mindmapGraph.addEdge(centerId, childId, rel.why || '', 0.7);
            });
        }
    } catch (e) { /* ignore */ }

    // Also expand each similar_to node — weaker (second-level) connections
    for (const nodeId of simNodes) {
        const node = mindmapGraph.nodes?.get(nodeId);
        if (!node || node.expanded) continue;
        node.expanded = true;
        try {
            const resp = await fetch(
                `/api/mindmap/expand?artist=${encodeURIComponent(node.sublabel)}&album=${encodeURIComponent(node.label)}&ai_model=${_aiModel}`
            );
            if (!resp.ok) continue;
            const data = await resp.json();
            (data.related || []).forEach((rel, i) => {
                const childId = `${nodeId}-c${i}`;
                const isDuplicate = mindmapGraph.nodes && Array.from(mindmapGraph.nodes.values()).some(
                    n => n.sublabel && n.sublabel.toLowerCase() === (rel.artist || '').toLowerCase()
                );
                if (isDuplicate) return;
                mindmapGraph.addNode({
                    id: childId,
                    label: rel.album || '',
                    sublabel: rel.artist,
                    type: rel.in_collection ? 'collection' : 'recommended',
                    radius: 20,
                    depth: 2,
                });
                mindmapGraph.addEdge(nodeId, childId, rel.why || '', 0.35);
            });
        } catch (e) { /* ignore */ }
    }
}

async function onMindmapNodeClick(node) {
    if (node.expanded || node.id === 'current') return;
    node.expanded = true;
    const depth = (node.depth || 1) + 1;
    const strength = Math.max(0.2, 0.7 - depth * 0.15);
    try {
        const resp = await fetch(
            `/api/mindmap/expand?artist=${encodeURIComponent(node.sublabel)}&album=${encodeURIComponent(node.label)}&ai_model=${encodeURIComponent(getActiveAiModel())}`
        );
        if (!resp.ok) return;
        const data = await resp.json();
        (data.related || []).forEach((rel, i) => {
            const childId = `${node.id}-c${i}`;
            const isDuplicate = mindmapGraph.nodes && Array.from(mindmapGraph.nodes.values()).some(
                n => n.sublabel && n.sublabel.toLowerCase() === (rel.artist || '').toLowerCase()
            );
            if (isDuplicate) return;
            mindmapGraph.addNode({
                id: childId,
                label: rel.album || '',
                sublabel: rel.artist,
                type: rel.in_collection ? 'collection' : 'recommended',
                radius: Math.max(16, 26 - depth * 4),
                depth: depth,
            });
            mindmapGraph.addEdge(node.id, childId, rel.why || '', strength);
        });
    } catch (e) { /* ignore */ }
}

document.getElementById('btn-mindmap')?.addEventListener('click', toggleMindmap);

// ---- Upload File Preview ----
document.getElementById('upload-file-input')?.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    document.getElementById('upload-preview-name').textContent = file.name;
    document.getElementById('upload-preview-size').textContent = (file.size / 1024).toFixed(1) + ' KB';
    document.getElementById('upload-preview').style.display = 'flex';
    const nameInput = document.getElementById('channel-name-input');
    if (!nameInput.value) {
        nameInput.value = file.name.replace(/\.(txt|pdf)$/i, '');
    }
});

// Now-playing share buttons
document.getElementById('btn-copy-text')?.addEventListener('click', () => {
    const track = queue[currentIndex];
    if (!track) return;
    copyToClipboard(getTrackText(track), 'Song info');
});

document.getElementById('btn-copy-youtube')?.addEventListener('click', () => {
    const track = queue[currentIndex];
    if (!track) return;
    const url = getYouTubeUrl(track);
    if (url) copyToClipboard(url, 'YouTube link');
});

document.getElementById('btn-copy-spotify')?.addEventListener('click', () => {
    const track = queue[currentIndex];
    if (!track) return;
    copyToClipboard(getSpotifySearchUrl(track), 'Spotify link');
});

// Queue share buttons (delegated)
document.getElementById('queue-list')?.addEventListener('click', (e) => {
    const btn = e.target.closest('.queue-share-btn');
    if (!btn) return;
    e.stopPropagation();
    const idx = parseInt(btn.dataset.idx);
    const track = queue[idx];
    if (!track) return;

    if (btn.dataset.action === 'youtube') {
        const url = getYouTubeUrl(track);
        if (url) copyToClipboard(url, 'YouTube link');
    } else if (btn.dataset.action === 'spotify') {
        copyToClipboard(getSpotifySearchUrl(track), 'Spotify link');
    }
});

// ====================================================================
// LYRICS & MEANING PANEL
// ====================================================================

let lyricsVisible = false;
let lyricsCache = {};       // { "artist|title": { synced, plain, instrumental } }
let meaningCache = {};      // { "artist|title": { summary, themes, mood, ... } }
let parsedLyrics = [];      // [{ time: seconds, text: string }]
let lyricsSyncInterval = null;
let activeLyricIndex = -1;

// ---- Toggle Panel ----
function toggleLyrics() {
    lyricsVisible = !lyricsVisible;
    const panel = document.getElementById('lyrics-panel');
    const artworkSection = document.querySelector('.player-artwork-section');
    const btn = document.getElementById('btn-lyrics');

    if (lyricsVisible) {
        // Close mindmap if open
        if (mindmapVisible) toggleMindmap();

        panel.style.display = 'flex';
        artworkSection.style.display = 'none';
        btn.classList.add('mindmap-active');

        // Fetch lyrics for current track
        if (currentIndex >= 0 && queue[currentIndex]) {
            fetchLyricsForTrack(queue[currentIndex]);
        }
        startLyricsSync();
    } else {
        panel.style.display = 'none';
        artworkSection.style.display = '';
        btn.classList.remove('mindmap-active');
        stopLyricsSync();
    }
}

document.getElementById('btn-lyrics')?.addEventListener('click', toggleLyrics);

// ---- Tab Switching ----
document.querySelectorAll('.lyrics-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        const target = tab.dataset.tab;
        document.querySelectorAll('.lyrics-tab').forEach(t => t.classList.remove('lyrics-tab-active'));
        document.querySelectorAll('.lyrics-tab-content').forEach(c => c.classList.remove('lyrics-tab-visible'));
        tab.classList.add('lyrics-tab-active');
        document.getElementById(`lyrics-tab-${target}`)?.classList.add('lyrics-tab-visible');

        // Lazy-load meaning/vibe when tab is first clicked
        if ((target === 'meaning' || target === 'vibe') && currentIndex >= 0) {
            fetchMeaningForTrack(queue[currentIndex]);
        }
    });
});

// ---- Fetch Lyrics ----
async function fetchLyricsForTrack(track) {
    if (!track) return;
    const key = `${track.artist}|${track.title}`;
    const container = document.getElementById('lyrics-container');

    // Check cache
    if (lyricsCache[key]) {
        renderLyrics(lyricsCache[key]);
        return;
    }

    container.innerHTML = '<div class="lyrics-placeholder">Loading lyrics...</div>';

    try {
        const resp = await fetch(`/api/lyrics?artist=${encodeURIComponent(track.artist)}&title=${encodeURIComponent(track.title)}&ai_model=${encodeURIComponent(getActiveAiModel())}`);
        const data = await resp.json();
        lyricsCache[key] = data;

        // Cap cache size
        const keys = Object.keys(lyricsCache);
        if (keys.length > 200) delete lyricsCache[keys[0]];

        renderLyrics(data);
    } catch (e) {
        container.innerHTML = '<div class="lyrics-placeholder">Could not load lyrics</div>';
    }
}

// ---- Render Lyrics ----
function renderLyrics(data) {
    const container = document.getElementById('lyrics-container');
    parsedLyrics = [];
    activeLyricIndex = -1;

    if (!data.found) {
        container.innerHTML = '<div class="lyrics-placeholder">No lyrics found for this song</div>';
        return;
    }

    if (data.instrumental) {
        container.innerHTML = `
            <div class="lyrics-instrumental">
                <svg viewBox="0 0 24 24" width="48" height="48"><path fill="currentColor" d="M12 3v10.55c-.59-.34-1.27-.55-2-.55C7.79 13 6 14.79 6 17s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>
                <span>Instrumental</span>
            </div>`;
        return;
    }

    if (data.syncedLyrics) {
        parsedLyrics = parseSyncedLyrics(data.syncedLyrics);
        container.innerHTML = parsedLyrics.map((line, i) =>
            `<div class="lyrics-line" data-index="${i}" data-time="${line.time}">${line.text || '&nbsp;'}</div>`
        ).join('');

        // Click-to-seek
        container.querySelectorAll('.lyrics-line').forEach(el => {
            el.addEventListener('click', () => {
                const time = parseFloat(el.dataset.time);
                if (player && !isNaN(time)) player.seekTo(time, true);
            });
        });

        startLyricsSync();
    } else if (data.plainLyrics) {
        const sourceNote = data.source === 'ai' ? '<div class="lyrics-source">Lyrics recalled by AI — may not be exact</div>' : '';
        container.innerHTML = `${sourceNote}<div class="lyrics-plain">${escapeHtml(data.plainLyrics)}</div>`;
    } else {
        container.innerHTML = '<div class="lyrics-placeholder">No lyrics found for this song</div>';
    }
}

function parseSyncedLyrics(text) {
    const lines = [];
    const regex = /\[(\d{2}):(\d{2})\.(\d{2,3})\]\s*(.*)/;
    for (const raw of text.split('\n')) {
        const m = raw.match(regex);
        if (m) {
            const mins = parseInt(m[1]);
            const secs = parseInt(m[2]);
            const ms = parseInt(m[3].padEnd(3, '0'));
            lines.push({ time: mins * 60 + secs + ms / 1000, text: m[4] });
        }
    }
    return lines;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ---- Lyrics Sync Engine ----
function startLyricsSync() {
    stopLyricsSync();
    if (parsedLyrics.length === 0) return;
    lyricsSyncInterval = setInterval(syncLyrics, 100);
}

function stopLyricsSync() {
    if (lyricsSyncInterval) {
        clearInterval(lyricsSyncInterval);
        lyricsSyncInterval = null;
    }
}

function syncLyrics() {
    if (!player || parsedLyrics.length === 0 || !lyricsVisible) return;
    try {
        const currentTime = player.getCurrentTime() || 0;
        let newIndex = -1;

        for (let i = parsedLyrics.length - 1; i >= 0; i--) {
            if (currentTime >= parsedLyrics[i].time - 0.1) {
                newIndex = i;
                break;
            }
        }

        if (newIndex === activeLyricIndex) return;
        activeLyricIndex = newIndex;

        const container = document.getElementById('lyrics-container');
        const lines = container.querySelectorAll('.lyrics-line');

        lines.forEach((el, i) => {
            el.classList.remove('lyrics-active', 'lyrics-near', 'lyrics-passed');
            if (i === newIndex) {
                el.classList.add('lyrics-active');
            } else if (Math.abs(i - newIndex) <= 2) {
                el.classList.add('lyrics-near');
            } else if (i < newIndex) {
                el.classList.add('lyrics-passed');
            }
        });

        // Scroll active line into view
        if (newIndex >= 0 && lines[newIndex]) {
            lines[newIndex].scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    } catch (e) { /* ignore */ }
}

// ---- Fetch Song Meaning ----
async function fetchMeaningForTrack(track) {
    if (!track) return;
    const key = `${track.artist}|${track.title}`;
    const meaningEl = document.getElementById('meaning-container');
    const vibeEl = document.getElementById('vibe-container');

    // Check cache
    if (meaningCache[key]) {
        renderMeaning(meaningCache[key]);
        renderVibe(meaningCache[key]);
        applyDynamicTheme(meaningCache[key]);
        return;
    }

    meaningEl.innerHTML = '<div class="meaning-loading"><div class="loading-spinner"></div>Analyzing song...</div>';
    vibeEl.innerHTML = '<div class="meaning-loading"><div class="loading-spinner"></div>Detecting vibe...</div>';

    try {
        const resp = await fetch(
            `/api/song-meaning?artist=${encodeURIComponent(track.artist)}&title=${encodeURIComponent(track.title)}&album=${encodeURIComponent(track.album || '')}&ai_model=${encodeURIComponent(getActiveAiModel())}`
        );
        const data = await resp.json();
        meaningCache[key] = data;

        // Cap cache size
        const keys = Object.keys(meaningCache);
        if (keys.length > 200) delete meaningCache[keys[0]];

        renderMeaning(data);
        renderVibe(data);
        applyDynamicTheme(data);
    } catch (e) {
        meaningEl.innerHTML = '<div class="lyrics-placeholder">Could not load interpretation</div>';
        vibeEl.innerHTML = '<div class="lyrics-placeholder">Could not detect vibe</div>';
    }
}

function renderMeaning(data) {
    const el = document.getElementById('meaning-container');
    if (!data.found) {
        el.innerHTML = '<div class="lyrics-placeholder">No interpretation available</div>';
        return;
    }

    let html = '';
    if (data.summary) {
        html += `<div class="meaning-section">
            <div class="meaning-section-label">Interpretation</div>
            <p class="meaning-summary">${escapeHtml(data.summary)}</p>
        </div>`;
    }
    if (data.artist_context && data.artist_context !== 'No known artist commentary.') {
        html += `<div class="meaning-section">
            <div class="meaning-section-label">Artist Commentary</div>
            <p class="meaning-context">${escapeHtml(data.artist_context)}</p>
        </div>`;
    }
    el.innerHTML = html || '<div class="lyrics-placeholder">No interpretation available</div>';
}

function renderVibe(data) {
    const el = document.getElementById('vibe-container');
    if (!data.found) {
        el.innerHTML = '<div class="lyrics-placeholder">No vibe data available</div>';
        return;
    }

    let html = '';

    // Mood badge
    if (data.mood) {
        html += `<div class="meaning-section" style="text-align:center;">
            <div class="vibe-mood-badge">${escapeHtml(data.mood)}</div>
        </div>`;
    }

    // Genre tags
    if (data.genres && data.genres.length > 0) {
        html += `<div class="meaning-section">
            <div class="meaning-section-label">Genres</div>
            <div class="vibe-tags">${data.genres.map(g => `<span class="vibe-tag genre-vibe">${escapeHtml(g)}</span>`).join('')}</div>
        </div>`;
    }

    // Theme tags
    if (data.themes && data.themes.length > 0) {
        html += `<div class="meaning-section">
            <div class="meaning-section-label">Emotional Themes</div>
            <div class="vibe-tags">${data.themes.map(t => `<span class="vibe-tag theme-vibe">${escapeHtml(t)}</span>`).join('')}</div>
        </div>`;
    }

    // Color palette preview
    if (data.color_palette && data.color_palette.primary) {
        const cp = data.color_palette;
        html += `<div class="meaning-section">
            <div class="meaning-section-label">Color Palette</div>
            <div class="vibe-color-preview">
                <div class="vibe-color-swatch" style="background:${cp.primary}"><span>Primary</span></div>
                <div class="vibe-color-swatch" style="background:${cp.secondary || cp.primary}"><span>Secondary</span></div>
                <div class="vibe-color-swatch" style="background:${cp.accent || cp.primary}"><span>Accent</span></div>
            </div>
        </div>`;
    }

    el.innerHTML = html || '<div class="lyrics-placeholder">No vibe data available</div>';
}

// ---- Dynamic Theme ----
function applyDynamicTheme(data) {
    const playerEl = document.getElementById('radio-player');
    if (!playerEl || !data.found) return;

    playerEl.classList.add('themed');

    const cp = data.color_palette || {};
    const rawPrimary = cp.primary || '#e8a03e';
    const rawSecondary = cp.secondary || '#e85d75';
    const rawAccent = cp.accent || rawPrimary;

    // Ensure accent/text colors are light enough to read on dark backgrounds
    const primary = ensureLightColor(rawPrimary);
    const secondary = ensureLightColor(rawSecondary);
    const accent = ensureLightColor(rawAccent);

    // Set CSS custom properties on the player element
    playerEl.style.setProperty('--dynamic-primary', primary);
    playerEl.style.setProperty('--dynamic-secondary', secondary);
    playerEl.style.setProperty('--dynamic-accent', accent);
    playerEl.style.setProperty('--dynamic-glow', hexToRgba(primary, 0.35));
    playerEl.style.setProperty('--dynamic-accent-bg', hexToRgba(accent, 0.15));
    playerEl.style.setProperty('--dynamic-accent-border', hexToRgba(accent, 0.3));

    // Background gradient — force dark colors so text stays readable
    if (data.bg_gradient) {
        playerEl.style.setProperty('--dynamic-bg', darkenGradient(data.bg_gradient));
    } else {
        playerEl.style.setProperty('--dynamic-bg',
            `radial-gradient(ellipse at 20% 50%, ${hexToRgba(primary, 0.1)} 0%, transparent 70%),
             radial-gradient(ellipse at 80% 50%, ${hexToRgba(secondary, 0.08)} 0%, transparent 70%)`
        );
    }

    // Also theme the lyrics panel
    const lyricsPanel = document.getElementById('lyrics-panel');
    if (lyricsPanel) {
        lyricsPanel.style.setProperty('--dynamic-accent', accent);
        lyricsPanel.style.setProperty('--dynamic-glow', hexToRgba(accent, 0.5));
        lyricsPanel.style.setProperty('--dynamic-accent-bg', hexToRgba(accent, 0.15));
        lyricsPanel.style.setProperty('--dynamic-accent-border', hexToRgba(accent, 0.3));
    }

    // Visualizer color hue shift
    const hue = hexToHue(primary);
    if (vizBars) vizBaseHue = hue;
}

let vizBaseHue = 200; // default

function hexToRgb(hex) {
    hex = hex.replace('#', '');
    if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
    return {
        r: parseInt(hex.substring(0, 2), 16),
        g: parseInt(hex.substring(2, 4), 16),
        b: parseInt(hex.substring(4, 6), 16)
    };
}

function hexToRgba(hex, alpha) {
    const {r, g, b} = hexToRgb(hex);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function relativeLuminance({r, g, b}) {
    const [rs, gs, bs] = [r, g, b].map(c => {
        c = c / 255;
        return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
}

function rgbToHsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h = 0, s = 0, l = (max + min) / 2;
    if (max !== min) {
        const d = max - min;
        s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
        else if (max === g) h = ((b - r) / d + 2) / 6;
        else h = ((r - g) / d + 4) / 6;
    }
    return {h: h * 360, s, l};
}

function hslToHex(h, s, l) {
    h /= 360;
    const hue2rgb = (p, q, t) => {
        if (t < 0) t += 1; if (t > 1) t -= 1;
        if (t < 1/6) return p + (q - p) * 6 * t;
        if (t < 1/2) return q;
        if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
        return p;
    };
    let r, g, b;
    if (s === 0) { r = g = b = l; } else {
        const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
        const p = 2 * l - q;
        r = hue2rgb(p, q, h + 1/3);
        g = hue2rgb(p, q, h);
        b = hue2rgb(p, q, h - 1/3);
    }
    return '#' + [r, g, b].map(c => Math.round(c * 255).toString(16).padStart(2, '0')).join('');
}

/** Ensure a hex color is light enough to read on a dark background (min luminance 0.18) */
function ensureLightColor(hex) {
    const rgb = hexToRgb(hex);
    const lum = relativeLuminance(rgb);
    if (lum >= 0.18) return hex;
    const {h, s, l} = rgbToHsl(rgb.r, rgb.g, rgb.b);
    // Boost lightness until luminance is sufficient
    let newL = l;
    for (let i = 0; i < 20 && newL < 0.95; i++) {
        newL = Math.min(newL + 0.05, 0.95);
        const test = hslToHex(h, s, newL);
        if (relativeLuminance(hexToRgb(test)) >= 0.18) return test;
    }
    return hslToHex(h, Math.max(s, 0.5), 0.75);
}

/** Force a bg_gradient to be dark by clamping lightness of any hex colors found */
function darkenGradient(gradientStr) {
    return gradientStr.replace(/#([0-9a-fA-F]{3,6})\b/g, (match) => {
        const rgb = hexToRgb(match);
        const {h, s, l} = rgbToHsl(rgb.r, rgb.g, rgb.b);
        if (l > 0.35) return hslToHex(h, s, Math.min(l, 0.25));
        return match;
    });
}

function hexToHue(hex) {
    const {r, g, b} = hexToRgb(hex);
    const {h} = rgbToHsl(r, g, b);
    return Math.round(h);
}

function clearDynamicTheme() {
    const playerEl = document.getElementById('radio-player');
    if (!playerEl) return;
    playerEl.classList.remove('themed');
    ['--dynamic-primary', '--dynamic-secondary', '--dynamic-accent', '--dynamic-glow',
     '--dynamic-accent-bg', '--dynamic-accent-border', '--dynamic-bg'].forEach(p => {
        playerEl.style.removeProperty(p);
    });
    vizBaseHue = 200;
}
