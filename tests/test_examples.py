"""Every example under examples/ must load and be accepted, field for field,
by the real API - so a settings rename can't silently rot an example."""
import json
from pathlib import Path

import pytest

EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "examples")
                  .glob("*/feed.json"))


def test_examples_exist():
    assert EXAMPLES, "no examples/*/feed.json found"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
def test_example_feed_applies_cleanly(client, path):
    spec = json.loads(path.read_text())
    r = client.post("/api/feeds", json={"url": spec["url"], "type": spec["type"],
                                        "categories": spec.get("categories", []),
                                        "refresh": False})
    assert r.status_code == 201, r.get_json()
    fid = r.get_json()["id"]
    assert client.put(f"/api/feeds/{fid}", json=spec["settings"]).status_code == 200
    f = next(x for x in client.get("/api/feeds").get_json() if x["id"] == fid)
    for k, v in spec["settings"].items():
        got = f[k]
        assert (bool(got) if isinstance(v, bool) else got) == v, (k, got, v)
    assert f["categories"] == spec.get("categories", [])
