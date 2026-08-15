# Changelog

Notable changes to Ferumind, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning, what a tag promises, and how a release is cut are documented in
[docs/releases.md](docs/releases.md).

## [Unreleased]

### Changed

- Uploads refuse more names and extensions. The extension denylist now covers
  scriptable documents (`.svg`, `.html`), launchers (`.desktop`, `.url`),
  Windows script hosts, macOS scripting, and mountable disk images. Filenames
  carrying `:` (an NTFS alternate data stream), the characters Windows cannot
  store, a trailing dot, or a reserved device name (`CON`, `NUL`, `COM1`…) are
  rejected. A workspace is meant to be synced and browsed; these were live on
  every filesystem but the one serving them.

## [0.1.0] - 2026-08-15

First public source release. Ferumind is a local-first, Markdown-backed
workspace shared by a person and their AI agents, served over MCP.

### Added

- Stateless MCP server with project-scoped tools: retrieval, guarded
  propose-then-apply edits, snapshots, archive and restore, chunked uploads,
  compacts, and an auditable operation log.
- Folder-derived document roles, behavioral frontmatter (`status`,
  `edit_policy`), and merged workspace/project rules delivered through
  `get_context`.
- SQLite FTS5 retrieval over derived Markdown sections, with reconcile-on-read
  for edits made outside Ferumind.
- Typer CLI covering bootstrap, migration, reindex, index verification,
  workspace lint, image compression, and project administration.
- Loopback-only, read-only operator dashboard and metadata-only observability.
- Outbound tunnel launcher for connecting a local workspace to a web chat
  client.
