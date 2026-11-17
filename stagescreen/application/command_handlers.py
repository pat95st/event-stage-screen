"""Command handlers (write side)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from ..domain.model import Ipv4Settings, OutputConfig, validate_hostname, validate_ntp_servers
from ..i18n import LANGUAGES
from ..infrastructure import system
from ..infrastructure.auth import hash_password, verify_password
from ..infrastructure.backup import store_pending_restore
from . import commands as c
from .bus import CommandBus, DomainError

if TYPE_CHECKING:
    from ..container import Services

log = logging.getLogger("ess.commands")


def register_command_handlers(bus: CommandBus, s: Services) -> None:
    # ------------------------------------------------------------ users / UI

    def create_admin(cmd: c.CreateAdmin) -> None:
        if s.user_repo.count() > 0:
            raise DomainError("An administrator already exists", 409)
        _validate_credentials(cmd.username, cmd.password)
        s.user_repo.save(cmd.username.strip(), hash_password(cmd.password))
        log.info('Administrator "%s" created', cmd.username.strip())

    def change_password(cmd: c.ChangePassword) -> None:
        if not verify_password(cmd.current_password, s.user_repo.get_hash(cmd.username)):
            raise DomainError("The current password is wrong", 403)
        _validate_credentials(cmd.username, cmd.new_password)
        s.user_repo.save(cmd.username, hash_password(cmd.new_password))
        log.info('Password of "%s" changed', cmd.username)

    def set_language(cmd: c.SetLanguage) -> str:
        if cmd.language not in LANGUAGES:
            raise DomainError("Unknown language")
        s.settings.set("ui.language", cmd.language)
        log.info("User interface language: %s", cmd.language)
        return cmd.language

    # ------------------------------------------------------------ outputs

    def save_output(cmd: c.SaveOutput) -> dict:
        cfg = OutputConfig.from_input(int(cmd.output_id), cmd.data)
        s.output_repo.save(cfg)
        log.info("Output %d saved: %s, mode %s, rotation %d°%s", cfg.id, cfg.connector,
                 cfg.mode.label if cfg.mode else "auto", cfg.rotation,
                 ", mirrors output 1" if cfg.mirror_of else "")
        s.notify_change("outputs")
        return cfg.to_dict()

    # ------------------------------------------------------------ system

    async def set_hostname(cmd: c.SetHostname) -> str:
        name = validate_hostname(cmd.hostname)
        await s.privileged.run("set-hostname", name)
        log.info("Host name set to %s", name)
        return name

    async def set_timezone(cmd: c.SetTimezone) -> str:
        zones = await asyncio.to_thread(system.list_timezones)
        if cmd.timezone not in zones:
            raise DomainError("Unknown time zone")
        await s.privileged.run("set-timezone", cmd.timezone)
        log.info("Time zone set to %s", cmd.timezone)
        return cmd.timezone

    async def set_ntp_servers(cmd: c.SetNtpServers) -> list[str]:
        servers = validate_ntp_servers(cmd.servers)
        await s.privileged.run("set-ntp", *servers)
        log.info("NTP servers: %s", ", ".join(servers) or "system default")
        return servers

    def set_ndi_limit(cmd: c.SetNdiLimit) -> int:
        if cmd.limit is None:
            s.settings.delete("ndi.max_sources")
        else:
            if not 0 <= int(cmd.limit) <= 8:
                raise DomainError("The NDI source limit must be between 0 and 8")
            s.settings.set("ndi.max_sources", int(cmd.limit))
        return s.ndi_limit()

    async def reboot(_: c.Reboot) -> None:
        log.warning("Reboot requested from the web interface")
        await s.privileged.run("reboot")

    async def shutdown(_: c.Shutdown) -> None:
        log.warning("Shutdown requested from the web interface")
        await s.privileged.run("poweroff")

    def restore_backup(cmd: c.RestoreBackup) -> dict:
        manifest = store_pending_restore(s.config, cmd.data)
        s.request_restart()
        return {"created_at": manifest.get("created_at"), "app_version": manifest.get("app_version")}

    # ------------------------------------------------------------ network

    async def apply_ipv4(cmd: c.ApplyIpv4) -> dict:
        return await s.network.apply_ipv4(cmd.device, Ipv4Settings.from_input(cmd.data))

    async def connect_wifi(cmd: c.ConnectWifi) -> dict:
        return await s.network.connect_wifi(cmd.device, cmd.ssid.strip(), cmd.password)

    async def confirm_network(_: c.ConfirmNetworkChange) -> None:
        await s.network.confirm()

    async def rollback_network(_: c.RollbackNetworkChange) -> None:
        await s.network.rollback()

    for message, handler in [
        (c.CreateAdmin, create_admin), (c.ChangePassword, change_password), (c.SetLanguage, set_language),
        (c.SaveOutput, save_output),
        (c.SetHostname, set_hostname), (c.SetTimezone, set_timezone), (c.SetNtpServers, set_ntp_servers),
        (c.SetNdiLimit, set_ndi_limit), (c.Reboot, reboot), (c.Shutdown, shutdown), (c.RestoreBackup, restore_backup),
        (c.ApplyIpv4, apply_ipv4), (c.ConnectWifi, connect_wifi),
        (c.ConfirmNetworkChange, confirm_network), (c.RollbackNetworkChange, rollback_network),
    ]:
        bus.register(message, handler)


def _validate_credentials(username: str, password: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", (username or "").strip()):
        raise DomainError("User name: 3–40 characters (letters, digits, . _ -)")
    if len(password or "") < 8:
        raise DomainError("The password must be at least 8 characters long")
