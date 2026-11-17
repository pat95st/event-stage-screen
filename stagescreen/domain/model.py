"""Domain model: HDMI output configuration and system settings validation.

Pure Python, no I/O. Validation errors raise :class:`DomainError` with
translatable messages (:class:`Msg`).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ..application.bus import DomainError
from ..i18n import Msg

OUTPUT_IDS = (1, 2)
DEFAULT_CONNECTORS = {1: "HDMI-A-1", 2: "HDMI-A-2"}
ROTATIONS = (0, 90, 180, 270)
COMMON_REFRESH_RATES = (24.0, 25.0, 30.0, 50.0, 59.94, 60.0)


class MirrorScaling(str, Enum):
    LETTERBOX = "letterbox"
    STRETCH = "stretch"


@dataclass(frozen=True)
class VideoMode:
    width: int
    height: int
    refresh: float

    def __post_init__(self) -> None:
        if not (320 <= self.width <= 7680 and 200 <= self.height <= 4320):
            raise DomainError(Msg("Resolution {w}×{h} is out of range", w=self.width, h=self.height))
        if not (1 <= self.refresh <= 240):
            raise DomainError(Msg("Refresh rate {r} Hz is out of range", r=self.refresh))

    @property
    def label(self) -> str:
        rate = f"{self.refresh:g}"
        return f"{self.width}x{self.height}@{rate}"

    @classmethod
    def parse(cls, text: str) -> VideoMode:
        """Parse ``WIDTHxHEIGHT@RATE`` (rate may be a decimal, e.g. 59.94)."""
        m = re.fullmatch(r"\s*(\d{3,4})\s*[xX×]\s*(\d{3,4})\s*@\s*(\d{1,3}(?:\.\d{1,3})?)\s*(?:Hz)?\s*", text or "")
        if not m:
            raise DomainError(Msg('Invalid video mode "{mode}" (expected e.g. 1920x1080@60)', mode=text))
        return cls(int(m.group(1)), int(m.group(2)), float(m.group(3)))


@dataclass
class MirrorSettings:
    """How output 2 shows output 1's scene when mirroring."""

    scaling: MirrorScaling = MirrorScaling.LETTERBOX
    rotation: int = 0
    flip_h: bool = False
    flip_v: bool = False


@dataclass
class OutputConfig:
    id: int
    name: str
    connector: str
    enabled: bool = True
    # None = the monitor's preferred mode
    mode: VideoMode | None = None
    custom_mode: bool = False
    rotation: int = 0
    # Only output 2 may mirror output 1.
    mirror_of: int | None = None
    mirror: MirrorSettings = field(default_factory=MirrorSettings)

    def validate(self) -> None:
        if self.id not in OUTPUT_IDS:
            raise DomainError(Msg("Output {n} does not exist", n=self.id), 404)
        if not re.fullmatch(r"[A-Za-z0-9-]{1,32}", self.connector or ""):
            raise DomainError(Msg('Invalid connector name "{name}"', name=self.connector))
        if not (self.name or "").strip() or len(self.name) > 40:
            raise DomainError("Name: 1–40 characters")
        if self.rotation not in ROTATIONS or self.mirror.rotation not in ROTATIONS:
            raise DomainError("Rotation must be 0, 90, 180 or 270 degrees")
        if self.mirror_of is not None and not (self.id == 2 and self.mirror_of == 1):
            raise DomainError("Only output 2 can mirror output 1")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mode"] = asdict(self.mode) if self.mode else None
        d["mode_label"] = self.mode.label if self.mode else None
        d["mirror"]["scaling"] = self.mirror.scaling.value
        return d

    @classmethod
    def default(cls, output_id: int) -> OutputConfig:
        return cls(id=output_id, name=f"HDMI {output_id}", connector=DEFAULT_CONNECTORS[output_id])

    @classmethod
    def from_input(cls, output_id: int, data: dict[str, Any]) -> OutputConfig:
        """Build from web/API input (raises DomainError on invalid data)."""
        mode_text = (data.get("mode") or "").strip() if isinstance(data.get("mode"), str) else None
        mode = VideoMode.parse(mode_text) if mode_text else None
        mirror_data = data.get("mirror") or {}
        try:
            scaling = MirrorScaling(mirror_data.get("scaling", "letterbox"))
        except ValueError:
            raise DomainError("Unknown mirror scaling") from None
        cfg = cls(
            id=output_id,
            name=str(data.get("name") or f"HDMI {output_id}").strip(),
            connector=str(data.get("connector") or DEFAULT_CONNECTORS.get(output_id, "")).strip(),
            enabled=bool(data.get("enabled", True)),
            mode=mode,
            custom_mode=bool(data.get("custom_mode", False)) and mode is not None,
            rotation=int(data.get("rotation") or 0),
            mirror_of=1 if (output_id == 2 and data.get("mirror_of") in (1, "1", True)) else None,
            mirror=MirrorSettings(
                scaling=scaling,
                rotation=int(mirror_data.get("rotation") or 0),
                flip_h=bool(mirror_data.get("flip_h", False)),
                flip_v=bool(mirror_data.get("flip_v", False)),
            ),
        )
        cfg.validate()
        return cfg


# ---------------------------------------------------------------------- system

HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
NTP_SERVER_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def validate_hostname(name: str) -> str:
    name = (name or "").strip().lower()
    if not HOSTNAME_RE.fullmatch(name):
        raise DomainError("Host name: 1–63 characters, lowercase letters, digits and hyphens")
    return name


def validate_ntp_servers(servers: list[str]) -> list[str]:
    result = []
    for server in servers:
        server = (server or "").strip()
        if not server:
            continue
        if not NTP_SERVER_RE.fullmatch(server):
            raise DomainError(Msg('Invalid NTP server "{server}"', server=server))
        result.append(server)
    if len(result) > 8:
        raise DomainError("At most 8 NTP servers")
    return result


@dataclass(frozen=True)
class Ipv4Settings:
    method: str  # "auto" (DHCP) or "manual"
    address: str | None = None  # CIDR, e.g. 192.168.1.50/24
    gateway: str | None = None
    dns: tuple[str, ...] = ()

    @classmethod
    def from_input(cls, data: dict[str, Any]) -> Ipv4Settings:
        method = data.get("method", "auto")
        if method == "auto":
            dns = _parse_dns(data.get("dns"))
            return cls("auto", dns=dns)
        if method != "manual":
            raise DomainError("Unknown IPv4 method")
        try:
            iface = ipaddress.IPv4Interface((data.get("address") or "").strip())
        except ValueError:
            raise DomainError("Invalid IPv4 address (expected e.g. 192.168.1.50/24)") from None
        if iface.network.prefixlen in (0, 32) or iface.ip in (iface.network.network_address, iface.network.broadcast_address):
            raise DomainError("Invalid IPv4 address (expected e.g. 192.168.1.50/24)")
        gateway = (data.get("gateway") or "").strip() or None
        if gateway:
            try:
                gw = ipaddress.IPv4Address(gateway)
            except ValueError:
                raise DomainError("Invalid gateway address") from None
            if gw not in iface.network:
                raise DomainError("The gateway must be in the same network as the address")
        return cls("manual", str(iface), gateway, _parse_dns(data.get("dns")))


def _parse_dns(value: Any) -> tuple[str, ...]:
    items = value if isinstance(value, list) else re.split(r"[\s,;]+", value or "")
    result = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        try:
            result.append(str(ipaddress.ip_address(item)))
        except ValueError:
            raise DomainError(Msg('Invalid DNS server "{server}"', server=item)) from None
    return tuple(result[:4])
