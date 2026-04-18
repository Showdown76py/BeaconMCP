"""Interactive TUI config wizard for BeaconMCP (`beaconmcp init`).

Three-pane layout: section menu on the left, section-specific form in the
middle, live ``beaconmcp.yaml`` preview on the right. The draft stays in
memory until the user saves — at which point the YAML gets written to
disk and any referenced ``${VAR}`` placeholders are appended to ``.env``
with empty values for the user to fill in.

The wizard is intentionally a **bootstrap** tool, not a full config
editor. It covers the capabilities (Proxmox, SSH, BMC), the critical
server fields (allowed_hosts / allowed_origins), and nothing else —
tweaks to dashboard settings or obscure fields happen by editing the
resulting YAML directly. Keeping the scope small means the preview pane
stays honest: what you see is the whole file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        Static,
        Switch,
        TextArea,
    )
except ImportError as _exc:  # pragma: no cover - import guard
    _WIZARD_IMPORT_ERROR = _exc

    # Stubs so the class definitions below can be loaded without textual
    # installed. Actual use is gated by `_WIZARD_IMPORT_ERROR` in
    # `run_wizard`, which prints an install hint and exits.
    class _Stub:
        def __init__(self, *args: Any, **kwargs: Any) -> None: ...
        def __class_getitem__(cls, item: Any) -> type:  # noqa: D401
            return cls

    App = ComposeResult = Binding = _Stub  # type: ignore[assignment,misc]
    Horizontal = Vertical = VerticalScroll = _Stub  # type: ignore[assignment,misc]
    ModalScreen = _Stub  # type: ignore[assignment,misc]
    Button = DataTable = Footer = Header = Input = Label = _Stub  # type: ignore[assignment,misc]
    ListItem = ListView = Static = Switch = TextArea = _Stub  # type: ignore[assignment,misc]
else:
    _WIZARD_IMPORT_ERROR = None


# ---------------------------------------------------------------------------
# Draft data model (lenient mirror of beaconmcp.config dataclasses)
# ---------------------------------------------------------------------------


@dataclass
class PVENodeDraft:
    name: str = ""
    host: str = ""
    token_id: str = ""
    token_secret_env: str = ""  # env var name, rendered as ${NAME}


@dataclass
class SSHDefaultsDraft:
    user: str = "root"
    port: int = 22
    # Exactly one of these two should be set in a valid draft. Wizard
    # enforces it via the form, but the model accepts both empty so the
    # user can start typing.
    password_env: str = ""
    key_file: str = ""


@dataclass
class SSHHostDraft:
    name: str = ""
    host: str = ""
    user: str = "root"
    port: int = 22
    password_env: str = ""
    key_file: str = ""


@dataclass
class SSHDraft:
    enabled: bool = True
    vmid_to_ip: str = ""
    inherit_proxmox_nodes: bool = True
    defaults: SSHDefaultsDraft = field(default_factory=SSHDefaultsDraft)
    hosts: list[SSHHostDraft] = field(default_factory=list)


@dataclass
class BMCDeviceDraft:
    id: str = ""
    type: str = "hp_ilo"
    host: str = ""
    user: str = ""
    password_env: str = ""
    jump_host: str = ""  # references ssh.hosts[].name


@dataclass
class ServerDraft:
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*"])
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "https://claude.ai",
            "https://chatgpt.com",
            "https://chat.mistral.ai",
            "https://gemini.google.com",
        ]
    )


@dataclass
class ConfigDraft:
    server: ServerDraft = field(default_factory=ServerDraft)
    pve_nodes: list[PVENodeDraft] = field(default_factory=list)
    ssh: SSHDraft = field(default_factory=SSHDraft)
    bmc_devices: list[BMCDeviceDraft] = field(default_factory=list)

    def referenced_env_vars(self) -> list[str]:
        """Collect every ``${VAR}`` name the draft references.

        Used when saving to append placeholders to ``.env`` so the user has
        one file to fill in after the wizard exits.
        """
        names: list[str] = []
        for n in self.pve_nodes:
            if n.token_secret_env:
                names.append(n.token_secret_env)
        if self.ssh.enabled:
            if self.ssh.defaults.password_env:
                names.append(self.ssh.defaults.password_env)
            for h in self.ssh.hosts:
                if h.password_env:
                    names.append(h.password_env)
        for d in self.bmc_devices:
            if d.password_env:
                names.append(d.password_env)
        # Dedupe while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out


# ---------------------------------------------------------------------------
# YAML rendering — hand-rolled so we control comments and quoting precisely.
# ---------------------------------------------------------------------------


def _q(value: str) -> str:
    """Quote a YAML scalar when it contains reserved characters."""
    if not value:
        return '""'
    if any(ch in value for ch in "!@:#&*`{}[]|>?,%"):
        return f'"{value}"'
    if value.lower() in {"true", "false", "yes", "no", "on", "off", "null", "~"}:
        return f'"{value}"'
    return value


def render_yaml(draft: ConfigDraft) -> str:
    """Render the draft as a ``beaconmcp.yaml`` string."""
    lines: list[str] = []
    lines.append("# Generated by `beaconmcp init`. Edit freely once saved.")
    lines.append("version: 1")
    lines.append("")

    # Server
    lines.append("server:")
    lines.append("  host: 0.0.0.0")
    lines.append("  port: 8420")
    if draft.server.allowed_hosts:
        lines.append("  allowed_hosts:")
        for h in draft.server.allowed_hosts:
            lines.append(f"    - {_q(h)}")
    if draft.server.allowed_origins:
        lines.append("  allowed_origins:")
        for o in draft.server.allowed_origins:
            lines.append(f"    - {o}")
    lines.append("")

    # Proxmox
    if draft.pve_nodes:
        lines.append("proxmox:")
        lines.append("  verify_ssl: false")
        lines.append("  nodes:")
        for n in draft.pve_nodes:
            lines.append(f"    - name: {_q(n.name)}")
            lines.append(f"      host: {_q(n.host)}")
            lines.append(f"      token_id: {_q(n.token_id)}")
            secret = f"${{{n.token_secret_env}}}" if n.token_secret_env else '""'
            lines.append(f"      token_secret: {secret}")
        lines.append("")

    # SSH
    if draft.ssh.enabled and (
        draft.ssh.hosts
        or draft.ssh.inherit_proxmox_nodes
        or draft.ssh.vmid_to_ip
    ):
        lines.append("ssh:")
        if draft.ssh.vmid_to_ip:
            lines.append(f"  vmid_to_ip: {_q(draft.ssh.vmid_to_ip)}")
        d = draft.ssh.defaults
        if draft.ssh.inherit_proxmox_nodes or d.password_env or d.key_file:
            lines.append("  defaults:")
            lines.append(f"    user: {_q(d.user)}")
            if d.port and d.port != 22:
                lines.append(f"    port: {d.port}")
            if d.key_file:
                lines.append(f"    key_file: {_q(d.key_file)}")
            elif d.password_env:
                lines.append(f"    password: ${{{d.password_env}}}")
        if draft.ssh.inherit_proxmox_nodes:
            lines.append("  inherit_proxmox_nodes: true")
        if draft.ssh.hosts:
            lines.append("  hosts:")
            for h in draft.ssh.hosts:
                lines.append(f"    - name: {_q(h.name)}")
                lines.append(f"      host: {_q(h.host)}")
                lines.append(f"      user: {_q(h.user)}")
                if h.port and h.port != 22:
                    lines.append(f"      port: {h.port}")
                if h.key_file:
                    lines.append(f"      key_file: {_q(h.key_file)}")
                elif h.password_env:
                    lines.append(f"      password: ${{{h.password_env}}}")
        lines.append("")

    # BMC
    if draft.bmc_devices:
        lines.append("bmc:")
        lines.append("  devices:")
        for b in draft.bmc_devices:
            lines.append(f"    - id: {_q(b.id)}")
            lines.append(f"      type: {b.type}")
            lines.append(f"      host: {_q(b.host)}")
            lines.append(f"      user: {_q(b.user)}")
            secret = f"${{{b.password_env}}}" if b.password_env else '""'
            lines.append(f"      password: {secret}")
            if b.jump_host:
                lines.append(f"      jump_host: {_q(b.jump_host)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Textual app
# ---------------------------------------------------------------------------

SECTIONS = [
    ("proxmox", "Proxmox nodes"),
    ("ssh", "SSH"),
    ("bmc", "BMC devices"),
    ("server", "Server"),
    ("save", "Save & exit"),
]


CSS = """
Screen {
    layout: vertical;
}

#body {
    layout: horizontal;
    height: 1fr;
}

#sidebar {
    width: 24;
    border-right: solid $primary-background;
    padding: 1;
}

#sidebar ListView {
    background: $surface;
    height: auto;
}

#main {
    width: 1fr;
    padding: 1 2;
}

#preview {
    width: 55;
    padding: 1;
    border-left: solid $primary-background;
}

#preview-title {
    color: $text-muted;
    text-style: bold;
    margin-bottom: 1;
}

#preview-area {
    background: $surface;
    border: solid $primary-background;
    height: 1fr;
}

.section-heading {
    text-style: bold;
    color: $accent;
    margin-bottom: 1;
}

.hint {
    color: $text-muted;
    margin-bottom: 1;
}

DataTable {
    height: auto;
    max-height: 12;
    margin: 1 0;
}

.form-row {
    layout: horizontal;
    height: auto;
    margin: 0 0 1 0;
}

.form-row Label {
    width: 16;
    padding: 1 1 0 0;
}

.form-row Input {
    width: 1fr;
}

.form-actions {
    layout: horizontal;
    height: auto;
    margin-top: 1;
}

.form-actions Button {
    margin-right: 1;
}

Switch {
    margin-right: 1;
}
"""


# ---------------------------------------------------------------------------
# Modals — forms for add/edit flows
# ---------------------------------------------------------------------------


class _FormModal(ModalScreen[dict[str, str] | None]):
    """Generic modal with a list of (label, key, initial) fields.

    Returns a dict of entered values on Save, or None on Cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(
        self,
        title: str,
        fields: list[tuple[str, str, str]],
        hint: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._fields = fields
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(self._title, classes="section-heading")
            if self._hint:
                yield Static(self._hint, classes="hint")
            for label, key, initial in self._fields:
                with Horizontal(classes="form-row"):
                    yield Label(label + ":")
                    yield Input(value=initial, id=f"f-{key}")
            with Horizontal(classes="form-actions"):
                yield Button("Save", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        first = self.query(Input).first()
        if first is not None:
            first.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "ok":
            self.action_save()

    def action_save(self) -> None:
        out: dict[str, str] = {}
        for _label, key, _initial in self._fields:
            out[key] = self.query_one(f"#f-{key}", Input).value.strip()
        self.dismiss(out)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Section panels — each renders into the centre pane
# ---------------------------------------------------------------------------


class _ProxmoxPanel(Static):
    def __init__(self, draft: ConfigDraft, on_change: Callable[[], None]) -> None:
        super().__init__()
        self.draft = draft
        self.on_change = on_change

    def compose(self) -> ComposeResult:
        yield Static("Proxmox nodes", classes="section-heading")
        yield Static(
            "One entry per Proxmox node. Use LAN IPs in `host:` — same "
            "address will be reused for SSH inheritance.",
            classes="hint",
        )
        yield DataTable(id="pve-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="form-actions"):
            yield Button("Add", id="pve-add", variant="primary")
            yield Button("Edit", id="pve-edit")
            yield Button("Delete", id="pve-delete", variant="error")

    def on_mount(self) -> None:
        table = self.query_one("#pve-table", DataTable)
        table.add_columns("name", "host", "token_id", "secret env")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#pve-table", DataTable)
        table.clear()
        for n in self.draft.pve_nodes:
            table.add_row(
                n.name or "—",
                n.host or "—",
                n.token_id or "—",
                f"${{{n.token_secret_env}}}" if n.token_secret_env else "—",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pve-add":
            self._open_form(None)
        elif event.button.id == "pve-edit":
            idx = self._selected_row()
            if idx is not None:
                self._open_form(idx)
        elif event.button.id == "pve-delete":
            idx = self._selected_row()
            if idx is not None:
                del self.draft.pve_nodes[idx]
                self._refresh_table()
                self.on_change()

    def _selected_row(self) -> int | None:
        table = self.query_one("#pve-table", DataTable)
        if table.cursor_row is None or not self.draft.pve_nodes:
            return None
        idx = table.cursor_row
        if 0 <= idx < len(self.draft.pve_nodes):
            return idx
        return None

    def _open_form(self, idx: int | None) -> None:
        existing = self.draft.pve_nodes[idx] if idx is not None else PVENodeDraft()
        default_env = existing.token_secret_env or (
            f"PVE{len(self.draft.pve_nodes) + 1}_TOKEN_SECRET" if idx is None else ""
        )
        modal = _FormModal(
            title="Proxmox node" if idx is None else f"Edit {existing.name or 'node'}",
            hint=(
                "host: LAN IP of the node (e.g. 10.0.0.1). token_id: the "
                "Proxmox API token ID in user@realm!tokenname shape. The "
                "secret itself lives in .env — type the env-var name here."
            ),
            fields=[
                ("name", "name", existing.name),
                ("host", "host", existing.host),
                ("token id", "token_id", existing.token_id or "root@pam!beaconmcp"),
                ("secret env", "token_secret_env", default_env),
            ],
        )

        def after(result: dict[str, str] | None) -> None:
            if result is None:
                return
            entry = PVENodeDraft(
                name=result["name"],
                host=result["host"],
                token_id=result["token_id"],
                token_secret_env=result["token_secret_env"],
            )
            if idx is None:
                self.draft.pve_nodes.append(entry)
            else:
                self.draft.pve_nodes[idx] = entry
            self._refresh_table()
            self.on_change()

        self.app.push_screen(modal, after)


class _SSHPanel(Static):
    def __init__(self, draft: ConfigDraft, on_change: Callable[[], None]) -> None:
        super().__init__()
        self.draft = draft
        self.on_change = on_change

    def compose(self) -> ComposeResult:
        yield Static("SSH capability", classes="section-heading")
        yield Static(
            "Flip inheritance on to reach every Proxmox node via SSH using "
            "the `defaults` creds — no per-node duplication.",
            classes="hint",
        )

        ssh = self.draft.ssh
        with Horizontal(classes="form-row"):
            yield Label("Enable SSH:")
            yield Switch(value=ssh.enabled, id="ssh-enabled")
        with Horizontal(classes="form-row"):
            yield Label("vmid_to_ip:")
            yield Input(
                value=ssh.vmid_to_ip,
                placeholder="e.g. 192.168.1.{id} (leave empty to disable)",
                id="ssh-vmid",
            )
        with Horizontal(classes="form-row"):
            yield Label("Inherit PVE nodes:")
            yield Switch(value=ssh.inherit_proxmox_nodes, id="ssh-inherit")

        yield Static("Default credentials", classes="section-heading")
        yield Static(
            "Used for inherited Proxmox entries. Provide exactly one of "
            "key_file OR password (env var name).",
            classes="hint",
        )
        with Horizontal(classes="form-row"):
            yield Label("Default user:")
            yield Input(value=ssh.defaults.user, id="ssh-def-user")
        with Horizontal(classes="form-row"):
            yield Label("Key file:")
            yield Input(
                value=ssh.defaults.key_file,
                placeholder="~/.ssh/beaconmcp",
                id="ssh-def-key",
            )
        with Horizontal(classes="form-row"):
            yield Label("Password env:")
            yield Input(
                value=ssh.defaults.password_env,
                placeholder="(only if no key_file)",
                id="ssh-def-pw",
            )

        yield Static("Explicit hosts", classes="section-heading")
        yield Static(
            "Targets outside your Proxmox cluster (VPS, bastion, remote "
            "node with its own creds). Names may match a Proxmox node — "
            "the explicit entry shadows inheritance.",
            classes="hint",
        )
        yield DataTable(id="ssh-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="form-actions"):
            yield Button("Add host", id="ssh-add", variant="primary")
            yield Button("Edit", id="ssh-edit")
            yield Button("Delete", id="ssh-delete", variant="error")

    def on_mount(self) -> None:
        table = self.query_one("#ssh-table", DataTable)
        table.add_columns("name", "host", "user", "auth")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#ssh-table", DataTable)
        table.clear()
        for h in self.draft.ssh.hosts:
            auth = h.key_file or (f"${{{h.password_env}}}" if h.password_env else "—")
            table.add_row(h.name or "—", h.host or "—", h.user or "—", auth)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "ssh-enabled":
            self.draft.ssh.enabled = event.value
        elif event.switch.id == "ssh-inherit":
            self.draft.ssh.inherit_proxmox_nodes = event.value
        self.on_change()

    def on_input_changed(self, event: Input.Changed) -> None:
        ssh = self.draft.ssh
        if event.input.id == "ssh-vmid":
            ssh.vmid_to_ip = event.value.strip()
        elif event.input.id == "ssh-def-user":
            ssh.defaults.user = event.value.strip() or "root"
        elif event.input.id == "ssh-def-key":
            ssh.defaults.key_file = event.value.strip()
            if ssh.defaults.key_file:
                ssh.defaults.password_env = ""
        elif event.input.id == "ssh-def-pw":
            ssh.defaults.password_env = event.value.strip()
            if ssh.defaults.password_env:
                ssh.defaults.key_file = ""
        self.on_change()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ssh-add":
            self._open_form(None)
        elif event.button.id == "ssh-edit":
            idx = self._selected_row()
            if idx is not None:
                self._open_form(idx)
        elif event.button.id == "ssh-delete":
            idx = self._selected_row()
            if idx is not None:
                del self.draft.ssh.hosts[idx]
                self._refresh_table()
                self.on_change()

    def _selected_row(self) -> int | None:
        table = self.query_one("#ssh-table", DataTable)
        if table.cursor_row is None or not self.draft.ssh.hosts:
            return None
        idx = table.cursor_row
        if 0 <= idx < len(self.draft.ssh.hosts):
            return idx
        return None

    def _open_form(self, idx: int | None) -> None:
        existing = self.draft.ssh.hosts[idx] if idx is not None else SSHHostDraft()
        modal = _FormModal(
            title="SSH host" if idx is None else f"Edit {existing.name or 'host'}",
            hint=(
                "key_file or password env — one of the two, not both. "
                "Leave port empty for 22."
            ),
            fields=[
                ("name", "name", existing.name),
                ("host", "host", existing.host),
                ("user", "user", existing.user),
                ("port", "port", str(existing.port) if existing.port and existing.port != 22 else ""),
                ("key_file", "key_file", existing.key_file),
                ("password env", "password_env", existing.password_env),
            ],
        )

        def after(result: dict[str, str] | None) -> None:
            if result is None:
                return
            port = int(result["port"]) if result["port"].isdigit() else 22
            key = result["key_file"]
            pw = result["password_env"]
            # Enforce mutual exclusion
            if key and pw:
                pw = ""
            entry = SSHHostDraft(
                name=result["name"],
                host=result["host"],
                user=result["user"] or "root",
                port=port,
                key_file=key,
                password_env=pw,
            )
            if idx is None:
                self.draft.ssh.hosts.append(entry)
            else:
                self.draft.ssh.hosts[idx] = entry
            self._refresh_table()
            self.on_change()

        self.app.push_screen(modal, after)


class _BMCPanel(Static):
    def __init__(self, draft: ConfigDraft, on_change: Callable[[], None]) -> None:
        super().__init__()
        self.draft = draft
        self.on_change = on_change

    def compose(self) -> ComposeResult:
        yield Static("BMC devices", classes="section-heading")
        yield Static(
            "HP iLO, IPMI, iDRAC or Supermicro. `jump_host` (optional) "
            "references an ssh.hosts[] entry by name.",
            classes="hint",
        )
        yield DataTable(id="bmc-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="form-actions"):
            yield Button("Add", id="bmc-add", variant="primary")
            yield Button("Edit", id="bmc-edit")
            yield Button("Delete", id="bmc-delete", variant="error")

    def on_mount(self) -> None:
        table = self.query_one("#bmc-table", DataTable)
        table.add_columns("id", "type", "host", "jump_host")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#bmc-table", DataTable)
        table.clear()
        for d in self.draft.bmc_devices:
            table.add_row(d.id or "—", d.type, d.host or "—", d.jump_host or "—")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bmc-add":
            self._open_form(None)
        elif event.button.id == "bmc-edit":
            idx = self._selected_row()
            if idx is not None:
                self._open_form(idx)
        elif event.button.id == "bmc-delete":
            idx = self._selected_row()
            if idx is not None:
                del self.draft.bmc_devices[idx]
                self._refresh_table()
                self.on_change()

    def _selected_row(self) -> int | None:
        table = self.query_one("#bmc-table", DataTable)
        if table.cursor_row is None or not self.draft.bmc_devices:
            return None
        idx = table.cursor_row
        if 0 <= idx < len(self.draft.bmc_devices):
            return idx
        return None

    def _open_form(self, idx: int | None) -> None:
        existing = self.draft.bmc_devices[idx] if idx is not None else BMCDeviceDraft()
        default_env = existing.password_env or (
            f"BMC{len(self.draft.bmc_devices) + 1}_PASSWORD" if idx is None else ""
        )
        modal = _FormModal(
            title="BMC device" if idx is None else f"Edit {existing.id or 'device'}",
            hint=(
                "type: hp_ilo | ipmi | idrac | supermicro. jump_host is the "
                "name of an ssh.hosts[] entry used to tunnel into a private "
                "management VLAN (leave empty for direct access)."
            ),
            fields=[
                ("id", "id", existing.id),
                ("type", "type", existing.type),
                ("host", "host", existing.host),
                ("user", "user", existing.user or "Administrator"),
                ("password env", "password_env", default_env),
                ("jump_host", "jump_host", existing.jump_host),
            ],
        )

        def after(result: dict[str, str] | None) -> None:
            if result is None:
                return
            entry = BMCDeviceDraft(
                id=result["id"],
                type=result["type"] or "hp_ilo",
                host=result["host"],
                user=result["user"],
                password_env=result["password_env"],
                jump_host=result["jump_host"],
            )
            if idx is None:
                self.draft.bmc_devices.append(entry)
            else:
                self.draft.bmc_devices[idx] = entry
            self._refresh_table()
            self.on_change()

        self.app.push_screen(modal, after)


class _ServerPanel(Static):
    def __init__(self, draft: ConfigDraft, on_change: Callable[[], None]) -> None:
        super().__init__()
        self.draft = draft
        self.on_change = on_change

    def compose(self) -> ComposeResult:
        yield Static("Server", classes="section-heading")
        yield Static(
            "DNS-rebinding allowlist + CORS origins. One entry per line.",
            classes="hint",
        )
        yield Static("allowed_hosts")
        yield TextArea(
            "\n".join(self.draft.server.allowed_hosts),
            id="srv-hosts",
            show_line_numbers=False,
        )
        yield Static("allowed_origins")
        yield TextArea(
            "\n".join(self.draft.server.allowed_origins),
            id="srv-origins",
            show_line_numbers=False,
        )

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        lines = [line.strip() for line in event.text_area.text.splitlines() if line.strip()]
        if event.text_area.id == "srv-hosts":
            self.draft.server.allowed_hosts = lines
        elif event.text_area.id == "srv-origins":
            self.draft.server.allowed_origins = lines
        self.on_change()


class _SavePanel(Static):
    def __init__(
        self,
        draft: ConfigDraft,
        yaml_path: Path,
        env_path: Path,
        on_save: Callable[[Path, Path], None],
    ) -> None:
        super().__init__()
        self.draft = draft
        self.yaml_path = yaml_path
        self.env_path = env_path
        self.on_save = on_save

    def compose(self) -> ComposeResult:
        yield Static("Save & exit", classes="section-heading")
        yield Static(
            f"YAML will be written to: {self.yaml_path}\n"
            f".env will be extended at: {self.env_path}",
            classes="hint",
        )
        yield Static("Referenced env vars (need values in .env):", classes="section-heading")
        refs = self.draft.referenced_env_vars()
        yield Static("\n".join(f"  - {n}" for n in refs) if refs else "(none)")
        with Horizontal(classes="form-actions"):
            yield Button("Save config", id="save", variant="primary")
            yield Button("Cancel", id="cancel")
        yield Static("", id="save-status", classes="hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.app.exit()
        elif event.button.id == "save":
            try:
                self.on_save(self.yaml_path, self.env_path)
                self.query_one("#save-status", Static).update(
                    f"Saved. Edit {self.env_path} to fill in the secrets, then "
                    "run `beaconmcp validate-config`."
                )
            except Exception as exc:  # noqa: BLE001
                self.query_one("#save-status", Static).update(
                    f"[red]Save failed: {exc}[/red]"
                )


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


class ConfigWizardApp(App[None]):
    CSS = CSS
    TITLE = "BeaconMCP — config wizard"
    SUB_TITLE = "beaconmcp init"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+s", "quick_save", "Save"),
    ]

    def __init__(self, yaml_path: Path, env_path: Path) -> None:
        super().__init__()
        self.draft = ConfigDraft()
        self.yaml_path = yaml_path
        self.env_path = env_path
        self._current_section = "proxmox"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("Sections", classes="section-heading")
                yield ListView(
                    *[ListItem(Label(label), id=f"sect-{key}") for key, label in SECTIONS],
                    id="sections",
                )
                yield Static("", classes="hint")
                yield Static(
                    "Tip: arrow keys to move, enter to open a section, "
                    "tab to jump between panes.",
                    classes="hint",
                )
            with VerticalScroll(id="main"):
                yield Static("Select a section on the left.", id="main-content")
            with Vertical(id="preview"):
                yield Static("beaconmcp.yaml (live preview)", id="preview-title")
                yield TextArea("", id="preview-area", read_only=True, show_line_numbers=False)
        yield Footer()

    def on_mount(self) -> None:
        lv = self.query_one("#sections", ListView)
        lv.focus()
        self._show_section("proxmox")
        self._refresh_preview()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if not item_id.startswith("sect-"):
            return
        self._show_section(item_id[len("sect-"):])

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # Highlight on arrow keys also swaps the panel, so the user doesn't
        # have to press enter to preview each section.
        if event.item is None:
            return
        item_id = event.item.id or ""
        if item_id.startswith("sect-"):
            self._show_section(item_id[len("sect-"):])

    def _show_section(self, key: str) -> None:
        self._current_section = key
        container = self.query_one("#main", VerticalScroll)
        container.remove_children()
        panel: Static
        if key == "proxmox":
            panel = _ProxmoxPanel(self.draft, self._refresh_preview)
        elif key == "ssh":
            panel = _SSHPanel(self.draft, self._refresh_preview)
        elif key == "bmc":
            panel = _BMCPanel(self.draft, self._refresh_preview)
        elif key == "server":
            panel = _ServerPanel(self.draft, self._refresh_preview)
        elif key == "save":
            panel = _SavePanel(
                self.draft, self.yaml_path, self.env_path, self._write_files
            )
        else:
            panel = Static("Unknown section.")
        container.mount(panel)

    def _refresh_preview(self) -> None:
        self.query_one("#preview-area", TextArea).text = render_yaml(self.draft)

    def action_quick_save(self) -> None:
        # Triggered by Ctrl+S anywhere in the app. Doesn't exit — user can
        # keep editing. Status gets reflected on the save panel if open.
        try:
            self._write_files(self.yaml_path, self.env_path)
        except Exception:  # noqa: BLE001
            pass

    def _write_files(self, yaml_path: Path, env_path: Path) -> None:
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(render_yaml(self.draft), encoding="utf-8")
        _merge_env_placeholders(env_path, self.draft.referenced_env_vars())


def _merge_env_placeholders(env_path: Path, names: list[str]) -> None:
    """Ensure every referenced env var has a line in ``.env``.

    Existing values are preserved. Missing names get an empty placeholder
    with a comment noting the wizard added them. Passing an empty list is
    a no-op.
    """
    if not names:
        return
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                if key:
                    existing[key] = True
    to_add = [n for n in names if n not in existing]
    if not to_add:
        return
    with env_path.open("a", encoding="utf-8") as f:
        if env_path.stat().st_size and not env_path.read_text(encoding="utf-8").endswith("\n"):
            f.write("\n")
        f.write("\n# Added by `beaconmcp init` — fill these in.\n")
        for name in to_add:
            f.write(f"{name}=\n")


# ---------------------------------------------------------------------------
# Entry point used by the CLI
# ---------------------------------------------------------------------------


def run_wizard(yaml_path: Path | None = None, env_path: Path | None = None) -> int:
    """Launch the wizard. Returns a process exit code."""
    if _WIZARD_IMPORT_ERROR is not None:
        print(
            "The interactive wizard needs the optional 'textual' dependency.\n"
            "Install it with:\n"
            "  pip install 'beaconmcp[wizard]'\n"
            f"Import failed with: {_WIZARD_IMPORT_ERROR}",
        )
        return 1

    yaml_path = yaml_path or Path(os.environ.get("BEACONMCP_CONFIG", "beaconmcp.yaml"))
    env_path = env_path or Path(".env")
    ConfigWizardApp(yaml_path=yaml_path, env_path=env_path).run()
    return 0
