import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "docker-compose.yml").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()


def test_portainer_template_matches_the_compose_file():
    doc = json.loads((ROOT / "portainer" / "templates.json").read_text())
    assert doc["version"] == "3"
    (t,) = doc["templates"]
    assert (t["id"], t["type"], t["title"]) == (1, 3, "snsdl")
    assert t["repository"] == {"url": "https://github.com/jonchaka/snsdl", "stackfile": "docker-compose.yml"}
    names = [e["name"] for e in t["env"]]
    assert names == ["DATA_PATH", "PUID", "PGID", "PORT", "TZ"]
    for e in t["env"]:
        assert f"${{{e['name']}" in COMPOSE, e["name"]
        assert e["label"] and e["description"] and "default" in e


def test_unraid_template_points_at_the_image_and_known_variables():
    root = ET.parse(ROOT / "unraid" / "snsdl.xml").getroot()
    assert root.tag == "Container" and root.get("version") == "2"
    assert root.findtext("Repository") == "ghcr.io/jonchaka/snsdl:latest"
    assert root.findtext("WebUI") == "http://[IP]:[PORT:8080]"
    ports = [c for c in root.findall("Config") if c.get("Type") == "Port"]
    assert [p.get("Target") for p in ports] == ["8080"]
    paths = {c.get("Target") for c in root.findall("Config") if c.get("Type") == "Path"}
    assert paths == {"/data", "/keep"}
    for c in root.findall("Config"):
        if c.get("Type") == "Variable":
            assert re.search(rf"^#?{re.escape(c.get('Target'))}=", ENV_EXAMPLE, re.M), c.get("Target")
    pin = next(c for c in root.findall("Config") if c.get("Target") == "ACCESS_PIN")
    assert pin.get("Mask") == "true" and pin.get("Display") == "advanced"
