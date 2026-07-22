# iDeep WhatsApp Bot API

A powerful, containerized WhatsApp automation API built with Python, FastAPI, and Neonize. Designed specifically for seamless integration with **Manus AI**, n8n, and other automation platforms.

This service allows your AI agents to read your WhatsApp chats, send messages, and even act as an auto-reply assistant on your behalf.

---

## 🌟 Key Features

*   **QR Code & Pair Code Login**: Easy web-based authentication flow.
*   **Chat History & Search**: Store and search through your messages programmatically (e.g., *"when was my date with Masoud Nayebi?"*).
*   **iDeep AI Assistant**: Configurable auto-reply system that can answer messages when you are unavailable.
*   **Manus Integration Ready**: Built-in API endpoints designed specifically for AI agents to query and interact with WhatsApp.
*   **Webhook Support**: Push real-time events (new messages, connection status) to external services like n8n.
*   **Secure API**: All endpoints are protected by an API key.
*   **Dockerized**: Ready for immediate deployment on any Linux server.

---

## 🚀 Quick Start Guide

### 1. Prerequisites

Ensure you have the following installed on your Linux server:
*   Docker
*   Docker Compose

### 2. Installation

Clone the repository and set up the environment:

```bash
# Clone the project (or copy the files)
cd whatsapp-bot-api

# Create the environment file and generate a secure API key
cp .env .env

# Start the service using Docker
./start.sh prod
```

### 3. Connecting WhatsApp

1. Open your browser and navigate to `http://YOUR_SERVER_IP:8000`.
2. You will be prompted for an API key. Find the generated `API_KEY` inside your `.env` file and enter it.
3. In the dashboard, go to the **Connection** tab and click **Connect**.
4. A QR code will appear. Open WhatsApp on your phone, go to **Linked Devices**, and scan the QR code.
5. Once connected, the status will change to "Connected" and your bot is ready!

---

## 🧩 Multi-Instance Deployment

`docker-compose.yml` runs **two independent bot instances** (e.g. one WhatsApp
number behind `wa1.cumran.ir`, another behind `wa2.cumran.ir`), sharing a
single Mongo container but writing to separate logical databases:

| Instance | Container | Host port (loopback only) | Env file | Mongo DB | Data volume |
|---|---|---|---|---|---|
| 1 | `ideep-whatsapp-bot-1` | `127.0.0.1:8011` | `.env.instance1` | `ideep_whatsapp` | `./data/instance1` |
| 2 | `ideep-whatsapp-bot-2` | `127.0.0.1:8012` | `.env.instance2` | `ideep_whatsapp_2` | `./data/instance2` |

Each instance has its own WhatsApp session (own QR login), its own
`API_KEY`/`JWT_SECRET`, and its own Mongo database/collections — but they
share the same `mongo` container and Docker image build. To add a third
instance, copy the `whatsapp-bot-2` block, give it a new container name, host
port, `env_file`, volume path, and a unique `MONGO_DB_NAME`.

Ports are bound to `127.0.0.1` only — neither instance publishes to the
public interface or to 80/443. Point your own host-level reverse proxy
(nginx, Caddy, Traefik, etc., set up outside this compose stack) at the two
loopback ports, e.g.:

```
wa1.cumran.ir  -> 127.0.0.1:8011
wa2.cumran.ir  -> 127.0.0.1:8012
```

The proxy terminates TLS; these containers only ever speak plain HTTP on
loopback.

Start everything with:

```bash
docker compose up -d
```

## 🤖 iDeep AI Assistant

The iDeep AI Assistant is a built-in feature that can automatically reply to messages when you are away.

### Configuration via Web UI
In the Web Dashboard, navigate to the **Assistant** tab to:
*   Enable/disable the auto-reply globally.
*   Set the assistant's name (default: *iDeep AI*).
*   Set the default away message (e.g., *"Hi, I am iDeep AI Assistant. Alireza is not available right now..."*).
*   Create custom rules based on specific contacts or keywords.

### Manus "Reply as Assistant" Flow
Your Manus AI can use the `/api/v1/assistant/reply-as-assistant` endpoint to dynamically generate and send replies. When Manus uses this endpoint, the message is automatically prefixed with `*iDeep AI*` (or your configured assistant name) so the recipient knows they are speaking with your AI assistant.

---

## 🔌 API Integration (For Manus / External Tools)

All API endpoints require authentication using the `X-API-Key` header.

### 1. Read Chats & Search

Your AI agent can read recent chats or search for specific information:

**Search Messages:**
```bash
curl -X POST http://YOUR_SERVER_IP:8000/api/v1/messages/search \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "date with Masoud", "contact": "Masoud Nayebi"}'
```

**Get Recent Chats:**
```bash
curl -X GET http://YOUR_SERVER_IP:8000/api/v1/messages/chats \
  -H "X-API-Key: YOUR_API_KEY"
```

### 2. Send Messages

**Send a standard message:**
```bash
curl -X POST http://YOUR_SERVER_IP:8000/api/v1/messages/send \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"phone": "989123456789", "message": "Hello from Manus!"}'
```

**Send as iDeep Assistant:**
```bash
curl -X POST http://YOUR_SERVER_IP:8000/api/v1/assistant/reply-as-assistant \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"phone": "989123456789", "message": "Alireza will be available in a few days."}'
```

### 3. API Documentation

For a complete list of endpoints, interactive testing, and schemas, visit the Swagger UI documentation:
*   **Swagger UI:** `http://YOUR_SERVER_IP:8000/docs`
*   **ReDoc:** `http://YOUR_SERVER_IP:8000/redoc`

---

## 📁 Project Structure

*   `app/main.py`: FastAPI application entry point.
*   `app/core/whatsapp_client.py`: Core WhatsApp integration using Neonize.
*   `app/api/`: REST API route handlers.
*   `app/models/schemas.py`: Pydantic validation models.
*   `templates/`: HTML templates for the Web UI.
*   `static/`: CSS and JavaScript for the Web UI.
*   `data/`: Persistent storage for SQLite databases and WhatsApp session data (mounted as a Docker volume).

---

## 🛠️ Advanced Configuration

You can customize the behavior of the bot by editing the `.env` file:

*   `MESSAGE_STORE_ENABLED`: Set to `true` to store incoming messages in SQLite for searching.
*   `MAX_STORED_MESSAGES`: Limit the number of stored messages (default: 10000).
*   `RATE_LIMIT_MESSAGES_PER_MINUTE`: Prevent spam by limiting outgoing messages.

After modifying the `.env` file, restart the Docker container:
```bash
docker compose down
docker compose up -d
```
