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
