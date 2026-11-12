"""Form-elicitation confirmation for destructive tools."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ElicitationCapability, FormElicitationCapability, UrlElicitationCapability

from beaconmcp.confirm import (
    DEFAULT_CONFIRM_TOOLS,
    ConfirmationDeclined,
    ConfirmationUnavailable,
    confirm_destructive,
)


class Ctx:
    def __init__(self, elicitation, *, action="accept", confirm=True):
        caps = SimpleNamespace(elicitation=elicitation)
        self.request_context = SimpleNamespace(
            session=SimpleNamespace(client_params=SimpleNamespace(capabilities=caps)),
        )
        self.messages: list[str] = []
        self._action, self._confirm = action, confirm

    async def elicit(self, message, schema):
        self.messages.append(message)
        data = schema(confirm=self._confirm) if self._action == "accept" else None
        return SimpleNamespace(action=self._action, data=data)


FORM = ElicitationCapability(form=FormElicitationCapability())
URL_ONLY = ElicitationCapability(url=UrlElicitationCapability())


async def test_unlisted_tool_untouched() -> None:
    ctx = Ctx(FORM)
    assert await confirm_destructive(ctx, "proxmox_vm_start", {"vmid": 1}) == {"vmid": 1}
    assert ctx.messages == []


async def test_accept_runs() -> None:
    ctx = Ctx(FORM)
    kw = {"device_id": "srv-02", "force": False}
    assert await confirm_destructive(ctx, "bmc_power_off", kw) == kw
    assert ctx.messages == ["Power off srv-02 via its BMC?"]


async def test_decline_raises() -> None:
    with pytest.raises(ConfirmationDeclined, match="Cancelled"):
        await confirm_destructive(Ctx(FORM, action="decline"), "bmc_power_reset", {})


async def test_cancel_or_unchecked_raises() -> None:
    with pytest.raises(ToolError):
        await confirm_destructive(Ctx(FORM, action="cancel"), "bmc_power_reset", {})
    with pytest.raises(ToolError):
        await confirm_destructive(Ctx(FORM, confirm=False), "bmc_power_reset", {})


async def test_no_form_capability_runs_unconfirmed() -> None:
    for ctx in (Ctx(None), Ctx(URL_ONLY)):
        assert await confirm_destructive(ctx, "bmc_power_off", {}) == {}
        assert ctx.messages == []


async def test_legacy_bare_elicitation_is_form() -> None:
    ctx = Ctx(ElicitationCapability())
    await confirm_destructive(ctx, "bmc_power_reset", {"device_id": "x"})
    assert ctx.messages


async def test_dry_run_skips() -> None:
    ctx = Ctx(FORM)
    await confirm_destructive(ctx, "proxmox_snapshot_rollback", {"dry_run": True})
    assert ctx.messages == []


async def test_backup_restore_only_when_force() -> None:
    ctx = Ctx(FORM)
    await confirm_destructive(ctx, "proxmox_backup_restore", {"force": False})
    assert ctx.messages == []
    await confirm_destructive(ctx, "proxmox_backup_restore",
                              {"force": True, "archive": "a.zst", "vmid": 9, "node": "pve"})
    assert ctx.messages == ["Restore a.zst over VM 9 on pve? This overwrites it."]


async def test_self_update_confirm_injected() -> None:
    got = await confirm_destructive(Ctx(FORM), "beaconmcp_self_update", {"restart": True})
    assert got == {"restart": True, "confirm": True}


async def test_config_override_list() -> None:
    ctx = Ctx(FORM)
    await confirm_destructive(ctx, "bmc_power_off", {}, tools=["proxmox_vm_stop"])
    assert ctx.messages == []
    await confirm_destructive(ctx, "proxmox_vm_stop", {}, tools=["proxmox_vm_stop"])
    assert ctx.messages == ["Run proxmox_vm_stop?"]


async def test_no_ctx_noop() -> None:
    assert await confirm_destructive(None, "bmc_power_off", {"a": 1}) == {"a": 1}


def test_default_list() -> None:
    assert "bmc_power_off" in DEFAULT_CONFIRM_TOOLS and "proxmox_vm_stop" not in DEFAULT_CONFIRM_TOOLS



async def test_client_capabilities_property_preferred() -> None:
    class Session:
        client_capabilities = SimpleNamespace(elicitation=FORM)

    ctx = Ctx(None)
    ctx.request_context = SimpleNamespace(session=Session())
    await confirm_destructive(ctx, "bmc_power_reset", {"device_id": "x"})
    assert ctx.messages == ["Hard reset x via its BMC?"]


async def test_elicitation_failure_gives_clear_error() -> None:
    class Broken(Ctx):
        async def elicit(self, message, schema):
            raise TimeoutError("no answer")

    with pytest.raises(ConfirmationUnavailable, match="could not show it .*Nothing was run"):
        await confirm_destructive(Broken(FORM), "bmc_power_off", {})


def test_declined_and_unavailable_are_distinct_tool_errors() -> None:
    assert issubclass(ConfirmationDeclined, ToolError)
    assert issubclass(ConfirmationUnavailable, ToolError)
    assert not issubclass(ConfirmationUnavailable, ConfirmationDeclined)


# --- server-side gates on host-reaching config and strict mode ----------------

from beaconmcp.confirm import _needs_confirmation  # noqa: E402

_T = DEFAULT_CONFIRM_TOOLS


def _need(tool: str, **kw) -> bool:
    return _needs_confirmation(tool, kw, _T)


def test_default_list_has_vm_create_and_config() -> None:
    assert "proxmox_vm_create" in _T and "proxmox_vm_config" in _T


def test_vm_create_plain_is_not_gated() -> None:
    cfg = {"cores": 2, "memory": 512, "net0": "name=eth0,bridge=vmbr0",
           "rootfs": "local-lvm:8", "ostemplate": "local:vztmpl/debian.tar.zst"}
    assert not _need("proxmox_vm_create", config=cfg)
    assert not _need("proxmox_vm_create", config=None)
    assert not _need("proxmox_vm_create", config={})


@pytest.mark.parametrize("cfg", [
    {"ostemplate": "local:backup/x.tar.zst", "restore": 1, "force": 1},
    {"restore": True},
    {"force": "1"},
    {"archive": "local:backup/vzdump-qemu-1.vma.zst", "force": 1},
    {"archive": "local:backup/x"},
    {"hookscript": "local:snippets/h.pl"},
    {"args": "-chardev x"},
    {"mp0": "/etc,mp=/mnt"},
    {"mp12": "local-lvm:vm-105-disk-0,mp=/m"},
    {"dev0": "/dev/sda"},
    {"hostpci0": "0000:01:00"},
    {"usb1": "host=1-1"},
    {"lxc.mount.entry": "/ x none bind"},
    {"lxc.cgroup2.devices.allow": "a"},
    {"unprivileged": 0},
    {"unprivileged": False},
    {"unprivileged": "0"},
    {"cores": 1, "HookScript": "x"},
])
def test_sensitive_config_is_gated(cfg: dict) -> None:
    assert _need("proxmox_vm_create", config=cfg)
    assert _need("proxmox_vm_config", updates=cfg)


@pytest.mark.parametrize("cfg", [
    {"unprivileged": 1}, {"unprivileged": True}, {"restore": 0}, {"force": False},
    {"hookscript": ""}, {"description": "x"}, {"memory": 1024, "cores": 2, "onboot": 1},
    {"mpx": "a"}, {"net0": "x"},
])
def test_harmless_config_is_not_gated(cfg: dict) -> None:
    assert not _need("proxmox_vm_config", updates=cfg)


def test_vm_config_read_shape_not_gated() -> None:
    assert not _need("proxmox_vm_config")
    assert not _need("proxmox_vm_config", updates=None)
    assert not _need("proxmox_vm_config", updates={"description": "x"})
    assert _need("proxmox_vm_config", updates={"mp0": "/,mp=/h"})


@pytest.mark.parametrize("tool", ["ssh_run", "proxmox_run"])
def test_exec_polling_exempt(tool: str) -> None:
    tools = [*_T, tool]
    assert not _needs_confirmation(tool, {"exec_id": "abc"}, tools)
    assert not _needs_confirmation(tool, {"exec_id": "abc", "command": ""}, tools)
    assert _needs_confirmation(tool, {"command": "id"}, tools)
    assert _needs_confirmation(tool, {"exec_id": "abc", "command": "id"}, tools)


def test_backup_restore_without_force_not_gated() -> None:
    assert not _need("proxmox_backup_restore")
    assert not _need("proxmox_backup_restore", force=False)
    assert _need("proxmox_backup_restore", force=True)


def test_dry_run_and_unlisted() -> None:
    assert not _need("proxmox_vm_create", config={"hookscript": "x"}, dry_run=True)
    assert not _needs_confirmation("proxmox_vm_create", {"config": {"hookscript": "x"}}, [])


async def test_strict_refuses_without_elicitation() -> None:
    for ctx in (Ctx(None), Ctx(URL_ONLY)):
        with pytest.raises(ConfirmationUnavailable, match="confirm_strict"):
            await confirm_destructive(ctx, "bmc_power_off", {}, strict=True)
        assert ctx.messages == []


async def test_strict_refuses_without_request_context() -> None:
    ctx = SimpleNamespace()  # no request_context attribute
    with pytest.raises(ConfirmationUnavailable, match="confirm_strict"):
        await confirm_destructive(ctx, "bmc_power_off", {}, strict=True)
    with pytest.raises(ConfirmationUnavailable, match="confirm_strict"):
        await confirm_destructive(None, "bmc_power_off", {}, strict=True)


async def test_strict_off_keeps_running_unconfirmed() -> None:
    assert await confirm_destructive(Ctx(None), "bmc_power_off", {"a": 1}, strict=False) == {"a": 1}


async def test_strict_ignores_calls_that_need_no_confirmation() -> None:
    kw = {"config": {"cores": 1}}
    assert await confirm_destructive(Ctx(None), "proxmox_vm_create", kw, strict=True) == kw
    assert await confirm_destructive(Ctx(None), "proxmox_vm_start", {}, strict=True) == {}


async def test_strict_with_form_client_still_asks() -> None:
    ctx = Ctx(FORM)
    await confirm_destructive(ctx, "bmc_power_off", {}, strict=True)
    assert ctx.messages


@pytest.mark.parametrize("cfg", [
    {"mp1": "local:102/vm-102-disk-9.raw,mp=/mnt/autre"},
    {"scsi1": "local-lvm:vm-201-disk-0"},
    {"virtio0": "/dev/sdb"},
    {"sata0": "local-lvm:base-100-disk-0,size=8G"},
    {"scsi0": "local-lvm:0,import-from=local:100/vm-100-disk-0.raw"},
    {"scsi0": "local-lvm:32,import-from=/tmp/x.qcow2"},
    {"rootfs": "local:101/vm-101-disk-0.raw"},
    {"rootfs": "local-lvm:vm-101-disk-0,size=8G"},
    {"ide2": "local:iso/debian.iso"},  # no media=cdrom
    {"ide2": "/dev/sr0,media=cdrom"},
    {"unused0": "local-lvm:vm-9-disk-1"},
])
def test_existing_volume_is_gated(cfg: dict) -> None:
    assert _need("proxmox_vm_config", updates=cfg)
    assert _need("proxmox_vm_create", config=cfg)


@pytest.mark.parametrize("cfg", [
    {"mp2": "local:1,mp=/x"},
    {"mp2": "local:0.5,mp=/x"},
    {"scsi0": "local-lvm:32"},
    {"ide2": "local:iso/debian.iso,media=cdrom"},
    {"ide2": "cdrom,media=cdrom"},
    {"ide2": "none,media=cdrom"},
    {"scsi3": "none"},
    {"rootfs": "local-lvm:8"},
    {"description": "only text"},
])
def test_new_or_empty_volume_is_not_gated(cfg: dict) -> None:
    assert not _need("proxmox_vm_config", updates=cfg)
    assert not _need("proxmox_vm_create", config=cfg)


@pytest.mark.parametrize("cfg", [
    {"features": "nesting=1,keyctl=1"},
    {"features": "fuse=1"},
    {"features": "mount=nfs;cifs"},
    {"features": "mknod=1,nesting=1"},
    {"features": "force_rw_sys=1"},
    {"cicustom": "user=local:snippets/user.yaml"},
])
def test_host_reaching_features_and_cicustom_are_gated(cfg: dict) -> None:
    assert _need("proxmox_vm_config", updates=cfg)
    assert _need("proxmox_vm_create", config=cfg)


@pytest.mark.parametrize("cfg", [
    {"features": "nesting=1"},
    {"features": "nesting=1,keyctl=0"},
    {"features": ""},
    {"cicustom": ""},
])
def test_nesting_only_and_empty_values_are_not_gated(cfg: dict) -> None:
    assert not _need("proxmox_vm_config", updates=cfg)
    assert not _need("proxmox_vm_create", config=cfg)


@pytest.mark.parametrize("cfg", [
    {"serial0": "/dev/ttyS0"},
    {"serial3": "/dev/ttyUSB0"},
    {"parallel0": "/dev/parport0"},
    {"parallel2": "/dev/usb/lp0"},
    {"virtiofs0": "shared,cache=always"},
    {"virtiofs9": "dirid=shared"},
])
def test_serial_parallel_virtiofs_passthrough_is_gated(cfg: dict) -> None:
    assert _need("proxmox_vm_config", updates=cfg)
    assert _need("proxmox_vm_create", config=cfg)


def test_serial_socket_is_not_gated() -> None:
    # serial0: socket is what most cloud images use for the console
    assert not _need("proxmox_vm_create", config={"serial0": "socket"})
    assert not _need("proxmox_vm_config", updates={"serial1": "Socket"})


@pytest.mark.parametrize("value", [
    "local:iso/../../102/vm-102-disk-0.raw,media=cdrom",
    "local:iso/sub/debian.iso,media=cdrom",
    "local:iso/debian.qcow2,media=cdrom",
    "local:iso/,media=cdrom",
])
def test_iso_cdrom_must_be_a_plain_iso_or_img(value: str) -> None:
    assert _need("proxmox_vm_config", updates={"ide2": value})


@pytest.mark.parametrize("value", [
    "local:iso/debian-13.iso,media=cdrom",
    "local:iso/Windows.ISO,media=cdrom",
    "nas-iso:iso/virtio-win.img,media=cdrom",
])
def test_plain_iso_or_img_cdrom_is_not_gated(value: str) -> None:
    assert not _need("proxmox_vm_config", updates={"ide2": value})


async def test_strict_with_form_client_elicits_instead_of_refusing() -> None:
    ctx = Ctx(FORM)
    assert await confirm_destructive(ctx, "bmc_power_off", {"a": 1}, strict=True) == {"a": 1}
    assert len(ctx.messages) == 1


def test_active_tools_default_and_override() -> None:
    from beaconmcp import confirm

    try:
        assert confirm.active_tools() == frozenset(DEFAULT_CONFIRM_TOOLS)
        assert confirm.set_active_tools(["ssh_run"]) == frozenset({"ssh_run"})
        assert confirm.active_tools() == frozenset({"ssh_run"})
        assert confirm.set_active_tools(None) == frozenset(DEFAULT_CONFIRM_TOOLS)
    finally:
        confirm.set_active_tools(None)


def test_requires_confirmation_public_rule() -> None:
    from beaconmcp import confirm

    assert confirm.requires_confirmation("bmc_power_off", {})
    assert not confirm.requires_confirmation("bmc_power_off", {"dry_run": True})
    assert not confirm.requires_confirmation("proxmox_vm_start", {"vmid": 1})
    assert confirm.requires_confirmation("proxmox_vm_start", {"vmid": 1}, tools={"proxmox_vm_start"})
