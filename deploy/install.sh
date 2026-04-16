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

    # Générer un secret URL automatiquement
    SECRET=$(openssl rand -hex 32)
    echo "" >> .env
    echo "TARKAMCP_SECRET=$SECRET" >> .env

    echo ""
    echo "============================================="
    echo "  Secret URL généré."
    echo ""
    echo "  Ton endpoint MCP sera :"
    echo "  https://<ton-domaine>/s/$SECRET/mcp"
    echo ""
    echo "  Traite cette URL comme un mot de passe."
    echo "============================================="
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
echo "  1. Éditer /opt/tarkamcp/.env avec tes credentials"
echo "  2. Démarrer le service : systemctl start tarkamcp"
echo "  3. Vérifier : curl http://localhost:8420/health"
echo "  4. Configurer ton tunnel Cloudflare vers localhost:8420"
echo ""
