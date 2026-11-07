# Granola Sync

**Status:** Complete
**Last Updated:** 2026-11-07
**Audience:** Operators

Export Granola meeting notes and transcripts into `Granola/` under
`LIFEOS_VAULT_PATH`. The importer uses Granola's official API and requires
an API key with access to the desired notes.

## Configure and run

Create a key in Granola's Settings → Connectors → API keys and save it as
`GRANOLA_API_TOKEN` in the checkout's private `.env`. Never commit the key.
See [Granola API access](https://docs.granola.ai/help-center/sharing/integrations/granola-api)
for plan requirements and available scopes.

From the LifeOS checkout:

```bash
~/.venvs/lifeos/bin/python scripts/sync_granola.py
```

The command downloads new and updated notes, then requests indexing from
LifeOS at `http://127.0.0.1:8000`. Start the API with `./scripts/server.sh start`.
If indexing cannot be requested, it remains pending for the next sync.
Unchanged notes are skipped; missing local files are restored. Remote deletion
does not remove local notes. Files managed by the importer are overwritten when
Granola changes a note; keep personal annotations in separate notes.

The private incremental state and lock live in `data/granola-sync/`, ignored by
Git. Use `--state-dir` to select a different state directory. Logs report counts
and error classes without printing meeting content or API response bodies.

## macOS five-minute schedule

Render `config/launchd/com.lifeos.granola-sync.plist.template`, replacing
`__HOME__` and `__LIFEOS_PATH__` with this machine's home and absolute checkout
paths. Save it to `~/Library/LaunchAgents/com.lifeos.granola-sync.plist`.
Create `data/granola-sync/` before loading the agent so its log paths exist.

```bash
mkdir -p data/granola-sync
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.lifeos.granola-sync.plist
```

The agent runs at login and every 300 seconds (five minutes) while the Mac is awake. It does
not start the LifeOS API. To inspect or trigger the loaded agent:

```bash
launchctl print "gui/$(id -u)/com.lifeos.granola-sync"
launchctl kickstart "gui/$(id -u)/com.lifeos.granola-sync"
```

## Related Documents

- [Installation](installation.md) — LifeOS prerequisites and startup
- [Data and Sync](../specs/technical/data-and-sync.md) — Vault indexing and source ingestion
- [Launchd Setup](launchd-setup.md) — macOS service management
