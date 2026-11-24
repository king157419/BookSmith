#!/bin/sh
# 在入口服务器上为 IP 地址申请 Let's Encrypt 证书并切到 HTTPS。
# 前提：腾讯云防火墙已放行 80 和 443；用户已同意 Let's Encrypt 订户协议
#       （https://letsencrypt.org/repository/）。
# IP 证书只有约 6 天有效期（shortlived 配置），由 certbot.timer 每天自动续期。
set -eu
IP="${1:-124.220.163.249}"
CB=/opt/certbot/bin/certbot
sudo $CB certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --webroot -w /var/www/acme --ip-address "$IP" --preferred-profile shortlived \
    --cert-name booksmith-ip \
    --deploy-hook "systemctl reload nginx"
sudo ln -sf /etc/letsencrypt/live/booksmith-ip/fullchain.pem /etc/nginx/ssl/booksmith.crt
sudo ln -sf /etc/letsencrypt/live/booksmith-ip/privkey.pem   /etc/nginx/ssl/booksmith.key
# 80 端口改成跳转到 HTTPS（ACME 验证路径除外）
printf 'location / { return 301 https://$host$request_uri; }\n' | sudo tee /etc/nginx/snippets/booksmith-http.conf >/dev/null
sudo nginx -t && sudo systemctl reload nginx
# 自动续期：每天两次
sudo tee /etc/systemd/system/certbot-renew.service >/dev/null <<UNIT
[Unit]
Description=Renew Let's Encrypt certificates
[Service]
Type=oneshot
ExecStart=$CB renew --quiet
UNIT
sudo tee /etc/systemd/system/certbot-renew.timer >/dev/null <<UNIT
[Unit]
Description=Renew Let's Encrypt certificates twice a day
[Timer]
OnCalendar=*-*-* 03,15:17
RandomizedDelaySec=30min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now certbot-renew.timer
echo "完成：https://$IP/booksmith/"
