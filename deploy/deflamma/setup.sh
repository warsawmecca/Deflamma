#!/bin/bash
# Run from this project on the Oracle Ubuntu server: sudo bash deploy/deflamma/setup.sh http|https
set -euo pipefail
cd -- "$(dirname -- "$0")"
[[ $EUID == 0 ]] || { echo 'Run with sudo.'; exit 1; }
case "${1:-}" in
http)
    apt-get update
    apt-get install -y nginx apache2-utils certbot
    mkdir -p /var/www/letsencrypt /etc/nginx/snippets
    if [[ -e /etc/nginx/sites-available/deflamma ]]; then
        cp -a /etc/nginx/sites-available/deflamma "/etc/nginx/sites-available/deflamma.backup.$(date +%Y%m%d%H%M%S)"
    fi
    install -m 644 bootstrap.conf /etc/nginx/sites-available/deflamma
    ln -sfn /etc/nginx/sites-available/deflamma /etc/nginx/sites-enabled/deflamma
    nginx -t
    systemctl enable --now nginx
    systemctl reload nginx
    echo 'HTTP ready. Check DNS and inbound TCP 80, then run the certbot command in README.'
    ;;
https)
    test -s /etc/letsencrypt/live/deflamma.duckdns.org/fullchain.pem
    test -s /etc/letsencrypt/live/deflamma.duckdns.org/privkey.pem
    systemctl cat himawari.service >/dev/null
    if [[ ! -s /etc/nginx/himawari.htpasswd ]]; then
        htpasswd -c /etc/nginx/himawari.htpasswd admin
    fi
    chown root:www-data /etc/nginx/himawari.htpasswd
    chmod 640 /etc/nginx/himawari.htpasswd
    mkdir -p /etc/systemd/system/himawari.service.d
    install -m 644 public-origin.conf /etc/systemd/system/himawari.service.d/public-origin.conf
    install -m 644 proxy.conf /etc/nginx/snippets/himawari-proxy.conf
    cp -a /etc/nginx/sites-available/deflamma "/etc/nginx/sites-available/deflamma.backup.$(date +%Y%m%d%H%M%S)"
    install -m 644 https.conf /etc/nginx/sites-available/deflamma
    nginx -t
    systemctl daemon-reload
    systemctl restart himawari
    systemctl reload nginx
    mkdir -p /etc/letsencrypt/renewal-hooks/deploy
    install -m 755 reload-nginx /etc/letsencrypt/renewal-hooks/deploy/reload-nginx
    systemctl enable --now certbot.timer
    echo 'Ready: https://deflamma.duckdns.org ; admin: https://deflamma.duckdns.org/admin'
    ;;
*) echo 'Usage: sudo bash deploy/deflamma/setup.sh http|https'; exit 2 ;;
esac
