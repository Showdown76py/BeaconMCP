from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class PVENode:
    name: str
    host: str
    token_id: str
    token_secret: str


@dataclass
class ILOConfig:
    host: str
    user: str
    password: str
    jump_host: str  # Proxmox node name used as SSH tunnel


@dataclass
class SSHConfig:
    user: str
    password: str


@dataclass
class Config:
    pve_nodes: list[PVENode]
    ilo: ILOConfig | None
    ssh: SSHConfig | None
    verify_ssl: bool
    infrastructure: dict

    @classmethod
    def from_env(cls) -> Config:
        # PVE1 is required
        pve1_host = os.environ.get("PVE1_HOST", "")
        pve1_token_id = os.environ.get("PVE1_TOKEN_ID", "")
        pve1_token_secret = os.environ.get("PVE1_TOKEN_SECRET", "")

        if not pve1_host or not pve1_token_id or not pve1_token_secret:
            print(
                "ERROR: PVE1_HOST, PVE1_TOKEN_ID, and PVE1_TOKEN_SECRET are required.",
                file=sys.stderr,
            )
            sys.exit(1)

        nodes = [PVENode("pve1", pve1_host, pve1_token_id, pve1_token_secret)]

        # PVE2 is optional
        pve2_host = os.environ.get("PVE2_HOST", "")
        pve2_token_id = os.environ.get("PVE2_TOKEN_ID", "")
        pve2_token_secret = os.environ.get("PVE2_TOKEN_SECRET", "")
        if pve2_host and pve2_token_id and pve2_token_secret:
            nodes.append(PVENode("pve2", pve2_host, pve2_token_id, pve2_token_secret))

        # iLO is optional
        ilo = None
        ilo_host = os.environ.get("ILO_HOST", "")
        ilo_user = os.environ.get("ILO_USER", "")
        ilo_password = os.environ.get("ILO_PASSWORD", "")
        ilo_jump = os.environ.get("ILO_JUMP_HOST", "pve1")
        if ilo_host and ilo_user and ilo_password:
            ilo = ILOConfig(ilo_host, ilo_user, ilo_password, ilo_jump)

        # SSH is optional
        ssh = None
        ssh_user = os.environ.get("SSH_USER", "")
        ssh_password = os.environ.get("SSH_PASSWORD", "")
        if ssh_user and ssh_password:
            ssh = SSHConfig(ssh_user, ssh_password)

        verify_ssl = os.getenv("PVE_VERIFY_SSL", "false").lower() == "true"

        # Load infrastructure context
        infra_path = Path(os.getenv("INFRA_YAML_PATH", "infrastructure.yaml"))
        infrastructure = {}
        if infra_path.exists():
            with open(infra_path) as f:
                infrastructure = yaml.safe_load(f) or {}

        return cls(
            pve_nodes=nodes,
            ilo=ilo,
            ssh=ssh,
            verify_ssl=verify_ssl,
            infrastructure=infrastructure,
        )

    def get_node(self, name: str) -> PVENode | None:
        for node in self.pve_nodes:
            if node.name == name:
                return node
        return None

    def get_node_host(self, name: str) -> str | None:
        node = self.get_node(name)
        return node.host if node else None
