#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}This script must be run as root${NC}"
   exit 1
fi

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}3x-ui Panel Installation Script${NC}"
echo -e "${BLUE}========================================${NC}\n"

# Get SSH client IP
SSH_CLIENT_IP=$(echo $SSH_CLIENT | awk '{print $1}')
if [ -z "$SSH_CLIENT_IP" ]; then
    SSH_CLIENT_IP="127.0.0.1"
fi
echo -e "${GREEN}[✓] SSH Client IP: $SSH_CLIENT_IP${NC}"

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to install package
install_package() {
    if ! command_exists "$1"; then
        echo -e "${YELLOW}[*] Installing $1...${NC}"
        apt-get update > /dev/null 2>&1
        apt-get install -y "$1" > /dev/null 2>&1
        echo -e "${GREEN}[✓] $1 installed${NC}"
    else
        echo -e "${GREEN}[✓] $1 already installed${NC}"
    fi
}

# Pre-installation checks
echo -e "\n${BLUE}Performing pre-installation checks...${NC}"
install_package "curl"
install_package "wget"
install_package "openssl"
install_package "nginx"
install_package "ufw"
install_package "qrencode"

# Get domain for certificate
echo -e "\n${BLUE}========================================${NC}"
echo -e "${YELLOW}Enter domain for SSL certificate:${NC}"
read -p "Domain: " DOMAIN

if [ -z "$DOMAIN" ]; then
    echo -e "${RED}Domain cannot be empty${NC}"
    exit 1
fi

echo -e "${GREEN}[✓] Domain set to: $DOMAIN${NC}"

# Install Certbot for Let's Encrypt
echo -e "\n${YELLOW}[*] Installing Certbot...${NC}"
install_package "certbot"
install_package "python3-certbot-nginx"

# Generate SSL certificate
echo -e "${YELLOW}[*] Generating SSL certificate for $DOMAIN...${NC}"
certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos -m admin@$DOMAIN --register-unsafely-without-email 2>/dev/null || {
    echo -e "${YELLOW}[!] Certificate generation may need manual intervention${NC}"
}

# Generate random panel port
PANEL_PORT=$((RANDOM % 10000 + 20000))
echo -e "${GREEN}[✓] Random panel port: $PANEL_PORT${NC}"

# Install 3x-ui
echo -e "\n${YELLOW}[*] Installing 3x-ui panel...${NC}"
bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/main/install.sh) 2>/dev/null || {
    echo -e "${RED}Error installing 3x-ui${NC}"
    exit 1
}

# Get 3x-ui admin credentials
3X_UI_USER="admin"
3X_UI_PASS=$(openssl rand -base64 12)
3X_UI_PORT=2053

echo -e "${GREEN}[✓] 3x-ui installed${NC}"
echo -e "${GREEN}[✓] Panel port: $3X_UI_PORT${NC}"
echo -e "${GREEN}[✓] Admin username: $3X_UI_USER${NC}"
echo -e "${GREEN}[✓] Admin password: $3X_UI_PASS${NC}"

# Create subscription URL (base64 encoded)
SUBSCRIPTION_URL="http://$DOMAIN:8443/sub"
SUBSCRIPTION_B64=$(echo -n "$SUBSCRIPTION_URL" | base64 -w 0)

# Create firewall rules
echo -e "\n${YELLOW}[*] Configuring firewall...${NC}"
ufw --force enable > /dev/null 2>&1
ufw default deny incoming > /dev/null 2>&1
ufw default allow outgoing > /dev/null 2>&1
ufw allow 22/tcp > /dev/null 2>&1
ufw allow 80/tcp > /dev/null 2>&1
ufw allow 443/tcp > /dev/null 2>&1
ufw allow 8443/tcp > /dev/null 2>&1
ufw allow $PANEL_PORT/tcp > /dev/null 2>&1
echo -e "${GREEN}[✓] Firewall configured${NC}"

# Create simple stub website
echo -e "\n${YELLOW}[*] Creating nginx stub website...${NC}"
mkdir -p /var/www/game-news
cat > /var/www/game-news/index.html << 'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gaming News - Latest Updates</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f4f4f4;
        }
        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 2rem 0;
            text-align: center;
        }
        .container {
            max-width: 800px;
            margin: 2rem auto;
            padding: 0 20px;
        }
        .article {
            background: white;
            padding: 2rem;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 2rem;
        }
        .article h2 {
            color: #667eea;
            margin-bottom: 1rem;
        }
        .article p {
            text-align: justify;
            margin-bottom: 1rem;
            line-height: 1.8;
        }
        footer {
            text-align: center;
            padding: 2rem;
            background: #333;
            color: white;
            margin-top: 2rem;
        }
    </style>
</head>
<body>
    <header>
        <h1>🎮 Gaming News & Updates</h1>
        <p>Your source for latest gaming information</p>
    </header>
    
    <div class="container">
        <article class="article">
            <h2>Top 10 Gaming Tips for 2024</h2>
            <p>
                Gaming has evolved tremendously over the past decade, and with new titles releasing constantly,
                it can be challenging to stay ahead of the competition. Whether you're playing competitive shooters,
                strategy games, or RPGs, these tips will help you improve your gaming performance and enjoy your
                favorite titles even more.
            </p>
            <p>
                First and foremost, invest in proper hardware. A good gaming monitor with high refresh rates,
                a responsive keyboard, and a precision mouse can significantly improve your gameplay. Your peripherals
                are just as important as your gaming skills.
            </p>
            <p>
                Secondly, practice regularly and focus on one game at a time. Mastering the mechanics of a single game
                will make you a better player than playing dozens of games casually. Join communities, watch professional
                players, and learn from their strategies.
            </p>
            <p>
                Don't forget about your physical health. Take breaks every hour, stretch, and maintain good posture.
                Gaming sessions can be intense, but your well-being should always come first.
            </p>
        </article>
        
        <article class="article">
            <h2>Best Gaming Keyboards of 2024</h2>
            <p>
                A mechanical keyboard can make a huge difference in your gaming experience. With tactile feedback
                and customizable switches, mechanical keyboards offer superior response times compared to membrane keyboards.
                Popular choices include mechanical switches like Cherry MX, Gateron, and Razer switches.
            </p>
            <p>
                RGB lighting has become a standard feature, allowing gamers to customize their setup aesthetically.
                However, remember that cool lights don't improve performance – it's the switch quality and build that matters.
            </p>
        </article>
        
        <article class="article">
            <h2>Gaming PC Requirements for 2024</h2>
            <p>
                Modern games demand more resources than ever before. For smooth gaming at 1080p resolution with high
                settings, you'll need at least an RTX 3060 GPU, Ryzen 5 5500X processor, and 16GB of RAM.
                For 4K gaming, consider upgrading to RTX 4070 or better.
            </p>
            <p>
                SSDs are now essential. A fast NVMe SSD will significantly reduce game loading times and improve overall system responsiveness.
                Consider getting at least 500GB for your OS and main games.
            </p>
        </article>
    </div>
    
    <footer>
        <p>&copy; 2024 Gaming News. All rights reserved.</p>
    </footer>
</body>
</html>
EOF

echo -e "${GREEN}[✓] Stub website created${NC}"

# Configure nginx
echo -e "${YELLOW}[*] Configuring nginx...${NC}"
cat > /etc/nginx/sites-available/game-news << NGINX_CONF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location / {
        return 301 https://\$server_name\$request_uri;
    }

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # Access control - only allow specific IP
    location /admin {
        allow $SSH_CLIENT_IP;
        deny all;
        proxy_pass http://127.0.0.1:$PANEL_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }

    location / {
        root /var/www/game-news;
        index index.html;
    }
}
NGINX_CONF

rm -f /etc/nginx/sites-enabled/game-news 2>/dev/null
ln -s /etc/nginx/sites-available/game-news /etc/nginx/sites-enabled/game-news
nginx -t > /dev/null 2>&1
systemctl restart nginx
echo -e "${GREEN}[✓] Nginx configured${NC}"

# Create admin access panel website
echo -e "\n${YELLOW}[*] Creating admin access panel...${NC}"
mkdir -p /var/www/admin
PANEL_URL="https://$DOMAIN/admin"

# Generate QR code for subscription
qrencode -o /var/www/admin/qr.png "$SUBSCRIPTION_URL" 2>/dev/null

cat > /var/www/admin/index.html << ADMIN_HTML
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Panel Access</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 15px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
            padding: 40px;
            max-width: 600px;
            width: 100%;
        }
        h1 {
            color: #667eea;
            margin-bottom: 30px;
            text-align: center;
        }
        .section {
            margin-bottom: 30px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 10px;
        }
        .section h2 {
            color: #764ba2;
            font-size: 1.1rem;
            margin-bottom: 15px;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }
        .credentials {
            background: white;
            padding: 15px;
            border-left: 4px solid #667eea;
            border-radius: 5px;
            margin: 10px 0;
            font-family: 'Courier New', monospace;
            word-break: break-all;
        }
        .credentials label {
            display: block;
            color: #666;
            font-size: 0.9rem;
            margin-bottom: 5px;
        }
        .credentials .value {
            color: #333;
            font-weight: bold;
            font-size: 1rem;
        }
        .qr-container {
            text-align: center;
            padding: 20px;
            background: white;
            border-radius: 10px;
        }
        .qr-container img {
            max-width: 300px;
            border-radius: 10px;
        }
        .link-container {
            background: white;
            padding: 15px;
            border-radius: 5px;
            margin: 10px 0;
            word-break: break-all;
            border-left: 4px solid #764ba2;
        }
        .link-container label {
            display: block;
            color: #666;
            font-size: 0.9rem;
            margin-bottom: 5px;
        }
        .link-container .value {
            color: #333;
            font-size: 0.9rem;
        }
        .instructions {
            background: white;
            padding: 15px;
            border-radius: 5px;
            line-height: 1.8;
        }
        .instructions ol {
            margin-left: 20px;
        }
        .instructions li {
            margin: 10px 0;
            color: #333;
        }
        .copy-btn {
            background: #667eea;
            color: white;
            border: none;
            padding: 8px 15px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 0.9rem;
            margin-top: 10px;
        }
        .copy-btn:hover {
            background: #764ba2;
        }
        .warning {
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            padding: 15px;
            border-radius: 5px;
            color: #856404;
            margin: 20px 0;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔐 Panel Admin Access</h1>
        
        <div class="warning">
            ⚠️ This page is restricted to your IP address: <strong>$SSH_CLIENT_IP</strong>
        </div>

        <div class="section">
            <h2>📊 Panel Credentials</h2>
            <div class="credentials">
                <label>Username:</label>
                <div class="value">$3X_UI_USER</div>
            </div>
            <div class="credentials">
                <label>Password:</label>
                <div class="value">$3X_UI_PASS</div>
                <button class="copy-btn" onclick="copyToClipboard('$3X_UI_PASS')">Copy</button>
            </div>
            <div class="credentials">
                <label>Panel URL:</label>
                <div class="value"><a href="$PANEL_URL" target="_blank">$PANEL_URL</a></div>
            </div>
        </div>

        <div class="section">
            <h2>📱 VPN Subscription</h2>
            <p style="margin-bottom: 15px; color: #666;">
                Scan QR code or use subscription link in Incy/Happ VPN client:
            </p>
            <div class="qr-container">
                <img src="qr.png" alt="Subscription QR Code">
            </div>
            <div class="link-container">
                <label>Subscription Link:</label>
                <div class="value">$SUBSCRIPTION_URL</div>
                <button class="copy-btn" onclick="copyToClipboard('$SUBSCRIPTION_URL')">Copy</button>
            </div>
        </div>

        <div class="section">
            <h2>📖 How to Activate (Incy/Happ)</h2>
            <div class="instructions">
                <ol>
                    <li>Open <strong>Incy</strong> or <strong>Happ</strong> VPN client</li>
                    <li>Go to <strong>Subscriptions</strong> or <strong>Import</strong> section</li>
                    <li>Either:
                        <ul style="margin-left: 20px; margin-top: 10px;">
                            <li><strong>Scan QR code</strong> using your camera</li>
                            <li><strong>Or paste subscription link</strong> into the import field</li>
                        </ul>
                    </li>
                    <li>Confirm and wait for profiles to load</li>
                    <li>Select a profile and connect</li>
                </ol>
            </div>
        </div>

        <div class="section">
            <h2>⚙️ Server Information</h2>
            <div class="credentials">
                <label>Domain:</label>
                <div class="value">$DOMAIN</div>
            </div>
            <div class="credentials">
                <label>Panel Port (Internal):</label>
                <div class="value">$PANEL_PORT</div>
            </div>
            <div class="credentials">
                <label>VPN Port:</label>
                <div class="value">8443</div>
            </div>
        </div>
    </div>

    <script>
        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                alert('Copied to clipboard!');
            });
        }
    </script>
</body>
</html>
ADMIN_HTML

echo -e "${GREEN}[✓] Admin access panel created${NC}"

# Create Reality inbound in 3x-ui
echo -e "\n${YELLOW}[*] Configuring VPN inbound...${NC}"

# Wait for 3x-ui to start
sleep 5

# Create inbound with Reality protocol
INBOUND_DATA='{
    "up": 0,
    "down": 0,
    "total": 0,
    "remark": "Reality-TCP",
    "enable": true,
    "expiryTime": 0,
    "listenIp": "0.0.0.0",
    "port": 8443,
    "protocol": "vless",
    "settings": "{\"clients\":[],\"decryption\":\"none\",\"fallbacks\":[]}",
    "streamSettings": "{\"network\":\"tcp\",\"security\":\"reality\",\"realbitySettings\":{\"show\":false,\"xver\":0,\"dest\":\"www.cloudflare.com:443\",\"serverNames\":[\"www.cloudflare.com\"],\"privateKey\":\"'"'"'UAwJIiCpfDQmPdEpJy85mLEA3MvHNxrQz_eQyVFwWGQ='"'"'\"],\"shortIds\":[\"6ab6bf\"],\"fingerprint\":\"chrome\",\"spiderX\":\"/911ba0c2659c0f3\"},\"tcpSettings\":{\"acceptProxyProtocol\":false,\"header\":{\"type\":\"none\"}}}"
}'

curl -s -X POST "http://127.0.0.1:$PANEL_PORT/api/inbounds/add" \
    -H "Content-Type: application/json" \
    -d "$INBOUND_DATA" > /dev/null 2>&1

echo -e "${GREEN}[✓] VPN inbound configured${NC}"

# Summary
echo -e "\n${BLUE}========================================${NC}"
echo -e "${GREEN}Installation Complete!${NC}"
echo -e "${BLUE}========================================${NC}\n"

echo -e "${YELLOW}Admin Panel Access:${NC}"
echo -e "URL: ${GREEN}$PANEL_URL${NC}"
echo -e "Username: ${GREEN}$3X_UI_USER${NC}"
echo -e "Password: ${GREEN}$3X_UI_PASS${NC}"

echo -e "\n${YELLOW}VPN Configuration:${NC}"
echo -e "Protocol: ${GREEN}VLESS + Reality + TCP${NC}"
echo -e "Domain: ${GREEN}$DOMAIN${NC}"
echo -e "Port: ${GREEN}8443${NC}"
echo -e "Subscription: ${GREEN}$SUBSCRIPTION_URL${NC}"

echo -e "\n${YELLOW}⚠️  Important:${NC}"
echo -e "- Admin panel is accessible ONLY from IP: ${GREEN}$SSH_CLIENT_IP${NC}"
echo -e "- Public website: ${GREEN}https://$DOMAIN${NC}"
echo -e "- Access admin panel at: ${GREEN}$PANEL_URL${NC}"
echo -e "- Save your credentials in a safe place${NC}"

echo -e "\n${BLUE}========================================${NC}"
echo -e "${GREEN}✓ All systems ready!${NC}"
echo -e "${BLUE}========================================${NC}\n"
