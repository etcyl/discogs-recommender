/* ====================================================================
   ForceGraph — Canvas-based force-directed graph with particle effects
   ==================================================================== */

class MindmapNode {
    constructor({ id, label, sublabel = '', imageUrl = '', type = 'recommended', radius = 30, depth = 1 }) {
        this.id = id;
        this.label = label;
        this.sublabel = sublabel;
        this.imageUrl = imageUrl;
        this.type = type;
        this.x = 0;
        this.y = 0;
        this.vx = 0;
        this.vy = 0;
        this.radius = radius;
        this.depth = depth;
        this.isHovered = false;
        this.isDragging = false;
        this.expanded = false;
        this._img = null;
        this._imgLoaded = false;
        this._pulsePhase = Math.random() * Math.PI * 2;

        if (imageUrl) {
            this._img = new Image();
            this._img.crossOrigin = 'anonymous';
            this._img.onload = () => { this._imgLoaded = true; };
            this._img.src = imageUrl;
        }
    }

    get color() {
        return MindmapNode.COLORS[this.type] || MindmapNode.COLORS.recommended;
    }
}

MindmapNode.COLORS = {
    current: '#e8a03e',
    collection: '#a8d5a2',
    recommended: '#a2c5d5',
    playlist: '#c5a2d5',
};


class MindmapEdge {
    constructor(sourceId, targetId, label = '', strength = 0.5) {
        this.sourceId = sourceId;
        this.targetId = targetId;
        this.label = label;
        this.strength = Math.max(0.1, Math.min(1.0, strength));
        this.particles = [];
        // Fewer particles, much slower — gentle drift, not a zippy stream
        const count = 1 + Math.floor(this.strength * 2);
        for (let i = 0; i < count; i++) {
            this.particles.push({
                progress: Math.random(),
                speed: 0.0005 + Math.random() * 0.001,  // ~5-10x slower
                size: 2 + this.strength * 2,
            });
        }
    }
}


class ForceGraph {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.nodes = new Map();
        this.edges = [];
        this.centerId = null;
        this.animFrame = null;
        this.isRunning = false;
        this.onNodeClick = options.onNodeClick || null;
        this.time = 0;

        // Mouse state
        this.mouse = { x: 0, y: 0 };
        this.hoveredNode = null;
        this.dragNode = null;
        this.isDragging = false;

        // Physics
        this.CENTER_GRAVITY = 0.01;
        this.REPULSION = 5000;
        this.SPRING_K = 0.005;
        this.SPRING_LENGTH = 140;
        this.DAMPING = 0.88;
        this.BOUNDARY_FORCE = 0.05;
        this.MAX_NODES = 30;

        this._boundMouseMove = this._onMouseMove.bind(this);
        this._boundMouseDown = this._onMouseDown.bind(this);
        this._boundMouseUp = this._onMouseUp.bind(this);
        this._boundClick = this._onClick.bind(this);
        this._boundResize = this.resize.bind(this);

        this._bindEvents();
        this.resize();
    }

    // --- Public API ---

    addNode(data) {
        if (this.nodes.size >= this.MAX_NODES) return null;
        const node = new MindmapNode(data);
        // Place near center with some jitter
        const cx = this.canvas.width / (2 * this._dpr);
        const cy = this.canvas.height / (2 * this._dpr);
        node.x = cx + (Math.random() - 0.5) * 60;
        node.y = cy + (Math.random() - 0.5) * 60;
        this.nodes.set(node.id, node);
        return node;
    }

    addEdge(sourceId, targetId, label = '', strength = 0.5) {
        if (!this.nodes.has(sourceId) || !this.nodes.has(targetId)) return null;
        // Prevent duplicates
        const exists = this.edges.some(
            e => (e.sourceId === sourceId && e.targetId === targetId) ||
                 (e.sourceId === targetId && e.targetId === sourceId)
        );
        if (exists) return null;
        const edge = new MindmapEdge(sourceId, targetId, label, strength);
        this.edges.push(edge);
        return edge;
    }

    setCenter(nodeId) {
        this.centerId = nodeId;
        const node = this.nodes.get(nodeId);
        if (node) {
            const cx = this.canvas.width / (2 * this._dpr);
            const cy = this.canvas.height / (2 * this._dpr);
            node.x = cx;
            node.y = cy;
        }
    }

    clear() {
        this.nodes.clear();
        this.edges = [];
        this.centerId = null;
        this.hoveredNode = null;
        this.dragNode = null;
        this.time = 0;
    }

    resize() {
        const rect = this.canvas.getBoundingClientRect();
        this._dpr = window.devicePixelRatio || 1;
        this.canvas.width = rect.width * this._dpr;
        this.canvas.height = rect.height * this._dpr;
        this.ctx.setTransform(this._dpr, 0, 0, this._dpr, 0, 0);
    }

    start() {
        if (this.isRunning) return;
        this.isRunning = true;
        const tick = () => {
            if (!this.isRunning) return;
            this.time += 0.016;
            this._applyForces();
            this._updateParticles();
            this._draw();
            this.animFrame = requestAnimationFrame(tick);
        };
        tick();
    }

    stop() {
        this.isRunning = false;
        if (this.animFrame) {
            cancelAnimationFrame(this.animFrame);
            this.animFrame = null;
        }
    }

    destroy() {
        this.stop();
        this.canvas.removeEventListener('mousemove', this._boundMouseMove);
        this.canvas.removeEventListener('mousedown', this._boundMouseDown);
        window.removeEventListener('mouseup', this._boundMouseUp);
        this.canvas.removeEventListener('click', this._boundClick);
        window.removeEventListener('resize', this._boundResize);
    }

    // --- Physics ---

    _applyForces() {
        const w = this.canvas.width / this._dpr;
        const h = this.canvas.height / this._dpr;
        const cx = w / 2;
        const cy = h / 2;
        const nodesArr = [...this.nodes.values()];

        // Center gravity
        for (const n of nodesArr) {
            if (n.isDragging) continue;
            n.vx += (cx - n.x) * this.CENTER_GRAVITY;
            n.vy += (cy - n.y) * this.CENTER_GRAVITY;
        }

        // Repulsion (all pairs)
        for (let i = 0; i < nodesArr.length; i++) {
            for (let j = i + 1; j < nodesArr.length; j++) {
                const a = nodesArr[i];
                const b = nodesArr[j];
                let dx = b.x - a.x;
                let dy = b.y - a.y;
                let distSq = dx * dx + dy * dy;
                if (distSq < 1) distSq = 1;
                const dist = Math.sqrt(distSq);
                const force = this.REPULSION / distSq;
                const fx = (dx / dist) * force;
                const fy = (dy / dist) * force;
                if (!a.isDragging) { a.vx -= fx; a.vy -= fy; }
                if (!b.isDragging) { b.vx += fx; b.vy += fy; }
            }
        }

        // Edge springs — stronger connections pull tighter
        for (const edge of this.edges) {
            const a = this.nodes.get(edge.sourceId);
            const b = this.nodes.get(edge.targetId);
            if (!a || !b) continue;
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const dist = Math.sqrt(dx * dx + dy * dy) || 1;
            // Stronger edges have shorter ideal length
            const idealLen = this.SPRING_LENGTH * (1.2 - edge.strength * 0.4);
            const displacement = dist - idealLen;
            const force = this.SPRING_K * displacement * (0.5 + edge.strength * 0.5);
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            if (!a.isDragging) { a.vx += fx; a.vy += fy; }
            if (!b.isDragging) { b.vx -= fx; b.vy -= fy; }
        }

        // Boundary containment + damping + integrate
        const pad = 20;
        for (const n of nodesArr) {
            if (n.isDragging) { n.vx = 0; n.vy = 0; continue; }

            // Soft boundary
            if (n.x < pad + n.radius) n.vx += this.BOUNDARY_FORCE * (pad + n.radius - n.x);
            if (n.x > w - pad - n.radius) n.vx -= this.BOUNDARY_FORCE * (n.x - (w - pad - n.radius));
            if (n.y < pad + n.radius) n.vy += this.BOUNDARY_FORCE * (pad + n.radius - n.y);
            if (n.y > h - pad - n.radius) n.vy -= this.BOUNDARY_FORCE * (n.y - (h - pad - n.radius));

            n.vx *= this.DAMPING;
            n.vy *= this.DAMPING;
            n.x += n.vx;
            n.y += n.vy;

            // Hard clamp
            n.x = Math.max(pad, Math.min(w - pad, n.x));
            n.y = Math.max(pad, Math.min(h - pad, n.y));
        }
    }

    _updateParticles() {
        for (const edge of this.edges) {
            for (const p of edge.particles) {
                p.progress += p.speed;
                if (p.progress >= 1) p.progress -= 1;
            }
        }
    }

    // --- Rendering ---

    _draw() {
        const w = this.canvas.width / this._dpr;
        const h = this.canvas.height / this._dpr;
        const ctx = this.ctx;

        ctx.clearRect(0, 0, w, h);

        // Draw edges (line + glow together)
        for (const edge of this.edges) {
            this._drawEdge(edge);
        }

        // Draw nodes
        for (const [, node] of this.nodes) {
            this._drawNode(node);
        }

        // Draw tooltip for hovered node
        if (this.hoveredNode && !this.isDragging) {
            this._drawTooltip(this.hoveredNode);
        }
    }

    _drawEdge(edge) {
        const source = this.nodes.get(edge.sourceId);
        const target = this.nodes.get(edge.targetId);
        if (!source || !target) return;
        const ctx = this.ctx;
        const s = edge.strength;

        const cp = this._bezierControl(source, target);

        // Edge line — width and opacity scale with strength
        ctx.beginPath();
        ctx.moveTo(source.x, source.y);
        ctx.quadraticCurveTo(cp.x, cp.y, target.x, target.y);
        ctx.strokeStyle = `rgba(255, 255, 255, ${0.04 + s * 0.12})`;
        ctx.lineWidth = 0.5 + s * 2;
        ctx.stroke();

        // Soft glow particles — gentle drift along the edge
        const color = source.color;
        for (const p of edge.particles) {
            const pt = this._pointOnBezier(source, target, cp, p.progress);
            // Gentle breathing: slow sine wave modulates alpha
            const breath = 0.4 + 0.6 * Math.sin(this.time * 0.8 + p.progress * Math.PI * 2);
            const alpha = s * 0.35 * breath;
            const glowSize = p.size * (1 + s);

            const grad = ctx.createRadialGradient(pt.x, pt.y, 0, pt.x, pt.y, glowSize * 2.5);
            grad.addColorStop(0, this._colorAlpha(color, alpha));
            grad.addColorStop(0.4, this._colorAlpha(color, alpha * 0.4));
            grad.addColorStop(1, this._colorAlpha(color, 0));

            ctx.beginPath();
            ctx.arc(pt.x, pt.y, glowSize * 2.5, 0, Math.PI * 2);
            ctx.fillStyle = grad;
            ctx.fill();
        }
    }

    _drawNode(node) {
        const ctx = this.ctx;
        const r = node.radius;
        const isCenter = node.id === this.centerId;

        // Gentle glow for center node — slow soft breathing, NOT a strobe
        if (isCenter) {
            const breath = 0.5 + 0.5 * Math.sin(this.time * 0.6 + node._pulsePhase);
            const glowR = r + 6 + breath * 4;
            const glow = ctx.createRadialGradient(node.x, node.y, r * 0.6, node.x, node.y, glowR);
            glow.addColorStop(0, this._colorAlpha(node.color, 0.10 + breath * 0.05));
            glow.addColorStop(1, this._colorAlpha(node.color, 0));
            ctx.beginPath();
            ctx.arc(node.x, node.y, glowR, 0, Math.PI * 2);
            ctx.fillStyle = glow;
            ctx.fill();
        }

        // Subtle glow for non-center nodes on hover
        if (!isCenter && node.isHovered) {
            const glowR = r + 5;
            const glow = ctx.createRadialGradient(node.x, node.y, r * 0.6, node.x, node.y, glowR);
            glow.addColorStop(0, this._colorAlpha(node.color, 0.08));
            glow.addColorStop(1, this._colorAlpha(node.color, 0));
            ctx.beginPath();
            ctx.arc(node.x, node.y, glowR, 0, Math.PI * 2);
            ctx.fillStyle = glow;
            ctx.fill();
        }

        // Node background
        ctx.beginPath();
        ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
        ctx.fillStyle = this._colorAlpha(node.color, node.isHovered ? 0.35 : 0.2);
        ctx.fill();
        ctx.strokeStyle = this._colorAlpha(node.color, node.isHovered ? 0.9 : 0.6);
        ctx.lineWidth = isCenter ? 2.5 : 1.5;
        ctx.stroke();

        // Image or initials
        if (node._imgLoaded && node._img) {
            ctx.save();
            ctx.beginPath();
            ctx.arc(node.x, node.y, r - 2, 0, Math.PI * 2);
            ctx.clip();
            const size = (r - 2) * 2;
            ctx.drawImage(node._img, node.x - r + 2, node.y - r + 2, size, size);
            ctx.restore();
        } else {
            // Draw initials
            const initials = this._getInitials(node.sublabel || node.label);
            ctx.fillStyle = node.color;
            ctx.font = `bold ${Math.max(10, r * 0.55)}px -apple-system, sans-serif`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(initials, node.x, node.y);
        }

        // Label below node
        const labelY = node.y + r + 12;
        ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
        ctx.font = `${isCenter ? 'bold ' : ''}${isCenter ? 11 : 10}px -apple-system, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        const maxLabelW = Math.max(80, r * 4);
        ctx.fillText(this._truncate(node.label, 28), node.x, labelY, maxLabelW);

        if (node.sublabel) {
            ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
            ctx.font = `${isCenter ? 10 : 9}px -apple-system, sans-serif`;
            ctx.fillText(this._truncate(node.sublabel, 24), node.x, labelY + 13, maxLabelW);
        }
    }

    _drawTooltip(node) {
        // Find edge labels connected to this node
        const connections = this.edges
            .filter(e => e.sourceId === node.id || e.targetId === node.id)
            .map(e => e.label)
            .filter(Boolean);

        if (!connections.length && !node.sublabel) return;

        const ctx = this.ctx;
        const lines = [];
        if (node.label) lines.push(node.label);
        if (node.sublabel) lines.push(node.sublabel);
        connections.forEach(c => lines.push(c));

        const fontSize = 11;
        ctx.font = `${fontSize}px -apple-system, sans-serif`;
        const padding = 8;
        const lineHeight = fontSize + 4;
        const maxTextW = Math.max(...lines.map(l => ctx.measureText(l).width));
        const boxW = maxTextW + padding * 2;
        const boxH = lines.length * lineHeight + padding * 2;

        let tx = node.x - boxW / 2;
        let ty = node.y - node.radius - boxH - 10;
        const w = this.canvas.width / this._dpr;
        if (tx < 5) tx = 5;
        if (tx + boxW > w - 5) tx = w - 5 - boxW;
        if (ty < 5) ty = node.y + node.radius + 10;

        ctx.fillStyle = 'rgba(20, 20, 35, 0.92)';
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
        ctx.lineWidth = 1;
        this._roundRect(ctx, tx, ty, boxW, boxH, 6);
        ctx.fill();
        ctx.stroke();

        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        lines.forEach((line, i) => {
            ctx.fillStyle = i === 0 ? 'rgba(255, 255, 255, 0.9)' : 'rgba(255, 255, 255, 0.55)';
            ctx.font = i === 0 ? `bold ${fontSize}px -apple-system, sans-serif` : `${fontSize - 1}px -apple-system, sans-serif`;
            ctx.fillText(line, tx + padding, ty + padding + i * lineHeight, maxTextW);
        });
    }

    // --- Interaction ---

    _bindEvents() {
        this.canvas.addEventListener('mousemove', this._boundMouseMove);
        this.canvas.addEventListener('mousedown', this._boundMouseDown);
        window.addEventListener('mouseup', this._boundMouseUp);
        this.canvas.addEventListener('click', this._boundClick);
        window.addEventListener('resize', this._boundResize);

        // Touch support
        this.canvas.addEventListener('touchstart', (e) => {
            e.preventDefault();
            const touch = e.touches[0];
            const rect = this.canvas.getBoundingClientRect();
            this.mouse.x = touch.clientX - rect.left;
            this.mouse.y = touch.clientY - rect.top;
            this._onMouseDown({ clientX: touch.clientX, clientY: touch.clientY });
        }, { passive: false });

        this.canvas.addEventListener('touchmove', (e) => {
            e.preventDefault();
            const touch = e.touches[0];
            this._onMouseMove({ clientX: touch.clientX, clientY: touch.clientY });
        }, { passive: false });

        this.canvas.addEventListener('touchend', (e) => {
            const wasNode = this.dragNode;
            this._onMouseUp(e);
            if (wasNode && !this.isDragging && this.onNodeClick) {
                this.onNodeClick(wasNode);
            }
        });
    }

    _onMouseMove(e) {
        const rect = this.canvas.getBoundingClientRect();
        this.mouse.x = (e.clientX || 0) - rect.left;
        this.mouse.y = (e.clientY || 0) - rect.top;

        if (this.isDragging && this.dragNode) {
            this.dragNode.x = this.mouse.x;
            this.dragNode.y = this.mouse.y;
            return;
        }

        const hit = this._hitTest(this.mouse.x, this.mouse.y);
        if (this.hoveredNode && this.hoveredNode !== hit) {
            this.hoveredNode.isHovered = false;
        }
        this.hoveredNode = hit;
        if (hit) hit.isHovered = true;
        this.canvas.style.cursor = hit ? 'pointer' : 'default';
    }

    _onMouseDown(e) {
        const rect = this.canvas.getBoundingClientRect();
        const mx = (e.clientX || 0) - rect.left;
        const my = (e.clientY || 0) - rect.top;
        const hit = this._hitTest(mx, my);
        if (hit) {
            this.dragNode = hit;
            hit.isDragging = true;
            this.isDragging = false; // will become true on move
            this._dragStartX = mx;
            this._dragStartY = my;
        }
    }

    _onMouseUp() {
        if (this.dragNode) {
            this.dragNode.isDragging = false;
            this.dragNode = null;
        }
        this.isDragging = false;
    }

    _onClick(e) {
        const rect = this.canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const my = e.clientY - rect.top;

        // Only trigger click if we didn't drag significantly
        if (this._dragStartX !== undefined) {
            const dx = mx - this._dragStartX;
            const dy = my - this._dragStartY;
            if (dx * dx + dy * dy > 25) return; // dragged, not a click
        }

        const hit = this._hitTest(mx, my);
        if (hit && this.onNodeClick) {
            this.onNodeClick(hit);
        }
    }

    _hitTest(x, y) {
        for (const [, node] of this.nodes) {
            const dx = x - node.x;
            const dy = y - node.y;
            if (dx * dx + dy * dy <= node.radius * node.radius) {
                return node;
            }
        }
        return null;
    }

    // --- Helpers ---

    _bezierControl(source, target) {
        const mx = (source.x + target.x) / 2;
        const my = (source.y + target.y) / 2;
        const dx = target.x - source.x;
        const dy = target.y - source.y;
        const len = Math.sqrt(dx * dx + dy * dy) || 1;
        const offset = 25;
        return {
            x: mx - (dy / len) * offset,
            y: my + (dx / len) * offset,
        };
    }

    _pointOnBezier(source, target, cp, t) {
        const u = 1 - t;
        return {
            x: u * u * source.x + 2 * u * t * cp.x + t * t * target.x,
            y: u * u * source.y + 2 * u * t * cp.y + t * t * target.y,
        };
    }

    _colorAlpha(hex, alpha) {
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }

    _getInitials(text) {
        if (!text) return '?';
        const words = text.trim().split(/\s+/);
        if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
        return (words[0][0] + words[1][0]).toUpperCase();
    }

    _truncate(text, max) {
        if (!text || text.length <= max) return text || '';
        return text.slice(0, max - 1) + '\u2026';
    }

    _roundRect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.arcTo(x + w, y, x + w, y + r, r);
        ctx.lineTo(x + w, y + h - r);
        ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
        ctx.lineTo(x + r, y + h);
        ctx.arcTo(x, y + h, x, y + h - r, r);
        ctx.lineTo(x, y + r);
        ctx.arcTo(x, y, x + r, y, r);
        ctx.closePath();
    }
}
