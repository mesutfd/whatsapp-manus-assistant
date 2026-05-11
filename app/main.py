"""
iDeep WhatsApp Bot API - Main Application
A comprehensive WhatsApp automation API built with FastAPI and Neonize.
Designed for integration with Manus AI and other automation platforms.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.assistant import router as assistant_router
from app.api.connection import router as connection_router
from app.api.contacts import router as contacts_router
from app.api.instructions import router as instructions_router
from app.api.messages import router as messages_router
from app.api.permissions import router as permissions_router
from app.api.schedule import router as schedule_router
from app.api.webhooks import router as webhooks_router
from app.api.smart import router as smart_router
from app.core.config import settings
from app.core.database import db
from app.core.scheduler import scheduler
from app.core.webhooks import webhook_service
from app.core.whatsapp_client import wa_client

# ─── Logging Configuration ───────────────────────────────────────────────────

log_dir = Path(settings.LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper()),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(settings.LOG_FILE, mode="a"),
    ],
)
logger = logging.getLogger(__name__)

# ─── Application Startup/Shutdown ────────────────────────────────────────────

START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager."""
    logger.info("=" * 60)
    logger.info(f"  {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info(f"  Starting up...")
    logger.info("=" * 60)

    # Ensure data directories exist
    Path(settings.WA_STORE_PATH).mkdir(parents=True, exist_ok=True)
    Path(settings.MESSAGE_STORE_DB).parent.mkdir(parents=True, exist_ok=True)

    # Initialize app DB (rules, personas, scheduled sends, quiet hours)
    await db.initialize()

    # Initialize WhatsApp client (will sync settings from the DB)
    await wa_client.initialize()

    # Start scheduled-send service
    await scheduler.start()

    # Start webhook service
    await webhook_service.start()

    # Wire webhook events from WA client
    wa_client.on_event("message", lambda data: asyncio.create_task(webhook_service.emit("message", data)))
    wa_client.on_event("message_sent", lambda data: asyncio.create_task(webhook_service.emit("message_sent", data)))
    wa_client.on_event("auto_reply_sent", lambda data: asyncio.create_task(webhook_service.emit("auto_reply_sent", data)))
    wa_client.on_event("connected", lambda data: asyncio.create_task(webhook_service.emit("connected", data)))
    wa_client.on_event("disconnected", lambda data: asyncio.create_task(webhook_service.emit("disconnected", data)))
    wa_client.on_event("qr", lambda data: asyncio.create_task(webhook_service.emit("qr", data)))

    logger.info("All services initialized successfully")
    logger.info(f"API Key: {settings.API_KEY[:8]}...{settings.API_KEY[-4:]}")
    logger.info(f"Server: http://{settings.HOST}:{settings.PORT}")
    logger.info(f"Docs: http://{settings.HOST}:{settings.PORT}/docs")

    yield

    # Shutdown
    logger.info("Shutting down services...")
    await scheduler.stop()
    await wa_client.disconnect()
    await webhook_service.stop()
    logger.info("All services stopped. Goodbye!")


# ─── FastAPI Application ─────────────────────────────────────────────────────

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="""
## iDeep WhatsApp Bot API

A powerful WhatsApp automation API designed for integration with **Manus AI** and other automation platforms.

### Features:
- **WhatsApp Login** via QR Code or Pair Code
- **Read Messages** - Access chat history and search conversations
- **Send Messages** - Send text messages to any WhatsApp number
- **Auto-Reply** - Configure iDeep AI Assistant for automatic responses
- **Contacts & Groups** - Access contacts, groups, and profiles
- **Webhooks** - Push events to external services (n8n, Manus, etc.)
- **API Authentication** - Secure access via API Key or JWT tokens

### Authentication:
All endpoints require authentication via:
- **API Key**: Pass in `X-API-Key` header
- **JWT Token**: Pass as `Bearer` token in `Authorization` header

### Quick Start:
1. Start the service with Docker
2. Open the web UI at `/` to scan QR code
3. Use the API key from `.env` to authenticate API calls
4. Integrate with Manus using the API endpoints
    """,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── Middleware ──────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_logging(request: Request, call_next):
    """Log all API requests."""
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start

    if not request.url.path.startswith("/static"):
        logger.debug(
            f"{request.method} {request.url.path} -> {response.status_code} ({duration:.3f}s)"
        )

    return response


# ─── Include Routers ─────────────────────────────────────────────────────────

app.include_router(connection_router)
app.include_router(messages_router)
app.include_router(contacts_router)
app.include_router(assistant_router)
app.include_router(schedule_router)
app.include_router(webhooks_router)
app.include_router(smart_router)
app.include_router(instructions_router)
app.include_router(permissions_router)

# ─── Static Files & Templates ────────────────────────────────────────────────

# Mount static files
static_path = Path(__file__).parent.parent / "static"
templates_path = Path(__file__).parent.parent / "templates"

if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

templates = Jinja2Templates(directory=str(templates_path)) if templates_path.exists() else None


# ─── Root & Health Endpoints ─────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request):
    """Serve the web UI for QR code login and management."""
    if templates:
        return templates.TemplateResponse(request, "index.html")
    return HTMLResponse("<h1>iDeep WhatsApp Bot API</h1><p>Web UI not available. Use /docs for API documentation.</p>")


@app.get("/health")
async def health_check():
    """Health check endpoint (no auth required)."""
    uptime_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "uptime": f"{hours}h {minutes}m {seconds}s",
        "whatsapp_connected": wa_client.is_connected,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/v1/info")
async def api_info():
    """Get API information and available endpoints (no auth required)."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "description": "WhatsApp automation API for Manus AI integration",
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "endpoints": {
            "instructions": "/api/v1/instructions (START HERE - bootstrap prompt for AI agents)",
            "smart": "/api/v1/smart (RECOMMENDED - Manus primary interface)",
            "connection": "/api/v1/connection",
            "messages": "/api/v1/messages",
            "contacts": "/api/v1/contacts",
            "assistant": "/api/v1/assistant",
            "permissions": "/api/v1/permissions (allow-list for assistant sends)",
            "schedule": "/api/v1/schedule",
            "webhooks": "/api/v1/webhooks",
            "health": "/health",
        },
        "authentication": {
            "api_key_header": settings.API_KEY_HEADER,
            "bearer_token": "Authorization: Bearer <jwt_token>",
        },
    }
