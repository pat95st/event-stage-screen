"""Queries (read side). Handlers return plain dicts ready for JSON."""

from __future__ import annotations

from dataclasses import dataclass

from .bus import Query


@dataclass
class GetDashboard(Query):
    pass


@dataclass
class GetStatus(Query):
    pass


@dataclass
class GetOutputs(Query):
    pass


@dataclass
class GetNetwork(Query):
    pass


@dataclass
class GetWifiNetworks(Query):
    pass


@dataclass
class GetSystemInfo(Query):
    pass


@dataclass
class GetTimeSettings(Query):
    pass


@dataclass
class GetLogs(Query):
    after: int = 0
    level: str = "DEBUG"
    text: str = ""
    limit: int = 500


@dataclass
class ListScenes(Query):
    pass


@dataclass
class GetScene(Query):
    scene_id: str


@dataclass
class ExportScene(Query):
    scene_id: str
    include_media: bool = False


@dataclass
class ListMedia(Query):
    pass


@dataclass
class GetFonts(Query):
    pass


@dataclass
class GetRenderState(Query):
    output_id: int


@dataclass
class GetKioskConfig(Query):
    pass


@dataclass
class GetErmStatus(Query):
    pass


@dataclass
class GetRenderErm(Query):
    output_id: int


@dataclass
class GetNdiStatus(Query):
    pass


@dataclass
class GetNdiSources(Query):
    pass


@dataclass
class GetThumbnail(Query):
    output_id: int


@dataclass
class GetAutomation(Query):
    pass


@dataclass
class ListApiKeys(Query):
    pass


@dataclass
class GetRemoteStatus(Query):
    pass


@dataclass
class CheckForUpdates(Query):
    pass


@dataclass
class ListBackups(Query):
    pass
