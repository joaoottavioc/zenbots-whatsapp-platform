#!/bin/bash
set -euo pipefail

# Install Caddy ARM64 binary
CADDY_VERSION="2.9.1"
curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v$${CADDY_VERSION}/caddy_$${CADDY_VERSION}_linux_arm64.tar.gz" \
  -o /tmp/caddy.tar.gz
tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy
chmod +x /usr/local/bin/caddy
rm /tmp/caddy.tar.gz

# Create caddy user (non-root)
useradd --system --home /var/lib/caddy --shell /usr/sbin/nologin caddy || true
mkdir -p /var/lib/caddy /etc/caddy /var/log/caddy
chown caddy:caddy /var/lib/caddy /var/log/caddy

# Write Caddyfile
cat > /etc/caddy/Caddyfile <<'CADDYEOF'
${domain} {
  reverse_proxy ${upstream} {
    flush_interval -1
    transport http {
      read_timeout 0
      write_timeout 0
    }
    health_uri /health/live
    health_interval 15s
    health_timeout 5s
  }
}
CADDYEOF

chown caddy:caddy /etc/caddy/Caddyfile

# Create systemd service
cat > /etc/systemd/system/caddy.service <<'SYSTEMDEOF'
[Unit]
Description=Caddy reverse proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
TimeoutStopSec=5s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=XDG_DATA_HOME=/var/lib/caddy
Environment=XDG_CONFIG_HOME=/var/lib/caddy

[Install]
WantedBy=multi-user.target
SYSTEMDEOF

# Enable and start Caddy
systemctl daemon-reload
systemctl enable caddy
systemctl start caddy
