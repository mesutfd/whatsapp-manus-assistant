/**
 * iDeep WhatsApp Bot - Control Panel JavaScript
 * Handles authentication, connection management, messaging, and UI interactions.
 */

class IDeepApp {
    constructor() {
        this.apiKey = localStorage.getItem('ideep_api_key') || '';
        this.baseUrl = window.location.origin;
        this.pollInterval = null;
        this.statusInterval = null;

        this.init();
    }

    init() {
        this.bindEvents();

        if (this.apiKey) {
            this.verifyAuth();
        }
    }

    // ─── API Helper ─────────────────────────────────────────────────────

    async api(method, path, body = null) {
        const headers = {
            'Content-Type': 'application/json',
            'X-API-Key': this.apiKey,
        };

        const options = { method, headers };
        if (body) {
            options.body = JSON.stringify(body);
        }

        try {
            const response = await fetch(`${this.baseUrl}${path}`, options);
            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.detail || `HTTP ${response.status}`);
            }

            return data;
        } catch (error) {
            if (error.message.includes('401')) {
                this.showLogin();
            }
            throw error;
        }
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
            this.toast('Authenticated successfully', 'success');
        } catch (e) {
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

    // ─── UI Navigation ──────────────────────────────────────────────────

    showLogin() {
        document.getElementById('loginScreen').style.display = 'flex';
        document.getElementById('dashboardScreen').style.display = 'none';
        document.getElementById('logoutBtn').style.display = 'none';
    }

    showDashboard() {
        document.getElementById('loginScreen').style.display = 'none';
        document.getElementById('dashboardScreen').style.display = 'block';
        document.getElementById('logoutBtn').style.display = 'block';
        this.startStatusPolling();
        this.loadAssistantConfig();
        this.loadWebhooks();
        this.updateApiBaseUrl();
    }

    updateApiBaseUrl() {
        document.getElementById('apiBaseUrl').textContent = this.baseUrl;
    }

    // ─── Connection Management ──────────────────────────────────────────

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
        if (!confirm('This will log out of WhatsApp and wipe the saved session. You will need to scan a fresh QR. Continue?')) {
            return;
        }
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
        if (!phone) {
            this.toast('Enter a phone number', 'error');
            return;
        }

        try {
            const data = await this.api('POST', '/api/v1/connection/pair-code', { phone_number: phone });
            if (data.success) {
                document.getElementById('pairCodeDisplay').style.display = 'block';
                document.getElementById('pairCodeValue').textContent = data.pair_code;
                this.toast('Pair code generated! Enter it on your phone.', 'success');
            } else {
                this.toast(data.message || 'Failed to get pair code', 'error');
            }
        } catch (e) {
            this.toast(`Pair code error: ${e.message}`, 'error');
        }
    }

    startQRPolling() {
        this.stopQRPolling();
        this.pollInterval = setInterval(() => this.pollQR(), 2000);
    }

    stopQRPolling() {
        if (this.pollInterval) {
            clearInterval(this.pollInterval);
            this.pollInterval = null;
        }
    }

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
                this.setQrHint('Open WhatsApp -> Settings -> Linked Devices -> Link a device, then scan.');
            } else if (data.message) {
                this.setQrHint(data.message);
            }
        } catch (e) {
            // Silently retry
        }
    }

    startStatusPolling() {
        this.stopStatusPolling();
        this.pollStatus();
        this.statusInterval = setInterval(() => this.pollStatus(), 5000);
    }

    stopStatusPolling() {
        if (this.statusInterval) {
            clearInterval(this.statusInterval);
            this.statusInterval = null;
        }
    }

    stopPolling() {
        this.stopQRPolling();
        this.stopStatusPolling();
    }

    async pollStatus() {
        try {
            const data = await this.api('GET', '/api/v1/connection/status');
            this.updateStatusDisplay(data);
            this.updateConnectionUI(data.state);
            if (data.state === 'logged_out' && this._lastState !== 'logged_out') {
                this.toast('WhatsApp session expired. Click "Re-link / Switch Account" to scan a fresh QR.', 'error');
            }
            this._lastState = data.state;
        } catch (e) {
            // Silently handle
        }
    }

    updateStatusDisplay(data) {
        document.getElementById('statusState').textContent = data.state || '-';
        document.getElementById('statusConnectedAt').textContent = data.connected_at
            ? new Date(data.connected_at).toLocaleString()
            : '-';
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
            case 'logged_out':
            case 'disconnected':
            default:
                text.textContent = state === 'logged_out' ? 'Logged out' : 'Disconnected';
                connectBtn.style.display = 'inline-flex';
                disconnectBtn.style.display = 'none';
                if (relinkBtn) relinkBtn.style.display = 'none';
                qrImage.style.display = 'none';
                qrPlaceholder.style.display = 'block';
                connectedBadge.style.display = 'none';
                break;
        }
    }

    // ─── Messages ───────────────────────────────────────────────────────

    async sendMessage() {
        const phone = document.getElementById('msgPhone').value.trim();
        const message = document.getElementById('msgText').value.trim();

        if (!phone || !message) {
            this.toast('Phone and message are required', 'error');
            return;
        }

        try {
            const data = await this.api('POST', '/api/v1/messages/send', { phone, message });
            if (data.success) {
                this.toast('Message sent!', 'success');
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

        if (!query) {
            this.toast('Enter a search query', 'error');
            return;
        }

        try {
            const data = await this.api('POST', '/api/v1/messages/search', {
                query,
                contact: contact || null,
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
        } catch (e) {
            // Silently handle
        }
    }

    renderMessages(messages, containerId) {
        const container = document.getElementById(containerId);
        if (!messages || messages.length === 0) {
            container.innerHTML = '<div class="message-item"><p style="color:var(--text-muted)">No messages found</p></div>';
            return;
        }

        container.innerHTML = messages.map(msg => `
            <div class="message-item">
                <div class="message-sender">${msg.sender_name || msg.from || 'Unknown'}</div>
                <div class="message-text">${this.escapeHtml(msg.text || '[Media]')}</div>
                <div class="message-time">${msg.timestamp ? new Date(msg.timestamp).toLocaleString() : ''}</div>
            </div>
        `).join('');
    }

    // ─── Assistant ──────────────────────────────────────────────────────

    async loadAssistantConfig() {
        try {
            const data = await this.api('GET', '/api/v1/assistant/config');
            document.getElementById('autoReplyEnabled').checked = data.enabled;
            document.getElementById('assistantName').value = data.assistant_name || 'iDeep AI';
            document.getElementById('autoReplyMessage').value = data.message || '';
            this.renderRules(data.rules || []);
        } catch (e) {
            // Silently handle
        }
    }

    async saveAssistantConfig() {
        const config = {
            enabled: document.getElementById('autoReplyEnabled').checked,
            assistant_name: document.getElementById('assistantName').value,
            message: document.getElementById('autoReplyMessage').value,
        };

        try {
            await this.api('PUT', '/api/v1/assistant/config', config);
            this.toast('Assistant configuration saved', 'success');
        } catch (e) {
            this.toast(`Save failed: ${e.message}`, 'error');
        }
    }

    async addRule() {
        const contact = document.getElementById('ruleContact').value.trim();
        const keyword = document.getElementById('ruleKeyword').value.trim();
        const message = document.getElementById('ruleMessage').value.trim();

        if (!message) {
            this.toast('Reply message is required', 'error');
            return;
        }

        try {
            await this.api('POST', '/api/v1/assistant/rules/add', {
                contact: contact || null,
                keyword: keyword || null,
                message,
                enabled: true,
            });
            this.toast('Rule added', 'success');
            document.getElementById('addRuleForm').style.display = 'none';
            this.loadAssistantConfig();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    renderRules(rules) {
        const container = document.getElementById('rulesList');
        if (!rules || rules.length === 0) {
            container.innerHTML = '<p style="color:var(--text-muted);font-size:13px;">No rules configured</p>';
            return;
        }

        container.innerHTML = rules.map((rule, i) => `
            <div class="rule-item">
                <div class="rule-info">
                    <strong>${rule.keyword ? `Keyword: "${rule.keyword}"` : ''}${rule.contact ? `Contact: ${rule.contact}` : ''}${!rule.keyword && !rule.contact ? 'All messages' : ''}</strong>
                    <span>${rule.message}</span>
                </div>
                <button class="btn btn-sm btn-danger" onclick="app.deleteRule(${i})">Remove</button>
            </div>
        `).join('');
    }

    async deleteRule(index) {
        try {
            await this.api('DELETE', `/api/v1/assistant/rules/${index}`);
            this.toast('Rule removed', 'success');
            this.loadAssistantConfig();
        } catch (e) {
            this.toast(`Error: ${e.message}`, 'error');
        }
    }

    // ─── Webhooks ───────────────────────────────────────────────────────

    async loadWebhooks() {
        try {
            const data = await this.api('GET', '/api/v1/webhooks/');
            this.renderWebhooks(data.webhooks || []);
        } catch (e) {
            // Silently handle
        }
    }

    async addWebhook() {
        const url = document.getElementById('webhookUrl').value.trim();
        const name = document.getElementById('webhookName').value.trim() || 'custom';
        const events = document.getElementById('webhookEvents').value.trim().split(',').map(e => e.trim());

        if (!url) {
            this.toast('Webhook URL is required', 'error');
            return;
        }

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
            container.innerHTML = '<p style="color:var(--text-muted);font-size:13px;">No webhooks registered</p>';
            return;
        }

        container.innerHTML = webhooks.map(wh => `
            <div class="webhook-item">
                <div class="webhook-info">
                    <strong>${wh.name}</strong>
                    <span>${wh.url} | Events: ${wh.events.join(', ')} | ${wh.active ? 'Active' : 'Inactive'}</span>
                </div>
                <button class="btn btn-sm btn-danger" onclick="app.deleteWebhook(${wh.id})">Remove</button>
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

    // ─── Event Binding ──────────────────────────────────────────────────

    bindEvents() {
        // Login
        document.getElementById('loginForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.login(document.getElementById('apiKeyInput').value.trim());
        });

        document.getElementById('logoutBtn').addEventListener('click', () => this.logout());

        // Tabs
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                btn.classList.add('active');
                document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
            });
        });

        // Connection
        document.getElementById('connectBtn').addEventListener('click', () => this.connect());
        document.getElementById('disconnectBtn').addEventListener('click', () => this.disconnect());
        const relinkBtn = document.getElementById('relinkBtn');
        if (relinkBtn) relinkBtn.addEventListener('click', () => this.relink());
        document.getElementById('pairCodeBtn').addEventListener('click', () => this.getPairCode());

        // Messages
        document.getElementById('sendMessageForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.sendMessage();
        });

        document.getElementById('searchForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.searchMessages();
        });

        document.getElementById('refreshMessages').addEventListener('click', () => this.loadRecentMessages());

        // Assistant
        document.getElementById('assistantForm').addEventListener('submit', (e) => {
            e.preventDefault();
            this.saveAssistantConfig();
        });

        document.getElementById('addRuleBtn').addEventListener('click', () => {
            const form = document.getElementById('addRuleForm');
            form.style.display = form.style.display === 'none' ? 'block' : 'none';
        });

        document.getElementById('saveRuleBtn').addEventListener('click', () => this.addRule());

        // Webhooks
        document.getElementById('addWebhookBtn').addEventListener('click', () => {
            const form = document.getElementById('addWebhookForm');
            form.style.display = form.style.display === 'none' ? 'block' : 'none';
        });

        document.getElementById('saveWebhookBtn').addEventListener('click', () => this.addWebhook());
    }

    // ─── Utilities ──────────────────────────────────────────────────────

    toast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize app
const app = new IDeepApp();
