"""Read the "Open from Environment" results table on macOS from a window screenshot.

On macOS the dialog's content is rendered by embedded Chromium (CEF), and
Service Studio's Avalonia app does not forward Chromium's accessibility switch,
so the table is not in the Accessibility tree. We read it with Apple's Vision
text recognition instead. Everything here is read-only and pure, except
``recognize_text`` (runs Vision on an image) and ``capture_window`` (takes a
screenshot of one Service Studio window).

Observed layout (Service Studio 11.55 for Mac, see docs/macos.md): a search box,
then a header row ``Module | Application | Changed | By`` (the sort arrow is
read as a stray ``*`` or similar), then one text line per cell, then the footer
buttons ``View Versions | Open | Cancel``. Long cells end in "...".
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any

from os_module_navigator.automation import service_studio as ss

COLUMNS: tuple[str, ...] = ("Module", "Application", "Changed", "By")
FOOTER_LABELS: frozenset[str] = frozenset({"Open", "Cancel", "View Versions"})
# A row whose text is this much shorter than the typical row is cut off by the
# list viewport (top or bottom) and its text is unreliable.
CLIPPED_HEIGHT_RATIO = 0.85
# Saturated pixels (accent colour) that mark the selected row.
SATURATION_MIN = 60
SELECTED_FRACTION_MIN = 0.5

# Characters Vision confuses in UI fonts (I/l/1/|, O/0).
_CONFUSABLES = str.maketrans({"l": "i", "1": "i", "|": "i", "!": "i", "0": "o"})


@dataclass(frozen=True, slots=True)
class TextBox:
    """One recognized text line, in image pixels (origin top-left)."""

    x: float
    y: float
    w: float
    h: float
    text: str

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass(frozen=True, slots=True)
class TableRow:
    row: ss.ModuleSearchRow
    top: float
    bottom: float


def ocr_equivalent(a: str, b: str) -> bool:
    """Equal after casefolding and folding characters OCR commonly confuses."""

    def fold(s: str) -> str:
        return " ".join(s.split()).casefold().translate(_CONFUSABLES)

    return fold(a) == fold(b)


def repair_confusables(ocr_text: str, reference: str) -> str:
    """Take ``reference``'s character wherever OCR read a confusable one; keep OCR case."""
    ocr, ref = " ".join(ocr_text.split()), " ".join(reference.split())
    if len(ocr) != len(ref):
        return ocr
    return "".join(o if o.casefold() == r.casefold() else r for o, r in zip(ocr, ref, strict=True))


def _clean_header(text: str) -> str:
    return re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", text)


def _find_header(boxes: list[TextBox]) -> list[tuple[str, TextBox]] | None:
    module = next((b for b in boxes if _clean_header(b.text) == "Module"), None)
    if module is None:
        return None
    tolerance = module.h * 0.6
    same_line = [b for b in boxes if abs(b.cy - module.cy) <= tolerance]
    header = []
    for box in sorted(same_line, key=lambda b: b.x):
        name = _clean_header(box.text)
        if name in COLUMNS:
            header.append((name, box))
    return header if header and header[0][0] == "Module" else None


def _group_lines(boxes: list[TextBox]) -> list[list[TextBox]]:
    lines: list[list[TextBox]] = []
    for box in sorted(boxes, key=lambda b: b.cy):
        if lines:
            last = lines[-1]
            ref = statistics.median(b.cy for b in last)
            if abs(box.cy - ref) <= max(b.h for b in last) * 0.6:
                last.append(box)
                continue
        lines.append([box])
    return lines


_MODULE_SUFFIXES = frozenset(
    {"CW", "BL", "CS", "IS", "LIB", "PAT", "TH", "RES", "API", "ASYNC", "FE", "WF"}
)


def _split_merged_module(cells: dict[str, str]) -> None:
    """Split a Module cell that Vision read together with the Application cell.

    When the two columns are close, Vision returns "Name Application text" as
    one line, which lands in the Module column. Module names cannot contain
    spaces, so the first space is the column boundary.
    """
    if cells.get("Application"):
        return
    name, sep, rest = cells.get("Module", "").partition(" ")
    # Vision sometimes reads the "_" before a module suffix as a space
    # ("ORDERS CW" for ORDERS_CW); a known suffix belongs to the name.
    while sep and rest:
        token, sep2, tail = rest.partition(" ")
        suffix = token.lstrip("_")
        if suffix not in _MODULE_SUFFIXES:
            break
        name = f"{name}_{suffix}"
        sep, rest = sep2, tail
    if sep and rest:
        cells["Module"] = name
        cells["Application"] = rest
    elif name != cells.get("Module", ""):
        cells["Module"] = name


def parse_results(boxes: list[TextBox]) -> list[TableRow] | None:
    """Rebuild table rows (top to bottom) from recognized text, or None if no table."""
    header = _find_header(boxes)
    if header is None:
        # With no matches the dialog hides the whole table (header included) but
        # still shows its footer buttons.
        labels = {b.text.strip() for b in boxes}
        return [] if {"Open", "Cancel"} <= labels else None
    header_bottom = max(b.y + b.h for _, b in header)
    footer = [b.y for b in boxes if b.text.strip() in FOOTER_LABELS and b.y > header_bottom]
    footer_top = min(footer) if footer else float("inf")
    body = [b for b in boxes if b.y > header_bottom and b.y + b.h < footer_top]

    # Cell -> column: the right-most column whose header starts at or before the cell.
    starts = [(name, box.x) for name, box in header]
    slack = statistics.median(b.h for _, b in header) * 1.5

    def column_of(box: TextBox) -> str:
        chosen = starts[0][0]
        for name, x in starts:
            if box.x >= x - slack:
                chosen = name
        return chosen

    lines = _group_lines(body)
    if not lines:
        return []
    typical_h = statistics.median(b.h for line in lines for b in line)
    rows: list[TableRow] = []
    for line in lines:
        cells = {name: "" for name, _ in header}
        for box in sorted(line, key=lambda b: b.x):
            col = column_of(box)
            cells[col] = f"{cells[col]} {box.text}".strip()
        _split_merged_module(cells)
        if sum(1 for v in cells.values() if v) < 2:
            continue  # a message such as "No results", not a module row
        clipped = max(b.h for b in line) < typical_h * CLIPPED_HEIGHT_RATIO
        rows.append(
            TableRow(
                row=ss.ModuleSearchRow(
                    cells={} if clipped else cells,
                    on_screen=not clipped,
                ),
                top=min(b.y for b in line),
                bottom=max(b.y + b.h for b in line),
            )
        )
    return rows


def selected_row_index(rows: list[TableRow], saturated_fraction: Any) -> int | None:
    """Index of the one row painted with the selection accent colour, else None.

    ``saturated_fraction(top, bottom)`` returns the share of strongly coloured
    pixels in that horizontal band of the screenshot.
    """
    hits = [
        i
        for i, r in enumerate(rows)
        if r.row.on_screen and saturated_fraction(r.top, r.bottom) >= SELECTED_FRACTION_MIN
    ]
    return hits[0] if len(hits) == 1 else None


# --- macOS frameworks (imported lazily so the parser is testable anywhere) ---


class Screenshot:
    """A captured window image plus pixel access for the selection check."""

    def __init__(self, image: Any) -> None:
        import Quartz

        self.image = image
        self.width = int(Quartz.CGImageGetWidth(image))
        self.height = int(Quartz.CGImageGetHeight(image))
        self._stride = int(Quartz.CGImageGetBytesPerRow(image))
        self._bpp = max(int(Quartz.CGImageGetBitsPerPixel(image)) // 8, 3)
        self._data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image)))

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return (0, 0, 0)
        i = y * self._stride + x * self._bpp
        r, g, b = self._data[i : i + 3]
        return (r, g, b)

    def has_bright_text(self, box: TextBox) -> bool:
        """True if the text in ``box`` is drawn white (enabled), not greyed out."""
        for y in range(int(box.y), int(box.y + box.h), 2):
            for x in range(int(box.x), int(box.x + box.w), 2):
                if min(self.pixel(x, y)) >= 200:
                    return True
        return False

    def is_accent(self, box: TextBox) -> bool:
        """True if the area around ``box`` is filled with the accent colour (active)."""
        samples = [
            self.pixel(int(box.x - box.h * 0.3), int(box.cy)),
            self.pixel(int(box.x + box.w + box.h * 0.3), int(box.cy)),
            self.pixel(int(box.x + box.w / 2), int(box.y - box.h * 0.2)),
        ]
        return sum(1 for p in samples if max(p) - min(p) >= SATURATION_MIN) >= 2

    def saturated_fraction(self, top: float, bottom: float) -> float:
        """Share of strongly coloured pixels sampled across a horizontal band."""
        height = bottom - top
        ys = [top - height * 0.35, top + height * 0.5, bottom + height * 0.35]
        hits = total = 0
        for y in ys:
            yi = int(y)
            if not 0 <= yi < self.height:
                continue
            row = yi * self._stride
            for xi in range(int(self.width * 0.03), int(self.width * 0.95), 6):
                i = row + xi * self._bpp
                px = self._data[i : i + 3]
                total += 1
                if max(px) - min(px) >= SATURATION_MIN:
                    hits += 1
        return hits / total if total else 0.0


def capture_window(window_number: int) -> Screenshot | None:
    import Quartz

    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_number,
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution,
    )
    return Screenshot(image) if image is not None else None


def recognize_text(shot: Screenshot) -> list[TextBox]:
    import Vision

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(shot.image, {})
    ok, _ = handler.performRequests_error_([request], None)
    if not ok:
        return []
    boxes: list[TextBox] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        bb = observation.boundingBox()  # normalized, origin bottom-left
        boxes.append(
            TextBox(
                x=bb.origin.x * shot.width,
                y=(1 - bb.origin.y - bb.size.height) * shot.height,
                w=bb.size.width * shot.width,
                h=bb.size.height * shot.height,
                text=str(candidates[0].string()),
            )
        )
    return boxes


# --- Manage Dependencies ------------------------------------------------------------
#
# Observed layout (Service Studio 11.55 for Mac, image pixels at 2x): a header row
# ``[Search modules] [All|In use]  [Search public elements] [All|In use|Modified or
# missing]``; below it the module list (left) and the public-elements tree (right);
# then an information panel and the footer ``Apply | Refresh all | Cancel``. Tree
# rows are ``[chevron] [checkbox] [icon] label``, indented 20 points per level; the
# root (the module itself) is the first row. Vision sometimes reads the icon as a
# leading "•", "-" or "O".

DEPENDENCY_FOOTER = frozenset({"Apply", "Refresh all", "Cancel"})
FORGE_ROW_PREFIX = 'Search for "'
_ICON_PREFIX = re.compile(r"^(?:[^\w(]+|[Oo0]\s+)\s*")
# In points: where the tree's checkbox column starts relative to the elements
# search box, the indentation step, and the label offset from its checkbox.
TREE_ORIGIN_PT = 6.0
TREE_STEP_PT = 20.0
LABEL_TO_BOX_PT = 31.5
ICON_LABEL_TO_BOX_PT = 11.5


@dataclass(frozen=True, slots=True)
class DependencyRow:
    label: str
    box: tuple[float, float]  # checkbox centre, image pixels
    top: float


@dataclass(frozen=True, slots=True)
class DependencyLayout:
    module_search: TextBox | None
    element_search: TextBox | None
    modules: list[tuple[str, bool]]
    elements: list[DependencyRow]  # rows that show a checkbox candidate, top to bottom
    buttons: dict[str, TextBox]  # Apply / Refresh all / Cancel / All (elements) / Refresh


def _clean_label(text: str) -> tuple[str, bool]:
    """Strip an icon Vision read as text. Returns (label, had_icon_prefix)."""
    stripped = _ICON_PREFIX.sub("", text.strip())
    return stripped, stripped != text.strip()


def parse_dependency_dialog(boxes: list[TextBox], scale: float) -> DependencyLayout | None:
    """Rebuild the Manage Dependencies dialog from recognized text, or None if not shown."""
    alls = sorted((b for b in boxes if b.text.strip() == "All"), key=lambda b: b.x)
    if len(alls) < 2:
        return None
    left_all, right_all = alls[0], alls[-1]
    header = [b for b in boxes if abs(b.cy - left_all.cy) <= left_all.h]
    header_bottom = max(b.y + b.h for b in header)
    module_search = min((b for b in header if b.x < left_all.x), key=lambda b: b.x, default=None)
    element_search = next(
        (
            b
            for b in sorted(header, key=lambda b: b.x)
            if left_all.x < b.x < right_all.x and b.text.strip() != "In use"
        ),
        None,
    )
    footer = {b.text.strip(): b for b in boxes if b.text.strip() in DEPENDENCY_FOOTER}
    footer_top = min((b.y for b in footer.values()), default=float("inf"))
    buttons: dict[str, TextBox] = {**footer, "All": right_all}
    body = [b for b in boxes if b.y > header_bottom and b.y + b.h < footer_top]

    split = element_search.x - 20 * scale if element_search else right_all.x - 200 * scale
    left_edge = (module_search.x if module_search else left_all.x - 220 * scale) + 10 * scale
    modules: list[tuple[str, bool]] = []
    for line in _group_lines([b for b in body if b.x < left_edge and b.x + b.w < split]):
        text = " ".join(b.text.strip() for b in sorted(line, key=lambda b: b.x))
        if not text or text.startswith(FORGE_ROW_PREFIX):
            continue
        is_extension = ss.EXTENSION_TAG in text
        name = text.replace(ss.EXTENSION_TAG, "").strip()
        if name:
            modules.append((name, is_extension))

    elements: list[DependencyRow] = []
    if element_search is not None:
        origin = element_search.x + TREE_ORIGIN_PT * scale
        step = TREE_STEP_PT * scale
        tree = [b for b in body if b.x >= split and len(b.text.strip()) > 1]
        for line in _group_lines(tree):
            box = max(line, key=lambda b: b.w)
            if box.text.strip() == "Refresh":
                buttons["Refresh"] = box
                continue
            label, had_icon = _clean_label(box.text)
            if not label:
                continue
            offset = (ICON_LABEL_TO_BOX_PT if had_icon else LABEL_TO_BOX_PT) * scale
            level = max(round((box.x - offset - origin) / step), 0)
            elements.append(DependencyRow(label, (origin + level * step, box.cy), box.y))
    return DependencyLayout(module_search, element_search, modules, elements, buttons)


def is_selection_fill(pixel: Any, cx: float, cy: float, scale: float) -> bool:
    """True if the area is plain accent colour with no white: a selected row's background."""
    half = 4 * scale
    step = max(int(scale), 1)
    pixels = [
        pixel(int(cx + dx), int(cy + dy))
        for dx in range(-int(half), int(half) + 1, step)
        for dy in range(-int(half), int(half) + 1, step)
    ]
    if not pixels:
        return False
    sat = sum(1 for p in pixels if max(p) - min(p) >= SATURATION_MIN) / len(pixels)
    return sat >= 0.9 and not any(min(p) >= 200 for p in pixels)


def checkbox_stats(pixel: Any, cx: float, cy: float, scale: float) -> str:
    """Pixel statistics around a checkbox centre, for error messages."""
    half = 4 * scale
    step = max(int(scale), 1)
    points = [
        (int(cx + dx), int(cy + dy))
        for dx in range(-int(half), int(half) + 1, step)
        for dy in range(-int(half), int(half) + 1, step)
    ]
    pixels = [pixel(x, y) for x, y in points]
    n = len(pixels) or 1
    black = sum(1 for p in pixels if max(p) <= 12) / n
    sat = sum(1 for p in pixels if max(p) - min(p) >= SATURATION_MIN) / n
    white = sorted({y for (x, y), p in zip(points, pixels, strict=True) if min(p) >= 200})
    span = (white[-1] - white[0]) if white else -1
    centre = pixel(int(cx), int(cy))
    return f"black={black:.2f} sat={sat:.2f} white_span={span} centre={tuple(centre)}"


def checkbox_state(pixel: Any, cx: float, cy: float, scale: float) -> int | None:
    """0 unchecked, 1 checked, 2 indeterminate, None when no checkbox is drawn there.

    ``pixel(x, y)`` returns an (r, g, b)-like triple (channel order does not matter).
    Unchecked boxes are filled black; checked and indeterminate ones are filled with
    the accent colour and carry a white tick (several rows tall) or dash (flat).
    """
    half = 4 * scale
    points = [
        (int(cx + dx), int(cy + dy))
        for dx in range(-int(half), int(half) + 1, max(int(scale), 1))
        for dy in range(-int(half), int(half) + 1, max(int(scale), 1))
    ]
    pixels = [pixel(x, y) for x, y in points]
    if not pixels:
        return None
    black = sum(1 for p in pixels if max(p) <= 12) / len(pixels)
    if black >= 0.6:
        return 0
    saturated = sum(1 for p in pixels if max(p) - min(p) >= SATURATION_MIN) / len(pixels)
    white_rows = {y for (x, y), p in zip(points, pixels, strict=True) if min(p) >= 200}
    if saturated < 0.4 or not white_rows:
        return None
    return 1 if max(white_rows) - min(white_rows) > 3.5 * scale else 2


# --- Find Usages --------------------------------------------------------------------
#
# Observed layout (Service Studio 11.55 for Mac, main window at 2x): Cmd+F opens the
# module search box in the toolbar with a drop-down: "Search in ..." rows (including
# "Search in this Module"), then one section per element type listing the elements
# whose name matches, left-aligned with those rows. Clicking an element suggestion
# opens it and selects it in the elements tree (right panel); the properties panel
# below the tree repeats the name as its header. Right-clicking the tree row opens a
# native (Avalonia) context menu whose items AX exposes without text, so it is read
# from a screenshot too. "Find Usages in all Modules" fills a bottom pane: a tab
# "'<name>' usages in all modules", a table "Module Name | Usages" with one row per
# module ("(local)" is the module itself, cells like "14 usages"), and the footer
# "20 usages found in 2 consumer modules" next to a "Close" button. Tree rows may
# start with an icon read as "• ".

MODULE_SEARCH_ITEM = "Search in this Module"
FIND_USAGES_ALL_ITEM = "Find Usages in all Modules"
# The elements tree and the properties panel sit in the right part of the window.
RIGHT_PANEL_MIN_X = 0.62
_USAGES_FOOTER = re.compile(
    r"(?P<total>\d[\d,.\s]*)\s+usages?\s+found\s+in\s+(?P<modules>\d[\d,.\s]*)\s+consumer\s+modules?",
    re.IGNORECASE,
)
_NO_USAGES = re.compile(r"\bno\s+usages?\s+found\b", re.IGNORECASE)
_USAGE_COUNT = re.compile(r"(\d[\d,.]*)\s*usages?", re.IGNORECASE)
_MENU_ICON = re.compile(r"^(?:Q|O|o|0|[^\w(]+)\s+")


def _number(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def find_text(boxes: list[TextBox], text: str) -> list[TextBox]:
    """Boxes that read ``text`` (a menu or drop-down item), ignoring a leading icon."""
    wanted = " ".join(text.split()).casefold()
    found = []
    for box in boxes:
        read = " ".join(box.text.split())
        if read.casefold() == wanted or _MENU_ICON.sub("", read).casefold() == wanted:
            found.append(box)
    return found


def element_suggestions(boxes: list[TextBox], name: str) -> tuple[bool, list[TextBox]]:
    """Element suggestions named ``name`` in the module search drop-down.

    Returns (drop-down seen, suggestions). The drop-down's rows ("Search in this
    Module", ..., then one section per element type with matching elements) are
    left-aligned; tree rows behind it start further right.
    """
    anchors = find_text(boxes, MODULE_SEARCH_ITEM)
    if len(anchors) != 1:
        return False, []
    anchor = anchors[0]
    found = [
        b
        for b in boxes
        if b.y > anchor.y
        and abs(b.x - anchor.x) <= anchor.h * 2
        and ss.same_tree_label(_clean_label(b.text)[0], name)
    ]
    return True, found


def element_rows(boxes: list[TextBox], name: str, width: float) -> list[TextBox]:
    """Boxes in the right panel (elements tree and properties) that show ``name``.

    Top to bottom; the first is the tree row, the next the properties panel header.
    """
    matches = [
        b
        for b in boxes
        if b.x >= width * RIGHT_PANEL_MIN_X and ss.same_tree_label(_clean_label(b.text)[0], name)
    ]
    return sorted(matches, key=lambda b: b.y)


def _pane_title(boxes: list[TextBox]) -> str:
    for box in boxes:
        text = box.text.strip()
        if "usage" not in text.casefold() or "'" not in text:
            continue
        inner = text.split("'", 2)
        if len(inner) >= 2 and inner[1]:
            return inner[1].strip()
    return ""


def parse_usages_pane(boxes: list[TextBox]) -> ss.UsagesPane | None:
    """Read the Find Usages pane, or None while its footer is not shown."""
    footer_box = next((b for b in boxes if _USAGES_FOOTER.search(b.text)), None)
    if footer_box is None:
        empty = next((b for b in boxes if _NO_USAGES.search(b.text)), None)
        if empty is None:
            return None
        return ss.UsagesPane(title=_pane_title(boxes), rows=[], total_usages=0, consumer_modules=0)
    match = _USAGES_FOOTER.search(footer_box.text)
    assert match is not None
    total, consumers = _number(match["total"]), _number(match["modules"])

    module_header = next((b for b in boxes if b.text.strip() == "Module Name"), None)
    if module_header is None:
        return ss.UsagesPane(
            title=_pane_title(boxes), rows=[], total_usages=total, consumer_modules=consumers
        )
    same_line = [
        b
        for b in boxes
        if abs(b.cy - module_header.cy) <= module_header.h * 0.6 and b.x > module_header.x
    ]
    usages_header = next((b for b in same_line if b.text.strip().startswith("Usages")), None)
    split = usages_header.x - module_header.h if usages_header else module_header.x + 300
    # The pane ends at its footer's "Close" button; the properties panel is further right.
    close = next(
        (
            b
            for b in boxes
            if b.text.strip() == "Close" and abs(b.cy - footer_box.cy) <= footer_box.h
        ),
        None,
    )
    right_limit = close.x + close.w if close else split + 2 * (split - module_header.x)
    header_bottom = module_header.y + module_header.h
    body = [
        b
        for b in boxes
        if header_bottom < b.y
        and b.y + b.h <= footer_box.y
        and module_header.x - module_header.h <= b.x < right_limit
    ]
    rows: list[tuple[str, int]] = []
    for line in _group_lines(body):
        ordered = sorted(line, key=lambda b: b.x)
        left = " ".join(b.text.strip() for b in ordered if b.x < split).strip()
        right = " ".join(b.text.strip() for b in ordered if b.x >= split)
        count = _USAGE_COUNT.search(right)
        if left and count:
            # Module names have no spaces; anything after the first space is OCR noise.
            rows.append((left.split()[0], _number(count[1])))
    return ss.UsagesPane(
        title=_pane_title(boxes), rows=rows, total_usages=total, consumer_modules=consumers
    )
