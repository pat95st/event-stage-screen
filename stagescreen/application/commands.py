"""Commands (write side). Each command has exactly one handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .bus import Command

# ------------------------------------------------------------------ users / UI

@dataclass
class CreateAdmin(Command):
    username: str
    password: str


@dataclass
class ChangePassword(Command):
    username: str
    current_password: str
    new_password: str


@dataclass
class SetLanguage(Command):
    language: str


# ------------------------------------------------------------------ outputs

@dataclass
class SaveOutput(Command):
    output_id: int
    data: dict[str, Any]


# ------------------------------------------------------------------ system

@dataclass
class SetHostname(Command):
    hostname: str


@dataclass
class SetTimezone(Command):
    timezone: str


@dataclass
class SetNtpServers(Command):
    servers: list[str] = field(default_factory=list)


@dataclass
class SetNdiLimit(Command):
    limit: int | None


@dataclass
class Reboot(Command):
    pass


@dataclass
class Shutdown(Command):
    pass


@dataclass
class RestoreBackup(Command):
    data: bytes


# ------------------------------------------------------------------ network

@dataclass
class ApplyIpv4(Command):
    device: str
    data: dict[str, Any]


@dataclass
class ConnectWifi(Command):
    device: str
    ssid: str
    password: str


@dataclass
class ConfirmNetworkChange(Command):
    pass


@dataclass
class RollbackNetworkChange(Command):
    pass
