"""Offline PTX open-risk audit helpers.

The checks here are deliberately conservative.  They only flag structural
conditions that our control files have shown to be risky, while preserving
informational fields that are still being reverse-engineered.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .mixed_order import analyze_mixed_track_order, validate_mixed_track_open_risk
from .writer import (
    _FINAL_INDEX_OFFSET_MARKER,
    _audio_file_private_id_offset,
    _final_index_records,
    _flatten_block_starts,
    load_unxored,
    top_level_refs,
)


_TOLERATED_INVALID_MARKER_REF_TYPES = {
    0x1058,  # MIDI active-track cache refs can lag in current writer outputs.
    0x2519,  # Name-list records contain marker-shaped local refs in known-open files.
    0x2587,  # Overview/cache records often retain stale marker refs.
    0x2597,  # Adjacent cache-ish records have the same behavior in controls.
    0x2624,  # Playlist refs can lag in known-opening large scaffold probes.
}


class SessionAuditError(RuntimeError):
    """Raised when a generated PTX fails the offline open-risk audit."""

    def __init__(
        self,
        path: str | Path,
        issues: Sequence[str],
        report: str,
    ) -> None:
        self.path = Path(path)
        self.issues = tuple(issues)
        self.report = report
        issue_text = "; ".join(self.issues)
        super().__init__(f"generated PTX failed audit: {self.path}: {issue_text}")


@dataclass(frozen=True)
class FinalIndexMarkerRefIssue:
    final_offset: int
    referenced_offset: int
    record_index: int | None
    record_type: int | None


@dataclass(frozen=True)
class SessionAuditSummary:
    path: Path
    parse_error: str | None
    top_level_block_count: int
    final_index_start: int | None
    final_index_is_last: bool
    final_marker_value: int | None
    final_marker_ok: bool | None
    final_expected_record_count: int | None
    final_detected_record_count: int | None
    final_marker_ref_count: int
    invalid_marker_refs: tuple[FinalIndexMarkerRefIssue, ...]
    critical_invalid_marker_refs: tuple[FinalIndexMarkerRefIssue, ...]
    suspicious_invalid_marker_refs: tuple[FinalIndexMarkerRefIssue, ...]
    audio_file_list_count: int
    audio_metadata_count: int
    audio_link_issues: tuple[str, ...]
    mixed_order_checked: bool
    mixed_order_issues: tuple[str, ...]


def analyze_session_audit(
    path: str | Path,
    *,
    natural_order: Sequence[str] | None = None,
) -> SessionAuditSummary:
    """Run offline structural checks that catch known Pro Tools open hazards."""

    session_path = Path(path)
    try:
        data = load_unxored(session_path)
        refs = top_level_refs(data)
        starts_and_types = _flatten_block_starts(data)
    except Exception as exc:  # pragma: no cover - exact parser errors vary by input
        return SessionAuditSummary(
            path=session_path,
            parse_error=str(exc),
            top_level_block_count=0,
            final_index_start=None,
            final_index_is_last=False,
            final_marker_value=None,
            final_marker_ok=None,
            final_expected_record_count=None,
            final_detected_record_count=None,
            final_marker_ref_count=0,
            invalid_marker_refs=(),
            critical_invalid_marker_refs=(),
            suspicious_invalid_marker_refs=(),
            audio_file_list_count=0,
            audio_metadata_count=0,
            audio_link_issues=(),
            mixed_order_checked=False,
            mixed_order_issues=(),
        )

    final_ref = refs[-1] if refs and refs[-1].block.content_type == 0x0002 else None
    final_index_start = final_ref.start if final_ref is not None else None
    final_marker_value = _final_marker_value(data, refs)
    final_marker_ok = (
        None
        if final_index_start is None or final_marker_value is None
        else final_marker_value == (final_index_start & 0xFFFF)
    )

    expected_record_count = None
    detected_record_count = None
    marker_ref_count = 0
    invalid_refs: tuple[FinalIndexMarkerRefIssue, ...] = ()
    critical_invalid_refs: tuple[FinalIndexMarkerRefIssue, ...] = ()
    suspicious_invalid_refs: tuple[FinalIndexMarkerRefIssue, ...] = ()
    if final_ref is not None:
        if len(final_ref.data) >= 13:
            expected_record_count = int.from_bytes(final_ref.data[9:13], "little")
        known_content_types = {content_type for _offset, content_type in starts_and_types}
        records = _final_index_records(final_ref.data, known_content_types)
        detected_record_count = len(records) if records else None
        marker_ref_count, invalid_refs = _final_marker_ref_issues(
            final_ref.data,
            {offset for offset, _content_type in starts_and_types},
            records,
        )
        critical_invalid_refs = tuple(
            issue for issue in invalid_refs if issue.record_index == 0
        )
        suspicious_invalid_refs = tuple(
            issue
            for issue in invalid_refs
            if issue.record_index != 0
            and issue.record_type not in _TOLERATED_INVALID_MARKER_REF_TYPES
        )

    audio_file_list_count, audio_metadata_count, audio_link_issues = _audio_link_issues(
        data,
        refs,
    )

    mixed_order_checked = False
    mixed_order_issues: tuple[str, ...] = ()
    if _has_mixed_order_blocks(refs):
        try:
            mixed = analyze_mixed_track_order(session_path)
            mixed_names = set(mixed.global_order)
            mixed_order_checked = (
                bool(mixed.midi_names)
                and bool(mixed.audio_order)
                and bool(mixed_names)
                and set(mixed.playlist_order).issubset(mixed_names)
            )
            if mixed_order_checked:
                mixed_order_issues = tuple(
                    validate_mixed_track_open_risk(mixed)
                )
        except Exception as exc:  # pragma: no cover - defensive for malformed probes
            mixed_order_checked = True
            mixed_order_issues = (f"mixed-order parser failed: {exc}",)

    return SessionAuditSummary(
        path=session_path,
        parse_error=None,
        top_level_block_count=len(refs),
        final_index_start=final_index_start,
        final_index_is_last=final_ref is not None,
        final_marker_value=final_marker_value,
        final_marker_ok=final_marker_ok,
        final_expected_record_count=expected_record_count,
        final_detected_record_count=detected_record_count,
        final_marker_ref_count=marker_ref_count,
        invalid_marker_refs=invalid_refs,
        critical_invalid_marker_refs=critical_invalid_refs,
        suspicious_invalid_marker_refs=suspicious_invalid_refs,
        audio_file_list_count=audio_file_list_count,
        audio_metadata_count=audio_metadata_count,
        audio_link_issues=audio_link_issues,
        mixed_order_checked=mixed_order_checked,
        mixed_order_issues=mixed_order_issues,
    )


def validate_session_audit(
    summary: SessionAuditSummary,
    *,
    strict_final_index_refs: bool = False,
    check_audio_links: bool = True,
) -> list[str]:
    """Return high-confidence open-risk issues from an audit summary."""

    issues: list[str] = []
    if summary.parse_error is not None:
        return [f"parser failed: {summary.parse_error}"]
    if not summary.final_index_is_last:
        issues.append("missing final 0x0002 index block at end of session")
    if summary.final_marker_ok is False:
        issues.append(
            "header final-index marker "
            f"{_format_optional_int(summary.final_marker_value)} does not match "
            f"final index start {_format_optional_int(summary.final_index_start)}"
        )
    if summary.critical_invalid_marker_refs:
        examples = ", ".join(
            _format_marker_ref(issue) for issue in summary.critical_invalid_marker_refs[:3]
        )
        suffix = (
            ""
            if len(summary.critical_invalid_marker_refs) <= 3
            else f", +{len(summary.critical_invalid_marker_refs) - 3} more"
        )
        issues.append(f"critical final-index marker refs point outside block starts: {examples}{suffix}")
    if strict_final_index_refs and summary.suspicious_invalid_marker_refs:
        examples = ", ".join(
            _format_marker_ref(issue) for issue in summary.suspicious_invalid_marker_refs[:3]
        )
        suffix = (
            ""
            if len(summary.suspicious_invalid_marker_refs) <= 3
            else f", +{len(summary.suspicious_invalid_marker_refs) - 3} more"
        )
        issues.append(f"suspicious final-index marker refs point outside block starts: {examples}{suffix}")
    if check_audio_links:
        issues.extend(f"audio-link: {issue}" for issue in summary.audio_link_issues)
    issues.extend(f"mixed-order: {issue}" for issue in summary.mixed_order_issues)
    return issues


def format_session_audit(
    summary: SessionAuditSummary,
    *,
    strict_final_index_refs: bool = False,
    check_audio_links: bool = True,
) -> str:
    """Format a compact audit summary."""

    lines = [f"{summary.path}"]
    if summary.parse_error is not None:
        lines.append(f"  parser: failed: {summary.parse_error}")
        return "\n".join(lines)

    lines.extend(
        [
            f"  top-level blocks: {summary.top_level_block_count}",
            (
                "  final 0x0002: "
                f"start={_format_optional_int(summary.final_index_start)}, "
                f"record-count {_format_record_count(summary)}, "
                f"marker-refs={summary.final_marker_ref_count}"
            ),
            (
                "  final marker: "
                f"{_format_optional_int(summary.final_marker_value)} "
                f"({'ok' if summary.final_marker_ok else 'unchecked' if summary.final_marker_ok is None else 'bad'})"
            ),
            (
                "  invalid final marker refs: "
                f"{len(summary.invalid_marker_refs)} "
                f"(critical={len(summary.critical_invalid_marker_refs)}, "
                f"suspicious={len(summary.suspicious_invalid_marker_refs)}, "
                f"tolerated={_tolerated_invalid_ref_count(summary)})"
            ),
            (
                "  audio links: "
                f"files={summary.audio_file_list_count}, "
                f"metadata={summary.audio_metadata_count}, "
                f"issues={len(summary.audio_link_issues)}"
            ),
        ]
    )
    if summary.audio_link_issues:
        lines.append("  audio-link validation:")
        for issue in summary.audio_link_issues:
            lines.append(f"    - {issue}")
    if summary.mixed_order_checked:
        if summary.mixed_order_issues:
            lines.append("  mixed-order validation:")
            for issue in summary.mixed_order_issues:
                lines.append(f"    - {issue}")
        else:
            lines.append("  mixed-order validation: ok")
    else:
        lines.append("  mixed-order validation: skipped")

    issues = validate_session_audit(
        summary,
        strict_final_index_refs=strict_final_index_refs,
        check_audio_links=check_audio_links,
    )
    if issues:
        lines.append("  audit issues:")
        for issue in issues:
            lines.append(f"    - {issue}")
    else:
        lines.append("  audit: ok")
    return "\n".join(lines)


def _final_marker_value(data: bytes, refs) -> int | None:
    if not refs:
        return None
    first = refs[0]
    if first.start != 0x14 or first.block.block_size != 4:
        return None
    return int.from_bytes(data[first.block.offset : first.block.offset + 2], "little")


def _final_marker_ref_issues(
    final_data: bytes,
    block_starts: set[int],
    records: Sequence[tuple[int, int, int]],
) -> tuple[int, tuple[FinalIndexMarkerRefIssue, ...]]:
    record_starts = [record[0] for record in records]
    invalid: list[FinalIndexMarkerRefIssue] = []
    count = 0
    scan_stop = max(len(final_data) - len(_FINAL_INDEX_OFFSET_MARKER) - 4 + 1, 0)
    for pos in range(scan_stop):
        if final_data[pos : pos + len(_FINAL_INDEX_OFFSET_MARKER)] != _FINAL_INDEX_OFFSET_MARKER:
            continue
        count += 1
        value_pos = pos + len(_FINAL_INDEX_OFFSET_MARKER)
        referenced_offset = int.from_bytes(final_data[value_pos : value_pos + 4], "little")
        if referenced_offset in block_starts:
            continue
        record_index, record_type = _record_at(pos, records, record_starts)
        invalid.append(
            FinalIndexMarkerRefIssue(
                final_offset=pos,
                referenced_offset=referenced_offset,
                record_index=record_index,
                record_type=record_type,
            )
        )
    return count, tuple(invalid)


def _record_at(
    pos: int,
    records: Sequence[tuple[int, int, int]],
    record_starts: Sequence[int],
) -> tuple[int | None, int | None]:
    if not records:
        return None, None
    index = bisect_right(record_starts, pos) - 1
    if index < 0:
        return None, None
    record_start, record_end, content_type = records[index]
    if record_start <= pos < record_end:
        return index, content_type
    return None, None


def _has_mixed_order_blocks(refs) -> bool:
    top_level_types = {ref.block.content_type for ref in refs}
    return {0x1015, 0x1054, 0x1058, 0x2107, 0x2519, 0x2624}.issubset(top_level_types)


def _audio_link_issues(data: bytes, refs) -> tuple[int, int, tuple[str, ...]]:
    table = next((ref.block for ref in refs if ref.block.content_type == 0x1004), None)
    if table is None:
        return 0, 0, ()

    issues: list[str] = []
    table_content = data[table.offset : table.offset + table.block_size]
    table_audio_count = (
        int.from_bytes(table_content[2:6], "little")
        if len(table_content) >= 6
        else None
    )

    metadata_indexes: list[int] = []
    private_ids: list[bytes] = []
    for child in sorted(table.child, key=lambda item: item.offset):
        if child.content_type != 0x1003:
            continue
        content = data[child.offset : child.offset + child.block_size]
        metadata_indexes.append(int.from_bytes(content[2:6], "little") if len(content) >= 6 else -1)
        try:
            private_id_offset = _audio_file_private_id_offset(content)
        except ValueError:
            issues.append(f"0x1003 metadata record {len(metadata_indexes)} has no private file ID")
        else:
            private_ids.append(content[private_id_offset : private_id_offset + 16])

    entries = _first_audio_file_list_entries(data, table)
    audio_entries = [
        (entry_index, name, suffix)
        for entry_index, name, suffix in entries
        if _looks_audio_filename(name)
    ]
    audio_file_count = len(audio_entries)
    metadata_count = len(metadata_indexes)

    if audio_file_count or metadata_count:
        if table_audio_count is not None and table_audio_count != metadata_count:
            issues.append(
                f"0x1004 audio count {table_audio_count} != metadata count {metadata_count}"
            )
        if audio_file_count != metadata_count:
            issues.append(
                f"0x103a audio file count {audio_file_count} != 0x1003 metadata count "
                f"{metadata_count}"
            )

    expected_indexes = list(range(1, metadata_count + 1))
    if metadata_indexes and metadata_indexes != expected_indexes:
        issues.append(
            f"0x1003 indexes {tuple(metadata_indexes)!r} != "
            f"{tuple(expected_indexes)!r}"
        )

    duplicate_private_ids = _duplicate_hex_values(private_ids)
    if duplicate_private_ids:
        issues.append(
            "duplicate 0x2106 private file IDs: "
            + ", ".join(duplicate_private_ids[:3])
            + ("" if len(duplicate_private_ids) <= 3 else f", +{len(duplicate_private_ids) - 3} more")
        )

    issues.extend(_file_list_chain_issues(data, table, entries, audio_entries))
    return audio_file_count, metadata_count, tuple(issues)


def _first_audio_file_list_entries(
    data: bytes,
    table,
) -> tuple[tuple[int, str, bytes], ...]:
    for child in sorted(table.child, key=lambda item: item.offset):
        if child.content_type != 0x103A:
            continue
        content = data[child.offset : child.offset + child.block_size]
        entries = _audio_file_list_entries_from_content(content)
        if entries:
            return entries
    return ()


def _audio_file_list_entries_from_content(content: bytes) -> tuple[tuple[int, str, bytes], ...]:
    entries: list[tuple[int, str, bytes]] = []
    pos = 11
    while pos + 13 <= len(content):
        name_len = int.from_bytes(content[pos : pos + 4], "little")
        name_start = pos + 4
        name_end = name_start + name_len
        entry_end = name_end + 9
        if name_len < 0 or entry_end > len(content):
            break
        try:
            name = content[name_start:name_end].decode("latin-1")
        except UnicodeDecodeError:
            name = ""
        entries.append((len(entries), name, bytes(content[name_end:entry_end])))
        pos = entry_end
    return tuple(entries)


def _file_list_chain_issues(
    data: bytes,
    table,
    entries: Sequence[tuple[int, str, bytes]],
    audio_entries: Sequence[tuple[int, str, bytes]],
) -> list[str]:
    if not entries and not audio_entries:
        return []

    issues: list[str] = []
    file_list = next(
        (child for child in sorted(table.child, key=lambda item: item.offset) if child.content_type == 0x103A),
        None,
    )
    if file_list is None:
        return ["0x1004 has audio metadata but no 0x103a file list"]

    content = data[file_list.offset : file_list.offset + file_list.block_size]
    if len(content) >= 11:
        header_count_plus_one = int.from_bytes(content[2:6], "little")
        header_count = int.from_bytes(content[7:11], "little")
        if header_count_plus_one != len(entries) + 1 or header_count != len(entries):
            issues.append(
                f"0x103a header counts ({header_count_plus_one}, {header_count}) "
                f"!= ({len(entries) + 1}, {len(entries)})"
            )

    if not audio_entries:
        return issues

    last_audio_entry_index = audio_entries[-1][0]
    for position, (entry_index, name, suffix) in enumerate(audio_entries):
        if len(suffix) != 9:
            issues.append(f"0x103a audio entry {entry_index} {name!r} has truncated suffix")
            continue
        expected_tail = b"\x00\xff\xff\xff\xff" if position == len(audio_entries) - 1 else b"\x02\x00\x00\x00\x00"
        if suffix[4:9] != expected_tail:
            issues.append(
                f"0x103a audio entry {entry_index} {name!r} suffix tail "
                f"{suffix[4:9].hex()} != {expected_tail.hex()}"
            )

    for entry_index, name, suffix in entries[last_audio_entry_index + 1 :]:
        if len(suffix) != 9:
            continue
        expected_index = int(entry_index).to_bytes(4, "little")
        if suffix[5:9] != expected_index:
            issues.append(
                f"0x103a path entry {entry_index} {name!r} index "
                f"{int.from_bytes(suffix[5:9], 'little')} != {entry_index}"
            )
    return issues


def _looks_audio_filename(name: str) -> bool:
    lower = name.lower()
    return lower.endswith((".wav", ".wave", ".aif", ".aiff"))


def _duplicate_hex_values(values: Sequence[bytes]) -> list[str]:
    seen: set[bytes] = set()
    duplicates: list[str] = []
    for value in values:
        if not value or value == b"\0" * len(value):
            continue
        if value in seen and value.hex() not in duplicates:
            duplicates.append(value.hex())
        seen.add(value)
    return duplicates


def _format_record_count(summary: SessionAuditSummary) -> str:
    expected = _format_optional_int(summary.final_expected_record_count)
    detected = _format_optional_int(summary.final_detected_record_count)
    return f"expected={expected}, detected={detected}"


def _tolerated_invalid_ref_count(summary: SessionAuditSummary) -> int:
    return (
        len(summary.invalid_marker_refs)
        - len(summary.critical_invalid_marker_refs)
        - len(summary.suspicious_invalid_marker_refs)
    )


def _format_optional_int(value: int | None) -> str:
    return "n/a" if value is None else f"{value} ({value:#x})"


def _format_marker_ref(issue: FinalIndexMarkerRefIssue) -> str:
    record = (
        "record=n/a"
        if issue.record_index is None
        else f"record={issue.record_index}"
    )
    record_type = (
        "type=n/a"
        if issue.record_type is None
        else f"type={issue.record_type:#06x}"
    )
    return (
        f"final+{issue.final_offset} -> {issue.referenced_offset:#x} "
        f"({record}, {record_type})"
    )
