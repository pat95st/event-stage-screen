import pytest

from stagescreen.application.bus import DomainError
from stagescreen.domain.model import (
    Ipv4Settings,
    OutputConfig,
    VideoMode,
    validate_hostname,
    validate_ntp_servers,
)
from stagescreen.infrastructure.display import detect_connectors
from stagescreen.infrastructure.network import parse_key_values, split_terse
from stagescreen.infrastructure.system import default_ndi_limit, model_family, throttled_flags
from tests.fakes import make_drm


@pytest.mark.parametrize("text,label", [
    ("1920x1080@60", "1920x1080@60"), ("1280 x 720 @ 50Hz", "1280x720@50"),
    ("2560x720@59.94", "2560x720@59.94"), ("3840×2160@30", "3840x2160@30"),
])
def test_video_mode_parse(text, label):
    assert VideoMode.parse(text).label == label


@pytest.mark.parametrize("text", ["", "1920x1080", "1920x1080@0", "100x100@60", "9000x1080@60", "abc"])
def test_video_mode_invalid(text):
    with pytest.raises(DomainError):
        VideoMode.parse(text)


def test_output_config_rules():
    cfg = OutputConfig.from_input(2, {"mirror_of": "1", "mode": "", "rotation": 90})
    assert cfg.mirror_of == 1 and cfg.mode is None and cfg.rotation == 90 and cfg.custom_mode is False
    assert OutputConfig.from_input(1, {"mirror_of": 1}).mirror_of is None
    with pytest.raises(DomainError):
        OutputConfig.from_input(1, {"connector": "HDMI A 1"})
    with pytest.raises(DomainError):
        OutputConfig.from_input(1, {"mirror": {"scaling": "zoom"}})
    with pytest.raises(DomainError):
        OutputConfig.from_input(1, {"name": "x" * 41})


def test_ipv4_settings():
    s = Ipv4Settings.from_input({"method": "manual", "address": "192.168.1.50/24", "gateway": "192.168.1.1", "dns": "1.1.1.1;9.9.9.9"})
    assert s.address == "192.168.1.50/24" and s.dns == ("1.1.1.1", "9.9.9.9")
    assert Ipv4Settings.from_input({"method": "auto"}).method == "auto"
    for bad in ({"method": "manual", "address": "192.168.1.0/24"}, {"method": "manual", "address": "192.168.1.5/32"},
                {"method": "dhcp"}, {"method": "manual", "address": "1.2.3.4/24", "gateway": "x"}):
        with pytest.raises(DomainError):
            Ipv4Settings.from_input(bad)


def test_hostname_and_ntp():
    assert validate_hostname(" Stage-1 ") == "stage-1"
    for bad in ("-stage", "stage_1", "", "a" * 64):
        with pytest.raises(DomainError):
            validate_hostname(bad)
    assert validate_ntp_servers(["pool.ntp.org", "", "10.0.0.1"]) == ["pool.ntp.org", "10.0.0.1"]
    with pytest.raises(DomainError):
        validate_ntp_servers(["a b"])


def test_detect_connectors(tmp_path):
    connectors = detect_connectors(make_drm(tmp_path))
    assert [(c.name, c.connected) for c in connectors] == [("HDMI-A-1", True), ("HDMI-A-2", False)]
    assert connectors[0].modes == ["1920x1080", "1280x720", "3840x2160"]
    assert detect_connectors(tmp_path / "missing") == []


def test_nmcli_parsing():
    assert split_terse(r"*:Crew\:Net:55:WPA2") == ["*", "Crew:Net", "55", "WPA2"]
    kv = parse_key_values("IP4.ADDRESS[1]:10.0.0.2/24\nIP4.ADDRESS[2]:10.0.0.3/24\nIP4.GATEWAY:--\nipv4.dns:1.1.1.1,9.9.9.9\n")
    assert kv == {"IP4.ADDRESS": ["10.0.0.2/24", "10.0.0.3/24"], "IP4.GATEWAY": [], "ipv4.dns": ["1.1.1.1", "9.9.9.9"]}


def test_model_family_and_limits():
    assert model_family("Raspberry Pi 5 Model B Rev 1.0") == "pi5"
    assert model_family("Raspberry Pi 4 Model B Rev 1.4") == "pi4"
    assert model_family("x86_64") == "other"
    assert default_ndi_limit("pi4") == 1 and default_ndi_limit("pi5") == 4
    assert throttled_flags(0x50005) == ["Under-voltage detected", "Currently throttled", "Under-voltage has occurred", "Throttling has occurred"]
