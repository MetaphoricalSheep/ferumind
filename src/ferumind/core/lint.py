"""Deterministic, report-only lint for a Ferumind workspace.

Lint is operator tooling, not a policy engine.  It reports mechanical facts
about Markdown, links, role folders, and the derived index; it never decides
what knowledge means and never writes user documents.  Reconcile may refresh
derived SQLite rows and record ordinary out-of-band operations, exactly as a
project-wide read does.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field

from ferumind.core.document_map import derive_sections, frontmatter_line_range, split_document_lines
from ferumind.core.documents import DocumentInspection, inspect_document_content
from ferumind.core.errors import FerumindError, FrontmatterInvalidError, UnknownFolderError
from ferumind.core.folders import (
    ROLE_FOLDERS,
    Folder,
    archive_path_for,
    folder_of,
    is_archived_path,
    origin_path_for,
)
from ferumind.core.format import SUPPORTED_FORMAT, read_format
from ferumind.core.frontmatter import extract_frontmatter_block
from ferumind.core.locks import LockError, acquire_project_lock
from ferumind.core.markdown_links import MarkdownLink, extract_markdown_links
from ferumind.core.paths import (
    PathSafetyError,
    WorkspaceRoot,
    contained_path,
    contained_project_root,
)
from ferumind.core.reconcile import reconcile_document, reconcile_project
from ferumind.core.types import DbConnection, StrictModel
from ferumind.core.verify_index import IndexFinding, verify_index

LintSeverity = Literal["error", "warning", "info"]
LintCheckId = Literal[
    "workspace_format",
    "invalid_frontmatter",
    "invalid_description",
    "duplicate_document_id",
    "broken_internal_link",
    "unresolvable_link",
    "missing_file",
    "archived_target",
    "invalid_fragment",
    "index_inconsistency",
    "illegal_folder",
]

_SEVERITY_ORDER: Final[dict[LintSeverity, int]] = {
    "error": 0,
    "warning": 1,
    "info": 2,
}

_INDEX_FINDING_KINDS: Final = frozenset(
    {
        "missing_on_disk",
        "missing_in_index",
        "hash_mismatch",
        "section_mismatch",
        "orphan_section",
        "duplicate_section",
        "search_index_count",
        "orphan_search",
    }
)


class LintFinding(StrictModel):
    """One actionable, mechanically established workspace finding."""

    project: str
    path: str | None = None
    line: int | None = Field(default=None, ge=1)
    check_id: LintCheckId
    severity: LintSeverity
    message: str
    target: str | None = None


class LintCheckCount(StrictModel):
    check_id: LintCheckId
    count: int = Field(ge=1)


class LintSummary(StrictModel):
    findings: int
    errors: int
    warnings: int
    infos: int
    by_check: list[LintCheckCount]


class LintReport(StrictModel):
    """Stable result of one lint pass, suitable for JSON handoff."""

    findings: list[LintFinding]
    summary: LintSummary
    projects_checked: list[str]
    documents_checked: int
    links_checked: int

    @property
    def has_errors(self) -> bool:
        return self.summary.errors > 0

    def at_or_above(self, minimum: LintSeverity) -> LintReport:
        """Return a report filtered to *minimum*, with filtered summary totals."""
        ceiling = _SEVERITY_ORDER[minimum]
        filtered = [
            finding for finding in self.findings if _SEVERITY_ORDER[finding.severity] <= ceiling
        ]
        return _report(
            filtered,
            projects_checked=self.projects_checked,
            documents_checked=self.documents_checked,
            links_checked=self.links_checked,
        )


@dataclass(frozen=True, slots=True)
class _DocumentRecord:
    project: str
    path: str
    content: str
    folder: Folder | None
    status: str
    document_id: str | None
    reconcile_safe: bool
    links: tuple[MarkdownLink, ...]


@dataclass(frozen=True, slots=True)
class _ProjectScan:
    records: tuple[_DocumentRecord, ...]
    invalid_index_paths: frozenset[str]
    findings: tuple[LintFinding, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedLink:
    actual_path: str | None
    fragment: str | None


@dataclass(frozen=True, slots=True)
class _InternalDestination:
    path: str
    fragment: str | None


def lint_workspace(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    project_keys: list[str],
) -> LintReport:
    """Lint selected projects without changing any Markdown bytes.

    Older or unreadable format markers produce one format finding and still
    permit format-independent checks.  A newer marker fails closed after that
    finding, matching :class:`~ferumind.core.format.FormatGate`.
    """
    keys = sorted(set(project_keys))
    findings: list[LintFinding] = []
    found_format = read_format(workspace)
    if found_format != SUPPORTED_FORMAT:
        findings.append(_format_finding(found_format))
    if found_format is not None and found_format > SUPPORTED_FORMAT:
        return _report(
            findings,
            projects_checked=[],
            documents_checked=0,
            links_checked=0,
        )

    documents_checked = 0
    links_checked = 0
    for project_key in keys:
        try:
            project_root = contained_project_root(workspace, project_key)
            with acquire_project_lock(project_root, project_key):
                scan = _scan_project(
                    workspace,
                    project_key,
                    check_description=found_format == SUPPORTED_FORMAT,
                )
                findings.extend(scan.findings)
                documents_checked += len(scan.records)
                links_checked += sum(len(record.links) for record in scan.records)
                findings.extend(_reconcile_for_lint(conn, workspace, project_key, scan))
                findings.extend(_duplicate_id_findings(project_key, scan.records))
                findings.extend(_link_findings(workspace, project_key, scan.records))
                index_report = verify_index(
                    conn,
                    workspace,
                    [project_key],
                    include_workspace_checks=False,
                )
                invalid_paths = {(project_key, path) for path in scan.invalid_index_paths}
                findings.extend(_index_findings(index_report.findings, invalid_paths))
        except (LockError, PathSafetyError) as exc:
            findings.append(
                _finding(
                    project_key,
                    None,
                    "index_inconsistency",
                    "error",
                    f"Project could not be inspected atomically ({type(exc).__name__}).",
                )
            )
    return _report(
        findings,
        projects_checked=keys,
        documents_checked=documents_checked,
        links_checked=links_checked,
    )


def lint_newer_format_report(workspace: WorkspaceRoot) -> LintReport | None:
    """Return the complete fail-closed report for a newer workspace, if any.

    The CLI calls this before registry or database access.  The regular core
    entry point repeats the same gate so non-CLI callers cannot bypass it.
    """
    found_format = read_format(workspace)
    if found_format is None or found_format <= SUPPORTED_FORMAT:
        return None
    return _report(
        [_format_finding(found_format)],
        projects_checked=[],
        documents_checked=0,
        links_checked=0,
    )


def _format_finding(found_format: int | None) -> LintFinding:
    if found_format is not None and found_format > SUPPORTED_FORMAT:
        message = (
            f"Workspace format {found_format} is newer than supported format "
            f"{SUPPORTED_FORMAT}; refusing all project access. Remedy: upgrade Ferumind."
        )
    elif found_format is None:
        message = (
            f"Workspace format marker is unreadable; this build supports format "
            f"{SUPPORTED_FORMAT}. Format-specific checks were skipped."
        )
    else:
        message = (
            f"Workspace format {found_format} is older than supported format "
            f"{SUPPORTED_FORMAT}. Format-specific checks were skipped; run "
            "`ferumind migrate` before writing."
        )
    return LintFinding(
        project="workspace",
        check_id="workspace_format",
        severity="error",
        message=message,
    )


def _scan_project(
    workspace: WorkspaceRoot,
    project_key: str,
    *,
    check_description: bool,
) -> _ProjectScan:
    project_root = contained_project_root(workspace, project_key)
    records: list[_DocumentRecord] = []
    findings: list[LintFinding] = []
    invalid_index_paths: set[str] = set()
    if not project_root.is_dir():
        return _ProjectScan(records=(), invalid_index_paths=frozenset(), findings=())

    for candidate in sorted(project_root.rglob("*.md")):
        relative = candidate.relative_to(project_root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        path = relative.as_posix()
        record, record_findings = _scan_document(
            project_root,
            project_key,
            path,
            check_description=check_description,
        )
        findings.extend(record_findings)
        if record is None:
            invalid_index_paths.add(path)
            continue
        records.append(record)
        if not record.reconcile_safe:
            invalid_index_paths.add(path)
    return _ProjectScan(
        records=tuple(records),
        invalid_index_paths=frozenset(invalid_index_paths),
        findings=tuple(findings),
    )


def _scan_document(
    project_root: Path,
    project_key: str,
    path: str,
    *,
    check_description: bool,
) -> tuple[_DocumentRecord | None, list[LintFinding]]:
    findings: list[LintFinding] = []
    try:
        absolute = contained_path(project_root, path)
    except PathSafetyError:
        return None, [
            _finding(
                project_key,
                path,
                "illegal_folder",
                "error",
                "Document path is unsafe or traverses a symlink.",
            )
        ]

    if not absolute.is_file():
        return None, [
            _finding(
                project_key,
                path,
                "invalid_frontmatter",
                "error",
                "Markdown path is not a regular file.",
            )
        ]

    try:
        content = absolute.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, [
            _finding(
                project_key,
                path,
                "invalid_frontmatter",
                "error",
                f"Document is not readable UTF-8 ({type(exc).__name__}).",
            )
        ]

    try:
        folder: Folder | None = folder_of(path)
    except FerumindError:
        folder = None
        findings.append(
            _finding(
                project_key,
                path,
                "illegal_folder",
                "error",
                "Markdown document is outside the legal role folders.",
            )
        )

    inspection: DocumentInspection | None = None
    try:
        inspection = inspect_document_content(
            content,
            project_key=project_key,
            path=path,
            # Preserve tolerance for one reportable description defect while
            # all other canonical validation remains fail-closed.
            require_description=False,
        )
    except FrontmatterInvalidError as exc:
        findings.append(
            LintFinding(
                project=project_key,
                path=path,
                line=1,
                check_id="invalid_frontmatter",
                severity="error",
                message=_bounded_error(exc),
            )
        )
    except UnknownFolderError:
        # ``folder_of`` above already emitted the precise role-folder finding.
        inspection = None

    if inspection is not None:
        if check_description and inspection.managed and not inspection.description_valid:
            findings.append(
                LintFinding(
                    project=project_key,
                    path=path,
                    line=_frontmatter_key_line(content, "description"),
                    check_id="invalid_description",
                    severity="error",
                    message="Managed document description is missing or invalid.",
                )
            )
        document_id_value = inspection.frontmatter.get("id")
        document_id = (
            document_id_value if inspection.managed and isinstance(document_id_value, str) else None
        )
        status = inspection.status
        links = tuple(extract_markdown_links(_link_visible_content(content)))
    else:
        document_id = None
        status = "active"
        links = ()
    return (
        _DocumentRecord(
            project=project_key,
            path=path,
            content=content,
            folder=folder,
            status=status,
            document_id=document_id,
            reconcile_safe=(
                folder is not None and inspection is not None and inspection.description_valid
            ),
            links=links,
        ),
        findings,
    )


def _link_visible_content(content: str) -> str:
    """Blank frontmatter while preserving exact body line numbers."""
    try:
        block, body = extract_frontmatter_block(content)
    except FrontmatterInvalidError:
        return ""
    masked = "".join(character if character in "\r\n" else " " for character in block)
    return masked + body


def _frontmatter_key_line(content: str, key: str) -> int:
    for line_number, line in enumerate(content.splitlines(), start=1):
        if line.startswith(f"{key}:"):
            return line_number
        if line_number > 1 and line.strip() == "---":
            break
    return 1


def _bounded_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:512] or type(exc).__name__


def _reconcile_for_lint(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    project_key: str,
    scan: _ProjectScan,
) -> list[LintFinding]:
    """Reconcile valid documents, tolerating malformed paths long enough to report them."""
    findings: list[LintFinding] = []
    if not scan.invalid_index_paths:
        try:
            reconcile_project(conn, workspace, project_key)
        except (FerumindError, PathSafetyError, OSError) as exc:
            findings.append(_reconcile_failure(project_key, None, exc))
        return findings

    on_disk = {record.path for record in scan.records}
    for record in scan.records:
        if not record.reconcile_safe:
            continue
        try:
            reconcile_document(conn, workspace, project_key, record.path)
        except (FerumindError, PathSafetyError, OSError) as exc:
            findings.append(_reconcile_failure(project_key, record.path, exc))

    rows = conn.execute(
        "SELECT path FROM documents WHERE project_key = ? ORDER BY path",
        (project_key,),
    ).fetchall()
    for row in rows:
        path = str(row["path"])
        if path in on_disk or path in scan.invalid_index_paths:
            continue
        try:
            reconcile_document(conn, workspace, project_key, path)
        except (FerumindError, PathSafetyError, OSError) as exc:
            findings.append(_reconcile_failure(project_key, path, exc))
    return findings


def _reconcile_failure(
    project_key: str,
    path: str | None,
    exc: BaseException,
) -> LintFinding:
    return _finding(
        project_key,
        path,
        "index_inconsistency",
        "error",
        f"Reconcile did not converge ({type(exc).__name__}).",
    )


def _duplicate_id_findings(
    project_key: str,
    records: tuple[_DocumentRecord, ...],
) -> list[LintFinding]:
    by_id: dict[str, list[_DocumentRecord]] = {}
    for record in records:
        if record.document_id is not None:
            by_id.setdefault(record.document_id, []).append(record)
    findings: list[LintFinding] = []
    for _document_id, duplicates in sorted(by_id.items()):
        if len(duplicates) < 2:
            continue
        for record in sorted(duplicates, key=lambda item: item.path):
            peer_paths = sorted(item.path for item in duplicates if item.path != record.path)
            sample = ", ".join(peer_paths[:3])
            if len(sample) > 240:
                sample = sample[:237] + "..."
            findings.append(
                LintFinding(
                    project=project_key,
                    path=record.path,
                    line=_frontmatter_key_line(record.content, "id"),
                    check_id="duplicate_document_id",
                    severity="error",
                    message=(
                        f"Managed document id is shared by {len(peer_paths)} other path(s)"
                        f" (sample: {sample})."
                    ),
                )
            )
    return findings


def _link_findings(
    workspace: WorkspaceRoot,
    project_key: str,
    records: tuple[_DocumentRecord, ...],
) -> list[LintFinding]:
    project_root = contained_project_root(workspace, project_key)
    records_by_path = {record.path: record for record in records}
    findings: list[LintFinding] = []
    for record in records:
        findings.extend(_document_link_findings(project_root, record, records_by_path))
    return findings


def _document_link_findings(
    project_root: Path,
    source: _DocumentRecord,
    records_by_path: dict[str, _DocumentRecord],
) -> list[LintFinding]:
    """Check every link in one document."""
    findings: list[LintFinding] = []
    for link in source.links:
        resolved, link_finding = _resolve_link(project_root, source, link)
        if link_finding is not None:
            findings.append(link_finding)
        if resolved is None or resolved.actual_path is None:
            continue
        actual = resolved.actual_path
        if resolved.fragment and actual.endswith(".md"):
            fragment_finding = _fragment_finding(
                project_root,
                source,
                link,
                resolved,
                records_by_path,
            )
            if fragment_finding is not None:
                findings.append(fragment_finding)
    return findings


def _resolve_link(
    project_root: Path,
    source: _DocumentRecord,
    link: MarkdownLink,
) -> tuple[_ResolvedLink | None, LintFinding | None]:
    destination = _internal_destination(source, link)
    if isinstance(destination, LintFinding):
        return None, destination
    if destination is None:
        return None, None
    return _resolve_project_destination(project_root, source, link, destination)


def _internal_destination(
    source: _DocumentRecord,
    link: MarkdownLink,
) -> _InternalDestination | LintFinding | None:
    """Classify one destination as external, invalid, or project-internal."""
    destination = link.destination
    shape_error = _destination_shape_error(destination)
    if shape_error is not None:
        return _link_finding(
            source,
            link,
            "unresolvable_link",
            "error",
            shape_error,
        )
    if destination.startswith("//") or _has_external_scheme(destination):
        return None
    try:
        split = urlsplit(destination)
    except ValueError:
        return _link_finding(
            source,
            link,
            "unresolvable_link",
            "error",
            "Link target is not a valid URI or project-relative path.",
        )
    fragment = unquote(split.fragment) or None
    raw_path = unquote(split.path)
    if not raw_path:
        return _InternalDestination(source.path, fragment)
    intended = _normalize_link_path(source.path, raw_path)
    if intended is None:
        return _link_finding(
            source,
            link,
            "unresolvable_link",
            "error",
            "Link target is not a safe project-relative path.",
        )
    return _InternalDestination(intended, fragment)


def _destination_shape_error(destination: str) -> str | None:
    if "\\" in destination:
        return "Link target is not a canonical project-relative POSIX path."
    if destination.casefold().startswith("ferumind:"):
        return "Durable ferumind:// links are unsupported; use a project-relative path."
    return None


def _resolve_project_destination(
    project_root: Path,
    source: _DocumentRecord,
    link: MarkdownLink,
    destination: _InternalDestination,
) -> tuple[_ResolvedLink | None, LintFinding | None]:
    intended = destination.path
    try:
        target = contained_path(project_root, intended)
    except PathSafetyError:
        return None, _link_finding(
            source,
            link,
            "unresolvable_link",
            "error",
            "Link target traverses an unsafe or symlinked path.",
        )
    if target.exists():
        return _resolve_present_destination(source, link, destination, target)
    return _resolve_absent_destination(project_root, source, link, destination)


def _resolve_present_destination(
    source: _DocumentRecord,
    link: MarkdownLink,
    destination: _InternalDestination,
    target: Path,
) -> tuple[_ResolvedLink | None, LintFinding | None]:
    intended = destination.path
    if target.is_file():
        return _ResolvedLink(intended, destination.fragment), None
    if target.is_dir():
        # A folder reference — commonly trailing-slash, as in a spine document
        # map — names something that exists and is simply not a document.
        return _ResolvedLink(intended, None), None
    return None, _link_finding(
        source,
        link,
        "unresolvable_link",
        "error",
        f"Link target at {intended!r} is neither a regular file nor a directory.",
    )


def _resolve_absent_destination(
    project_root: Path,
    source: _DocumentRecord,
    link: MarkdownLink,
    destination: _InternalDestination,
) -> tuple[_ResolvedLink | None, LintFinding | None]:
    intended = destination.path
    mirrored = _mirror_destination(project_root, destination)
    if mirrored is not None:
        resolved, message = mirrored
        if _is_archived(source):
            # An archived document can never be edited — ``archive_document``
            # targets are a hard refusal (DOCUMENT_ARCHIVED) — so a link that
            # still resolves across the boundary states a fact about cold
            # storage that nobody can act on. Lint reports repairs, not trivia.
            return resolved, None
        return resolved, _link_finding(source, link, "archived_target", "warning", message)

    check_id: LintCheckId = "broken_internal_link" if intended.endswith(".md") else "missing_file"
    noun = "Markdown document" if check_id == "broken_internal_link" else "project file"
    return None, _link_finding(
        source,
        link,
        check_id,
        "error",
        f"Linked {noun} does not exist at {intended!r} or its archive mirror.",
    )


def _is_archived(record: _DocumentRecord) -> bool:
    """Return whether *record* is cold storage, and therefore uneditable."""
    return record.folder == "archive" or record.status == "archived"


def _mirror_destination(
    project_root: Path,
    destination: _InternalDestination,
) -> tuple[_ResolvedLink, str] | None:
    """Resolve a link across the archive boundary, which archiving never rewrites.

    ``archive_document`` moves a document under ``archive/`` and rewrites no
    link, so staleness appears in both directions: a live document may cite a
    target that has since been archived, and an archived document's relative
    links resolve one level too deep, into an ``archive/`` mirror that was
    never created.  Both are the designed consequence of archiving rather than
    a missing target, so both are warnings naming where the link does resolve.
    """
    if is_archived_path(destination.path):
        try:
            candidate = origin_path_for(destination.path)
        except UnknownFolderError:
            return None
        message = (
            f"Link resolves only outside the archive, at {candidate!r}; the citing "
            "document was archived and its relative links were not rewritten."
        )
    else:
        candidate = archive_path_for(destination.path)
        message = f"Link resolves only through the archive mirror at {candidate!r}."
    try:
        mirrored = contained_path(project_root, candidate)
    except PathSafetyError:
        return None
    if not (mirrored.is_file() or mirrored.is_dir()):
        return None
    return _ResolvedLink(candidate, destination.fragment), message


def _has_external_scheme(destination: str) -> bool:
    """Recognize a URI scheme lexically without validating an external URL."""
    scheme, separator, _remainder = destination.partition(":")
    return bool(
        separator
        and scheme
        and scheme[0].isalpha()
        and all(character.isalnum() or character in "+-." for character in scheme)
    )


def _normalize_link_path(source_path: str, raw_path: str) -> str | None:
    if raw_path.startswith("/") or "\\" in raw_path:
        return None
    raw_parts = PurePosixPath(raw_path).parts
    if not raw_parts:
        return None
    if raw_parts[0] in ROLE_FOLDERS or raw_path == "spine.md":
        parts: list[str] = []
    else:
        parts = list(PurePosixPath(source_path).parent.parts)
        if parts == ["."]:
            parts = []
    for part in raw_parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        if part.startswith("."):
            return None
        parts.append(part)
    return PurePosixPath(*parts).as_posix() if parts else None


def _fragment_finding(
    project_root: Path,
    source: _DocumentRecord,
    link: MarkdownLink,
    resolved: _ResolvedLink,
    records_by_path: dict[str, _DocumentRecord],
) -> LintFinding | None:
    target_path = resolved.actual_path
    fragment = resolved.fragment
    if target_path is None or fragment is None:
        return None
    record = records_by_path.get(target_path)
    try:
        content = (
            record.content
            if record is not None
            else contained_path(project_root, target_path).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, PathSafetyError):
        return None
    lines = split_document_lines(content)
    try:
        _frontmatter, body_start = frontmatter_line_range(content)
    except FrontmatterInvalidError:
        body_start = 1
    section_ids = {section.section_id for section in derive_sections(lines, body_start, len(lines))}
    if fragment in section_ids:
        return None
    return LintFinding(
        project=source.project,
        path=source.path,
        line=link.line,
        check_id="invalid_fragment",
        severity="warning",
        message=f"Fragment {fragment!r} does not match a derived section id in {target_path!r}.",
        target=f"{target_path}#{fragment}",
    )


def _index_findings(
    source: list[IndexFinding],
    invalid_paths: set[tuple[str, str]],
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for item in source:
        if item.kind not in _INDEX_FINDING_KINDS or item.project_key is None:
            continue
        if item.path is not None and (item.project_key, item.path) in invalid_paths:
            continue
        findings.append(
            _finding(
                item.project_key,
                item.path,
                "index_inconsistency",
                "error",
                f"Derived index did not converge ({item.kind}): {item.message}",
            )
        )
    return findings


def _finding(
    project: str,
    path: str | None,
    check_id: LintCheckId,
    severity: LintSeverity,
    message: str,
) -> LintFinding:
    return LintFinding(
        project=project,
        path=path,
        check_id=check_id,
        severity=severity,
        message=message,
    )


def _link_finding(
    source: _DocumentRecord,
    link: MarkdownLink,
    check_id: LintCheckId,
    severity: LintSeverity,
    message: str,
) -> LintFinding:
    return LintFinding(
        project=source.project,
        path=source.path,
        line=link.line,
        check_id=check_id,
        severity=severity,
        message=message,
        target=link.destination,
    )


def _report(
    findings: list[LintFinding],
    *,
    projects_checked: list[str],
    documents_checked: int,
    links_checked: int,
) -> LintReport:
    ordered = sorted(findings, key=_finding_sort_key)
    counts: dict[LintCheckId, int] = {}
    for finding in ordered:
        counts[finding.check_id] = counts.get(finding.check_id, 0) + 1
    return LintReport(
        findings=ordered,
        summary=LintSummary(
            findings=len(ordered),
            errors=sum(finding.severity == "error" for finding in ordered),
            warnings=sum(finding.severity == "warning" for finding in ordered),
            infos=sum(finding.severity == "info" for finding in ordered),
            by_check=[
                LintCheckCount(check_id=check_id, count=count)
                for check_id, count in sorted(counts.items())
            ],
        ),
        projects_checked=list(projects_checked),
        documents_checked=documents_checked,
        links_checked=links_checked,
    )


def _finding_sort_key(
    finding: LintFinding,
) -> tuple[str, str, int, int, str, str, str]:
    return (
        finding.project,
        finding.path or "",
        finding.line or 0,
        _SEVERITY_ORDER[finding.severity],
        finding.check_id,
        finding.target or "",
        finding.message,
    )


__all__ = [
    "LintCheckCount",
    "LintCheckId",
    "LintFinding",
    "LintReport",
    "LintSeverity",
    "LintSummary",
    "lint_newer_format_report",
    "lint_workspace",
]
