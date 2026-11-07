"""Incremental Granola API export to local Markdown meeting notes."""

import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

API_URL = "https://public-api.granola.ai/v1"


def atomic_write(path: Path, text: str) -> None:
    """Publish a private file without exposing partially written contents."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".granola-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_note(note: dict) -> str:
    """Render source metadata and content in LifeOS's meeting-note format."""
    metadata = {
        "granola_id": note["id"],
        "title": note.get("title") or "Untitled meeting",
        "created_at": note.get("created_at"),
        "updated_at": note.get("updated_at"),
        "source": "granola",
        "tags": ["meeting", "granola"],
    }
    for field in ("web_url", "attendees", "owner", "calendar_event"):
        if note.get(field):
            metadata[field] = note[field]
    body = [
        "# " + metadata["title"],
        "",
        "## Summary",
        "",
        note.get("summary_markdown") or note.get("summary_text") or "",
        "",
        "## Transcript",
        "",
    ]
    for segment in note.get("transcript") or []:
        speaker = segment.get("speaker") or {}
        label = speaker.get("diarization_label") or speaker.get("source")
        text = segment.get("text") or ""
        body.append(f"{label}: {text}" if label else text)
    return (
        "---\n"
        + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + "\n".join(body)
        + "\n"
    )


class GranolaSync:
    """Export changed notes; retain local copies when a remote note disappears."""

    def __init__(
        self, token: str, destination: Path, state_file: Path, client: httpx.Client
    ):
        self.destination = destination
        self.state_file = state_file
        self.client = client
        self.headers = {"Authorization": "Bearer " + token}
        self.state = (
            json.loads(state_file.read_text()) if state_file.exists() else {"notes": {}}
        )
        if not isinstance(self.state, dict) or not isinstance(
            self.state.get("notes"), dict
        ):
            raise TypeError("Invalid Granola sync state")

    def _get(self, path: str, params=None):
        response = self.client.get(API_URL + path, params=params, headers=self.headers)
        response.raise_for_status()
        time.sleep(0.22)  # Granola's sustained API limit is five requests/second.
        return response.json()

    def _target(self, filename: str) -> Path:
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or "\\" in filename
        ):
            raise ValueError("Invalid Granola filename")
        target = self.destination / filename
        if target.is_symlink() or not filename.endswith(".md"):
            raise ValueError("Invalid Granola destination")
        return target

    def save(self):
        atomic_write(self.state_file, json.dumps(self.state, ensure_ascii=False))

    def run(self) -> dict:
        self.destination.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        exported = 0
        cursor = None
        seen = set()
        while True:
            page = self._get("/notes", {"cursor": cursor} if cursor else None)
            if not isinstance(page.get("notes"), list):
                raise TypeError("Invalid Granola note listing")
            for summary in page["notes"]:
                identifier = summary["id"]
                if not isinstance(identifier, str) or not re.fullmatch(
                    r"[a-zA-Z0-9_-]+", identifier
                ):
                    raise ValueError("Invalid Granola note ID")
                previous = self.state["notes"].get(identifier) or {}
                filename = previous.get("filename") or ("granola-" + identifier + ".md")
                target = self._target(filename)
                if (
                    summary.get("updated_at")
                    and previous.get("updated_at") == summary["updated_at"]
                    and target.exists()
                ):
                    continue
                note = self._get(
                    "/notes/" + quote(identifier, safe=""), {"include": "transcript"}
                )
                if note.get("id") != identifier:
                    raise ValueError("Granola note ID mismatch")
                atomic_write(target, render_note(note))
                self.state["notes"][identifier] = {
                    "filename": filename,
                    "updated_at": summary.get("updated_at"),
                }
                self.state["index_pending"] = True
                self.save()
                exported += 1
            if not page.get("hasMore"):
                break
            cursor = page.get("cursor")
            if not cursor or not isinstance(cursor, str) or cursor in seen:
                raise ValueError("Invalid Granola pagination")
            seen.add(cursor)
        self.save()
        return {
            "exported": exported,
            "index_pending": bool(self.state.get("index_pending")),
        }

    def request_index(self, api_url: str) -> bool:
        """Keep pending work until the local API accepts an indexing job."""
        if not self.state.get("index_pending"):
            return True
        response = self.client.post(
            api_url.rstrip("/") + "/api/admin/reindex", timeout=15
        )
        response.raise_for_status()
        # A running job may have scanned the vault before this export.
        if response.json().get("status") != "started":
            return False
        self.state["index_pending"] = False
        self.save()
        return True
