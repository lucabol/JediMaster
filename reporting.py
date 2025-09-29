from __future__ import annotations

from typing import Iterable, Sequence, Any, Optional, List


def _stringify(cell: Any) -> str:
    if cell is None:
        return ""
    return str(cell)


def _build_border(widths: List[int], left: str, middle: str, right: str) -> str:
    segments = ["".join(["─" for _ in range(width + 2)]) for width in widths]
    return f"{left}{middle.join(segments)}{right}"


def format_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    empty_message: str = "(none)",
) -> str:
    """Render a simple box-drawn table as a string."""

    header_cells: List[str] = [_stringify(cell) for cell in headers]
    body_rows: List[List[str]] = [[_stringify(cell) for cell in row] for row in rows]

    column_count = len(header_cells)
    if column_count == 0:
        raise ValueError("Table must contain at least one header column")

    # Ensure every row has the correct number of columns.
    for idx, row in enumerate(body_rows):
        if len(row) != column_count:
            raise ValueError(
                f"Row {idx} has {len(row)} cells but expected {column_count}"
            )

    # Determine column widths.
    widths = [len(cell) for cell in header_cells]
    for row in body_rows:
        for col, cell in enumerate(row):
            widths[col] = max(widths[col], len(cell))

    lines: List[str] = []
    top_border = _build_border(widths, "┌", "┬", "┐")
    lines.append(top_border)

    header_line_cells = [cell.ljust(widths[idx]) for idx, cell in enumerate(header_cells)]
    header_line = "│ " + " │ ".join(header_line_cells) + " │"
    lines.append(header_line)

    separator = _build_border(widths, "├", "┼", "┤")
    lines.append(separator)

    if not body_rows:
        placeholder_cells = [empty_message] + [""] * (column_count - 1)
        padded_placeholder = [
            placeholder_cells[idx].ljust(widths[idx]) for idx in range(column_count)
        ]
        lines.append("│ " + " │ ".join(padded_placeholder) + " │")
    else:
        for row in body_rows:
            padded_cells = [row[idx].ljust(widths[idx]) for idx in range(column_count)]
            lines.append("│ " + " │ ".join(padded_cells) + " │")

    bottom_border = _build_border(widths, "└", "┴", "┘")
    lines.append(bottom_border)
    return "\n".join(lines)
