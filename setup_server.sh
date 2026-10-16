#!/bin/bash
# ==============================================================================
# Automated Setup Script for KhmerPDF (damspey.com) on Ubuntu / Oracle Cloud VM
# ==============================================================================

set -e

DOMAIN="damspey.com"
WWW_DOMAIN="www.damspey.com"
APP_DIR="/home/ubuntu/app"

echo "=== 1. Updating Ubuntu packages ==="
sudo apt-get update && sudo apt-get upgrade -y

echo "=== 2. Configuring Firewall (Fix for Oracle Cloud iptables) ==="
sudo apt-get install -y iptables-persistent netfilter-persistent
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save

echo "=== 3. Adding 2GB Swap space (for heavy OCR stability) ==="
if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "=== 4. Installing Docker, Docker Compose, Nginx, and Certbot ==="
sudo apt-get install -y docker.io docker-compose-v2 git nginx certbot python3-certbot-nginx
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu || true

echo "=== 5. Setting up Application Directory ==="
if [ ! -d "$APP_DIR" ]; then
    git clone https://github.com/dyseyha68-maker/khmerpdf.git "$APP_DIR"
else
    cd "$APP_DIR" && (git pull origin master 2>/dev/null || git pull origin main 2>/dev/null || true)
fi

cd "$APP_DIR"

# Generate .env if not exists
if [ ! -f .env ]; then
    echo "Creating .env configuration..."
    SECRET_KEY=$(openssl rand -hex 32)
    PUBLIC_IP=$(curl -s ifconfig.me || echo "127.0.0.1")
    cat <<EOF > .env
SECRET_KEY=$SECRET_KEY
DEBUG=False
ALLOWED_HOSTS=$DOMAIN,$WWW_DOMAIN,$PUBLIC_IP,localhost,127.0.0.1
DATA_DIR=/data
EOF
    echo ".env created with random SECRET_KEY and public IP: $PUBLIC_IP"
fi

echo "=== 6. Configuring Nginx Reverse Proxy ==="
sudo tee /etc/nginx/sites-available/khmerpdf > /dev/null <<EOF
server {
    listen 80;
    server_name $DOMAIN $WWW_DOMAIN;

    client_max_body_size 400M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 1200;
        proxy_connect_timeout 1200;
        proxy_send_timeout 1200;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/khmerpdf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

echo "=== 7. Building and Starting Docker Containers ==="
sudo docker compose up -d --build

echo "=============================================================================="
echo " Application is now running!"
echo " Check containers with: sudo docker compose ps"
echo " View logs with:        sudo docker compose logs -f"
echo ""
echo " To enable free SSL (HTTPS) after pointing your domain DNS, run:"
echo "   sudo certbot --nginx -d $DOMAIN -d $WWW_DOMAIN"
echo "=============================================================================="
