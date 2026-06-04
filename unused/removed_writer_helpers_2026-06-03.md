# Removed Writer Helpers - 2026-06-03

These helpers were removed from `ptxformatwriter/writer.py` during a repo-wide
unused-code sweep.

Criteria:

- zero in-repo callers across `ptxformatwriter/`, `tests/`, CLI wrappers, README, and
  workbench scripts
- private helpers only
- no package exports

They are preserved verbatim here so they can be restored if needed.

## `_child_block_bytes`

```python
def _child_block_bytes(data: bytes, parent: Block, content_type: int) -> bytes | None:
    for child in parent.child:
        if child.content_type == content_type:
            return _full_block_bytes(data, child)
    return None
```

## `_build_offset_map`

```python
def _build_offset_map(old_data: bytes, new_data: bytes) -> dict[int, int]:
    return _build_offset_maps(old_data, new_data)[0]
```
