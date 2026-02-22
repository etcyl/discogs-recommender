// ---------------------------------------------------------------------------
// Radio Mode — YouTube IFrame API + Queue Management
// ---------------------------------------------------------------------------

let player = null;
let queue = [];
let currentIndex = -1;
let isPlaying = false;
let progressInterval = null;
let isSeeking = false;
let likedSet = new Set();

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
    // Error codes: 2=invalid param, 5=HTML5 error, 100=not found, 101/150=embed blocked
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
    } else if (event.data === YT.PlayerState.PAUSED) {
        isPlaying = false;
        showPlayIcon();
        stopProgressUpdates();
    }
}

// ---- Playlist Loading (SSE with progress) ----
function loadPlaylistSSE() {
    showLoading(true);
    const es = new EventSource('/api/radio/playlist-stream');

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
            showError('No songs found. Try adding more to your Discogs collection.');
        }
    });

    es.addEventListener('error', (e) => {
        // SSE sends a custom "error" event with data, or the connection may just fail
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
    await fetch('/api/radio/refresh-playlist');
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

    // Render "similar to" collection items
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

// Progress bar seeking
const progressBar = document.getElementById('progress-bar');
if (progressBar) {
    progressBar.addEventListener('click', (e) => {
        if (!player) return;
        const rect = progressBar.getBoundingClientRect();
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

// ---- Thumbs Down (Dislike + Auto-Skip) ----
let dislikedSet = new Set();

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

// ---- Keyboard Shortcuts ----
document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    switch (e.code) {
        case 'Space': e.preventDefault(); togglePlay(); break;
        case 'ArrowRight': playNext(); break;
        case 'ArrowLeft': playPrev(); break;
        case 'KeyD': thumbsDown(); break;
        case 'KeyL': thumbsUp(); break;
    }
});

// ---- Visualizer (canvas bars animation) ----
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

    // Simulate audio-reactive bars
    for (let i = 0; i < barCount; i++) {
        if (isPlaying) {
            // Random target heights that change smoothly
            if (Math.random() < 0.1) {
                vizBars[i].target = Math.random() * h * 0.85 + h * 0.05;
            }
        } else {
            vizBars[i].target = h * 0.03;
        }
        // Smooth spring animation
        const bar = vizBars[i];
        const spring = 0.08;
        const damping = 0.75;
        bar.velocity = (bar.velocity + (bar.target - bar.height) * spring) * damping;
        bar.height += bar.velocity;

        const x = i * (barWidth + gap) + gap / 2;
        const barH = Math.max(2, bar.height);

        // Gradient color per bar
        const hue = 200 + (i / barCount) * 160; // blue to pink
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
