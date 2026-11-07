"""Granola export uses synthetic API payloads and isolated local files."""

import json

import httpx
import pytest
import yaml

from api.services.granola_sync import GranolaSync, render_note

pytestmark = pytest.mark.unit


def note(identifier="not_example", updated="2026-11-07T12:00:00Z"):
    return {
        "id": identifier,
        "title": "Synthetic: meeting\nexample",
        "created_at": updated,
        "updated_at": updated,
        "summary_markdown": "Synthetic summary",
        "transcript": [
            {"text": "Synthetic transcript", "speaker": {"source": "microphone"}}
        ],
    }


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr("api.services.granola_sync.time.sleep", lambda _: None)


def make_sync(tmp_path, handler):
    return GranolaSync(
        "synthetic-token",
        tmp_path / "vault",
        tmp_path / "state.json",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_export_incremental_update_and_index_retry(tmp_path):
    current = note()
    calls = []
    available = False

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert "authorization" not in request.headers
            if not available:
                raise httpx.ConnectError("synthetic failure", request=request)
            return httpx.Response(200, json={"status": "started"})
        assert request.headers["authorization"] == "Bearer synthetic-token"
        if request.url.path.endswith("/notes"):
            return httpx.Response(200, json={"notes": [current], "hasMore": False})
        return httpx.Response(200, json=current)

    sync = make_sync(tmp_path, handler)
    assert sync.run()["exported"] == 1
    target = next(sync.destination.glob("*.md"))
    assert target.stat().st_mode & 0o777 == 0o600
    assert sync.run()["exported"] == 0
    with pytest.raises(httpx.ConnectError):
        sync.request_index("http://localhost:8000")
    assert json.loads(sync.state_file.read_text())["index_pending"]
    current = note(updated="2026-11-08T12:00:00Z")
    assert sync.run()["exported"] == 1
    assert list(sync.destination.glob("*.md")) == [target]
    available = True
    assert sync.request_index("http://localhost:8000")
    assert not json.loads(sync.state_file.read_text())["index_pending"]


def test_render_metadata_and_transcript():
    rendered = render_note(note())
    metadata = yaml.safe_load(rendered.split("---", 2)[1])
    assert metadata["granola_id"] == "not_example"
    assert metadata["title"] == note()["title"]
    assert "Synthetic summary" in rendered
    assert "microphone: Synthetic transcript" in rendered


def test_migrated_state_preserves_existing_filename(tmp_path):
    destination = tmp_path / "vault"
    destination.mkdir()
    target = destination / "existing-synthetic.md"
    target.write_text("Existing note")
    state = {
        "notes": {
            "not_example": {"filename": target.name, "updated_at": note()["updated_at"]}
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    sync = make_sync(tmp_path, lambda r: httpx.Response(200, json={"notes": [note()]}))
    assert sync.run()["exported"] == 0
    target.unlink()
    sync.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={"notes": [note()]} if r.url.path.endswith("/notes") else note(),
            )
        )
    )
    assert sync.run()["exported"] == 1
    assert target.exists()


def test_pagination_and_deleted_remote_notes_retained(tmp_path):
    def handler(request):
        if request.url.path.endswith("/notes"):
            second = "cursor" in request.url.params
            return httpx.Response(
                200,
                json={
                    "notes": [note("not_two" if second else "not_one")],
                    "hasMore": not second,
                    "cursor": "next",
                },
            )
        return httpx.Response(200, json=note(request.url.path.rsplit("/", 1)[1]))

    sync = make_sync(tmp_path, handler)
    assert sync.run()["exported"] == 2
    sync.client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"notes": []}))
    )
    assert sync.run()["exported"] == 0
    assert len(list(sync.destination.glob("*.md"))) == 2


@pytest.mark.parametrize("filename", ["../outside.md", "/outside.md", "..\\outside.md"])
def test_state_cannot_write_outside_destination(tmp_path, filename):
    (tmp_path / "state.json").write_text(
        json.dumps({"notes": {"not_example": {"filename": filename}}})
    )
    sync = make_sync(tmp_path, lambda r: httpx.Response(200, json={"notes": [note()]}))
    with pytest.raises(ValueError):
        sync.run()


def test_failed_download_does_not_advance_state(tmp_path):
    sync = make_sync(
        tmp_path,
        lambda r: (
            httpx.Response(200, json={"notes": [note()]})
            if r.url.path.endswith("/notes")
            else httpx.Response(429)
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        sync.run()
    assert not sync.state_file.exists()
    assert not list(sync.destination.glob("*.md"))


def test_symlink_and_corrupt_state_rejected(tmp_path):
    sync = make_sync(tmp_path, lambda r: httpx.Response(200, json={"notes": [note()]}))
    sync.destination.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Keep")
    (sync.destination / "granola-not_example.md").symlink_to(outside)
    with pytest.raises(ValueError):
        sync.run()
    assert outside.read_text() == "Keep"
    sync.state_file.write_text("not json")
    with pytest.raises(ValueError):
        make_sync(tmp_path, lambda r: None)


def test_running_index_job_keeps_pending_state(tmp_path):
    sync = make_sync(
        tmp_path, lambda r: httpx.Response(200, json={"status": "already_queued"})
    )
    sync.state["index_pending"] = True
    assert not sync.request_index("http://localhost:8000")
    assert sync.state["index_pending"]


def load_cli(monkeypatch):
    import importlib.util
    from pathlib import Path

    cwd = Path.cwd()
    spec = importlib.util.spec_from_file_location(
        "granola_cli_test", cwd / "scripts/sync_granola.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("GRANOLA_API_TOKEN", "synthetic-secret")
    return module


def test_cli_lock_contention_does_not_contact_api(tmp_path, monkeypatch, capsys):
    import fcntl

    cli = load_cli(monkeypatch)
    lock = (tmp_path / "sync.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(
            cli.httpx, "Client", lambda **kw: pytest.fail("API called under contention")
        )
        assert cli.main(["--state-dir", str(tmp_path)]) == 0
        assert "already running" in capsys.readouterr().out
    finally:
        lock.close()


def test_cli_redacts_provider_failures(tmp_path, monkeypatch, capsys):
    cli = load_cli(monkeypatch)

    def fail(*args, **kwargs):
        raise ValueError("synthetic-secret PRIVATE MEETING BODY")

    monkeypatch.setattr(cli, "GranolaSync", fail)
    assert cli.main(["--state-dir", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "ValueError" in output.out
    assert "synthetic-secret" not in output.out + output.err
    assert "PRIVATE MEETING BODY" not in output.out + output.err
