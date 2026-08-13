"""Non-functional gate (Monday Packet R6.4): with PIPELINE_HYGIENE_PACKETS unset
the app is byte-identical to the packets-disabled build end to end — no Packets
tab, no Appendix dismiss analytics, no packets DOM, and every packet route 404s.
(tests/parity separately proves the view-model output itself is byte-identical
flag-off; this asserts the served HTTP surface.)
"""
from starlette.testclient import TestClient

from app.server import app
from tests.parity._build import build_full_multi

client = TestClient(app)

_MUTATION_ROUTES = (
    "/work-item/1/accept", "/work-item/1/dismiss",
    "/work-item/1/edit", "/work-item/1/reopen",
)
_EXPORT_ROUTES = ("/packet/rowan.md", "/packet/rowan.html")


def test_flag_off_no_packets_surface_anywhere(tmp_path, monkeypatch):
    env = build_full_multi(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PIPELINE_HYGIENE_PACKETS", raising=False)

    html = client.get("/").text
    assert ">Packets<" not in html, "Packets tab must be absent when flag off"
    assert "packets-panel" not in html
    assert "Dismissed work items" not in html  # R5.3 Appendix analytics absent

    # Every packet mutation route 404s (the flag gate runs first, before auth).
    for path in _MUTATION_ROUTES:
        assert client.post(path).status_code == 404, path
    for path in _EXPORT_ROUTES:
        assert client.get(path).status_code == 404, path
