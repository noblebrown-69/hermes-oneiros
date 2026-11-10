import pytest

from dream_cycle.config import default_config
from dream_cycle.propose import _normalize, extract_by_label

L = {l["name"]: l for l in default_config()["labels"]}


def vals(text, name):
    return sorted({h[0] for h in extract_by_label(text, L[name])})


def test_colon_ports():
    assert vals("dashboard on :9119 and wiki :8080\n", "port") == ["8080", "9119"]


def test_port_not_in_ip_or_time():
    assert vals("at 10.0.0.1 version 1.2:3\n", "port") == []


def test_pid_dropped():
    assert vals("pid :12345 process\n", "port") == []


@pytest.mark.parametrize("line,expected", [
    ("llama build b12000 ok", ["b12000"]),
    ("random b12000 token", []),
    ("driver b5555 installed", ["b5555"]),
])
def test_build_needs_label(line, expected):
    assert vals(line, "build") == expected


def test_throughput():
    assert vals("gen 55 tok/s then 80.5 tok/s", "throughput") == ["55 tok/s", "80.5 tok/s"]


def test_normalize():
    assert _normalize("~55  tok/s") == "55 tok/s"


def test_line_numbers():
    hits = extract_by_label("a\nport :1000\n", L["port"])
    assert hits[0][1] == 2


def test_capture_group_used():
    hits = extract_by_label("port :4242", L["port"])
    assert hits[0][0] == "4242" and hits[0][2] == "4242"
