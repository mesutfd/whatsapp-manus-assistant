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
    stopPolling()        { this.stopQRPolling(); this.stopStatusPolling(); if (this.scheduleInterval) clearInterval(this.scheduleInterval); }

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

    async loadRecentMessages() {
        try {
            const data = await this.api('GET', '/api/v1/messages/history?limit=50');
            this.renderMessages(data.messages, 'messageList');
        } catch {}
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
                <div class="message-text">${this.escapeHtml(msg.text || '[Media]')}</div>
                <div class="message-time">${msg.timestamp ? new Date(msg.timestamp).toLocaleString() : ''}</div>
            </div>
        `).join('');
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
                if (btn.dataset.tab === 'messages') this.loadRecentMessages();
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
        document.getElementById('refreshMessages').addEventListener('click', () => this.loadRecentMessages());

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
