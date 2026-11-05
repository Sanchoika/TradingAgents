import io
import json
import tarfile

from tradingagents.demo import syncguard


def archive(built_at, extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, body in {"data.json": json.dumps({"built_at": built_at}), "index.html": "x",
                           **(extra or {})}.items():
            b = body.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
    return buf.getvalue()


def test_newer_accepted_older_refused_and_previous_kept(tmp_path):
    dest = tmp_path / "site"
    assert syncguard.receive(dest, archive("2026-11-05T10:00:00"))[0] == 0
    assert syncguard.receive(dest, archive("2026-11-05T12:00:00"))[0] == 0
    assert (tmp_path / "site.prev" / "data.json").exists()
    code, rec = syncguard.receive(dest, archive("2026-11-05T09:00:00"))   # stale PC copy
    assert code == 3 and rec["decision"] == "refused_stale"
    assert json.loads((dest / "data.json").read_text())["built_at"] == "2026-11-05T12:00:00"
    lines = (tmp_path / "site.sync-log.jsonl").read_text().splitlines()
    assert [json.loads(x)["decision"] for x in lines] == ["accepted", "accepted", "refused_stale"]


def test_bad_archives_are_refused_and_leave_dest_alone(tmp_path):
    dest = tmp_path / "site"
    syncguard.receive(dest, archive("2026-11-05T10:00:00"))
    code, rec = syncguard.receive(dest, archive("2026-11-06T10:00:00", {"../evil": "x"}))
    assert code == 2 and "refused archive member" in rec["error"]
    assert syncguard.receive(dest, b"not a tar")[0] == 2
    assert json.loads((dest / "data.json").read_text())["built_at"] == "2026-11-05T10:00:00"
    assert not (tmp_path / "evil").exists()
