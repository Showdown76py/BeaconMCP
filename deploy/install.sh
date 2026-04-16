#!/bin/bash
# TarkaMCP - Installation rapide sur un noeud Proxmox
# Usage: curl -sSL ... | bash  OU  bash deploy/install.sh

set -e

INSTALL_DIR="/opt/tarkamcp"
REPO="https://github.com/Showdown76py/TarkaMCP.git"

echo "=== TarkaMCP - Installation ==="

# 1. Cloner ou mettre à jour
if [ -d "$INSTALL_DIR" ]; then
    echo "[*] Mise à jour de TarkaMCP..."
    cd "$INSTALL_DIR" && git pull
else
    echo "[*] Clonage de TarkaMCP..."
    git clone "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 2. Installer les dépendances
echo "[*] Installation des dépendances Python..."
pip3 install -e . --quiet

# 3. Fichier .env
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "[*] Création du fichier .env..."
    cp .env.example .env

    echo ""
    echo "  .env créé. Remplis-le avec tes credentials Proxmox."
    echo ""
    echo "[!] Édite /opt/tarkamcp/.env pour ajouter tes credentials Proxmox."
else
    echo "[*] .env existant conservé."
fi

# 4. Service systemd
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
