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


# ------------------------------------------------------------------ scenes

@dataclass
class CreateScene(Command):
    output_id: int
    name: str
    width: int | None = None
    height: int | None = None


@dataclass
class SaveSceneDraft(Command):
    scene_id: str
    content: dict[str, Any]


@dataclass
class PublishScene(Command):
    """Make the draft (or the given content, for live editing) the live version."""

    scene_id: str
    content: dict[str, Any] | None = None


@dataclass
class DiscardSceneDraft(Command):
    scene_id: str


@dataclass
class RenameScene(Command):
    scene_id: str
    name: str


@dataclass
class DuplicateScene(Command):
    scene_id: str
    output_id: int | None = None


@dataclass
class DeleteScene(Command):
    scene_id: str


@dataclass
class ReorderScenes(Command):
    output_id: int
    scene_ids: list[str]


@dataclass
class ActivateScene(Command):
    output_id: int
    scene_id: str | None


@dataclass
class ImportScene(Command):
    output_id: int
    data: dict[str, Any]


# ------------------------------------------------------------------ media

@dataclass
class UploadMedia(Command):
    data: bytes
    name: str
    family: str | None = None


@dataclass
class RenameMedia(Command):
    media_id: str
    name: str
    family: str | None = None


@dataclass
class DeleteMedia(Command):
    media_id: str


@dataclass
class SaveSplash(Command):
    mode: str
    logo_media_id: str | None = None


# ------------------------------------------------------------------ kiosk

@dataclass
class ReportKioskState(Command):
    report: dict[str, Any]


# ------------------------------------------------------------------ eventrentmanager

@dataclass
class SaveErmSettings(Command):
    base_url: str
    verify_tls: bool = True


@dataclass
class SetErmToken(Command):
    """Token and source of an output. ``token=None`` keeps the stored token, "" removes it."""

    output_id: int
    token: str | None = None
    source: str = "own"


@dataclass
class TestErmConnection(Command):
    base_url: str
    verify_tls: bool = True
    token: str | None = None
    output_id: int | None = None


# ------------------------------------------------------------------ NDI

@dataclass
class SetNdiDiscoveryServers(Command):
    servers: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ setup aids

@dataclass
class SetBlackout(Command):
    """``output_id=None`` → all outputs; ``on=None`` → toggle."""

    output_id: int | None = None
    on: bool | None = None


@dataclass
class SetTestPattern(Command):
    output_id: int | None = None
    pattern: str | None = None  # bars | grid | edges | None (off)


@dataclass
class IdentifyOutputs(Command):
    output_id: int | None = None
    seconds: float = 6.0


@dataclass
class StoreThumbnail(Command):
    output_id: int
    data: bytes


# ------------------------------------------------------------------ automation

@dataclass
class SaveAutomation(Command):
    output_id: int
    enabled: bool
    rules: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ResumeAutomation(Command):
    output_id: int


# ------------------------------------------------------------------ remote control (REST API v1)

@dataclass
class CreateApiKey(Command):
    name: str


@dataclass
class DeleteApiKey(Command):
    key_id: str


@dataclass
class ActivateSceneRef(Command):
    """Activate a scene by id or (case-insensitive) name."""

    output_id: int
    scene: str


@dataclass
class CycleScene(Command):
    output_id: int
    step: int = 1


@dataclass
class SetElementVisibility(Command):
    """Show/hide an element of the live scene (``visible=None`` toggles)."""

    output_id: int
    element: str
    visible: bool | None = None


# ------------------------------------------------------------------ updates

@dataclass
class InstallUpdate(Command):
    tag: str
