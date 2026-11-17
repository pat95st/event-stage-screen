"""Query handlers (read side)."""

from __future__ import annotations

import asyncio
import platform
import shutil
import socket
import time
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..domain.model import COMMON_REFRESH_RATES, ROTATIONS
from ..i18n import LANGUAGES, current_language, t
from ..infrastructure import system
from ..infrastructure.display import detect_connectors
from ..infrastructure.logs import ring
from . import queries as q
from .bus import QueryBus

if TYPE_CHECKING:
    from ..container import Services


def register_query_handlers(bus: QueryBus, s: Services) -> None:
    def status() -> dict[str, Any]:
        st = system.live_status()
        st["throttled_flags"] = [t(f) for f in st["throttled_flags"]]
        return st

    async def outputs_view() -> dict[str, Any]:
        connectors = {c.name: c for c in await asyncio.to_thread(detect_connectors, s.config.drm_dir)}
        kiosk = s.kiosk_outputs()
        items = []
        for cfg in s.output_repo.all():
            con = connectors.get(cfg.connector)
            reported = kiosk.get(cfg.connector)
            kiosk_modes = (reported or {}).get("modes") or []
            current = next((m for m in kiosk_modes if m.get("current")), None)
            active_id = s.settings.get(f"scene.active.{cfg.mirror_of or cfg.id}")
            active = s.scene_repo.get(active_id) if active_id else None
            items.append({
                "scene_name": active["name"] if active else None,
                **cfg.to_dict(),
                "detected": con is not None or reported is not None,
                "connected": bool((con and con.connected) or (reported and kiosk_modes)),
                "modes": con.modes if con else sorted({f"{m['width']}x{m['height']}" for m in kiosk_modes}),
                "kiosk_modes": kiosk_modes,
                "current_mode": current,
            })
        return {
            "outputs": items,
            "connectors": [c.to_dict() for c in connectors.values()],
            "refresh_rates": list(COMMON_REFRESH_RATES),
            "rotations": list(ROTATIONS),
            "kiosk_online": bool(s.kiosk_state) and time.time() - s.kiosk_state.get("received_at", 0) < 15,
        }

    async def dashboard(_: q.GetDashboard) -> dict[str, Any]:
        return {
            "outputs": (await outputs_view())["outputs"],
            "status": status(),
            "device": {
                "hostname": socket.gethostname(),
                "model": s.model,
                "ip_addresses": system.ip_addresses(),
                "version": __version__,
            },
            "network_pending": s.network.state(),
            "erm": [s.erm.output_status(n) for n in (1, 2)],
        }

    async def get_outputs(_: q.GetOutputs) -> dict[str, Any]:
        return await outputs_view()

    async def network(_: q.GetNetwork) -> dict[str, Any]:
        return {
            "available": await s.network.available(),
            "devices": await s.network.devices(),
            "pending": s.network.state(),
            "hostname": socket.gethostname(),
        }

    async def wifi(_: q.GetWifiNetworks) -> dict[str, Any]:
        return {"networks": await s.network.wifi_networks()}

    def system_info(_: q.GetSystemInfo) -> dict[str, Any]:
        disk = shutil.disk_usage(str(s.config.data_dir))
        return {
            "version": __version__,
            "hostname": socket.gethostname(),
            "ip_addresses": system.ip_addresses(),
            "model": s.model,
            "model_family": s.model_family,
            "os": system.os_release(),
            "kernel": platform.release(),
            "python": platform.python_version(),
            "uptime_s": int(time.time() - s.started_at),
            "data_dir": str(s.config.data_dir),
            "disk_free_mb": disk.free // (1024 * 1024),
            "users": s.user_repo.list(),
            "language": current_language(),
            "languages": LANGUAGES,
            "system_backend": s.config.system_backend,
            "ndi": {"limit": s.ndi_limit(), "default": system.default_ndi_limit(s.model_family),
                    "custom": s.settings.get("ndi.max_sources") is not None},
            "commands": {"commands": s.commands.registered(), "queries": s.queries.registered()},
        }

    async def time_settings(_: q.GetTimeSettings) -> dict[str, Any]:
        settings = await asyncio.to_thread(system.time_settings)
        settings["timezones"] = await asyncio.to_thread(system.list_timezones)
        settings["now"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        return settings

    def logs(query: q.GetLogs) -> dict[str, Any]:
        entries = ring().entries(query.after, query.level, query.text, query.limit)
        return {"entries": entries, "last": entries[-1]["seq"] if entries else query.after}

    for message, handler in [
        (q.GetDashboard, dashboard),
        (q.GetStatus, lambda _: status()),
        (q.GetOutputs, get_outputs),
        (q.GetNetwork, network),
        (q.GetWifiNetworks, wifi),
        (q.GetSystemInfo, system_info),
        (q.GetTimeSettings, time_settings),
        (q.GetLogs, logs),
    ]:
        bus.register(message, handler)
