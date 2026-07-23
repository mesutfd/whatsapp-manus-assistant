#!/usr/bin/env bash
###############################################################################
# iDeep WhatsApp Bot API - nginx + TLS setup
#
# Writes an nginx reverse-proxy vhost per bot instance (proxying each public
# domain to its instance's loopback port from docker-compose.yml) and,
# unless --no-ssl is passed, obtains Let's Encrypt certificates via certbot's
# standalone authenticator. Standalone needs port 80 to itself, so certbot is
# told (via --pre-hook/--post-hook) to stop nginx right before requesting the
# certificate and start it again right after — those hooks are saved into the
# renewal config too, so the same stop/start happens automatically on every
# future auto-renewal, not just this first run.
#
# Targets Debian/Ubuntu (apt + systemd). Run as root on the server, after:
#   - DNS for each domain below already points at this server's public IP
#   - `docker compose up -d` is running so the loopback ports respond
#
# Usage:
#   sudo ./scripts/setup_nginx.sh --email you@example.com
#   sudo ./scripts/setup_nginx.sh --no-ssl        # HTTP-only, add TLS later
#
# Safe to re-run: it overwrites the same vhost files with the same content,
# and certbot no-ops if a valid certificate already exists.
###############################################################################

set -euo pipefail

# ── Instances: "domain:loopback_port" — edit/add lines here for more ────────
INSTANCES=(
  "wa1.cumran.ir:8011"
  "wa2.cumran.ir:8012"
)

EMAIL=""
ENABLE_SSL=1

usage() {
  echo "Usage: sudo $0 [--email you@example.com] [--no-ssl]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  # Accept -- as well as the em-dash/en-dash (—/–) that some terminals and
  # copy-paste sources auto-substitute for "--", so a mangled paste still works.
  case "$1" in
    --email|—email|–email) EMAIL="${2:-}"; shift 2 ;;
    --no-ssl|—no-ssl|–no-ssl) ENABLE_SSL=0; shift ;;
    -h|--help) usage ;;
    *)
      echo "Unknown argument: $1" >&2
      case "$1" in
        *email*|*no-ssl*)
          echo "Hint: this looks like a copy-paste issue — your source turned" >&2
          echo "'--' into a curly dash (— or –). Try retyping the flag by hand:" >&2
          echo "  sudo $0 --email you@example.com" >&2
          ;;
      esac
      usage
      ;;
  esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Must run as root (sudo $0 ...)." >&2
  exit 1
fi

if [[ $ENABLE_SSL -eq 1 && -z "$EMAIL" ]]; then
  echo "--email is required unless --no-ssl is passed (Let's Encrypt needs an" >&2
  echo "address for expiry/security notices)." >&2
  exit 1
fi

# ── Install nginx / certbot if missing ──────────────────────────────────────

if ! command -v nginx >/dev/null 2>&1; then
  echo "==> Installing nginx..."
  apt-get update -y
  apt-get install -y nginx
fi

if [[ $ENABLE_SSL -eq 1 ]] && ! command -v certbot >/dev/null 2>&1; then
  echo "==> Installing certbot..."
  apt-get update -y
  apt-get install -y certbot
fi

# ── Where vhost files go (Debian-style sites-available/enabled, falling ────
# ── back to conf.d if this box doesn't use that layout) ─────────────────────

if [[ -d /etc/nginx/sites-available ]]; then
  VHOST_DIR=/etc/nginx/sites-available
  ENABLED_DIR=/etc/nginx/sites-enabled
  mkdir -p "$ENABLED_DIR"
else
  VHOST_DIR=/etc/nginx/conf.d
  ENABLED_DIR=""
  mkdir -p "$VHOST_DIR"
fi

# First domain is used as the certbot cert name, so re-runs always land on
# the same certificate/renewal config instead of accumulating -0001 etc.
CERT_NAME="${INSTANCES[0]%%:*}"
CERT_DIR="/etc/letsencrypt/live/$CERT_NAME"

# ── Write one vhost per instance. HTTP-only until a cert exists; once it ───
# ── does, includes the HTTPS server block + a plain HTTP->HTTPS redirect. ──

write_vhost() {
  local domain="$1" port="$2" conf_path="$VHOST_DIR/$domain.conf"

  if [[ -f "$CERT_DIR/fullchain.pem" ]]; then
    echo "==> Writing $conf_path (proxy -> 127.0.0.1:$port, HTTPS)"
    cat > "$conf_path" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $domain;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name $domain;

    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://127.0.0.1:$port;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 90s;
    }
}
EOF
  else
    echo "==> Writing $conf_path (proxy -> 127.0.0.1:$port, HTTP-only)"
    cat > "$conf_path" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $domain;

    location / {
        proxy_pass http://127.0.0.1:$port;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 90s;
    }
}
EOF
  fi

  if [[ -n "$ENABLED_DIR" ]]; then
    ln -sf "$conf_path" "$ENABLED_DIR/$domain.conf"
  fi
}

for entry in "${INSTANCES[@]}"; do
  write_vhost "${entry%%:*}" "${entry##*:}"
done

echo "==> Testing nginx config"
nginx -t

echo "==> Starting/reloading nginx"
systemctl reload nginx 2>/dev/null || systemctl restart nginx

# ── Open the firewall if ufw is active ──────────────────────────────────────

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "==> Opening 80/443 in ufw"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

# ── TLS via certbot's standalone authenticator ──────────────────────────────
# certbot needs to bind port 80 itself for the HTTP-01 challenge, so nginx is
# stopped for the few seconds this takes (--pre-hook) and started back up
# right after (--post-hook), win or lose. Those hooks are persisted into
# /etc/letsencrypt/renewal/$CERT_NAME.conf, so `certbot renew` (run twice a
# day by the certbot.timer systemd unit) repeats the same stop/start on its
# own — no manual intervention needed for renewals down the line.

if [[ $ENABLE_SSL -eq 1 ]]; then
  domain_args=()
  for entry in "${INSTANCES[@]}"; do
    domain_args+=(-d "${entry%%:*}")
  done

  echo "==> Requesting certificate via certbot (stopping nginx to free port 80)..."
  set +e
  certbot certonly --standalone --non-interactive --agree-tos -m "$EMAIL" \
    --cert-name "$CERT_NAME" \
    --pre-hook "systemctl stop nginx" \
    --post-hook "systemctl start nginx" \
    "${domain_args[@]}"
  CERTBOT_STATUS=$?
  set -e

  if [[ $CERTBOT_STATUS -ne 0 ]]; then
    echo "==> certbot failed (exit $CERTBOT_STATUS). nginx has been restarted via" >&2
    echo "    its post-hook, so the site is still up over plain HTTP. Common" >&2
    echo "    causes: DNS for these domains not pointing at this server yet, or" >&2
    echo "    a cloud provider firewall/security group blocking inbound port 80" >&2
    echo "    (separate from ufw/iptables on this box). Fix that and re-run:" >&2
    echo "      sudo $0 --email $EMAIL" >&2
    exit "$CERTBOT_STATUS"
  fi

  echo "==> Certificate obtained. Rewriting vhosts to add HTTPS..."
  for entry in "${INSTANCES[@]}"; do
    write_vhost "${entry%%:*}" "${entry##*:}"
  done

  echo "==> Testing nginx config after adding HTTPS"
  nginx -t
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
fi

echo "==> Done. Instances:"
for entry in "${INSTANCES[@]}"; do
  domain="${entry%%:*}"
  port="${entry##*:}"
  scheme="http"
  [[ -f "$CERT_DIR/fullchain.pem" ]] && scheme="https"
  echo "   $scheme://$domain -> 127.0.0.1:$port"
done
