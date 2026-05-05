#!/bin/bash
###############################################################################
# iDeep WhatsApp Bot API - Quick Start Script
# Usage: ./start.sh [dev|prod]
# Works on both macOS and Linux
###############################################################################

set -e

MODE=${1:-prod}

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════════╗"
echo "║          iDeep WhatsApp Bot API                         ║"
echo "║          Starting in ${MODE} mode...                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Detect OS for sed compatibility
SED_INPLACE() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "$@"
    else
        sed -i "$@"
    fi
}

# Check if .env exists
if [ ! -f .env ]; then
    echo -e "${YELLOW}No .env file found. Creating from .env.example...${NC}"
    cp .env .env
    # Generate secure API key
    API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -base64 32)
    JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))" 2>/dev/null || openssl rand -base64 64)
    SED_INPLACE "s|your-secure-api-key-change-this|${API_KEY}|g" .env
    SED_INPLACE "s|your-jwt-secret-change-this|${JWT_SECRET}|g" .env
    echo -e "${GREEN}Generated secure API key: ${API_KEY}${NC}"
    echo -e "${GREEN}Save this key! You'll need it to authenticate.${NC}"
    echo ""
fi

# Create data directory
mkdir -p data/logs

# Ensure libmagic is installed (required by python-magic, a neonize dependency)
ensure_libmagic() {
    # Quick presence check across the common locations
    if ldconfig -p 2>/dev/null | grep -q "libmagic\.so" \
        || [ -f /usr/lib/libmagic.dylib ] \
        || [ -f /opt/homebrew/lib/libmagic.dylib ] \
        || [ -f /usr/local/lib/libmagic.dylib ]; then
        return 0
    fi

    echo -e "${YELLOW}libmagic not found. Attempting to install...${NC}"

    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &> /dev/null; then
            brew install libmagic
        else
            echo -e "${RED}Homebrew not found. Install libmagic manually: brew install libmagic${NC}"
            exit 1
        fi
    elif command -v apt-get &> /dev/null; then
        SUDO=""
        [ "$(id -u)" -ne 0 ] && SUDO="sudo"
        $SUDO apt-get update -y && $SUDO apt-get install -y libmagic1
    elif command -v dnf &> /dev/null; then
        SUDO=""
        [ "$(id -u)" -ne 0 ] && SUDO="sudo"
        $SUDO dnf install -y file-libs
    elif command -v yum &> /dev/null; then
        SUDO=""
        [ "$(id -u)" -ne 0 ] && SUDO="sudo"
        $SUDO yum install -y file-libs
    elif command -v apk &> /dev/null; then
        SUDO=""
        [ "$(id -u)" -ne 0 ] && SUDO="sudo"
        $SUDO apk add --no-cache libmagic
    else
        echo -e "${RED}Could not detect package manager. Install libmagic manually.${NC}"
        exit 1
    fi
}

if [ "$MODE" = "dev" ]; then
    echo -e "${BLUE}Starting in development mode (hot-reload)...${NC}"
    echo ""

    # System libs required at runtime by Python deps
    ensure_libmagic

    # Detect Python command
    if command -v python3 &> /dev/null; then
        PYTHON_CMD=python3
    elif command -v python &> /dev/null; then
        PYTHON_CMD=python
    else
        echo -e "${RED}Python not found. Please install Python 3.10+${NC}"
        exit 1
    fi

    PYTHON_VERSION=$($PYTHON_CMD --version 2>&1)
    echo -e "${BLUE}Using: ${PYTHON_VERSION}${NC}"

    # Create or update venv
    if [ ! -d "venv" ]; then
        echo "Creating virtual environment..."
        $PYTHON_CMD -m venv venv
    fi

    # Activate venv
    source venv/bin/activate

    # Set PyO3 compatibility for Python 3.14+ (allows building pydantic-core with newer Python)
    export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

    # Install/upgrade dependencies
    echo "Installing dependencies..."
    pip install --upgrade pip
    pip install -r requirements.txt

    echo ""
    echo -e "${GREEN}Dependencies installed. Starting server...${NC}"
    echo ""

    # Run with reload
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

elif [ "$MODE" = "prod" ]; then
    echo -e "${BLUE}Starting in production mode (Docker)...${NC}"
    echo ""

    # Check Docker
    if ! command -v docker &> /dev/null; then
        echo -e "${YELLOW}Docker not found. Installing...${NC}"
        curl -fsSL https://get.docker.com | sh
    fi

    # Build and start
    docker compose up -d --build

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  Service is running!                                    ║${NC}"
    echo -e "${GREEN}║                                                         ║${NC}"
    echo -e "${GREEN}║  Web UI:  http://localhost:8000                         ║${NC}"
    echo -e "${GREEN}║  API Docs: http://localhost:8000/docs                   ║${NC}"
    echo -e "${GREEN}║  Health:  http://localhost:8000/health                  ║${NC}"
    echo -e "${GREEN}║                                                         ║${NC}"
    echo -e "${GREEN}║  Logs: docker compose logs -f                           ║${NC}"
    echo -e "${GREEN}║  Stop: docker compose down                              ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"

else
    echo "Usage: ./start.sh [dev|prod]"
    exit 1
fi
