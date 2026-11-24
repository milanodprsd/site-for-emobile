#!/bin/bash
# Emobile — установка сайта с редактированием по паролю на Ubuntu.
# Запуск:  sudo bash servis-setup.sh
set -e
DOMAIN="servismaster.duckdns.org"   # домен сайта (пусто — без домена и https)
AUTO=0; NEWPASS=0; for a in "$@"; do case "$a" in --auto) AUTO=1;; --password) NEWPASS=1;; --domain=*) DOMAIN="${a#--domain=}";; esac; done
[ "$(id -u)" = 0 ] || { echo "Запустите от root"; exit 1; }
echo "== Устанавливаю nginx и python3 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx python3 curl >/dev/null
[ -n "$DOMAIN" ] && apt-get install -y -qq certbot python3-certbot-nginx >/dev/null

echo "== Копирую файлы сайта =="
id servis >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin servis
install -d -m 755 /opt/servis-master /var/www/html
install -d -m 755 -o servis -g servis /var/lib/servis-master
install -d -m 750 -o root -g servis /etc/servis-master
cat > /opt/servis-master/server.py <<'PYEOF'
__SERVER_PY__
PYEOF
echo "__INDEX_B64__" | base64 -d > /var/www/html/index.html
chmod 644 /var/www/html/index.html /opt/servis-master/server.py

if [ "$AUTO" = 0 ] && { [ ! -f /etc/servis-master/auth.json ] || [ "$NEWPASS" = 1 ]; }; then
  echo "== Придумайте пароль для редактирования сайта =="
  until python3 /opt/servis-master/server.py --set-password; do echo "Попробуйте ещё раз."; done
fi
[ -f /etc/servis-master/auth.json ] && { chown servis:servis /etc/servis-master/auth.json; chmod 600 /etc/servis-master/auth.json; }
if [ "$AUTO" = 0 ]; then rm -f /var/lib/servis-master/tokens.json; fi

cat > /usr/local/bin/servis-password <<'SH'
#!/bin/bash
python3 /opt/servis-master/server.py --set-password && chown servis:servis /etc/servis-master/auth.json /var/lib/servis-master/tokens.json 2>/dev/null; systemctl restart servis-master
SH
chmod 755 /usr/local/bin/servis-password

# Автообновление: раз в 5 минут сервер проверяет GitHub и, если вышла новая версия, ставит её сам.
# Работает, только если репозиторий публичный. Цены и пароль не трогает.
if [ -f "$0" ]; then cp "$0" /opt/servis-master/installed.sh 2>/dev/null || true; fi
cat > /usr/local/bin/servis-update <<'SH'
#!/bin/bash
URL="https://raw.githubusercontent.com/milanodprsd/site-for-emobile/main/servis-setup.sh"
exec 9>/run/servis-update.lock; flock -n 9 || exit 0
TMP=$(mktemp) || exit 1
trap 'rm -f "$TMP"' EXIT
curl -fsSL --max-time 60 "$URL" -o "$TMP" || exit 0
grep -q "Emobile" "$TMP" && bash -n "$TMP" || exit 0
cmp -s "$TMP" /opt/servis-master/installed.sh && exit 0
echo "Найдена новая версия сайта, обновляю..."
bash "$TMP" --auto && cp "$TMP" /opt/servis-master/installed.sh
SH
chmod 755 /usr/local/bin/servis-update
cat > /etc/systemd/system/servis-update.service <<'UNIT'
[Unit]
Description=Servis Master auto-update from GitHub
[Service]
Type=oneshot
ExecStart=/usr/local/bin/servis-update
UNIT
cat > /etc/systemd/system/servis-update.timer <<'UNIT'
[Unit]
Description=Check GitHub for site updates every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
UNIT

cat > /etc/systemd/system/servis-master.service <<'UNIT'
[Unit]
Description=Servis Master price API
After=network.target
[Service]
User=servis
ExecStart=/usr/bin/python3 /opt/servis-master/server.py
Restart=always
NoNewPrivileges=true
ProtectSystem=full
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/nginx/sites-available/servis-master <<'NGX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name __SERVER_NAME__;
    root /var/www/html;
    index index.html;
    client_max_body_size 5m;
    location = /prices.json {
        alias /var/lib/servis-master/prices.json;
        add_header Cache-Control "no-store";
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header X-Real-IP $remote_addr;
    }
    location / {
        try_files $uri $uri/ =404;
        add_header Cache-Control "no-cache";
    }
}
NGX
sed -i "s/__SERVER_NAME__/${DOMAIN:-_}/" /etc/nginx/sites-available/servis-master
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/servis-master /etc/nginx/sites-enabled/servis-master

echo "== Запускаю =="
systemctl daemon-reload
systemctl enable --now servis-master >/dev/null 2>&1
systemctl restart servis-master
systemctl enable --now servis-update.timer >/dev/null 2>&1 || true
nginx -t -q && systemctl enable nginx >/dev/null 2>&1 && systemctl restart nginx
if command -v ufw >/dev/null; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow OpenSSH >/dev/null 2>&1 || true; fi
iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || true

HTTPS=0
if [ -n "$DOMAIN" ]; then
  echo "== Включаю https для $DOMAIN =="
  if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect --keep-until-expiring -q; then
    HTTPS=1
  else
    echo "!! https пока не включился (проверьте, что домен указывает на этот сервер). Сайт работает по http."
  fi
fi

sleep 1
IP=$(curl -s -4 --max-time 5 ifconfig.me || hostname -I | awk '{print $1}')
if curl -s -o /dev/null -w '%{http_code}' -H "Host: ${DOMAIN:-localhost}" http://127.0.0.1/ | grep -qE '200|301' && systemctl is-active -q servis-master; then
  echo ""
  if [ "$HTTPS" = 1 ]; then echo "ГОТОВО! Сайт: https://$DOMAIN"; elif [ -n "$DOMAIN" ]; then echo "ГОТОВО! Сайт: http://$DOMAIN  (или http://$IP)"; else echo "ГОТОВО! Сайт: http://$IP"; fi
  echo "Чтобы редактировать: нажмите на замок в шапке сайта и введите пароль."
  echo "Сменить пароль: servis-password"
else
  echo "Что-то пошло не так:"; systemctl --no-pager status nginx servis-master | tail -20
fi
