#!/bin/bash
# TarkaMCP - Installation rapide sur un noeud Proxmox
# Usage: bash deploy/install.sh

set -e

INSTALL_DIR="/opt/tarkamcp"
REPO="https://github.com/Showdown76py/TarkaMCP.git"
VENV_DIR="$INSTALL_DIR/.venv"

echo "=== TarkaMCP - Installation ==="

# 1. Dépendances système
if ! command -v python3 >/dev/null 2>&1; then
    echo "[*] Installation de python3..."
    apt-get install -y python3 python3-venv python3-pip git
fi

if ! python3 -c "import venv" 2>/dev/null; then
    echo "[*] Installation de python3-venv..."
    apt-get install -y python3-venv
fi

# 2. Cloner ou mettre à jour
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[*] Mise à jour de TarkaMCP..."
    cd "$INSTALL_DIR" && git pull
else
    echo "[*] Clonage de TarkaMCP..."
    git clone "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 3. Environnement virtuel Python
if [ ! -d "$VENV_DIR" ]; then
    echo "[*] Création du virtual env Python..."
    python3 -m venv "$VENV_DIR"
fi

# 4. Installer les dépendances dans le venv
echo "[*] Installation des dépendances Python..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -e . --quiet

# 5. Fichier .env
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "[*] Création du fichier .env..."
    cp .env.example .env
    echo "  .env créé. Remplis-le avec tes credentials Proxmox."
else
    echo "[*] .env existant conservé."
fi

# 6. Wrapper tarkamcp dans /usr/local/bin
echo "[*] Installation du wrapper 'tarkamcp' dans /usr/local/bin..."
cat > /usr/local/bin/tarkamcp <<EOF
#!/bin/bash
exec $VENV_DIR/bin/python -m tarkamcp "\$@"
EOF
chmod +x /usr/local/bin/tarkamcp

# 7. Service systemd
echo "[*] Installation du service systemd..."
cp deploy/tarkamcp.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable tarkamcp

echo ""
echo "=== Installation terminée ==="
echo ""
echo "Prochaines étapes :"
echo "  1. Éditer /opt/tarkamcp/.env avec tes credentials Proxmox"
echo "  2. Créer un client : tarkamcp auth create --name 'Claude Web'"
echo "  3. Démarrer le service : systemctl start tarkamcp"
echo "  4. Vérifier : curl http://localhost:8420/health"
echo "  5. Configurer ton tunnel Cloudflare vers localhost:8420"
echo ""
