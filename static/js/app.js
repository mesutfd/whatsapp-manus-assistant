/**
 * iDeep WhatsApp Bot — Control Panel JavaScript
 * v1.2 — adds LLM config, contact personas, scheduled sends, quiet hours.
 */

class IDeepApp {
    constructor() {
        this.apiKey = localStorage.getItem('ideep_api_key') || '';
        this.baseUrl = window.location.origin;
        this.pollInterval = null;
        this.statusInterval = null;
        this.scheduleInterval = null;
        this.conversations = [];
        this.activeConv = null;
        this.convMessages = [];
        this.convHasMore = false;
        this.convInterval = null;
        this.mediaCache = new Map();   // GridFS file id -> object URL
        this.pendingAttachment = null;

        this.init();
    }

    init() {
        this.bindEvents();
        if (this.apiKey) this.verifyAuth();
    }

    // ─── API helper ─────────────────────────────────────────────────────

    async api(method, path, body = null) {
        const headers = {
            'Content-Type': 'application/json',
            'X-API-Key': this.apiKey,
        };
        const options = { method, headers };
        if (body !== null) options.body = JSON.stringify(body);

        const response = await fetch(`${this.baseUrl}${path}`, options);
        let data = null;
        try { data = await response.json(); } catch (_) {}
        if (!response.ok) {
            const detail = (data && data.detail) || `HTTP ${response.status}`;
            if (response.status === 401) this.showLogin();
            throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
        }
        return data;
    }

    async apiUpload(path, formData) {
        const response = await fetch(`${this.baseUrl}${path}`, {
            method: 'POST',
            headers: { 'X-API-Key': this.apiKey },  // no Content-Type: browser sets multipart boundary
            body: formData,
        });
        let data = null;
        try { data = await response.json(); } catch (_) {}
        if (!response.ok) {
            const detail = (data && data.detail) || `HTTP ${response.status}`;
            if (response.status === 401) this.showLogin();
            throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
        }
        return data;
    }

    // ─── Authentication ─────────────────────────────────────────────────

    async verifyAuth() {
        try {
            await this.api('GET', '/api/v1/connection/token/verify');
            this.showDashboard();
        } catch {
            this.showLogin();
        }
    }

    async login(apiKey) {
        this.apiKey = apiKey;
        try {
            await this.api('GET', '/api/v1/connection/token/verify');
            localStorage.setItem('ideep_api_key', apiKey);
            this.showDashboard();
            this.toast('Authenticated', 'success');
        } catch {
            this.toast('Invalid API key', 'error');
            this.apiKey = '';
        }
    }

    logout() {
        this.apiKey = '';
        localStorage.removeItem('ideep_api_key');
        this.stopPolling();
        this.showLogin();
    }

    // ─── UI navigation ──────────────────────────────────────────────────

    showLogin() {
        document.getElementById('loginScreen').style.display = 'flex';
        document.getElementById('dashboardScreen').style.display = 'none';
        document.getElementById('logoutBtn').style.display = 'none';
    }

    showDashboard() {
        document.getElementById('loginScreen').style.display = 'none';
        document.getElementById('dashboardScreen').style.display = 'block';
        document.getElementById('logoutBtn').style.display = 'inline-flex';
        this.startStatusPolling();
        this.loadAssistantConfig();
        this.loadLLMInfo();
        this.loadPersonas();
        this.loadScheduled();
        this.loadWebhooks();
        this.loadPermissions();
        this.updateApiBaseUrl();
    }

    updateApiBaseUrl() {
        document.getElementById('apiBaseUrl').textContent = this.baseUrl;
    }

    // ─── Connection ─────────────────────────────────────────────────────

    async connect() {
        try {
            const res = await this.api('POST', '/api/v1/connection/connect');
            if (res.status === 'already_connected') {
                this.toast('Already linked to WhatsApp', 'info');
                this.updateConnectionUI('connected');
                return;
            }
            this.toast('Connecting... waiting for QR code', 'info');
            this.setQrHint('Waiting for QR. If a saved session exists, this will skip QR and connect directly.');
            this.startQRPolling();
        } catch (e) {
            this.toast(`Connection failed: ${e.message}`, 'error');
        }
    }

    async disconnect() {
        try {
            await this.api('POST', '/api/v1/connection/disconnect');
            this.toast('Disconnected', 'info');
            this.updateConnectionUI('disconnected');
        } catch (e) {
            this.toast(`Disconnect failed: ${e.message}`, 'error');
        }
    }

    async relink() {
        if (!confirm('This will log out of WhatsApp and wipe the saved session. You will need to scan a fresh QR. Continue?')) return;
        try {
            await this.api('POST', '/api/v1/connection/logout');
            this.toast('Session wiped. Click Connect to scan a fresh QR.', 'success');
            this.stopQRPolling();
            this.updateConnectionUI('disconnected');
            this.setQrHint('');
        } catch (e) {
            this.toast(`Logout failed: ${e.message}`, 'error');
        }
    }

    setQrHint(text) {
        const el = document.getElementById('qrHint');
        if (el) el.textContent = text || '';
    }

    async getPairCode() {
        const phone = document.getElementById('phoneInput').value.trim();
        if (!phone) { this.toast('Enter a phone number', 'error'); return; }
        try {
            const data = await this.api('POST', '/api/v1/connection/pair-code', { phone_number: phone });
            if (data.success) {
                document.getElementById('pairCodeDisplay').style.display = 'block';
                document.getElementById('pairCodeValue').textContent = data.pair_code;
                this.toast('Pair code generated.', 'success');
            } else {
                this.toast(data.message || 'Failed to get pair code', 'error');
            }
        } catch (e) {
            this.toast(`Pair code error: ${e.message}`, 'error');
        }
    }

    startQRPolling()  { this.stopQRPolling(); this.pollInterval = setInterval(() => this.pollQR(), 2000); }
    stopQRPolling()   { if (this.pollInterval) { clearInterval(this.pollInterval); this.pollInterval = null; } }

    async pollQR() {
        try {
            const data = await this.api('GET', '/api/v1/connection/qr');
            if (data.state === 'connected') {
                this.stopQRPolling();
                this.updateConnectionUI('connected');
                this.setQrHint('');
                this.toast('WhatsApp connected!', 'success');
                return;
            }
            if (data.qr_base64) {
                const img = document.getElementById('qrImage');
                img.src = `data:image/png;base64,${data.qr_base64}`;
                img.style.display = 'block';
                document.getElementById('qrPlaceholder').style.display = 'none';
                document.getElementById('connectedBadge').style.display = 'none';
                this.setQrHint('Open WhatsApp → Settings → Linked Devices → Link a device, then scan.');
            } else if (data.message) {
                this.setQrHint(data.message);
            }
        } catch {}
    }

    startStatusPolling() {
        this.stopStatusPolling();
        this.pollStatus();
        this.statusInterval = setInterval(() => this.pollStatus(), 5000);
    }
    stopStatusPolling()  { if (this.statusInterval) { clearInterval(this.statusInterval); this.statusInterval = null; } }
    stopPolling()        { this.stopQRPolling(); this.stopStatusPolling(); this.stopConvPolling(); if (this.scheduleInterval) clearInterval(this.scheduleInterval); }

    async pollStatus() {
        try {
            const data = await this.api('GET', '/api/v1/connection/status');
            this.updateStatusDisplay(data);
            this.updateConnectionUI(data.state);
            if (data.state === 'logged_out' && this._lastState !== 'logged_out') {
                this.toast('WhatsApp session expired. Click "Re-link / Switch Account".', 'error');
            }
            this._lastState = data.state;
        } catch {}
    }

    updateStatusDisplay(data) {
        document.getElementById('statusState').textContent = data.state || '-';
        document.getElementById('statusConnectedAt').textContent = data.connected_at
            ? new Date(data.connected_at).toLocaleString() : '-';
        document.getElementById('statusMessages').textContent = data.stored_messages_count || 0;
        document.getElementById('statusAutoReply').textContent = data.auto_reply_enabled ? 'Enabled' : 'Disabled';
        document.getElementById('statusContacts').textContent = data.contacts_cached || 0;
    }

    updateConnectionUI(state) {
        const dot = document.querySelector('.status-dot');
        const text = document.querySelector('.status-text');
        const connectBtn = document.getElementById('connectBtn');
        const disconnectBtn = document.getElementById('disconnectBtn');
        const relinkBtn = document.getElementById('relinkBtn');
        const qrImage = document.getElementById('qrImage');
        const qrPlaceholder = document.getElementById('qrPlaceholder');
        const connectedBadge = document.getElementById('connectedBadge');

        dot.className = 'status-dot';

        switch (state) {
            case 'connected':
                dot.classList.add('connected');
                text.textContent = 'Connected';
                connectBtn.style.display = 'none';
                disconnectBtn.style.display = 'inline-flex';
                if (relinkBtn) relinkBtn.style.display = 'inline-flex';
                qrImage.style.display = 'none';
                qrPlaceholder.style.display = 'none';
                connectedBadge.style.display = 'block';
                break;
            case 'connecting':
            case 'qr_ready':
                dot.classList.add('connecting');
                text.textContent = 'Connecting...';
                connectBtn.style.display = 'none';
                disconnectBtn.style.display = 'inline-flex';
                if (relinkBtn) relinkBtn.style.display = 'none';
                break;
            default:
                text.textContent = state === 'logged_out' ? 'Logged out' : 'Disconnected';
                connectBtn.style.display = 'inline-flex';
                disconnectBtn.style.display = 'none';
                if (relinkBtn) relinkBtn.style.display = 'none';
                qrImage.style.display = 'none';
                qrPlaceholder.style.display = 'block';
                connectedBadge.style.display = 'none';
        }
    }

    // ─── Messages ───────────────────────────────────────────────────────

    async sendMessage() {
        const phone = document.getElementById('msgPhone').value.trim();
        const message = document.getElementById('msgText').value.trim();
        if (!phone || !message) { this.toast('Phone and message are required', 'error'); return; }
        try {
            const data = await this.api('POST', '/api/v1/messages/send', { phone, message });
            if (data.success) {
                this.toast('Message sent', 'success');
                document.getElementById('msgText').value = '';
            } else {
                this.toast(`Send failed: ${data.error}`, 'error');
            }
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    async searchMessages() {
        const query = document.getElementById('searchQuery').value.trim();
        const contact = document.getElementById('searchContact').value.trim();
        if (!query) { this.toast('Enter a search query', 'error'); return; }
        try {
            const data = await this.api('POST', '/api/v1/messages/search', {
                query, contact: contact || null,
            });
            this.renderMessages(data.results, 'searchResults');
        } catch (e) {
            this.toast(`Search error: ${e.message}`, 'error');
        }
    }

    // ─── Conversation view ──────────────────────────────────────────────

    async loadConversations() {
        try {
            const data = await this.api('GET', '/api/v1/messages/conversations?limit=500');
            this.conversations = data.conversations || [];
            this.renderConvList();
        } catch (e) {
            document.getElementById('convList').innerHTML =
                `<div class="empty-state">Failed to load: ${this.escapeHtml(e.message)}</div>`;
        }
    }

    renderConvList() {
        const container = document.getElementById('convList');
        const filter = (document.getElementById('convFilter').value || '').toLowerCase();
        const items = (this.conversations || []).filter(c =>
            !filter ||
            (c.name || '').toLowerCase().includes(filter) ||
            (c.phone || '').includes(filter)
        );
        if (!items.length) {
            container.innerHTML = '<div class="empty-state">No conversations.</div>';
            return;
        }
        container.innerHTML = items.map(c => {
            const preview = c.last_text
                ? this.escapeHtml(c.last_text.split('\n')[0].slice(0, 60))
                : `<em>${this.mediaLabel(c.last_type)}</em>`;
            const youPrefix = c.last_from_me ? '<span class="muted">You: </span>' : '';
            const active = this.activeConv && this.activeConv.chat_jid === c.chat_jid ? ' active' : '';
            const initial = this.escapeHtml((c.name || '?').trim().charAt(0).toUpperCase() || '?');
            return `
                <div class="conv-item${active}" data-jid="${this.escapeHtml(c.chat_jid)}">
                    <div class="conv-avatar${c.is_group ? ' group' : ''}">${c.is_group ? '&#128101;' : initial}</div>
                    <div class="conv-body">
                        <div class="conv-top">
                            <span class="conv-name" dir="auto">${this.escapeHtml(c.name || c.chat_jid)}</span>
                            <span class="conv-time">${this.fmtShortTime(c.last_timestamp)}</span>
                        </div>
                        <div class="conv-preview" dir="auto">${youPrefix}${preview}</div>
                    </div>
                </div>
            `;
        }).join('');
    }

    fmtShortTime(ts) {
        if (!ts) return '';
        const d = new Date(ts);
        if (isNaN(d.getTime())) return '';
        const now = new Date();
        if (d.toDateString() === now.toDateString()) {
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        if (d.getFullYear() === now.getFullYear()) {
            return d.toLocaleDateString([], { day: 'numeric', month: 'short' });
        }
        return d.toLocaleDateString([], { day: '2-digit', month: '2-digit', year: '2-digit' });
    }

    async openConversation(jid) {
        const conv = (this.conversations || []).find(c => c.chat_jid === jid);
        this.activeConv = conv || { chat_jid: jid, name: jid, phone: null, is_group: false };
        this.renderConvList();

        const header = document.getElementById('convHeader');
        const sub = this.activeConv.is_group
            ? 'group'
            : (this.activeConv.phone || this.activeConv.chat_jid);
        header.innerHTML = `
            <div class="conv-avatar${this.activeConv.is_group ? ' group' : ''}">${this.activeConv.is_group ? '&#128101;' : this.escapeHtml((this.activeConv.name || '?').charAt(0).toUpperCase())}</div>
            <div>
                <div class="conv-name" dir="auto">${this.escapeHtml(this.activeConv.name || jid)}</div>
                <div class="conv-sub">${this.escapeHtml(sub)} · ${this.activeConv.message_count || 0} messages</div>
            </div>
        `;

        // Reply box only for real 1:1 phone chats
        const canSend = !!this.activeConv.phone && !this.activeConv.is_group;
        document.getElementById('convSendForm').style.display = canSend ? 'flex' : 'none';

        try {
            const data = await this.api('GET',
                `/api/v1/messages/conversation?chat_jid=${encodeURIComponent(jid)}&limit=60`);
            this.convMessages = data.messages || [];
            this.convHasMore = !!data.has_more;
            this.renderConversation(true);
        } catch (e) {
            document.getElementById('convMessages').innerHTML =
                `<div class="chat-empty"><p>${this.escapeHtml(e.message)}</p></div>`;
        }
        this.startConvPolling();
    }

    async loadOlderMessages() {
        if (!this.activeConv || !this.convMessages.length) return;
        const box = document.getElementById('convMessages');
        const prevHeight = box.scrollHeight;
        try {
            const before = this.convMessages[0].timestamp;
            const data = await this.api('GET',
                `/api/v1/messages/conversation?chat_jid=${encodeURIComponent(this.activeConv.chat_jid)}&limit=60&before=${encodeURIComponent(before)}`);
            this.convMessages = (data.messages || []).concat(this.convMessages);
            this.convHasMore = !!data.has_more;
            this.renderConversation(false);
            box.scrollTop = box.scrollHeight - prevHeight;  // keep viewport anchored
        } catch (e) {
            this.toast(`Load older failed: ${e.message}`, 'error');
        }
    }

    renderConversation(scrollToBottom) {
        const box = document.getElementById('convMessages');
        const msgs = this.convMessages || [];
        if (!msgs.length) {
            box.innerHTML = '<div class="chat-empty"><p>No messages in this chat yet.</p></div>';
            return;
        }
        const parts = [];
        if (this.convHasMore) {
            parts.push('<div class="load-older"><button class="btn btn-sm btn-outline" id="loadOlderBtn">Load older messages</button></div>');
        }
        let lastDay = '';
        for (let i = 0; i < msgs.length; i++) {
            const m = msgs[i];
            const d = m.timestamp ? new Date(m.timestamp) : null;
            const day = d && !isNaN(d.getTime()) ? d.toDateString() : '';
            if (day && day !== lastDay) {
                lastDay = day;
                parts.push(`<div class="day-divider"><span>${d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}</span></div>`);
            }
            const sent = !!m.is_from_me;
            const time = d && !isNaN(d.getTime())
                ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
            const senderLine = (!sent && m.is_group)
                ? `<div class="bubble-sender" dir="auto">${this.escapeHtml(m.sender_name || m.from_phone || '')}</div>` : '';
            const mediaHtml = this.mediaBubbleHtml(m, i);
            const textHtml = m.text
                ? `<div class="bubble-text" dir="auto">${this.escapeHtml(m.text)}</div>` : '';
            const body = (mediaHtml + textHtml)
                || '<div class="bubble-text media" dir="auto">&#128247; Media</div>';
            parts.push(`
                <div class="bubble-row ${sent ? 'sent' : 'received'}">
                    <div class="bubble${mediaHtml ? ' has-media' : ''}">
                        ${senderLine}
                        ${body}
                        <div class="bubble-time">${time}</div>
                    </div>
                </div>
            `);
        }
        box.innerHTML = parts.join('');
        const olderBtn = document.getElementById('loadOlderBtn');
        if (olderBtn) olderBtn.addEventListener('click', () => this.loadOlderMessages());
        if (scrollToBottom) box.scrollTop = box.scrollHeight;
        this.hydrateMedia(box);
    }

    // ─── Media rendering ────────────────────────────────────────────────

    mediaLabel(kind) {
        return {
            image: '&#128247; Photo', video: '&#127916; Video', gif: '&#127902; GIF',
            sticker: '&#127912; Sticker', voice: '&#127908; Voice message',
            audio: '&#127925; Audio', document: '&#128196; Document',
            contact: '&#128100; Contact', location: '&#128205; Location',
        }[kind] || '&#128247; Media';
    }

    fmtDuration(secs) {
        if (!secs) return '';
        const s = Math.round(secs);
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    fmtSize(bytes) {
        if (!bytes) return '';
        if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
        return `${Math.max(1, Math.round(bytes / 1e3))} KB`;
    }

    /** Build the media part of a chat bubble. `i` indexes this.convMessages
     *  so click handlers can look the media back up. */
    mediaBubbleHtml(m, i) {
        const media = m.media;
        const kind = (media && media.kind) || (m.type && m.type !== 'text' ? m.type : null);
        if (!kind) return '';
        if (!media) {
            // Old records (pre-media import) have only type='media'.
            return kind === 'text' ? '' :
                `<div class="media-chip inert">${this.mediaLabel(kind)}</div>`;
        }

        const dur = this.fmtDuration(media.duration);
        switch (kind) {
            case 'image':
            case 'sticker': {
                const thumbId = media.thumb_id || media.file_id;
                if (!thumbId) return `<div class="media-chip inert">${this.mediaLabel(kind)}</div>`;
                const cls = kind === 'sticker' ? 'media-frame sticker' : 'media-frame';
                const click = media.file_id ? ` data-action="lightbox" data-mi="${i}"` : '';
                return `<div class="${cls}"${click}><img class="media-img" data-thumb="${thumbId}" alt="${kind}"></div>`;
            }
            case 'video':
            case 'gif': {
                const badge = `<span class="media-play">&#9654;</span>` +
                              (dur ? `<span class="media-duration">${dur}</span>` : '');
                if (media.thumb_id) {
                    const click = media.file_id ? ` data-action="lightbox" data-mi="${i}"` : '';
                    return `<div class="media-frame video"${click}><img class="media-img" data-thumb="${media.thumb_id}" alt="video">${badge}</div>`;
                }
                if (media.file_id) {
                    return `<div class="media-frame video empty" data-action="lightbox" data-mi="${i}">${badge}</div>`;
                }
                return `<div class="media-chip inert">${this.mediaLabel(kind)}${dur ? ` · ${dur}` : ''}<span class="media-note">not in backup</span></div>`;
            }
            case 'voice':
            case 'audio': {
                if (media.file_id) {
                    return `<div class="media-audio"><span class="media-audio-label">${this.mediaLabel(kind)}</span><audio controls preload="none" data-audio="${media.file_id}"></audio></div>`;
                }
                return `<div class="media-chip inert">${this.mediaLabel(kind)}${dur ? ` · ${dur}` : ''}</div>`;
            }
            case 'document': {
                const name = this.escapeHtml(media.filename || 'Document');
                const size = this.fmtSize(media.size);
                if (media.file_id) {
                    return `<div class="media-chip clickable" data-action="download" data-mi="${i}">&#128196; ${name}${size ? `<span class="media-note">${size}</span>` : ''}</div>`;
                }
                return `<div class="media-chip inert">&#128196; ${name}<span class="media-note">not in backup</span></div>`;
            }
            case 'contact': {
                const name = this.escapeHtml(media.contact_name || 'Contact');
                const click = media.vcard ? ` data-action="vcard" data-mi="${i}"` : '';
                return `<div class="media-chip${media.vcard ? ' clickable' : ' inert'}"${click}>&#128100; ${name}${media.vcard ? '<span class="media-note">save .vcf</span>' : ''}</div>`;
            }
            case 'location': {
                const label = this.escapeHtml(media.location_name || media.address || 'Location');
                if (media.latitude || media.longitude) {
                    const url = `https://www.google.com/maps?q=${media.latitude},${media.longitude}`;
                    return `<a class="media-chip clickable" href="${url}" target="_blank" rel="noopener">&#128205; ${label}<span class="media-note">open map</span></a>`;
                }
                return `<div class="media-chip inert">&#128205; ${label}</div>`;
            }
            default:
                return `<div class="media-chip inert">${this.mediaLabel(kind)}</div>`;
        }
    }

    async fetchMediaBlob(fileId) {
        if (this.mediaCache.has(fileId)) return this.mediaCache.get(fileId);
        const response = await fetch(`${this.baseUrl}/api/v1/media/${fileId}`, {
            headers: { 'X-API-Key': this.apiKey },
        });
        if (!response.ok) throw new Error(`media HTTP ${response.status}`);
        const url = URL.createObjectURL(await response.blob());
        this.mediaCache.set(fileId, url);
        return url;
    }

    /** Fill in thumbnails and audio players after (re)rendering a chat. */
    hydrateMedia(box) {
        box.querySelectorAll('img.media-img[data-thumb]').forEach(async (img) => {
            const id = img.dataset.thumb;
            delete img.dataset.thumb;
            try {
                img.src = await this.fetchMediaBlob(id);
                img.closest('.media-frame')?.classList.add('loaded');
            } catch { img.closest('.media-frame')?.classList.add('failed'); }
        });
        box.querySelectorAll('audio[data-audio]').forEach(async (audio) => {
            const id = audio.dataset.audio;
            delete audio.dataset.audio;
            try { audio.src = await this.fetchMediaBlob(id); } catch {}
        });
    }

    async onConvMediaClick(e) {
        const el = e.target.closest('[data-action]');
        if (!el || !el.dataset.mi) return;
        const m = (this.convMessages || [])[parseInt(el.dataset.mi, 10)];
        const media = m && m.media;
        if (!media) return;
        try {
            if (el.dataset.action === 'lightbox') {
                await this.openLightbox(media);
            } else if (el.dataset.action === 'download') {
                const url = await this.fetchMediaBlob(media.file_id);
                this.triggerDownload(url, media.filename || 'document');
            } else if (el.dataset.action === 'vcard') {
                const blob = new Blob([media.vcard], { type: 'text/vcard' });
                this.triggerDownload(URL.createObjectURL(blob), `${media.contact_name || 'contact'}.vcf`);
            }
        } catch (err) {
            this.toast(`Media failed to load: ${err.message}`, 'error');
        }
    }

    triggerDownload(url, filename) {
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
    }

    async openLightbox(media) {
        const overlay = document.createElement('div');
        overlay.className = 'lightbox-overlay';
        overlay.innerHTML = '<div class="lightbox-loading">Loading…</div>';
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.classList.contains('lightbox-close')) overlay.remove();
        });
        document.body.appendChild(overlay);
        try {
            const url = await this.fetchMediaBlob(media.file_id);
            const isVideo = media.kind === 'video' || media.kind === 'gif';
            overlay.innerHTML = `
                <button class="lightbox-close" title="Close">&times;</button>
                ${isVideo
                    ? `<video class="lightbox-content" src="${url}" controls autoplay></video>`
                    : `<img class="lightbox-content" src="${url}" alt="media">`}
            `;
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay || e.target.classList.contains('lightbox-close')) overlay.remove();
            });
        } catch (err) {
            overlay.remove();
            this.toast(`Media failed to load: ${err.message}`, 'error');
        }
    }

    // ─── Composer attachments ───────────────────────────────────────────

    setAttachment(file) {
        this.pendingAttachment = file || null;
        const chip = document.getElementById('convAttachChip');
        if (!chip) return;
        if (file) {
            document.getElementById('convAttachName').textContent =
                `${file.name} (${this.fmtSize(file.size) || '0 KB'})`;
            chip.style.display = 'inline-flex';
        } else {
            chip.style.display = 'none';
            const input = document.getElementById('convAttachInput');
            if (input) input.value = '';
        }
    }

    async sendConvMessage() {
        const input = document.getElementById('convSendText');
        const text = input.value.trim();
        const attachment = this.pendingAttachment;
        if ((!text && !attachment) || !this.activeConv || !this.activeConv.phone) return;
        try {
            let res;
            if (attachment) {
                const fd = new FormData();
                fd.append('phone', this.activeConv.phone);
                fd.append('file', attachment);
                fd.append('caption', text);
                res = await this.apiUpload('/api/v1/messages/send-media', fd);
            } else {
                res = await this.api('POST', '/api/v1/messages/send', {
                    phone: this.activeConv.phone, message: text,
                });
            }
            if (res.success) {
                input.value = '';
                this.setAttachment(null);
                await this.refreshActiveConversation(true);
                this.loadConversations();
            } else {
                this.toast(`Send failed: ${res.error}`, 'error');
            }
        } catch (e) {
            this.toast(`Send failed: ${e.message}`, 'error');
        }
    }

    async refreshActiveConversation(forceScroll = false) {
        if (!this.activeConv) return;
        const box = document.getElementById('convMessages');
        try {
            const data = await this.api('GET',
                `/api/v1/messages/conversation?chat_jid=${encodeURIComponent(this.activeConv.chat_jid)}&limit=60`);
            const fresh = data.messages || [];
            const last = (arr) => arr.length ? `${arr[arr.length - 1].id}|${arr[arr.length - 1].timestamp}` : '';
            if (last(fresh) === last(this.convMessages || []) && fresh.length === (this.convMessages || []).length) {
                return;  // nothing new
            }
            const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
            const prevScroll = box.scrollTop;
            this.convMessages = fresh;
            this.convHasMore = !!data.has_more;
            this.renderConversation(forceScroll || nearBottom);
            if (!forceScroll && !nearBottom) box.scrollTop = prevScroll;
        } catch {}
    }

    startConvPolling() {
        this.stopConvPolling();
        this.convInterval = setInterval(() => {
            const tabActive = document.getElementById('tab-messages').classList.contains('active');
            if (tabActive && this.activeConv) this.refreshActiveConversation();
        }, 8000);
    }

    stopConvPolling() {
        if (this.convInterval) { clearInterval(this.convInterval); this.convInterval = null; }
    }

    renderMessages(messages, containerId) {
        const container = document.getElementById(containerId);
        if (!messages || messages.length === 0) {
            container.innerHTML = '<div class="message-item"><p class="muted">No messages found</p></div>';
            return;
        }
        container.innerHTML = messages.map(msg => `
            <div class="message-item">
                <div class="message-sender">${this.escapeHtml(msg.sender_name || msg.from || 'Unknown')}</div>
                <div class="message-text">${msg.text ? this.escapeHtml(msg.text) : `<em>${this.mediaLabel(msg.type)}</em>`}</div>
                <div class="message-time">${msg.timestamp ? new Date(msg.timestamp).toLocaleString() : ''}</div>
            </div>
        `).join('');
    }

    // ─── Restore from backup ────────────────────────────────────────────

    async importBackup() {
        const fileInput = document.getElementById('backupFile');
        const file = fileInput.files && fileInput.files[0];
        if (!file) { this.toast('Choose a backup file first', 'error'); return; }

        const btn = document.getElementById('backupImportBtn');
        const resultBox = document.getElementById('backupImportResult');
        const formData = new FormData();
        formData.append('file', file);
        formData.append('phone', document.getElementById('backupPhone').value.trim());
        formData.append('other_name', document.getElementById('backupOtherName').value.trim());

        btn.disabled = true;
        btn.textContent = 'Restoring… (large backups can take a few minutes)';
        resultBox.style.display = 'none';

        try {
            const res = await this.apiUpload('/api/v1/messages/import-backup', formData);
            if (res.success) {
                const kindLabel = {
                    txt: 'chat export (.txt)',
                    zip: 'export archive (.zip)',
                    android_sqlite: 'Android full backup (msgstore.db)',
                    ios_sqlite: 'iOS full backup (ChatStorage.sqlite)',
                    ios_sqlite_bundle: 'iOS media bundle (DB + media files)',
                    android_sqlite_bundle: 'Android media bundle (DB + media files)',
                }[res.kind] || res.kind;
                const lines = [
                    `<strong>Backup restored</strong> — detected as ${this.escapeHtml(kindLabel)}.`,
                    `Chats: ${res.chats ?? 1} · Messages parsed: ${res.parsed_messages}`,
                    `Newly saved: ${res.newly_inserted} · Duplicates skipped: ${res.duplicates_skipped}`,
                ];
                if (res.media_attached !== undefined) {
                    lines.push(`Media imported: ${res.media_attached} · Thumbnails: ${res.thumbnails_created}`
                        + ` · Videos skipped: ${res.videos_skipped} · Missing from bundle: ${res.media_missing_from_bundle}`);
                }
                if (res.owner_name_detected) lines.push(`Your messages detected as sender: "${this.escapeHtml(res.owner_name_detected)}"`);
                if (res.system_rows_skipped) lines.push(`System notices skipped: ${res.system_rows_skipped}`);
                if (res.skipped_files && res.skipped_files.length) {
                    lines.push(`Skipped files: ${this.escapeHtml(res.skipped_files.join(', '))}`);
                }
                resultBox.className = 'backup-result success';
                resultBox.innerHTML = lines.join('<br>');
                this.toast(`Restored ${res.newly_inserted} messages from backup`, 'success');
                fileInput.value = '';
                this.loadConversations();
            } else {
                resultBox.className = 'backup-result error';
                resultBox.innerHTML = this.escapeHtml(res.message || 'No messages could be read from that file.');
                this.toast('Nothing imported from that file', 'error');
            }
            resultBox.style.display = 'block';
        } catch (e) {
            resultBox.className = 'backup-result error';
            resultBox.innerHTML = this.escapeHtml(e.message);
            resultBox.style.display = 'block';
            this.toast(`Restore failed: ${e.message}`, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Restore Backup';
        }
    }

    // ─── Assistant config ───────────────────────────────────────────────

    async loadAssistantConfig() {
        try {
            const data = await this.api('GET', '/api/v1/assistant/config');
            document.getElementById('autoReplyEnabled').checked = !!data.enabled;
            document.getElementById('assistantName').value = data.assistant_name || 'iDeep AI';
            document.getElementById('autoReplyMessage').value = data.message || '';
            document.getElementById('llmEnabled').checked = !!data.llm_enabled;
            document.getElementById('llmSystemPrompt').value = data.llm_system_prompt || '';

            const qh = data.quiet_hours || {};
            document.getElementById('quietEnabled').checked = !!qh.enabled;
            document.getElementById('quietStart').value = qh.start || '22:00';
            document.getElementById('quietEnd').value = qh.end || '08:00';
            document.getElementById('quietTz').value = qh.timezone || 'UTC';
            document.getElementById('quietMessage').value = qh.message || '';
            document.getElementById('quietDefer').checked = qh.defer_scheduled !== false;

            document.getElementById('replyDelaySeconds').value = data.reply_delay_seconds ?? 60;
            document.getElementById('humanSnoozeMinutes').value = data.human_snooze_minutes ?? 30;
            document.getElementById('readHoldMinutes').value = data.read_hold_minutes ?? 5;
            document.getElementById('commandPrefix').value = data.command_prefix || '#';
            document.getElementById('controlContact').value = data.control_contact || '';
            this.renderMutedChats(data.muted_chats || []);

            this.renderRules(data.rules || []);
        } catch (e) {
            // silent
        }
    }

    async saveAssistantConfig() {
        const payload = {
            enabled: document.getElementById('autoReplyEnabled').checked,
            assistant_name: document.getElementById('assistantName').value,
            message: document.getElementById('autoReplyMessage').value,
            llm_enabled: document.getElementById('llmEnabled').checked,
            llm_system_prompt: document.getElementById('llmSystemPrompt').value,
            quiet_hours: {
                enabled: document.getElementById('quietEnabled').checked,
                start: document.getElementById('quietStart').value || '22:00',
                end: document.getElementById('quietEnd').value || '08:00',
                timezone: document.getElementById('quietTz').value || 'UTC',
                message: document.getElementById('quietMessage').value || '',
                defer_scheduled: document.getElementById('quietDefer').checked,
            },
            reply_delay_seconds: parseInt(document.getElementById('replyDelaySeconds').value, 10) || 0,
            human_snooze_minutes: parseInt(document.getElementById('humanSnoozeMinutes').value, 10) || 0,
            read_hold_minutes: parseInt(document.getElementById('readHoldMinutes').value, 10) || 0,
            command_prefix: document.getElementById('commandPrefix').value || '#',
            control_contact: document.getElementById('controlContact').value.trim(),
        };
        try {
            await this.api('PUT', '/api/v1/assistant/config', payload);
            this.toast('Configuration saved', 'success');
        } catch (e) {
            this.toast(`Save failed: ${e.message}`, 'error');
        }
    }

    async loadLLMInfo() {
        try {
            const info = await this.api('GET', '/api/v1/assistant/llm');
            const chip = document.getElementById('llmChip');
            const text = document.getElementById('llmChipText');
            if (info.configured) {
                chip.classList.add('configured');
                text.textContent = `${info.provider} · ${info.model}`;
            } else {
                chip.classList.remove('configured');
                text.textContent = info.provider === 'none'
                    ? 'not configured'
                    : `${info.provider} (no API key)`;
            }
            const list = document.getElementById('llmInfoList');
            list.innerHTML = `
                <div class="info-item"><span class="info-label">Provider</span><code>${this.escapeHtml(info.provider)}</code></div>
                <div class="info-item"><span class="info-label">Model</span><code>${this.escapeHtml(info.model)}</code></div>
                <div class="info-item"><span class="info-label">Status</span><span class="pill ${info.configured ? 'pill-green' : 'pill-warn'}">${info.configured ? 'configured' : 'not configured'}</span></div>
                <div class="info-item"><span class="info-label">Base URL</span><code>${this.escapeHtml(info.base_url || '(provider default)')}</code></div>
            `;
        } catch {}
    }

    // ─── Rules ──────────────────────────────────────────────────────────

    async addRule() {
        const payload = {
            contact: document.getElementById('ruleContact').value.trim() || null,
            keyword: document.getElementById('ruleKeyword').value.trim() || null,
            match_mode: document.getElementById('ruleMatchMode').value,
            message: document.getElementById('ruleMessage').value,
            use_llm: document.getElementById('ruleUseLlm').checked,
            cooldown_seconds: parseInt(document.getElementById('ruleCooldown').value || '0', 10),
            priority: parseInt(document.getElementById('rulePriority').value || '100', 10),
            enabled: true,
        };
        if (!payload.message && !payload.use_llm) {
            this.toast('Provide a reply message or enable Use LLM', 'error');
            return;
        }
        try {
            await this.api('POST', '/api/v1/assistant/rules', payload);
            this.toast('Rule added', 'success');
            this.resetRuleForm();
            this.loadAssistantConfig();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    resetRuleForm() {
        document.getElementById('addRuleForm').style.display = 'none';
        ['ruleContact', 'ruleKeyword', 'ruleMessage'].forEach(id => document.getElementById(id).value = '');
        document.getElementById('ruleMatchMode').value = 'contains';
        document.getElementById('ruleCooldown').value = '0';
        document.getElementById('rulePriority').value = '100';
        document.getElementById('ruleUseLlm').checked = false;
    }

    renderRules(rules) {
        const container = document.getElementById('rulesList');
        if (!rules || rules.length === 0) {
            container.innerHTML = '<div class="empty-state">No rules configured. Add one to start replying automatically.</div>';
            return;
        }
        container.innerHTML = rules.map(rule => {
            const pills = [];
            if (rule.contact) pills.push(`<span class="pill pill-teal">contact: ${this.escapeHtml(rule.contact)}</span>`);
            if (rule.keyword) pills.push(`<span class="pill pill-green">${this.escapeHtml(rule.match_mode || 'contains')}: "${this.escapeHtml(rule.keyword)}"</span>`);
            if (!rule.contact && !rule.keyword) pills.push(`<span class="pill pill-muted">all messages</span>`);
            if (rule.use_llm) pills.push(`<span class="pill pill-warn">LLM</span>`);
            if (rule.cooldown_seconds) pills.push(`<span class="pill">cooldown ${rule.cooldown_seconds}s</span>`);
            if (!rule.enabled) pills.push(`<span class="pill pill-danger">disabled</span>`);
            const preview = rule.message
                ? this.escapeHtml(rule.message)
                : '<em class="muted">(LLM-generated reply)</em>';
            return `
                <div class="card-row">
                    <div class="card-info">
                        <div class="title">${pills.join(' ')}</div>
                        <div class="meta">${preview}</div>
                    </div>
                    <div class="card-actions">
                        <button class="btn btn-sm btn-outline" onclick="app.toggleRule(${rule.id}, ${!rule.enabled})">${rule.enabled ? 'Disable' : 'Enable'}</button>
                        <button class="btn btn-sm btn-danger" onclick="app.deleteRule(${rule.id})">Remove</button>
                    </div>
                </div>
            `;
        }).join('');
    }

    renderMutedChats(muted) {
        const container = document.getElementById('mutedChatsList');
        if (!container) return;
        if (!muted || muted.length === 0) {
            container.innerHTML = '<div class="empty-state">No muted chats. Send <code>#mute</code> inside a chat to mute it.</div>';
            return;
        }
        container.innerHTML = muted.map(m => `
            <div class="card-row">
                <div class="card-info">
                    <div class="title">${this.escapeHtml(m.name || m.chat_key)}</div>
                    <div class="meta">muted ${this.escapeHtml((m.muted_at || '').slice(0, 16).replace('T', ' '))} UTC</div>
                </div>
                <div class="card-actions">
                    <button class="btn btn-sm btn-outline" onclick="app.unmuteChat('${this.escapeHtml(m.chat_key)}')">Unmute</button>
                </div>
            </div>
        `).join('');
    }

    async unmuteChat(chatKey) {
        try {
            await this.api('DELETE', `/api/v1/assistant/muted/${encodeURIComponent(chatKey)}`);
            this.toast('Chat unmuted', 'success');
            this.loadAssistantConfig();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    async toggleRule(id, enable) {
        try {
            await this.api('PATCH', `/api/v1/assistant/rules/${id}`, { enabled: enable });
            this.loadAssistantConfig();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    async deleteRule(id) {
        if (!confirm('Remove this rule?')) return;
        try {
            await this.api('DELETE', `/api/v1/assistant/rules/${id}`);
            this.toast('Rule removed', 'success');
            this.loadAssistantConfig();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    // ─── Personas ───────────────────────────────────────────────────────

    async loadPersonas() {
        try {
            const data = await this.api('GET', '/api/v1/assistant/personas');
            this.renderPersonas(data.personas || []);
        } catch {}
    }

    renderPersonas(personas) {
        const container = document.getElementById('personasList');
        if (!personas || personas.length === 0) {
            container.innerHTML = '<div class="empty-state">No personas yet. Add one to give the LLM context about a specific contact.</div>';
            return;
        }
        container.innerHTML = personas.map(p => {
            const pills = [
                `<span class="pill pill-teal">${this.escapeHtml(p.contact)}</span>`,
                p.use_llm ? `<span class="pill pill-green">LLM on</span>` : `<span class="pill pill-muted">LLM off</span>`,
            ];
            if (p.system_prompt_override) pills.push(`<span class="pill pill-warn">prompt override</span>`);
            return `
                <div class="card-row">
                    <div class="card-info">
                        <div class="title">${this.escapeHtml(p.display_name || p.contact)} ${pills.join(' ')}</div>
                        <div class="meta">${this.escapeHtml(p.notes || '(no notes)')}</div>
                    </div>
                    <div class="card-actions">
                        <button class="btn btn-sm btn-danger" onclick="app.deletePersona('${this.escapeJs(p.contact)}')">Remove</button>
                    </div>
                </div>
            `;
        }).join('');
    }

    async savePersona() {
        const payload = {
            contact: document.getElementById('personaContact').value.trim(),
            display_name: document.getElementById('personaName').value.trim() || null,
            notes: document.getElementById('personaNotes').value.trim() || null,
            system_prompt_override: document.getElementById('personaSystemPrompt').value.trim() || null,
            use_llm: document.getElementById('personaUseLlm').checked,
        };
        if (!payload.contact) { this.toast('Contact is required', 'error'); return; }
        try {
            await this.api('PUT', '/api/v1/assistant/personas', payload);
            this.toast('Persona saved', 'success');
            this.resetPersonaForm();
            this.loadPersonas();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    resetPersonaForm() {
        document.getElementById('addPersonaForm').style.display = 'none';
        ['personaContact', 'personaName', 'personaNotes', 'personaSystemPrompt']
            .forEach(id => document.getElementById(id).value = '');
        document.getElementById('personaUseLlm').checked = true;
    }

    async deletePersona(contact) {
        if (!confirm(`Remove persona for ${contact}?`)) return;
        try {
            await this.api('DELETE', `/api/v1/assistant/personas/${encodeURIComponent(contact)}`);
            this.toast('Persona removed', 'success');
            this.loadPersonas();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    // ─── Schedule ───────────────────────────────────────────────────────

    async createScheduled() {
        const phone = document.getElementById('schedulePhone').value.trim();
        const message = document.getElementById('scheduleMessage').value.trim();
        const localAt = document.getElementById('scheduleAt').value;
        if (!phone || !message || !localAt) {
            this.toast('Phone, message, and time are required', 'error');
            return;
        }
        const dt = new Date(localAt);
        if (isNaN(dt.getTime())) { this.toast('Invalid date/time', 'error'); return; }
        const iso = dt.toISOString();
        try {
            await this.api('POST', '/api/v1/schedule/', { phone, message, scheduled_at: iso });
            this.toast('Send scheduled', 'success');
            document.getElementById('scheduleMessage').value = '';
            this.loadScheduled();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    async loadScheduled() {
        try {
            const pending = await this.api('GET', '/api/v1/schedule/?status=pending&limit=100');
            this.renderScheduled(pending.items || [], 'scheduledPendingList', true);
            const all = await this.api('GET', '/api/v1/schedule/?limit=100');
            const history = (all.items || []).filter(x => x.status !== 'pending');
            this.renderScheduled(history, 'scheduledHistoryList', false);
        } catch {}
    }

    renderScheduled(items, containerId, allowCancel) {
        const container = document.getElementById(containerId);
        if (!items || items.length === 0) {
            container.innerHTML = `<div class="empty-state">${allowCancel ? 'No pending sends.' : 'No history yet.'}</div>`;
            return;
        }
        const statusPill = (s) => {
            const map = { pending: 'pill-warn', sent: 'pill-green', failed: 'pill-danger', cancelled: 'pill-muted' };
            return `<span class="pill ${map[s] || ''}">${s}</span>`;
        };
        container.innerHTML = items.map(it => {
            const when = it.scheduled_at ? new Date(it.scheduled_at).toLocaleString() : '';
            const sentAt = it.sent_at ? `<br><span class="muted text-sm">sent ${new Date(it.sent_at).toLocaleString()}</span>` : '';
            const err = it.error ? `<br><span class="pill pill-danger">${this.escapeHtml(it.error)}</span>` : '';
            const cancel = allowCancel
                ? `<button class="btn btn-sm btn-danger" onclick="app.cancelScheduled(${it.id})">Cancel</button>`
                : '';
            return `
                <div class="card-row">
                    <div class="card-info">
                        <div class="title">${statusPill(it.status)} <span class="muted">→</span> ${this.escapeHtml(it.phone)} <span class="muted">@ ${when}</span>${sentAt}${err}</div>
                        <div class="meta">${this.escapeHtml(it.message)}</div>
                    </div>
                    <div class="card-actions">${cancel}</div>
                </div>
            `;
        }).join('');
    }

    async cancelScheduled(id) {
        try {
            await this.api('DELETE', `/api/v1/schedule/${id}`);
            this.toast('Cancelled', 'success');
            this.loadScheduled();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    // ─── Permissions / Allowed Contacts ─────────────────────────────────

    async loadPermissions() {
        try {
            const data = await this.api('GET', '/api/v1/permissions/');
            document.getElementById('permissionsEnabled').checked = !!data.enabled;
            this.renderAllowedContacts(data.contacts || []);
            this.updatePermissionsStatus(data);
        } catch (e) {
            // Silently handle
        }
    }

    updatePermissionsStatus(data) {
        const el = document.getElementById('permissionsStatus');
        if (!el) return;
        const total = (data.contacts || []).length;
        const active = (data.contacts || []).filter(c => c.enabled !== false).length;
        el.textContent = data.enabled
            ? `Enforcing: ${active} active / ${total} allow-listed contacts. Sends to anyone else will be blocked.`
            : `Not enforcing. ${total} allow-listed contacts saved but sends are unrestricted.`;
    }

    async togglePermissions(enabled) {
        try {
            const data = await this.api('PUT', '/api/v1/permissions/toggle', { enabled });
            this.toast(enabled ? 'Allow-list enforcement enabled' : 'Allow-list enforcement disabled', 'success');
            this.updatePermissionsStatus(data);
        } catch (e) {
            this.toast(`Toggle failed: ${e.message}`, 'error');
            document.getElementById('permissionsEnabled').checked = !enabled;
        }
    }

    renderAllowedContacts(contacts) {
        const container = document.getElementById('allowedContactsList');
        if (!contacts || contacts.length === 0) {
            container.innerHTML = '<p style="color:var(--text-muted);font-size:13px;">No allowed contacts yet. Add one to let the assistant message them.</p>';
            return;
        }

        container.innerHTML = contacts.map(c => {
            const aliases = (c.llm_friendly_names || []).join(', ');
            const tags = (c.tags || []).map(t => `<span class="chip">${this.escapeHtml(t)}</span>`).join('');
            const disabledBadge = c.enabled === false ? '<span class="chip chip-warn">disabled</span>' : '';
            return `
                <div class="rule-item allowed-contact-item">
                    <div class="rule-info">
                        <strong>${this.escapeHtml(c.name || c.phone)} ${disabledBadge}</strong>
                        <span><b>Phone:</b> ${this.escapeHtml(c.phone)}${c.relation ? ` · <b>Relation:</b> ${this.escapeHtml(c.relation)}` : ''}</span>
                        ${aliases ? `<span><b>Aliases:</b> ${this.escapeHtml(aliases)}</span>` : ''}
                        ${tags ? `<span class="chip-row">${tags}</span>` : ''}
                        ${c.notes ? `<span><b>Notes:</b> ${this.escapeHtml(c.notes)}</span>` : ''}
                    </div>
                    <div class="rule-actions">
                        <button class="btn btn-sm btn-outline" onclick="app.editAllowedContact('${c.id}')">Edit</button>
                        <button class="btn btn-sm btn-danger" onclick="app.deleteAllowedContact('${c.id}')">Remove</button>
                    </div>
                </div>
            `;
        }).join('');
    }

    showAllowedContactForm(contact = null) {
        const form = document.getElementById('addAllowedContactForm');
        document.getElementById('allowedContactFormTitle').textContent = contact ? 'Edit Allowed Contact' : 'New Allowed Contact';
        document.getElementById('allowedContactId').value = contact?.id || '';
        document.getElementById('allowedName').value = contact?.name || '';
        document.getElementById('allowedPhone').value = contact?.phone || '';
        document.getElementById('allowedRelation').value = contact?.relation || '';
        document.getElementById('allowedTags').value = (contact?.tags || []).join(', ');
        document.getElementById('allowedAliases').value = (contact?.llm_friendly_names || []).join(', ');
        document.getElementById('allowedNotes').value = contact?.notes || '';
        document.getElementById('allowedAttributes').value = contact?.attributes && Object.keys(contact.attributes).length
            ? JSON.stringify(contact.attributes, null, 2)
            : '';
        document.getElementById('allowedEnabled').checked = contact ? contact.enabled !== false : true;
        form.style.display = 'block';
    }

    hideAllowedContactForm() {
        document.getElementById('addAllowedContactForm').style.display = 'none';
    }

    async editAllowedContact(id) {
        try {
            const c = await this.api('GET', `/api/v1/permissions/contacts/${id}`);
            this.showAllowedContactForm(c);
        } catch (e) {
            this.toast(`Load failed: ${e.message}`, 'error');
        }
    }

    async deleteAllowedContact(id) {
        if (!confirm('Remove this contact from the allow-list?')) return;
        try {
            await this.api('DELETE', `/api/v1/permissions/contacts/${id}`);
            this.toast('Contact removed', 'success');
            this.loadPermissions();
        } catch (e) {
            this.toast(`Delete failed: ${e.message}`, 'error');
        }
    }

    _parseCsv(value) {
        return (value || '')
            .split(',')
            .map(s => s.trim())
            .filter(Boolean);
    }

    async saveAllowedContact() {
        const id = document.getElementById('allowedContactId').value;
        const name = document.getElementById('allowedName').value.trim();
        const phone = document.getElementById('allowedPhone').value.trim();
        const relation = document.getElementById('allowedRelation').value.trim();
        const tags = this._parseCsv(document.getElementById('allowedTags').value);
        const llm_friendly_names = this._parseCsv(document.getElementById('allowedAliases').value);
        const notes = document.getElementById('allowedNotes').value.trim();
        const enabled = document.getElementById('allowedEnabled').checked;
        const attrRaw = document.getElementById('allowedAttributes').value.trim();

        if (!name || !phone) {
            this.toast('Name and phone are required', 'error');
            return;
        }

        let attributes = {};
        if (attrRaw) {
            try {
                attributes = JSON.parse(attrRaw);
                if (typeof attributes !== 'object' || Array.isArray(attributes)) {
                    throw new Error('must be a JSON object');
                }
            } catch (e) {
                this.toast(`Attributes JSON invalid: ${e.message}`, 'error');
                return;
            }
        }

        const payload = {
            name,
            phone,
            relation: relation || null,
            tags,
            llm_friendly_names,
            notes: notes || null,
            attributes,
            enabled,
        };

        try {
            if (id) {
                await this.api('PUT', `/api/v1/permissions/contacts/${id}`, payload);
                this.toast('Contact updated', 'success');
            } else {
                await this.api('POST', '/api/v1/permissions/contacts', payload);
                this.toast('Contact added', 'success');
            }
            this.hideAllowedContactForm();
            this.loadPermissions();
        } catch (e) {
            this.toast(`Save failed: ${e.message}`, 'error');
        }
    }

    // ─── Webhooks ───────────────────────────────────────────────────────

    async loadWebhooks() {
        try {
            const data = await this.api('GET', '/api/v1/webhooks/');
            this.renderWebhooks(data.webhooks || []);
        } catch {}
    }

    async addWebhook() {
        const url = document.getElementById('webhookUrl').value.trim();
        const name = document.getElementById('webhookName').value.trim() || 'custom';
        const events = document.getElementById('webhookEvents').value.trim().split(',').map(e => e.trim()).filter(Boolean);
        if (!url) { this.toast('Webhook URL is required', 'error'); return; }
        try {
            await this.api('POST', '/api/v1/webhooks/register', { url, name, events });
            this.toast('Webhook registered', 'success');
            document.getElementById('addWebhookForm').style.display = 'none';
            this.loadWebhooks();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    renderWebhooks(webhooks) {
        const container = document.getElementById('webhooksList');
        if (!webhooks || webhooks.length === 0) {
            container.innerHTML = '<div class="empty-state">No webhooks registered.</div>';
            return;
        }
        container.innerHTML = webhooks.map(wh => `
            <div class="card-row">
                <div class="card-info">
                    <div class="title">${this.escapeHtml(wh.name)} <span class="pill ${wh.active ? 'pill-green' : 'pill-muted'}">${wh.active ? 'active' : 'inactive'}</span></div>
                    <div class="meta">${this.escapeHtml(wh.url)} · ${this.escapeHtml((wh.events || []).join(', '))}</div>
                </div>
                <div class="card-actions">
                    <button class="btn btn-sm btn-danger" onclick="app.deleteWebhook(${wh.id})">Remove</button>
                </div>
            </div>
        `).join('');
    }

    async deleteWebhook(id) {
        try {
            await this.api('DELETE', `/api/v1/webhooks/${id}`);
            this.toast('Webhook removed', 'success');
            this.loadWebhooks();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    // ─── Event binding ──────────────────────────────────────────────────

    bindEvents() {
        document.getElementById('loginForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.login(document.getElementById('apiKeyInput').value.trim());
        });
        document.getElementById('logoutBtn').addEventListener('click', () => this.logout());

        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                btn.classList.add('active');
                document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
                if (btn.dataset.tab === 'schedule') this.loadScheduled();
                if (btn.dataset.tab === 'assistant') { this.loadAssistantConfig(); this.loadPersonas(); this.loadLLMInfo(); }
                if (btn.dataset.tab === 'messages') this.loadConversations();
                if (btn.dataset.tab !== 'messages') this.stopConvPolling();
                if (btn.dataset.tab === 'settings') this.loadLLMInfo();
            });
        });

        // Connection
        document.getElementById('connectBtn').addEventListener('click', () => this.connect());
        document.getElementById('disconnectBtn').addEventListener('click', () => this.disconnect());
        const relinkBtn = document.getElementById('relinkBtn');
        if (relinkBtn) relinkBtn.addEventListener('click', () => this.relink());
        document.getElementById('pairCodeBtn').addEventListener('click', () => this.getPairCode());

        // Messages
        document.getElementById('sendMessageForm').addEventListener('submit', (e) => { e.preventDefault(); this.sendMessage(); });
        document.getElementById('searchForm').addEventListener('submit', (e) => { e.preventDefault(); this.searchMessages(); });
        document.getElementById('refreshConversations').addEventListener('click', () => {
            this.loadConversations();
            this.refreshActiveConversation();
        });
        document.getElementById('convFilter').addEventListener('input', () => this.renderConvList());
        document.getElementById('convList').addEventListener('click', (e) => {
            const item = e.target.closest('.conv-item');
            if (item) this.openConversation(item.dataset.jid);
        });
        document.getElementById('convSendForm').addEventListener('submit', (e) => { e.preventDefault(); this.sendConvMessage(); });
        document.getElementById('convMessages').addEventListener('click', (e) => this.onConvMediaClick(e));
        const attachBtn = document.getElementById('convAttachBtn');
        if (attachBtn) {
            attachBtn.addEventListener('click', () => document.getElementById('convAttachInput').click());
            document.getElementById('convAttachInput').addEventListener('change', (e) => {
                this.setAttachment(e.target.files && e.target.files[0]);
            });
            document.getElementById('convAttachClear').addEventListener('click', () => this.setAttachment(null));
        }
        const backupForm = document.getElementById('backupImportForm');
        if (backupForm) backupForm.addEventListener('submit', (e) => { e.preventDefault(); this.importBackup(); });

        // Assistant
        document.getElementById('assistantForm').addEventListener('submit', (e) => { e.preventDefault(); this.saveAssistantConfig(); });
        document.getElementById('addRuleBtn').addEventListener('click', () => {
            const f = document.getElementById('addRuleForm');
            f.style.display = f.style.display === 'none' ? 'block' : 'none';
        });
        document.getElementById('saveRuleBtn').addEventListener('click', () => this.addRule());
        document.getElementById('cancelRuleBtn').addEventListener('click', () => this.resetRuleForm());

        document.getElementById('addPersonaBtn').addEventListener('click', () => {
            const f = document.getElementById('addPersonaForm');
            f.style.display = f.style.display === 'none' ? 'block' : 'none';
        });
        document.getElementById('savePersonaBtn').addEventListener('click', () => this.savePersona());
        document.getElementById('cancelPersonaBtn').addEventListener('click', () => this.resetPersonaForm());

        // Schedule
        document.getElementById('scheduleForm').addEventListener('submit', (e) => { e.preventDefault(); this.createScheduled(); });
        document.getElementById('refreshScheduledBtn').addEventListener('click', () => this.loadScheduled());

        // Webhooks
        document.getElementById('addWebhookBtn').addEventListener('click', () => {
            const f = document.getElementById('addWebhookForm');
            f.style.display = f.style.display === 'none' ? 'block' : 'none';
        });
        document.getElementById('saveWebhookBtn').addEventListener('click', () => this.addWebhook());

        // Permissions / allow-list
        const permToggle = document.getElementById('permissionsEnabled');
        if (permToggle) {
            permToggle.addEventListener('change', (e) => this.togglePermissions(e.target.checked));
        }
        const addAllowedBtn = document.getElementById('addAllowedContactBtn');
        if (addAllowedBtn) addAllowedBtn.addEventListener('click', () => this.showAllowedContactForm());
        const saveAllowedBtn = document.getElementById('saveAllowedContactBtn');
        if (saveAllowedBtn) saveAllowedBtn.addEventListener('click', () => this.saveAllowedContact());
        const cancelAllowedBtn = document.getElementById('cancelAllowedContactBtn');
        if (cancelAllowedBtn) cancelAllowedBtn.addEventListener('click', () => this.hideAllowedContactForm());
    }

    // ─── Utilities ──────────────────────────────────────────────────────

    toast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        const t = document.createElement('div');
        t.className = `toast toast-${type}`;
        t.textContent = message;
        container.appendChild(t);
        setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 4000);
    }

    escapeHtml(text) {
        const d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }

    escapeJs(text) {
        return String(text || '').replace(/'/g, "\\'");
    }
}

const app = new IDeepApp();
