"""PDF Studio v3 - a Google-Docs-style PDF editor with Markdown support.

Stack: PySide6 (GUI) + PyMuPDF (PDF engine) + markdown (MD -> HTML).
OCR additionally needs Tesseract installed (see ocr_document).
"""
import bisect
import io
import json
import os
import re
import sys
import uuid
from collections import OrderedDict
from pathlib import Path
from string import Template

import fitz  # PyMuPDF
import markdown
from PySide6.QtCore import (QBuffer, QIODevice, QPoint, QPointF, QRect, QSettings, QSize,
                            QStandardPaths, Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap, QPolygonF, QTextCursor)
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextCharFormat, QTextListFormat
from PySide6.QtWidgets import QFontComboBox, QStackedWidget, QTextEdit
from docs_editor import (CONTENT_H, EMBED_NAME, HL_COLORS, IMAGE_RES, MARGIN, STYLES, TEXT_COLORS,
                         PageEditor, export_pdf_bytes, load_doc_payload, new_qdoc, pdf_to_html,
                         serialize_doc)
from PySide6.QtWidgets import (QApplication, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QDockWidget, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMenuBar, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton,
                               QRubberBand, QScrollArea, QSpinBox, QSplitter, QTabBar, QTabWidget,
                               QTextBrowser, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

APP_NAME = "PDF Studio"
MAX_UNDO = 1000
MAX_UNDO_BYTES = 600 * 1024 * 1024
AUTOSAVE_MS = 60_000
MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists"]
LETTER = fitz.paper_rect("letter")

PDF_CSS = """
body { font-family: sans-serif; font-size: 11pt; }
h1 { font-size: 22pt; } h2 { font-size: 17pt; } h3 { font-size: 14pt; }
code, pre { font-family: monospace; background-color: #f0f0f0; }
pre { padding: 6px; }
blockquote { color: #555555; margin-left: 12px; }
table { border-collapse: collapse; }
th, td { border: 1px solid #999999; padding: 3px 6px; }
"""

LIGHT = dict(bg="#f9fbfd", fg="#1f1f1f", bar="#edf2fa", hover="#dde3ea", chk_bg="#d3e3fd",
             chk_fg="#0b57d0", menu="#ffffff", border="#dadce0", panel="#ffffff", pbg="#c2e7ff",
             pfg="#001d35", muted="#444746", input="#ffffff", handle="#e3e3e3", mhover="#f1f3f4",
             dis="#a0a4a8", btn="#ffffff", bhover="#f1f3f4")
DARK = dict(bg="#1b1c1e", fg="#e3e3e3", bar="#2b2d30", hover="#3a3d41", chk_bg="#2f4268",
            chk_fg="#a8c7fa", menu="#2b2d30", border="#444746", panel="#232426", pbg="#004a77",
            pfg="#c2e7ff", muted="#c4c7c5", input="#303134", handle="#444746", mhover="#3a3d41",
            dis="#6b6e72", btn="#2b2d30", bhover="#3a3d41")

STYLE_T = Template("""
QMainWindow, QDockWidget, QStatusBar, QDialog { background: $bg; color: $fg; }
QLabel { color: $fg; }
QToolBar { background: $bar; border: none; border-radius: 20px; margin: 4px 12px 2px 12px;
           padding: 3px 8px; spacing: 3px; }
QToolBar#fmt { margin: 0; border-radius: 0; background: $hover; }
QToolButton { border: none; border-radius: 6px; padding: 5px 8px; color: $fg; }
QToolButton:hover { background: $hover; }
QToolButton:checked { background: $chk_bg; color: $chk_fg; }
QToolButton:disabled { color: $dis; }
QMenuBar { background: transparent; color: $fg; }
QMenuBar::item { padding: 4px 8px; border-radius: 4px; }
QMenuBar::item:selected { background: $hover; }
QMenu { background: $menu; color: $fg; border: 1px solid $border; padding: 6px 0; }
QMenu::item { padding: 6px 30px 6px 24px; }
QMenu::item:selected { background: $mhover; }
QMenu::item:disabled { color: $dis; }
QLineEdit, QSpinBox, QComboBox { color: $fg; }
QLineEdit#title { border: 1px solid transparent; border-radius: 4px; font-size: 17px;
                  padding: 2px 6px; background: transparent; }
QLineEdit#title:hover { border-color: #747775; }
QLineEdit#title:focus { border: 2px solid $chk_fg; background: $input; }
QLineEdit#search { border: 1px solid $border; border-radius: 12px; padding: 3px 10px; background: $input; }
QComboBox { border: none; background: transparent; padding: 3px 6px; }
QComboBox QAbstractItemView { background: $menu; color: $fg; }
QDialog QLineEdit, QDialog QSpinBox, QDialog QComboBox { background: $input; border: 1px solid $border;
                                                        border-radius: 4px; padding: 3px 6px; }
QListWidget, QTreeWidget, QPlainTextEdit, QTextBrowser { background: $panel; color: $fg; border: none; }
QListWidget::item:selected { background: $chk_bg; color: $chk_fg; border-radius: 6px; }
QTabWidget::pane { border: none; }
QTabBar::tab { padding: 7px 14px; color: $muted; background: transparent; }
QTabBar::tab:selected { color: $chk_fg; border-bottom: 3px solid $chk_fg; }
QPushButton { background: $btn; color: $fg; border: 1px solid $border; border-radius: 16px; padding: 6px 14px; }
QPushButton:hover { background: $bhover; }
QPushButton#primary { background: $pbg; border: none; color: $pfg; font-weight: 600; padding: 8px 20px; }
QScrollArea { border: none; background: $bg; }
QSplitter::handle { background: $handle; }
QWidget#desk { background: $bg; }
QTextEdit#sheet { background: #ffffff; color: #000000; selection-background-color: #b3d4fc;
                  selection-color: #000000; }
""")


def build_style(dark: bool) -> str:
    return STYLE_T.substitute(DARK if dark else LIGHT)


def md_to_html(text: str) -> str:
    return markdown.markdown(text, extensions=MD_EXTENSIONS)


def md_to_pdf_bytes(text: str, margin: float = 54) -> bytes:
    """Render Markdown to a (multi-page) PDF with MuPDF's Story layout engine."""
    story = fitz.Story(html=md_to_html(text), user_css=PDF_CSS)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    where = LETTER + (margin, margin, -margin, -margin)
    more = True
    while more:
        dev = writer.begin_page(LETTER)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def pix_to_qpixmap(pix: fitz.Pixmap, invert=False) -> QPixmap:
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
    if invert:
        img = img.copy()
        img.invertPixels()
    return QPixmap.fromImage(img)  # copies the data


def qcolor_to_pdf(c: QColor):
    return (c.redF(), c.greenF(), c.blueF())


# --------------------------------------------------------------------------
# Pure PDF helpers (no GUI) - text editing, replace, compress, split, merge
# --------------------------------------------------------------------------
BASE14 = {"helv": ("helv", "hebo", "heit", "hebi"),
          "tiro": ("tiro", "tibo", "tiit", "tibi"),
          "cour": ("cour", "cobo", "coit", "cobi")}


def base14_font(flags: int, name: str) -> str:
    n = name.lower()
    bold = bool(flags & 16) or "bold" in n
    italic = bool(flags & 2) or "italic" in n or "oblique" in n
    fam = "cour" if (flags & 8 or "mono" in n or "courier" in n) else ("tiro" if flags & 4 else "helv")
    return BASE14[fam][int(bold) + 2 * int(italic)]


def background_color(page, rect):
    """Sample the page colour just outside the top-left of rect (so edits blend in)."""
    try:
        clip = fitz.Rect(rect.x0 - 3, rect.y0 - 3, rect.x0 - 1, rect.y0 - 1) & page.rect
        if clip.is_empty:
            return (1, 1, 1)
        pix = page.get_pixmap(clip=clip, dpi=72, alpha=False)
        r, g, b = pix.pixel(0, 0)[:3]
        return (r / 255, g / 255, b / 255)
    except Exception:
        return (1, 1, 1)


def apply_redact_keep_art(page):
    """Apply redactions but leave images and vector graphics alone."""
    try:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    except (TypeError, AttributeError):
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)


def span_style(page, rect):
    d = page.get_text("dict", clip=rect)
    for b in d["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                if fitz.Rect(s["bbox"]).intersects(rect):
                    return dict(font=base14_font(s["flags"], s["font"]), size=s["size"],
                                color=fitz.sRGB_to_pdf(s["color"]), y=s["origin"][1])
    return dict(font="helv", size=11, color=(0, 0, 0), y=rect.y1 - 2)


def line_at(page, pt):
    """Return (text, bbox, style) of the text line under pt, or None."""
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            if fitz.Rect(line["bbox"]).contains(pt) and line["spans"]:
                s0 = line["spans"][0]
                text = "".join(s["text"] for s in line["spans"])
                style = dict(font=base14_font(s0["flags"], s0["font"]), size=s0["size"],
                             color=fitz.sRGB_to_pdf(s0["color"]), x=s0["origin"][0], y=s0["origin"][1])
                return text, fitz.Rect(line["bbox"]), style
    return None


def rewrite_line(page, bbox, style, new_text):
    page.add_redact_annot(bbox, fill=background_color(page, bbox))
    apply_redact_keep_art(page)
    if new_text:
        page.insert_text(fitz.Point(style["x"], style["y"]), new_text, fontname=style["font"],
                         fontsize=style["size"], color=style["color"])


def count_matches(doc, find):
    return sum(len(p.search_for(find)) for p in doc)


def replace_text(doc, find, repl) -> int:
    total = 0
    for page in doc:
        rects = page.search_for(find)
        if not rects:
            continue
        work = [(r, span_style(page, r)) for r in rects]
        for r, _ in work:
            page.add_redact_annot(r, fill=background_color(page, r))
        apply_redact_keep_art(page)
        for r, st in work:
            page.insert_text(fitz.Point(r.x0, st["y"]), repl, fontname=st["font"],
                             fontsize=st["size"], color=st["color"])
        total += len(work)
    return total


COMPRESS_PRESETS = {"Light (150 dpi images, good quality)": (150, 80),
                    "Medium (110 dpi images)": (110, 60),
                    "Strong (72 dpi images, smallest file)": (72, 40)}


def compress_pdf_bytes(data: bytes, dpi: int, quality: int, tick=None) -> bytes:
    doc = fitz.open(stream=data, filetype="pdf")
    done = set()
    for page in doc:
        if tick:
            tick()
        for img in page.get_images(full=True):
            xref, smask = img[0], img[1]
            if xref in done or smask:
                continue
            done.add(xref)
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha or pix.colorspace is None:
                    continue
                rects = page.get_image_rects(xref)
                if not rects or rects[0].width <= 0:
                    continue
                eff_dpi = pix.width / (rects[0].width / 72)
                if eff_dpi <= dpi * 1.1:
                    continue
                if pix.colorspace.n >= 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                scale = dpi / eff_dpi
                small = fitz.Pixmap(pix, max(1, int(pix.width * scale)), max(1, int(pix.height * scale)))
                page.replace_image(xref, stream=small.tobytes("jpeg", jpg_quality=quality))
            except Exception:
                continue
    return doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True)


def parse_ranges(text: str, n: int):
    """'1-3, 5, 7-9' -> [(0,2),(4,4),(6,8)] (0-based, inclusive)."""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not m:
            raise ValueError(f"Can't read '{part}'. Use something like 1-3, 5, 7-9.")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1 <= a <= b <= n):
            raise ValueError(f"'{part}' is outside 1-{n}.")
        out.append((a - 1, b - 1))
    if not out:
        raise ValueError("Enter at least one range.")
    return out


def split_doc(doc, ranges, folder, base):
    paths = []
    for idx, (a, b) in enumerate(ranges, 1):
        out = fitz.open()
        out.insert_pdf(doc, from_page=a, to_page=b)
        p = os.path.join(folder, f"{base}_part{idx}.pdf")
        out.save(p, garbage=3, deflate=True)
        paths.append(p)
    return paths


def merge_paths(paths) -> bytes:
    out = fitz.open()
    for p in paths:
        with fitz.open(p) as d:
            out.insert_pdf(d)
    return out.tobytes()


def data_dir() -> Path:
    p = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    p.mkdir(parents=True, exist_ok=True)
    return p


def recovery_dir() -> Path:
    p = data_dir() / "recovery"
    p.mkdir(parents=True, exist_ok=True)
    return p


def drop_recovery(uid: str):
    for ext in (".pdf", ".psdoc", ".json"):
        try:
            (recovery_dir() / f"{uid}{ext}").unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------
# Signature pad dialog
# --------------------------------------------------------------------------
class SignaturePad(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(520, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.strokes = []

    def clear(self):
        self.strokes = []
        self.update()

    def mousePressEvent(self, e):
        self.strokes.append([e.position()])
        self.update()

    def mouseMoveEvent(self, e):
        if self.strokes and e.buttons() & Qt.MouseButton.LeftButton:
            self.strokes[-1].append(e.position())
            self.update()

    def _draw(self, p):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#111111"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        for s in self.strokes:
            if len(s) == 1:
                p.drawPoint(s[0])
            else:
                p.drawPolyline(QPolygonF(s))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("white"))
        p.setPen(QColor("#c7c7c7"))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.drawLine(20, 150, self.width() - 20, 150)
        self._draw(p)

    def png_bytes(self):
        pts = [pt for s in self.strokes for pt in s]
        if not pts:
            return None
        pad = 6
        x0, y0 = min(p.x() for p in pts) - pad, min(p.y() for p in pts) - pad
        x1, y1 = max(p.x() for p in pts) + pad, max(p.y() for p in pts) + pad
        scale = 3
        img = QImage(max(1, int((x1 - x0) * scale)), max(1, int((y1 - y0) * scale)), QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.scale(scale, scale)
        p.translate(-x0, -y0)
        self._draw(p)
        p.end()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())


class SignatureDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Draw your signature")
        lay = QVBoxLayout(self)
        self.pad = SignaturePad()
        lay.addWidget(self.pad)
        row = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.clicked.connect(self.pad.clear)
        row.addWidget(clear)
        row.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)


# --------------------------------------------------------------------------
# Merge / split / find-replace dialogs
# --------------------------------------------------------------------------
class MergeDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Merge PDFs")
        self.resize(520, 360)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Files are merged top to bottom. Drag to reorder."))
        self.list = QListWidget()
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        for label, fn in (("Add files...", self.add), ("Remove", self.remove),
                          ("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Merge")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add PDFs", "", "PDF files (*.pdf)")
        for p in paths:
            it = QListWidgetItem(os.path.basename(p))
            it.setData(Qt.ItemDataRole.UserRole, p)
            it.setToolTip(p)
            self.list.addItem(it)

    def remove(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def move(self, d):
        r = self.list.currentRow()
        if 0 <= r + d < self.list.count():
            it = self.list.takeItem(r)
            self.list.insertItem(r + d, it)
            self.list.setCurrentRow(r + d)

    def paths(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]


class SplitDialog(QDialog):
    def __init__(self, parent, n_pages):
        super().__init__(parent)
        self.setWindowTitle("Split PDF")
        form = QFormLayout(self)
        self.mode = QComboBox()
        self.mode.addItems(["Every N pages", "Custom ranges", "Each page as its own file"])
        self.param = QLineEdit("1")
        self.mode.currentIndexChanged.connect(self._hint)
        self.folder = QLineEdit()
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        frow = QHBoxLayout()
        frow.addWidget(self.folder)
        frow.addWidget(browse)
        form.addRow(QLabel(f"This document has {n_pages} pages."))
        form.addRow("Mode", self.mode)
        form.addRow("Value", self.param)
        form.addRow("Output folder", frow)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self._hint(0)

    def _hint(self, i):
        self.param.setEnabled(i != 2)
        self.param.setText("1" if i == 0 else ("1-3, 4-6" if i == 1 else ""))

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder")
        if d:
            self.folder.setText(d)


class ReplaceDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Find and replace")
        form = QFormLayout(self)
        self.find = QLineEdit()
        self.repl = QLineEdit()
        form.addRow("Find", self.find)
        form.addRow("Replace with", self.repl)
        form.addRow(QLabel("In opened PDFs the replacement uses a standard font,\nso styling can differ slightly from the original."))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Replace all")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


# --------------------------------------------------------------------------
# Continuous-scroll page canvas (pages on a grey desk, like Docs)
# --------------------------------------------------------------------------
POINT_TOOLS = {"comment", "form", "edit_text", "signature", "eraser"}
RECT_TOOLS = {"md", "highlight", "underline", "strike", "image", "redact", "rect", "ellipse"}
LINE_TOOLS = {"line", "arrow"}


class Canvas(QWidget):
    region = Signal(int, object)  # page index, Rect | Point | (Point, Point) | [Point...]
    zoom_step = Signal(int)
    MARGIN, GAP = 28, 18

    def __init__(self):
        super().__init__()
        self.doc = None
        self.zoom = 1.0
        self.tool = "hand"
        self.tops, self.sizes = [], []
        self.cache = OrderedDict()
        self.field_cache = {}
        self.hits = {}
        self.current_hit = None
        self.bg = QColor(LIGHT["bg"])
        self.invert = False
        self.pen_color = QColor("#d93025")
        self.pen_width = 2
        self.band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self.drag = None
        self.stroke = []
        self.live_line = None

    def set_doc(self, doc):
        self.doc = doc
        self.layout_pages()

    def layout_pages(self):
        self.cache.clear()
        self.field_cache.clear()
        self.tops, self.sizes = [], []
        y, maxw = self.MARGIN, 0
        if self.doc:
            m = fitz.Matrix(self.zoom, self.zoom)
            for page in self.doc:
                ir = (page.rect * m).irect
                self.tops.append(y)
                self.sizes.append((ir.width, ir.height))
                y += ir.height + self.GAP
                maxw = max(maxw, ir.width)
        self.setMinimumSize(maxw + 2 * self.MARGIN, y + self.MARGIN - self.GAP)
        self.update()

    def page_at_y(self, y):
        if not self.tops:
            return 0
        return max(0, min(bisect.bisect_right(self.tops, y) - 1, len(self.tops) - 1))

    def pixmap(self, i):
        if i in self.cache:
            self.cache.move_to_end(i)
            return self.cache[i]
        pix = self.doc[i].get_pixmap(matrix=fitz.Matrix(self.zoom, self.zoom), alpha=False)
        pm = pix_to_qpixmap(pix, self.invert)
        self.cache[i] = pm
        while len(self.cache) > 24:
            self.cache.popitem(last=False)
        return pm

    def fields(self, i):
        if i not in self.field_cache:
            try:
                self.field_cache[i] = [fitz.Rect(w.rect) for w in (self.doc[i].widgets() or [])]
            except Exception:
                self.field_cache[i] = []
        return self.field_cache[i]

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(ev.rect(), self.bg)
        if not self.doc:
            p.setPen(QColor("#80868b"))
            p.setFont(QFont("Segoe UI", 13))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Open a PDF (Ctrl+O), start a new document (Ctrl+N), or drop a file here")
            return
        vis = ev.rect()
        i = max(0, bisect.bisect_right(self.tops, vis.top()) - 1)
        while i < len(self.tops) and self.tops[i] <= vis.bottom():
            w, h = self.sizes[i]
            r = QRect((self.width() - w) // 2, self.tops[i], w, h)
            if r.intersects(vis):
                p.fillRect(r.translated(2, 3), QColor(0, 0, 0, 45))
                p.drawPixmap(r.topLeft(), self.pixmap(i))
                p.setPen(QColor("#dadce0"))
                p.drawRect(r.adjusted(0, 0, -1, -1))
                rot = self.doc[i].rotation_matrix
                p.setPen(Qt.PenStyle.NoPen)
                for rect in self.hits.get(i, []):
                    cur = self.current_hit == (i, rect)
                    p.setBrush(QColor(255, 150, 0, 130) if cur else QColor(255, 230, 0, 110))
                    self._draw_rect(p, r, rect * rot)
                if self.tool == "form":
                    p.setBrush(QColor(26, 115, 232, 45))
                    p.setPen(QPen(QColor(26, 115, 232), 1))
                    for rect in self.fields(i):
                        self._draw_rect(p, r, fitz.Rect(rect) * rot)
            i += 1
        # live overlays for the pen and line tools
        pen = QPen(self.pen_color, max(1.0, self.pen_width * self.zoom), Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        if len(self.stroke) > 1:
            p.drawPolyline(QPolygonF([QPointF(pt) for pt in self.stroke]))
        if self.live_line:
            p.drawLine(*self.live_line)

    def _draw_rect(self, p, page_rect, rr):
        p.drawRect(QRect(page_rect.x() + int(rr.x0 * self.zoom), page_rect.y() + int(rr.y0 * self.zoom),
                         int(rr.width * self.zoom) + 1, int(rr.height * self.zoom) + 1))

    # ----- mouse -----
    def _locate(self, pos, page=None):
        if not self.tops:
            return None
        i = page if page is not None else max(0, bisect.bisect_right(self.tops, pos.y()) - 1)
        w, h = self.sizes[i]
        origin = QPoint((self.width() - w) // 2, self.tops[i])
        local = pos - origin
        return i, QPoint(max(0, min(local.x(), w)), max(0, min(local.y(), h)))

    def _to_pdf(self, i, pt):
        return fitz.Point(pt.x() / self.zoom, pt.y() / self.zoom) * self.doc[i].derotation_matrix

    def mousePressEvent(self, e):
        if self.tool == "hand" or not self.doc or e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position().toPoint()
        loc = self._locate(pos)
        if not loc:
            return
        i, local = loc
        if self.tool in POINT_TOOLS:
            self.region.emit(i, self._to_pdf(i, local))
            return
        self.drag = (i, pos)
        if self.tool == "pen":
            self.stroke = [pos]
        elif self.tool in RECT_TOOLS:
            self.band.setGeometry(QRect(pos, QSize()))
            self.band.show()

    def mouseMoveEvent(self, e):
        if not self.drag:
            return
        pos = e.position().toPoint()
        if self.tool == "pen":
            self.stroke.append(pos)
            self.update()
        elif self.tool in LINE_TOOLS:
            self.live_line = (self.drag[1], pos)
            self.update()
        else:
            self.band.setGeometry(QRect(self.drag[1], pos).normalized())

    def mouseReleaseEvent(self, e):
        if not self.drag:
            return
        i, start = self.drag
        self.drag = None
        self.band.hide()
        end = e.position().toPoint()
        stroke, self.stroke, self.live_line = self.stroke, [], None
        self.update()
        a = self._locate(start, i)[1]
        b = self._locate(end, i)[1]
        if self.tool == "pen":
            if len(stroke) > 2:
                self.region.emit(i, [self._to_pdf(i, self._locate(pt, i)[1]) for pt in stroke])
        elif abs(a.x() - b.x()) + abs(a.y() - b.y()) > 10:
            p1, p2 = self._to_pdf(i, a), self._to_pdf(i, b)
            if self.tool in LINE_TOOLS:
                self.region.emit(i, (p1, p2))
            elif abs(a.x() - b.x()) > 8 and abs(a.y() - b.y()) > 8:
                self.region.emit(i, fitz.Rect(p1, p2).normalize())

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_step.emit(1 if e.angleDelta().y() > 0 else -1)
            e.accept()
        else:
            e.ignore()


# --------------------------------------------------------------------------
# Per-tab document state
# --------------------------------------------------------------------------
class DocState:
    def __init__(self, doc, path=None, dirty=False, uid=None, title=None):
        self.doc, self.path, self.dirty = doc, path, dirty
        self.kind, self.qdoc = "pdf", None
        self.undo, self.redo = [], []
        self.cur, self.zoom, self.scroll = 0, None, 0
        self.uid = uid or uuid.uuid4().hex
        self.title = title or (os.path.splitext(os.path.basename(path))[0] if path else "Untitled document")


TOOLS = [
    ("hand", "\u2196 Select", "Browse the document"),
    ("md", "\U0001F4DD Text box", "Drag a box, then type the text (Markdown allowed)"),
    ("edit_text", "\u270F Edit text", "Click a line of text to rewrite it in place"),
    ("highlight", "\U0001F58D Highlight", "Drag across text to highlight it"),
    ("underline", "Underline", "Drag across text to underline it"),
    ("strike", "Strikethrough", "Drag across text to strike it out"),
    ("image", "\U0001F5BC Image", "Drag a box, then choose an image"),
    ("comment", "\U0001F4AC Comment", "Click on the page to add a sticky-note comment"),
    ("form", "\U0001F4CB Fill form", "Click a (highlighted) form field to fill it in"),
    ("signature", "\u270D Sign", "Click where your signature should go"),
    ("pen", "\U0001F58A Pen", "Draw freehand"),
    ("line", "\u2571 Line", "Drag to draw a line"),
    ("arrow", "\u279C Arrow", "Drag to draw an arrow"),
    ("rect", "\u25AD Rectangle", "Drag to draw a rectangle outline"),
    ("ellipse", "\u25EF Ellipse", "Drag to draw an ellipse outline"),
    ("eraser", "\U0001F9FD Eraser", "Click an annotation (highlight, shape, note...) to delete it"),
    ("redact", "\u2B1B Redact", "Drag a box to permanently remove its content"),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("PDFStudio", "PDFStudio")
        self.dark = self.settings.value("dark", False, type=bool)
        self.autosave_to_file = self.settings.value("autosave_file", False, type=bool)
        # active-tab state (swapped when you change tabs)
        self.kind = "pdf"  # "doc" = a page you type on, "pdf" = an opened PDF
        self.qdoc = None
        self.doc = None
        self.path = None
        self.cur = 0
        self.zoom = 1.0
        self.dirty = False
        self.undo_stack, self.redo_stack = [], []
        self.states, self.active, self._busy = [], -1, False
        self.tool = "hand"
        self.matches, self.match_i, self.last_query = [], -1, ""
        self.thumb_queue = []
        self.pen_color = QColor("#d93025")
        self.pen_width = 2
        self.text_color = QColor("#000000")
        self.hl_color = QColor("#fff475")
        self.sig_png = None
        sig = data_dir() / "signature.png"
        if sig.exists():
            self.sig_png = sig.read_bytes()
        self.doc_actions, self.pdf_actions, self.tb_btns = [], [], {}
        self.setAcceptDrops(True)

        # --- opened-PDF canvas ---
        self.canvas = Canvas()
        self.canvas.region.connect(self.on_region)
        self.canvas.zoom_step.connect(lambda s: self.set_zoom(self.zoom * (1.1 if s > 0 else 1 / 1.1)))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.canvas)
        self.scroll.verticalScrollBar().valueChanged.connect(self.on_scroll)

        # --- the page you type on ---
        self.editor = PageEditor()
        self.editor.cursorPositionChanged.connect(self.on_editor_cursor)
        self.editor.currentCharFormatChanged.connect(lambda _f: self.sync_format_ui())
        self.editor.textChanged.connect(self.on_editor_text)
        self.editor.undoAvailable.connect(lambda _v: self.update_status())
        self.editor.redoAvailable.connect(lambda _v: self.update_status())
        desk = QWidget()
        desk.setObjectName("desk")
        dl = QHBoxLayout(desk)
        dl.setContentsMargins(0, 14, 0, 0)
        dl.addStretch()
        dl.addWidget(self.editor)
        dl.addStretch()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.scroll)
        self.stack.addWidget(desk)

        self.tabbar = QTabBar()
        self.tabbar.setTabsClosable(True)
        self.tabbar.setExpanding(False)
        self.tabbar.setDrawBase(False)
        self.tabbar.currentChanged.connect(self.on_tab_changed)
        self.tabbar.tabCloseRequested.connect(self.close_tab)
        plus = QToolButton()
        plus.setText("+")
        plus.setToolTip("New document (Ctrl+N)")
        plus.clicked.connect(self.new_document)
        trow = QHBoxLayout()
        trow.setContentsMargins(12, 0, 12, 0)
        trow.addWidget(self.tabbar)
        trow.addWidget(plus)
        trow.addStretch()
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addLayout(trow)
        cl.addWidget(self.stack)
        self.setCentralWidget(center)

        self.thumb_timer = QTimer(self)
        self.thumb_timer.setInterval(4)
        self.thumb_timer.timeout.connect(self.thumb_tick)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setInterval(AUTOSAVE_MS)
        self.autosave_timer.timeout.connect(self.autosave_tick)
        self.autosave_timer.start()
        self.words_timer = QTimer(self)
        self.words_timer.setSingleShot(True)
        self.words_timer.setInterval(300)
        self.words_timer.timeout.connect(self.update_word_count)
        self.outline_timer = QTimer(self)
        self.outline_timer.setSingleShot(True)
        self.outline_timer.setInterval(500)
        self.outline_timer.timeout.connect(self.on_outline_timer)

        self.build_left_dock()
        self.build_menus()
        self.build_header()
        self.build_toolbars()
        self.build_statusbar()
        self.left_dock.hide()
        self.apply_theme()
        self.resize(1400, 920)
        self.set_kind_ui()
        self.update_status()

    def build_left_dock(self):
        self.thumbs = QListWidget()
        self.thumbs.setIconSize(QSize(100, 130))
        self.thumbs.setSpacing(4)
        self.thumbs.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.thumbs.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.thumbs.currentRowChanged.connect(lambda r: r >= 0 and r != self.cur and self.goto(r))
        self.thumbs.model().rowsMoved.connect(self.on_thumb_moved)
        self.thumbs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.thumbs.customContextMenuRequested.connect(self.thumb_menu)
        self.outline = QTreeWidget()
        self.outline.setHeaderHidden(True)
        self.outline.itemClicked.connect(self.on_outline_click)
        self.left_tabs = QTabWidget()
        self.left_tabs.addTab(self.thumbs, "Pages")
        self.left_tabs.addTab(self.outline, "Outline")
        self.left_dock = QDockWidget("Outline", self)
        self.left_dock.setWidget(self.left_tabs)
        self.left_dock.setTitleBarWidget(QWidget())
        self.left_dock.setMinimumWidth(190)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.left_dock)
        blank = QPixmap(100, 130)
        blank.fill(QColor("white"))
        self.blank_icon = QIcon(blank)

    def mk(self, menu, text, fn, key=None, checkable=False, checked=False, scope=None):
        a = QAction(text, self)
        if key:
            a.setShortcut(QKeySequence(key))
        if checkable:
            a.setCheckable(True)
            a.setChecked(checked)
            a.triggered.connect(lambda c=False: fn(c))
        else:
            a.triggered.connect(lambda _=False: fn())
        if menu is not None:
            menu.addAction(a)
        self.addAction(a)
        if scope == "doc":
            self.doc_actions.append(a)
        elif scope == "pdf":
            self.pdf_actions.append(a)
        return a

    def build_menus(self):
        self.menubar = QMenuBar()
        mb = self.menubar
        A = Qt.AlignmentFlag

        f = mb.addMenu("File")
        self.mk(f, "New document", self.new_document, "Ctrl+N")
        self.mk(f, "Open...", self.open_pdf, "Ctrl+O")
        self.mk(f, "Close tab", lambda: self.close_tab(self.active), "Ctrl+W")
        f.addSeparator()
        self.mk(f, "Save", self.save, "Ctrl+S")
        self.mk(f, "Save as...", self.save_as, "Ctrl+Shift+S")
        self.mk(f, "Auto-save to the original file", self.set_autosave_file, None, True, self.autosave_to_file)
        f.addSeparator()
        self.mk(f, "Import Markdown file...", self.import_markdown)
        dl = f.addMenu("Download as")
        self.mk(dl, "Markdown (.md)", self.export_markdown, scope="doc")
        self.mk(dl, "Plain text (.txt)", self.export_text)
        self.mk(dl, "Current page as PNG", self.export_png)
        self.mk(f, "Save encrypted copy...", self.save_encrypted)
        f.addSeparator()
        self.mk(f, "Print...", self.print_doc, "Ctrl+P")
        self.mk(f, "Document properties...", self.doc_properties, scope="pdf")
        f.addSeparator()
        self.mk(f, "Exit", self.close, "Ctrl+Q")

        e = mb.addMenu("Edit")
        self.undo_a = self.mk(e, "Undo", self.undo, "Ctrl+Z")
        self.redo_a = self.mk(e, "Redo", self.redo)
        self.redo_a.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        e.addSeparator()
        self.mk(e, "Cut", lambda: self.editor.cut(), scope="doc")
        self.mk(e, "Copy", lambda: self.editor.copy(), scope="doc")
        self.mk(e, "Paste", lambda: self.editor.paste(), scope="doc")
        self.mk(e, "Select all", lambda: self.editor.selectAll(), scope="doc")
        self.mk(e, "Paste from Markdown", self.paste_markdown, "Ctrl+Shift+M", scope="doc")
        self.mk(e, "Copy as Markdown", self.copy_markdown, scope="doc")
        e.addSeparator()
        self.mk(e, "Find...", self.focus_search, "Ctrl+F")
        self.mk(e, "Find and replace...", self.find_replace, "Ctrl+H")

        v = mb.addMenu("View")
        self.mk(v, "Zoom in", lambda: self.set_zoom(self.zoom * 1.2), "Ctrl+=", scope="pdf")
        self.mk(v, "Zoom out", lambda: self.set_zoom(self.zoom / 1.2), "Ctrl+-", scope="pdf")
        self.mk(v, "Fit to width", self.fit_width, "Ctrl+Shift+0", scope="pdf")
        v.addSeparator()
        self.mk(v, "Dark mode", self.set_dark, None, True, self.dark)
        self.mk(v, "Dark pages (invert PDF colors)", self.set_invert, None, True, False, scope="pdf")
        self.mk(v, "Show page-break guides", self.set_show_breaks, None, True, True, scope="doc")
        self.mk(v, "Show outline / pages panel", self.toggle_left_dock, "Ctrl+Alt+H")

        ins = mb.addMenu("Insert")
        self.mk(ins, "Image...", self.insert_image_dialog)
        self.mk(ins, "Table...", self.insert_table_dialog, scope="doc")
        self.mk(ins, "Horizontal line", self.insert_hr, scope="doc")
        self.mk(ins, "Link...", lambda: self.editor.insert_link(), "Ctrl+K", scope="doc")
        ins.addSeparator()
        self.mk(ins, "Blank page", self.add_blank_page, scope="pdf")
        self.mk(ins, "Pages from another PDF...", self.merge_into_current, scope="pdf")
        self.mk(ins, "Page numbers", self.add_page_numbers, scope="pdf")
        self.mk(ins, "Watermark...", self.add_watermark, scope="pdf")
        self.mk(ins, "My signature...", self.draw_signature, scope="pdf")

        fm = mb.addMenu("Format")
        tm = fm.addMenu("Text")
        self.act_bold = self.mk(tm, "Bold", lambda on: self.editor.bold(on), "Ctrl+B", True, scope="doc")
        self.act_italic = self.mk(tm, "Italic", lambda on: self.editor.italic(on), "Ctrl+I", True, scope="doc")
        self.act_underline = self.mk(tm, "Underline", lambda on: self.editor.underline(on), "Ctrl+U", True,
                                     scope="doc")
        self.act_strike = self.mk(tm, "Strikethrough", lambda on: self.editor.strike(on), "Alt+Shift+5", True,
                                  scope="doc")
        self.act_sup = self.mk(tm, "Superscript", lambda on: self.editor.script("sup" if on else "normal"),
                               "Ctrl+.", True, scope="doc")
        self.act_sub = self.mk(tm, "Subscript", lambda on: self.editor.script("sub" if on else "normal"),
                               "Ctrl+,", True, scope="doc")
        sm = fm.addMenu("Paragraph styles")
        for name, key in (("Normal text", "Ctrl+Alt+0"), ("Title", None), ("Subtitle", None),
                          ("Heading 1", "Ctrl+Alt+1"), ("Heading 2", "Ctrl+Alt+2"), ("Heading 3", "Ctrl+Alt+3")):
            self.mk(sm, name, lambda n=name: self.editor.apply_style(n), key, scope="doc")
        am = fm.addMenu("Align")
        self.align_group = QActionGroup(self)
        self.align_acts = {}
        for key, label, sc, flag in (("left", "Left", "Ctrl+Shift+L", A.AlignLeft),
                                     ("center", "Center", "Ctrl+Shift+E", A.AlignHCenter),
                                     ("right", "Right", "Ctrl+Shift+R", A.AlignRight),
                                     ("justify", "Justify", "Ctrl+Shift+J", A.AlignJustify)):
            a = self.mk(am, label, lambda on, fl=flag: self.editor.setAlignment(fl), sc, True, scope="doc")
            self.align_group.addAction(a)
            self.align_acts[key] = a
        lsm = fm.addMenu("Line spacing")
        for factor in (1.0, 1.15, 1.5, 2.0):
            self.mk(lsm, f"{factor:g}", lambda fc=factor: self.editor.set_line_spacing(fc), scope="doc")
        lm = fm.addMenu("Bullets and numbering")
        self.act_num = self.mk(lm, "Numbered list",
                               lambda on: self.editor.toggle_list(QTextListFormat.Style.ListDecimal),
                               "Ctrl+Shift+7", True, scope="doc")
        self.act_bul = self.mk(lm, "Bulleted list",
                               lambda on: self.editor.toggle_list(QTextListFormat.Style.ListDisc),
                               "Ctrl+Shift+8", True, scope="doc")
        self.mk(fm, "Increase indent", lambda: self.editor.change_indent(1), "Ctrl+]", scope="doc")
        self.mk(fm, "Decrease indent", lambda: self.editor.change_indent(-1), "Ctrl+[", scope="doc")
        fm.addSeparator()
        self.mk(fm, "Clear formatting", lambda: self.editor.clear_formatting(), "Ctrl+\\", scope="doc")
        self.mk(fm, "Auto-format Markdown while typing", self.set_md_auto, None, True, True)

        pg = mb.addMenu("Page")
        self.mk(pg, "Rotate 90\u00b0", self.rotate_page, scope="pdf")
        self.mk(pg, "Move up", lambda: self.move_page(-1), scope="pdf")
        self.mk(pg, "Move down", lambda: self.move_page(1), scope="pdf")
        self.mk(pg, "Duplicate", self.duplicate_page, scope="pdf")
        self.mk(pg, "Delete", self.delete_page, scope="pdf")
        pg.addSeparator()
        self.mk(pg, "Previous page", lambda: self.goto(self.cur - 1), "PgUp", scope="pdf")
        self.mk(pg, "Next page", lambda: self.goto(self.cur + 1), "PgDown", scope="pdf")

        tl = mb.addMenu("Tools")
        self.mk(tl, "Word count", self.word_count_dialog, "Ctrl+Shift+C", scope="doc")
        self.mk(tl, "Mark up as PDF (annotate, sign, redact)", self.mark_up_as_pdf, scope="doc")
        self.mk(tl, "Edit this PDF as a document", self.edit_as_document, scope="pdf")
        tl.addSeparator()
        self.mk(tl, "Merge PDFs...", self.merge_dialog)
        self.mk(tl, "Split this PDF...", self.split_pdf)
        self.mk(tl, "Compress this PDF...", self.compress_pdf)
        self.mk(tl, "Make scanned pages searchable (OCR)...", self.ocr_document, scope="pdf")
        tl.addSeparator()
        self.tool_group = QActionGroup(self)
        self.tool_actions = {}
        for name, label, tip in TOOLS:
            a = QAction(label, self, checkable=True)
            a.setToolTip(tip)
            a.setChecked(name == "hand")
            a.triggered.connect(lambda _=False, n=name: self.set_tool(n))
            self.tool_group.addAction(a)
            self.tool_actions[name] = a
            self.pdf_actions.append(a)
            tl.addAction(a)

    def build_header(self):
        header = QWidget()
        lay = QHBoxLayout(header)
        lay.setContentsMargins(16, 8, 16, 0)
        icon = QLabel("\U0001F4C4")
        icon.setStyleSheet("font-size: 30px;")
        self.title_edit = QLineEdit("Untitled document")
        self.title_edit.setObjectName("title")
        self.title_edit.setMaximumWidth(420)
        self.saved_label = QLabel("")
        self.saved_label.setStyleSheet("color: #80868b; padding-left: 8px;")
        top = QHBoxLayout()
        top.addWidget(self.title_edit)
        top.addWidget(self.saved_label)
        top.addStretch()
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addLayout(top)
        col.addWidget(self.menubar)
        lay.addWidget(icon)
        lay.addLayout(col, 1)
        btn = QPushButton("Save")
        btn.setObjectName("primary")
        btn.clicked.connect(self.save)
        lay.addWidget(btn)
        self.setMenuWidget(header)

    def build_toolbars(self):
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        self.tb_undo = tb.addAction("\u21B6", self.undo)
        self.tb_undo.setToolTip("Undo (Ctrl+Z)")
        self.tb_redo = tb.addAction("\u21B7", self.redo)
        self.tb_redo.setToolTip("Redo (Ctrl+Y)")
        tb.addAction("\U0001F5A8", self.print_doc).setToolTip("Print (Ctrl+P)")
        tb.addSeparator()
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.addItems(["Fit", "50%", "75%", "100%", "125%", "150%", "200%", "300%"])
        self.zoom_box.setFixedWidth(84)
        self.zoom_box.activated.connect(lambda _i: self.zoom_from_text(self.zoom_box.currentText()))
        self.zoom_box.lineEdit().returnPressed.connect(lambda: self.zoom_from_text(self.zoom_box.currentText()))
        tb.addWidget(self.zoom_box)
        tb.addSeparator()
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Find in document")
        self.search.setMaximumWidth(240)
        self.search.returnPressed.connect(self.run_search)
        self.search.textChanged.connect(lambda _t: setattr(self, "last_query", ""))
        tb.addWidget(self.search)

        # ---- second row, typed page: Google-Docs-style formatting bar ----
        self.addToolBarBreak()
        self.tb_fmt = QToolBar()
        self.tb_fmt.setMovable(False)
        self.addToolBar(self.tb_fmt)
        t = self.tb_fmt
        self.style_box = QComboBox()
        self.style_box.addItems(list(STYLES))
        self.style_box.setFixedWidth(128)
        self.style_box.setToolTip("Paragraph style")
        self.style_box.activated.connect(lambda _i: self.editor.apply_style(self.style_box.currentText()))
        t.addWidget(self.style_box)
        self.font_box = QFontComboBox()
        self.font_box.setFixedWidth(150)
        self.font_box.setCurrentFont(QFont("Arial"))
        self.font_box.setToolTip("Font")
        self.font_box.activated.connect(lambda _i: self.editor.set_family(self.font_box.currentFont().family()))
        t.addWidget(self.font_box)
        self.size_box = QComboBox()
        self.size_box.setEditable(True)
        self.size_box.addItems([str(s) for s in (8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36, 48, 72)])
        self.size_box.setEditText("11")
        self.size_box.setFixedWidth(64)
        self.size_box.setToolTip("Font size")
        self.size_box.activated.connect(lambda _i: self.apply_size_text(self.size_box.currentText()))
        self.size_box.lineEdit().returnPressed.connect(lambda: self.apply_size_text(self.size_box.currentText()))
        t.addWidget(self.size_box)
        t.addSeparator()

        def tbtn(key, text, tip, fn, checkable=False, css=""):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            if css:
                b.setStyleSheet(css)
            if checkable:
                b.setCheckable(True)
                b.clicked.connect(lambda on=False: fn(on))
            else:
                b.clicked.connect(lambda _=False: fn())
            t.addWidget(b)
            self.tb_btns[key] = b
            return b

        tbtn("bold", "B", "Bold (Ctrl+B)", self.editor.bold, True, "font-weight: bold;")
        tbtn("italic", "I", "Italic (Ctrl+I)", self.editor.italic, True, "font-style: italic;")
        tbtn("underline", "U", "Underline (Ctrl+U)", self.editor.underline, True, "text-decoration: underline;")
        tbtn("strike", "S", "Strikethrough (Alt+Shift+5)", self.editor.strike, True,
             "text-decoration: line-through;")
        t.addSeparator()
        self.text_btn = self.color_button("Text color", TEXT_COLORS, self.apply_text_color, self.apply_text_color)
        self.hl_btn = self.color_button("Highlight color", HL_COLORS, self.click_highlight, self.pick_highlight,
                                        allow_none=True)
        t.addSeparator()
        tbtn("link", "\U0001F517", "Insert link (Ctrl+K)", self.editor.insert_link)
        tbtn("image", "\U0001F5BC", "Insert image (you can also paste one)", self.insert_image_dialog)
        t.addSeparator()
        A = Qt.AlignmentFlag
        for key, glyph, tip, flag in (("al_left", "L", "Align left (Ctrl+Shift+L)", A.AlignLeft),
                                      ("al_center", "C", "Center (Ctrl+Shift+E)", A.AlignHCenter),
                                      ("al_right", "R", "Align right (Ctrl+Shift+R)", A.AlignRight),
                                      ("al_justify", "J", "Justify (Ctrl+Shift+J)", A.AlignJustify)):
            tbtn(key, glyph, tip, lambda fl=flag: self.editor.setAlignment(fl), False)
        t.addSeparator()
        tbtn("bullet", "\u2022", "Bulleted list (Ctrl+Shift+8)",
             lambda on: self.editor.toggle_list(QTextListFormat.Style.ListDisc), True)
        tbtn("number", "1.", "Numbered list (Ctrl+Shift+7)",
             lambda on: self.editor.toggle_list(QTextListFormat.Style.ListDecimal), True)
        tbtn("outdent", "\u21E4", "Decrease indent (Ctrl+[)", lambda: self.editor.change_indent(-1))
        tbtn("indent", "\u21E5", "Increase indent (Ctrl+])", lambda: self.editor.change_indent(1))
        t.addSeparator()
        tbtn("clear", "Tx", "Clear formatting (Ctrl+\\)", self.editor.clear_formatting)

        # ---- second row, opened PDF: markup tools ----
        self.tb_tools = QToolBar()
        self.tb_tools.setMovable(False)
        self.addToolBar(self.tb_tools)
        for n in ("hand", "md", "edit_text", "highlight", "image", "comment", "form", "signature",
                  "pen", "line", "arrow", "rect", "ellipse", "eraser", "redact"):
            self.tb_tools.addAction(self.tool_actions[n])
        self.tb_tools.addSeparator()
        self.color_act = self.tb_tools.addAction("", self.pick_color)
        self.color_act.setToolTip("Color for pen, lines, and shapes")
        self.update_color_icon()
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 24)
        self.width_spin.setValue(self.pen_width)
        self.width_spin.setSuffix(" px")
        self.width_spin.setToolTip("Stroke width")
        self.width_spin.valueChanged.connect(self.set_pen_width)
        self.tb_tools.addWidget(self.width_spin)

    def build_statusbar(self):
        self.words_label = QLabel("")
        self.page_word = QLabel("Page")
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(lambda v: self.goto(v - 1))
        self.page_total = QLabel("")
        sb = self.statusBar()
        sb.addPermanentWidget(self.words_label)
        sb.addPermanentWidget(self.page_word)
        sb.addPermanentWidget(self.page_spin)
        sb.addPermanentWidget(self.page_total)

    def apply_theme(self):
        self.setStyleSheet(build_style(self.dark))
        pal = DARK if self.dark else LIGHT
        self.canvas.bg = QColor(pal["bg"])
        self.canvas.update()
        self.update_color_icon()
        self.update_text_icons()

    def set_dark(self, on):
        self.dark = on
        self.settings.setValue("dark", on)
        self.apply_theme()

    def set_invert(self, on):
        self.canvas.invert = on
        self.canvas.layout_pages()

    def set_autosave_file(self, on):
        self.autosave_to_file = on
        self.settings.setValue("autosave_file", on)

    def update_color_icon(self):
        pm = QPixmap(18, 18)
        pm.fill(self.pen_color)
        self.color_act.setIcon(QIcon(pm))
        self.canvas.pen_color = self.pen_color

    def pick_color(self):
        c = QColorDialog.getColor(self.pen_color, self, "Pen / shape color")
        if c.isValid():
            self.pen_color = c
            self.update_color_icon()

    def set_pen_width(self, v):
        self.pen_width = v
        self.canvas.pen_width = v

    # ===================== document / tab state =====================
    def require_doc(self) -> bool:
        """For tools that only make sense on an opened PDF."""
        if self.kind == "doc":
            self.statusBar().showMessage(
                "That tool is for opened PDFs. Use Tools > Mark up as PDF to annotate this document.", 6000)
            return False
        if self.doc is None:
            self.statusBar().showMessage("Open a PDF first (Ctrl+O).", 4000)
            return False
        return True

    def require_any(self) -> bool:
        if self.kind == "doc" and self.qdoc is not None:
            return True
        if self.kind == "pdf" and self.doc is not None:
            return True
        self.statusBar().showMessage("Nothing is open.", 3000)
        return False

    def fitz_view(self):
        """The current tab as a PyMuPDF document (renders typed pages on the fly)."""
        if self.kind == "doc":
            return fitz.open(stream=export_pdf_bytes(self.qdoc, self.title_edit.text().strip(), embed=False),
                             filetype="pdf")
        return self.doc

    def stash(self):
        if 0 <= self.active < len(self.states):
            s = self.states[self.active]
            s.kind, s.doc, s.qdoc, s.path, s.dirty = self.kind, self.doc, self.qdoc, self.path, self.dirty
            s.undo, s.redo, s.cur, s.zoom = self.undo_stack, self.redo_stack, self.cur, self.zoom
            bar = self.editor.verticalScrollBar() if self.kind == "doc" else self.scroll.verticalScrollBar()
            s.scroll = bar.value()
            s.title = self.title_edit.text()

    def restore(self, s):
        self.kind, self.doc, self.qdoc = s.kind, s.doc, s.qdoc
        self.path, self.dirty = s.path, s.dirty
        self.undo_stack, self.redo_stack, self.cur = s.undo, s.redo, s.cur
        self.zoom = s.zoom or 1.0
        self.title_edit.setText(s.title)
        self.set_kind_ui()
        if self.kind == "doc":
            self.editor.setDocument(self.qdoc)
            self.rebuild_thumbs()
            self.refresh_doc_ui()
            QTimer.singleShot(0, lambda: self.editor.verticalScrollBar().setValue(s.scroll))
        else:
            self.canvas.zoom = self.zoom
            self.refresh()
            self.zoom_box.setEditText(f"{round(self.zoom * 100)}%")
            QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(s.scroll))

    def clear_active(self):
        self.kind, self.qdoc = "pdf", None
        self.doc, self.path, self.dirty = None, None, False
        self.undo_stack, self.redo_stack, self.cur = [], [], 0
        self.title_edit.setText("Untitled document")
        self.set_kind_ui()
        self.refresh()

    def open_in_tab(self, data: bytes, path=None, dirty=False, uid=None, title=None):
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            pw, ok = QInputDialog.getText(self, "Password", "This PDF is encrypted. Password:",
                                          QLineEdit.EchoMode.Password)
            if not ok or not doc.authenticate(pw):
                QMessageBox.warning(self, APP_NAME, "Could not unlock the PDF.")
                return
        self.stash()
        s = DocState(doc, path, dirty, uid, title)
        self.states.append(s)
        self._busy = True
        idx = self.tabbar.addTab(s.title)
        self.tabbar.setCurrentIndex(idx)
        self._busy = False
        self.active = idx
        self.restore(s)
        self.fit_width()
        if doc.is_form_pdf:
            self.statusBar().showMessage("This PDF has form fields. Use the Fill form tool to complete them.", 8000)

    def on_tab_changed(self, i):
        if self._busy:
            return
        self.stash()
        self.active = i
        if 0 <= i < len(self.states):
            self.restore(self.states[i])
        else:
            self.clear_active()

    def close_tab(self, i):
        if not (0 <= i < len(self.states)):
            return
        if self.tabbar.currentIndex() != i:
            self.tabbar.setCurrentIndex(i)
        if not self.maybe_discard():
            return
        drop_recovery(self.states[i].uid)
        self._busy = True
        self.states.pop(i)
        self.tabbar.removeTab(i)
        self._busy = False
        if self.states:
            idx = min(i, len(self.states) - 1)
            self._busy = True
            self.tabbar.setCurrentIndex(idx)
            self._busy = False
            self.active = idx
            self.restore(self.states[idx])
        else:
            self.active = -1
            self.clear_active()
            self.new_document()

    def refresh(self, changed=None):
        """Re-lay-out the PDF canvas; rebuild thumbnails (all, or only `changed` pages)."""
        if self.kind == "doc":
            self.update_status()
            return
        self.cur = max(0, min(self.cur, len(self.doc) - 1)) if self.doc else 0
        self.canvas.set_doc(self.doc)
        self.matches, self.last_query = [], ""
        self.canvas.hits, self.canvas.current_hit = {}, None
        if changed is None:
            self.rebuild_thumbs()
            self.rebuild_outline()
        else:
            self.thumb_queue = list(changed) + self.thumb_queue
            self.thumb_timer.start()
        self.update_status()

    def update_status(self):
        is_doc = self.kind == "doc"
        has = (self.qdoc is not None) if is_doc else (self.doc is not None)
        name = os.path.basename(self.path) if self.path else (self.title_edit.text() or "Untitled")
        mark = " \u2022" if self.dirty else ""
        self.setWindowTitle(f"{name}{mark} - {APP_NAME}")
        self.saved_label.setText("Unsaved changes" if self.dirty else ("Saved" if has else ""))
        if 0 <= self.active < self.tabbar.count():
            self.tabbar.setTabText(self.active, f"{name}{mark}")
            self.tabbar.setTabToolTip(self.active, self.path or "Not saved yet")
        self.update_nav()
        if is_doc and self.qdoc is not None:
            can_undo, can_redo = self.qdoc.isUndoAvailable(), self.qdoc.isRedoAvailable()
        else:
            can_undo, can_redo = bool(self.undo_stack), bool(self.redo_stack)
        for a in (self.undo_a, self.tb_undo):
            a.setEnabled(can_undo)
        for a in (self.redo_a, self.tb_redo):
            a.setEnabled(can_redo)

    def update_nav(self):
        if self.kind == "doc":
            self.page_word.setVisible(False)
            self.page_spin.setVisible(False)
            if self.qdoc is not None:
                p, n = self.doc_page_info()
                self.page_total.setText(f"Page {p} of {n}")
            else:
                self.page_total.setText("")
            return
        self.page_word.setVisible(True)
        self.page_spin.setVisible(True)
        n = len(self.doc) if self.doc else 0
        self.page_total.setText(f"of {n}")
        self.page_spin.blockSignals(True)
        self.page_spin.setRange(1, max(n, 1))
        self.page_spin.setValue(self.cur + 1)
        self.page_spin.blockSignals(False)
        if self.doc and self.thumbs.count() == n:
            self.thumbs.blockSignals(True)
            self.thumbs.setCurrentRow(self.cur)
            self.thumbs.blockSignals(False)

    def snapshot(self):
        self.undo_stack.append(self.doc.tobytes())
        self.redo_stack.clear()
        total = sum(len(b) for b in self.undo_stack)
        while len(self.undo_stack) > 1 and (len(self.undo_stack) > MAX_UNDO or total > MAX_UNDO_BYTES):
            total -= len(self.undo_stack.pop(0))
        self.dirty = True

    def _restore_bytes(self, data):
        self.doc = fitz.open(stream=data, filetype="pdf")
        self.dirty = True
        self.refresh()

    def undo(self):
        if self.kind == "doc":
            self.editor.undo()
        elif self.doc and self.undo_stack:
            self.redo_stack.append(self.doc.tobytes())
            self._restore_bytes(self.undo_stack.pop())

    def redo(self):
        if self.kind == "doc":
            self.editor.redo()
        elif self.doc and self.redo_stack:
            self.undo_stack.append(self.doc.tobytes())
            self._restore_bytes(self.redo_stack.pop())

    def maybe_discard(self) -> bool:
        has = (self.qdoc is not None) if self.kind == "doc" else (self.doc is not None)
        if not (has and self.dirty):
            return True
        r = QMessageBox.question(self, APP_NAME, "Save changes first?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        return r == QMessageBox.StandardButton.Discard

    def closeEvent(self, e):
        self.stash()
        for i in range(len(self.states)):
            if self.states[i].dirty:
                self.tabbar.setCurrentIndex(i)
                if not self.maybe_discard():
                    e.ignore()
                    return
        for s in self.states:
            drop_recovery(s.uid)
        e.accept()

    # ----- crash recovery & autosave -----
    def autosave_tick(self):
        if not self.states:
            return
        self.stash()
        for s in self.states:
            if not s.dirty:
                continue
            try:
                d = recovery_dir()
                if s.kind == "doc":
                    if self.autosave_to_file and s.path:
                        with open(s.path, "wb") as f:
                            f.write(export_pdf_bytes(s.qdoc, s.title))
                        s.qdoc.setModified(False)
                        s.dirty = False
                        drop_recovery(s.uid)
                    else:
                        (d / f"{s.uid}.psdoc").write_bytes(serialize_doc(s.qdoc, s.title))
                        (d / f"{s.uid}.json").write_text(json.dumps({"title": s.title, "path": s.path}))
                elif self.autosave_to_file and s.path:
                    with open(s.path, "wb") as f:
                        f.write(s.doc.tobytes(garbage=3, deflate=True))
                    s.dirty = False
                    drop_recovery(s.uid)
                else:
                    (d / f"{s.uid}.pdf").write_bytes(s.doc.tobytes())
                    (d / f"{s.uid}.json").write_text(json.dumps({"title": s.title, "path": s.path}))
            except Exception:
                pass
        if 0 <= self.active < len(self.states):
            self.dirty = self.states[self.active].dirty
        self.update_status()

    def offer_recovery(self):
        d = recovery_dir()
        files = sorted(list(d.glob("*.pdf")) + list(d.glob("*.psdoc")))
        if not files:
            return
        r = QMessageBox.question(self, APP_NAME, f"Found {len(files)} unsaved document(s) from a previous "
                                 "session (the app may have closed unexpectedly). Recover them?")
        for f in files:
            if r != QMessageBox.StandardButton.Yes:
                drop_recovery(f.stem)
                continue
            meta = {}
            try:
                meta = json.loads(f.with_suffix(".json").read_text())
            except Exception:
                pass
            try:
                if f.suffix == ".psdoc":
                    q = load_doc_payload(json.loads(f.read_bytes().decode("utf-8")))
                    self.open_doc_tab(q, None, True, uid=f.stem, title=meta.get("title"))
                else:
                    self.open_in_tab(f.read_bytes(), None, True, uid=f.stem, title=meta.get("title"))
            except Exception:
                pass

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            low = p.lower()
            if low.endswith(".pdf"):
                self.open_path(p)
            elif low.endswith((".md", ".markdown")):
                self.import_markdown_file(p)
            elif low.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")) and self.kind == "doc":
                img = QImage(p)
                if not img.isNull():
                    self.editor.insert_image(img)

    def on_scroll(self):
        if not self.doc:
            return
        y = self.scroll.verticalScrollBar().value() + self.scroll.viewport().height() // 3
        n = self.canvas.page_at_y(y)
        if n != self.cur:
            self.cur = n
            self.update_nav()

    def goto(self, n):
        if self.doc and 0 <= n < len(self.doc):
            self.cur = n
            self.scroll.verticalScrollBar().setValue(max(0, self.canvas.tops[n] - 12))
            self.update_nav()

    def set_zoom(self, z):
        if not self.doc:
            return
        self.zoom = max(0.25, min(z, 5.0))
        self.canvas.zoom = self.zoom
        self.canvas.layout_pages()
        self.zoom_box.setEditText(f"{round(self.zoom * 100)}%")
        QTimer.singleShot(0, lambda: self.goto(self.cur))

    def fit_width(self):
        if not self.doc:
            return
        widest = max(p.rect.width for p in self.doc)
        avail = self.scroll.viewport().width() - 2 * Canvas.MARGIN - 4
        self.set_zoom(max(0.5, min(avail / widest, 3.0)))

    def zoom_from_text(self, text):
        if text.strip().lower().startswith("fit"):
            self.fit_width()
            return
        try:
            self.set_zoom(float(text.strip().rstrip("%")) / 100)
        except ValueError:
            pass

    # ===================== thumbnails & outline =====================
    def rebuild_thumbs(self):
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        n = len(self.doc) if self.doc else 0
        for i in range(n):
            self.thumbs.addItem(QListWidgetItem(self.blank_icon, str(i + 1)))
        self.thumbs.blockSignals(False)
        self.thumb_queue = list(range(n))
        self.thumb_timer.start()

    def thumb_tick(self):
        if not self.thumb_queue or not self.doc:
            self.thumb_timer.stop()
            return
        i = self.thumb_queue.pop(0)
        if i >= len(self.doc) or i >= self.thumbs.count():
            return
        page = self.doc[i]
        z = 100 / max(page.rect.width, 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        self.thumbs.item(i).setIcon(QIcon(pix_to_qpixmap(pix)))

    def rebuild_outline(self):
        self.outline.clear()
        if self.kind == "doc":
            items = list(self.editor.headings())
        elif self.doc:
            items = [(lvl, title, max(page, 1)) for lvl, title, page in self.doc.get_toc()]
        else:
            return
        stack = []
        for lvl, title, val in items:
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, val)
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            (stack[-1][1].addChild(item) if stack else self.outline.addTopLevelItem(item))
            stack.append((lvl, item))
        self.outline.expandAll()

    def on_thumb_moved(self, _parent, start, _end, _dest, row):
        if not self.doc:
            return
        n = len(self.doc)
        self.snapshot()
        self.doc.move_page(start, -1 if row >= n else row)
        new_cur = row - 1 if row > start else row
        self.cur = new_cur

        def finish():
            self.refresh()
            self.goto(new_cur)
        QTimer.singleShot(0, finish)

    def thumb_menu(self, pos):
        item = self.thumbs.itemAt(pos)
        if not item:
            return
        self.goto(self.thumbs.row(item))
        m = QMenu(self)
        m.addAction("Rotate 90\u00b0", self.rotate_page)
        m.addAction("Duplicate", self.duplicate_page)
        m.addAction("Insert blank page after", self.add_blank_page)
        m.addAction("Delete", self.delete_page)
        m.exec(self.thumbs.mapToGlobal(pos))

    # ===================== files =====================
    def open_pdf(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Open PDF", "", "PDF files (*.pdf)")
        for path in paths:
            self.open_path(path)

    def save(self) -> bool:
        if not self.require_any():
            return False
        return self.write_to(self.path) if self.path else self.save_as()

    def save_as(self) -> bool:
        if not self.require_any():
            return False
        start = self.path or (self.title_edit.text().strip() or "Untitled") + ".pdf"
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF", start, "PDF files (*.pdf)")
        return self.write_to(path) if path else False

    def write_to(self, path: str) -> bool:
        try:
            if self.kind == "doc":
                data = export_pdf_bytes(self.qdoc, self.title_edit.text().strip())
            else:
                data = self.doc.tobytes(garbage=3, deflate=True)
            with open(path, "wb") as f:
                f.write(data)
        except Exception as ex:
            QMessageBox.critical(self, APP_NAME, f"Save failed:\n{ex}")
            return False
        self.path, self.dirty = path, False
        self.title_edit.setText(os.path.splitext(os.path.basename(path))[0])
        if self.kind == "doc":
            self.qdoc.setModified(False)
        if 0 <= self.active < len(self.states):
            drop_recovery(self.states[self.active].uid)
        self.update_status()
        self.statusBar().showMessage(f"Saved {path}", 4000)
        return True

    def save_encrypted(self):
        if not self.require_any():
            return
        pw, ok = QInputDialog.getText(self, "Encrypt", "Password for the copy:", QLineEdit.EchoMode.Password)
        if not ok or not pw:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save encrypted copy", "", "PDF files (*.pdf)")
        if path:
            with open(path, "wb") as f:
                f.write(self.fitz_view().tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_AES_256,
                                                 owner_pw=pw, user_pw=pw))

    def export_png(self):
        if not self.require_any():
            return
        d = self.fitz_view()
        pno = min(self.doc_page_info()[0] - 1, len(d) - 1) if self.kind == "doc" else self.cur
        path, _ = QFileDialog.getSaveFileName(self, "Export PNG", "", "PNG (*.png)")
        if path:
            d[pno].get_pixmap(matrix=fitz.Matrix(2, 2)).save(path)

    def export_text(self):
        if not self.require_any():
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export text", "", "Text (*.txt)")
        if not path:
            return
        text = self.editor.toPlainText() if self.kind == "doc" else "\n\n".join(p.get_text() for p in self.doc)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def doc_properties(self):
        if not self.require_doc():
            return
        md = self.doc.metadata or {}
        dlg = QDialog(self)
        dlg.setWindowTitle("Document properties")
        form = QFormLayout(dlg)
        edits = {k: QLineEdit(md.get(k) or "") for k in ("title", "author", "subject", "keywords")}
        for k, ed in edits.items():
            form.addRow(k.title(), ed)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            self.snapshot()
            md.update({k: ed.text() for k, ed in edits.items()})
            self.doc.set_metadata(md)
            self.update_status()

    def print_doc(self):
        if not self.require_any():
            return
        d = self.fitz_view()
        n = len(d)
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dlg = QPrintDialog(printer, self)
        dlg.setMinMax(1, n)
        dlg.setOption(QPrintDialog.PrintDialogOption.PrintPageRange)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        first, last = (printer.fromPage() - 1, printer.toPage() - 1) if printer.fromPage() > 0 else (0, n - 1)
        painter = QPainter(printer)
        dpi = min(printer.resolution(), 300)
        target = printer.pageRect(QPrinter.Unit.DevicePixel)
        for k, i in enumerate(range(first, last + 1)):
            if k:
                printer.newPage()
            pm = pix_to_qpixmap(d[i].get_pixmap(dpi=dpi, alpha=False))
            size = pm.size().scaled(target.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio)
            painter.drawPixmap(int(target.x()), int(target.y()), size.width(), size.height(), pm)
        painter.end()

    def add_blank_page(self):
        if self.doc is None:
            d = fitz.open()
            d.new_page(width=LETTER.width, height=LETTER.height)
            self.open_in_tab(d.tobytes(), None, True)
            return
        r = self.doc[self.cur].rect
        self.snapshot()
        self.doc.new_page(pno=self.cur + 1, width=r.width, height=r.height)
        self.refresh()
        self.goto(self.cur + 1)

    def delete_page(self):
        if not self.require_doc():
            return
        if len(self.doc) == 1:
            self.statusBar().showMessage("A PDF needs at least one page.", 4000)
            return
        self.snapshot()
        self.doc.delete_page(self.cur)
        self.refresh()
        self.goto(self.cur)

    def duplicate_page(self):
        if not self.require_doc():
            return
        self.snapshot()
        n = len(self.doc)
        self.doc.copy_page(self.cur, -1 if self.cur + 1 >= n else self.cur + 1)
        self.refresh()
        self.goto(self.cur + 1)

    def rotate_page(self):
        if not self.require_doc():
            return
        self.snapshot()
        page = self.doc[self.cur]
        page.set_rotation((page.rotation + 90) % 360)
        self.refresh()

    def move_page(self, direction):
        if not self.require_doc():
            return
        n, p = len(self.doc), self.cur
        target = p + direction
        if not 0 <= target < n:
            return
        self.snapshot()
        if direction < 0:
            self.doc.move_page(p, target)
        else:  # move_page inserts *before* `to`; -1 = end
            self.doc.move_page(p, target + 1 if target + 1 < n else -1)
        self.cur = target
        self.refresh()
        self.goto(target)

    def merge_into_current(self):
        if not self.require_doc():
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Insert PDFs", "", "PDF files (*.pdf)")
        if not paths:
            return
        self.snapshot()
        at = self.cur + 1
        for path in paths:
            with fitz.open(path) as other:
                self.doc.insert_pdf(other, start_at=at)
                at += len(other)
        self.refresh()

    def add_page_numbers(self):
        if not self.require_doc():
            return
        self.snapshot()
        for i, page in enumerate(self.doc, 1):
            r = page.rect
            w = fitz.get_text_length(str(i), fontsize=10)
            page.insert_text((r.width / 2 - w / 2, r.height - 24), str(i), fontsize=10, color=(0.3, 0.3, 0.3))
        self.refresh()

    def add_watermark(self):
        if not self.require_doc():
            return
        text, ok = QInputDialog.getText(self, "Watermark", "Watermark text:", text="CONFIDENTIAL")
        if not ok or not text.strip():
            return
        self.snapshot()
        for page in self.doc:
            r = page.rect
            c = fitz.Point(r.width / 2, r.height / 2)
            w = fitz.get_text_length(text, fontsize=64)
            page.insert_text(fitz.Point(c.x - w / 2, c.y), text, fontsize=64, color=(0.6, 0.6, 0.6),
                             fill_opacity=0.3, morph=(c, fitz.Matrix(45)))
        self.refresh()

    # ===================== merge / split / compress =====================
    def merge_dialog(self):
        dlg = MergeDialog(self)
        if dlg.exec() and dlg.paths():
            try:
                self.open_in_tab(merge_paths(dlg.paths()), None, True, title="Merged document")
            except Exception as ex:
                QMessageBox.warning(self, APP_NAME, f"Merge failed:\n{ex}")

    def split_pdf(self):
        if not self.require_any():
            return
        d = self.fitz_view()
        n = len(d)
        dlg = SplitDialog(self, n)
        if not dlg.exec():
            return
        folder = dlg.folder.text().strip()
        if not os.path.isdir(folder):
            QMessageBox.warning(self, APP_NAME, "Choose an output folder first.")
            return
        try:
            mode, param = dlg.mode.currentIndex(), dlg.param.text()
            if mode == 0:
                k = max(1, int(param or 1))
                ranges = [(i, min(i + k, n) - 1) for i in range(0, n, k)]
            elif mode == 1:
                ranges = parse_ranges(param, n)
            else:
                ranges = [(i, i) for i in range(n)]
            base = os.path.splitext(os.path.basename(self.path))[0] if self.path else (
                self.title_edit.text() or "document")
            paths = split_doc(d, ranges, folder, base)
        except ValueError as ex:
            QMessageBox.warning(self, APP_NAME, str(ex))
            return
        QMessageBox.information(self, APP_NAME, f"Created {len(paths)} file(s) in\n{folder}")

    def compress_pdf(self):
        if not self.require_any():
            return
        label, ok = QInputDialog.getItem(self, "Compress", "Compression level:",
                                         list(COMPRESS_PRESETS), 0, False)
        if not ok:
            return
        dpi, quality = COMPRESS_PRESETS[label]
        d = self.fitz_view()
        src_bytes = d.tobytes()
        prog = QProgressDialog("Compressing...", "", 0, len(d), self)
        prog.setCancelButton(None)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        count = [0]

        def tick():
            count[0] += 1
            prog.setValue(count[0])
            QApplication.processEvents()
        try:
            out = compress_pdf_bytes(src_bytes, dpi, quality, tick)
        finally:
            prog.close()
        path, _ = QFileDialog.getSaveFileName(self, "Save compressed copy", "", "PDF files (*.pdf)")
        if path:
            with open(path, "wb") as f:
                f.write(out)
            QMessageBox.information(self, APP_NAME, f"{len(src_bytes) / 1e6:.2f} MB \u2192 {len(out) / 1e6:.2f} MB\n"
                                                     "(your open document was not changed)")

    def tesseract_ready(self) -> bool:
        if not os.environ.get("TESSDATA_PREFIX"):
            for c in (r"C:\Program Files\Tesseract-OCR\tessdata", r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
                      "/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tessdata", "/opt/homebrew/share/tessdata"):
                if os.path.isdir(c):
                    os.environ["TESSDATA_PREFIX"] = c
                    break
        if not os.environ.get("TESSDATA_PREFIX"):
            QMessageBox.information(
                self, APP_NAME,
                "OCR needs Tesseract's language data.\n\nInstall Tesseract (Windows installer: "
                "github.com/UB-Mannheim/tesseract/wiki), then restart this app. If it still can't find it, "
                "set the TESSDATA_PREFIX environment variable to the 'tessdata' folder.")
            return False
        return True

    def ocr_document(self):
        if not self.require_doc() or not self.tesseract_ready():
            return
        lang, ok = QInputDialog.getText(self, "OCR", "Language code(s), e.g. eng or eng+deu:", text="eng")
        if not ok or not lang.strip():
            return
        todo = [i for i, p in enumerate(self.doc) if not p.get_text().strip()]
        if not todo:
            QMessageBox.information(self, APP_NAME, "Every page already has searchable text.")
            return
        prog = QProgressDialog("Running OCR...", "Cancel", 0, len(todo), self)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        self.snapshot()
        done, error = 0, None
        for k, i in enumerate(todo):
            prog.setValue(k)
            QApplication.processEvents()
            if prog.wasCanceled():
                break
            try:
                page = self.doc[i]
                r = page.rect
                pix = page.get_pixmap(dpi=300, alpha=False)
                ocr = fitz.open("pdf", pix.pdfocr_tobytes(language=lang.strip()))
                new = self.doc.new_page(pno=i, width=r.width, height=r.height)
                new.show_pdf_page(new.rect, ocr, 0)
                self.doc.delete_page(i + 1)
                done += 1
            except Exception as ex:
                error = str(ex)
                break
        prog.setValue(len(todo))
        self.refresh()
        msg = f"Made {done} page(s) searchable."
        if error:
            msg += f"\n\nStopped on an error:\n{error}"
        QMessageBox.information(self, APP_NAME, msg)

    # ===================== signature =====================
    def draw_signature(self) -> bool:
        dlg = SignatureDialog(self)
        if dlg.exec():
            png = dlg.pad.png_bytes()
            if png:
                self.sig_png = png
                (data_dir() / "signature.png").write_bytes(png)
                return True
        return False

    def place_signature(self, pno, pt):
        if not self.sig_png and not self.draw_signature():
            return
        pix = fitz.Pixmap(self.sig_png)
        w = 150.0
        h = w * pix.height / pix.width
        rect = fitz.Rect(pt.x - w / 2, pt.y - h / 2, pt.x + w / 2, pt.y + h / 2)
        self.snapshot()
        self.doc[pno].insert_image(rect, stream=self.sig_png, keep_proportion=True)
        self.refresh(changed=[pno])

    # ===================== forms =====================
    def fill_form_at(self, pno, pt):
        page = self.doc[pno]
        field = next((w for w in (page.widgets() or []) if w.rect.contains(pt)), None)
        if field is None:
            self.statusBar().showMessage("No form field there.", 3000)
            return
        t, name = field.field_type, field.field_name or "Field"
        if t == fitz.PDF_WIDGET_TYPE_TEXT:
            cur = str(field.field_value or "")
            if field.field_flags & 4096:  # multiline
                val, ok = QInputDialog.getMultiLineText(self, name, "Value:", cur)
            else:
                val, ok = QInputDialog.getText(self, name, "Value:", text=cur)
            if not ok:
                return
            self.snapshot()
            field.field_value = val
        elif t in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
            on = field.field_value not in (False, None, "", "Off", 0)
            self.snapshot()
            field.field_value = not on
        elif t in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
            items = [c if isinstance(c, str) else str(c[-1]) for c in (field.choice_values or [])]
            if not items:
                return
            cur = items.index(field.field_value) if field.field_value in items else 0
            val, ok = QInputDialog.getItem(self, name, "Choose:", items, cur, False)
            if not ok:
                return
            self.snapshot()
            field.field_value = val
        elif t == fitz.PDF_WIDGET_TYPE_SIGNATURE:
            self.statusBar().showMessage("Use the Sign tool to place your signature on the page.", 5000)
            return
        else:
            self.statusBar().showMessage("That field type can't be edited here.", 4000)
            return
        field.update()
        self.refresh(changed=[pno])

    # ===================== in-place text editing & find/replace =====================
    def edit_text_at(self, pno, pt):
        page = self.doc[pno]
        found = line_at(page, pt)
        if not found:
            self.statusBar().showMessage("No text there (for scanned pages, run OCR first).", 5000)
            return
        text, bbox, style = found
        new, ok = QInputDialog.getText(self, "Edit text", "Replace this line with:", text=text)
        if not ok or new == text:
            return
        self.snapshot()
        rewrite_line(page, bbox, style, new)
        self.refresh(changed=[pno])

    def find_replace(self):
        if not self.require_any():
            return
        dlg = ReplaceDialog(self)
        if not dlg.exec() or not dlg.find.text():
            return
        find, repl = dlg.find.text(), dlg.repl.text()
        if self.kind == "doc":
            doc = self.editor.document()
            guard = QTextCursor(doc)
            guard.beginEditBlock()
            cur, n = QTextCursor(doc), 0
            while True:
                cur = doc.find(find, cur)
                if cur.isNull():
                    break
                cur.insertText(repl)
                n += 1
            guard.endEditBlock()
            self.statusBar().showMessage(f"Replaced {n} occurrence(s)." if n else "No matches found.", 5000)
            return
        if count_matches(self.doc, find) == 0:
            QMessageBox.information(self, APP_NAME, "No matches found.")
            return
        self.snapshot()
        total = replace_text(self.doc, find, repl)
        self.refresh()
        self.statusBar().showMessage(f"Replaced {total} occurrence(s).", 5000)

    def set_tool(self, name):
        self.tool = name
        self.canvas.tool = name
        self.canvas.setCursor(Qt.CursorShape.ArrowCursor if name == "hand" else Qt.CursorShape.CrossCursor)
        self.tool_actions[name].setChecked(True)
        self.canvas.update()
        self.statusBar().showMessage(next(t for n, _l, t in TOOLS if n == name) if name != "hand" else "")

    def on_region(self, pno, obj):
        if not self.require_doc():
            return
        page, tool = self.doc[pno], self.tool
        col, width = qcolor_to_pdf(self.pen_color), self.pen_width

        if tool == "comment":
            text, ok = QInputDialog.getMultiLineText(self, "Comment", "Comment:")
            if ok and text.strip():
                self.snapshot()
                page.add_text_annot(obj, text)
                self.refresh(changed=[pno])
            return
        if tool == "form":
            self.fill_form_at(pno, obj)
            return
        if tool == "edit_text":
            self.edit_text_at(pno, obj)
            return
        if tool == "signature":
            self.place_signature(pno, obj)
            return
        if tool == "eraser":
            target = next((a for a in page.annots() if a.rect.contains(obj)), None)
            if target is None:
                self.statusBar().showMessage("No annotation there.", 3000)
                return
            self.snapshot()
            page.delete_annot(target)
            self.refresh(changed=[pno])
            return
        if tool == "pen":
            self.snapshot()
            a = page.add_ink_annot([list(obj)])
            a.set_colors(stroke=col)
            a.set_border(width=width)
            a.update()
        elif tool in ("line", "arrow"):
            p1, p2 = obj
            self.snapshot()
            a = page.add_line_annot(p1, p2)
            if tool == "arrow":
                a.set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_CLOSED_ARROW)
            a.set_colors(stroke=col)
            a.set_border(width=width)
            a.update()
        elif tool in ("rect", "ellipse"):
            self.snapshot()
            a = page.add_rect_annot(obj) if tool == "rect" else page.add_circle_annot(obj)
            a.set_colors(stroke=col)
            a.set_border(width=width)
            a.update()
        elif tool == "md":
            text, ok = QInputDialog.getMultiLineText(self, "Text box", "Text (Markdown allowed):")
            if not ok or not text.strip():
                return
            self.snapshot()
            spare, _ = page.insert_htmlbox(obj, md_to_html(text.strip()), css=PDF_CSS)
            if spare < 0:
                QMessageBox.information(self, APP_NAME, "The text did not fully fit. Draw a larger box.")
        elif tool in ("highlight", "underline", "strike"):
            words = page.get_text("words", clip=obj)
            if not words:
                self.statusBar().showMessage("No selectable text in that area.", 4000)
                return
            self.snapshot()
            quads = [fitz.Rect(w[:4]) for w in words]
            {"highlight": page.add_highlight_annot, "underline": page.add_underline_annot,
             "strike": page.add_strikeout_annot}[tool](quads)
        elif tool == "image":
            path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif)")
            if not path:
                return
            self.snapshot()
            page.insert_image(obj, filename=path, keep_proportion=True)
        elif tool == "redact":
            self.snapshot()
            page.add_redact_annot(obj, fill=(1, 1, 1))
            page.apply_redactions()
        else:
            return
        self.refresh(changed=[pno])

    # ===================== search =====================
    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def run_search(self):
        q = self.search.text().strip()
        if not q:
            return
        if self.kind == "doc":
            self.run_search_doc(q)
            return
        if not self.doc:
            return
        if q != self.last_query:
            self.last_query = q
            self.matches = [(i, r) for i in range(len(self.doc)) for r in self.doc[i].search_for(q)]
            hits = {}
            for i, r in self.matches:
                hits.setdefault(i, []).append(r)
            self.canvas.hits = hits
            self.match_i = -1
        if not self.matches:
            self.canvas.current_hit = None
            self.canvas.update()
            self.statusBar().showMessage("No matches.", 4000)
            return
        self.match_i = (self.match_i + 1) % len(self.matches)
        pno, r = self.matches[self.match_i]
        self.canvas.current_hit = (pno, r)
        rr = fitz.Rect(r) * self.doc[pno].rotation_matrix
        y = self.canvas.tops[pno] + int(rr.y0 * self.zoom) - self.scroll.viewport().height() // 3
        self.scroll.verticalScrollBar().setValue(max(0, y))
        self.canvas.update()
        self.statusBar().showMessage(f"Match {self.match_i + 1} of {len(self.matches)}", 4000)

    # ===================== typed pages (Google-Docs-style) =====================
    def set_kind_ui(self):
        is_doc = self.kind == "doc"
        self.stack.setCurrentIndex(1 if is_doc else 0)
        self.tb_fmt.setVisible(is_doc)
        self.tb_tools.setVisible(not is_doc)
        self.zoom_box.setEnabled(not is_doc)
        for a in self.doc_actions:
            a.setEnabled(is_doc)
        for a in self.pdf_actions:
            a.setEnabled(not is_doc)
        self.left_tabs.setCurrentIndex(1 if is_doc else 0)
        self.words_label.setVisible(is_doc)
        if is_doc:
            self.editor.setFocus()

    def new_document(self):
        self.open_doc_tab(new_qdoc(), None, False, title="Untitled document")

    def open_doc_tab(self, qdoc, path=None, dirty=False, uid=None, title=None):
        self.stash()
        s = DocState(None, path, dirty, uid, title)
        s.kind, s.qdoc = "doc", qdoc
        qdoc.setModified(dirty)
        qdoc.modificationChanged.connect(lambda v, d=qdoc: self.on_doc_modified(d, v))
        self.states.append(s)
        self._busy = True
        idx = self.tabbar.addTab(s.title)
        self.tabbar.setCurrentIndex(idx)
        self._busy = False
        self.active = idx
        self.restore(s)

    def on_doc_modified(self, d, v):
        if d is self.qdoc and self.kind == "doc":
            self.dirty = v
            self.update_status()

    def open_path(self, path):
        with open(path, "rb") as f:
            self.open_pdf_data(f.read(), path)

    def open_pdf_data(self, data, path=None, dirty=False, uid=None, title=None):
        """PDFs made by PDF Studio reopen as an editable page; any other PDF opens for markup."""
        try:
            pdf = fitz.open(stream=data, filetype="pdf")
            if not pdf.needs_pass and EMBED_NAME in pdf.embfile_names():
                payload = json.loads(pdf.embfile_get(EMBED_NAME).decode("utf-8"))
                q = load_doc_payload(payload)
                self.open_doc_tab(q, path, dirty, uid, title or (None if path else payload.get("title")))
                return
        except Exception:
            pass
        self.open_in_tab(data, path, dirty, uid, title)
        self.statusBar().showMessage("Opened as a PDF. Tools > 'Edit this PDF as a document' turns its text "
                                     "into a page you can type on.", 9000)

    def mark_up_as_pdf(self):
        if self.kind != "doc":
            return
        title = self.title_edit.text().strip() or "Document"
        data = export_pdf_bytes(self.qdoc, title, embed=False)
        self.open_in_tab(data, None, True, title=f"{title} (markup)")

    def edit_as_document(self):
        if not self.require_doc():
            return
        html, images = pdf_to_html(self.doc)
        q = new_qdoc()
        q.setUndoRedoEnabled(False)
        for name, data in images.items():
            q.addResource(IMAGE_RES, QUrl(name), QImage.fromData(data))
        q.setHtml(html)
        q.setUndoRedoEnabled(True)
        self.open_doc_tab(q, None, True, title=self.title_edit.text().strip() or "Document")
        self.statusBar().showMessage("Converted the PDF's text and images into a page you can edit. "
                                     "Fonts and layout are approximated.", 9000)

    # ----- markdown in and out -----
    def import_markdown(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Markdown", "", "Markdown (*.md *.markdown *.txt)")
        if path:
            self.import_markdown_file(path)

    def import_markdown_file(self, path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        q = new_qdoc()
        q.setUndoRedoEnabled(False)
        q.setMarkdown(text)
        q.setUndoRedoEnabled(True)
        self.open_doc_tab(q, None, True, title=os.path.splitext(os.path.basename(path))[0])

    def export_markdown(self):
        if self.kind != "doc":
            return
        name = (self.title_edit.text().strip() or "document") + ".md"
        path, _ = QFileDialog.getSaveFileName(self, "Download as Markdown", name, "Markdown (*.md)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.qdoc.toMarkdown())

    def paste_markdown(self):
        text = QApplication.clipboard().text()
        if text.strip():
            self.editor.insert_markdown(text)

    def copy_markdown(self):
        md = self.editor.selection_markdown()
        if md:
            QApplication.clipboard().setText(md)

    # ----- formatting bar plumbing -----
    def color_button(self, tip, palette, on_click, on_pick, allow_none=False):
        btn = QToolButton()
        btn.setToolTip(tip)
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        menu = QMenu(btn)
        for name, hexv in palette:
            pm = QPixmap(16, 16)
            pm.fill(QColor(hexv))
            a = menu.addAction(QIcon(pm), name)
            a.triggered.connect(lambda _=False, h=hexv: on_pick(QColor(h)))
        if allow_none:
            menu.addAction("None").triggered.connect(lambda _=False: on_pick(None))
        menu.addSeparator()
        menu.addAction("Custom...").triggered.connect(lambda _=False: self.custom_color(on_pick))
        btn.setMenu(menu)
        btn.clicked.connect(lambda _=False: on_click())
        self.tb_fmt.addWidget(btn)
        return btn

    def custom_color(self, on_pick):
        c = QColorDialog.getColor(QColor("#000000"), self, "Pick a color")
        if c.isValid():
            on_pick(c)

    def apply_text_color(self, color=None):
        if color is not None:
            self.text_color = color
        self.update_text_icons()
        self.editor.set_color(self.text_color)

    def pick_highlight(self, color):
        self.hl_color = color
        self.update_text_icons()
        self.editor.set_highlight(color)

    def click_highlight(self):
        self.editor.set_highlight(self.hl_color)

    def glyph_icon(self, text, color):
        pm = QPixmap(22, 22)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        f = QFont("Segoe UI")
        f.setPixelSize(15)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#e3e3e3" if self.dark else "#1f1f1f"))
        p.drawText(QRect(0, 0, 22, 17), Qt.AlignmentFlag.AlignCenter, text)
        if color is not None:
            p.fillRect(3, 18, 16, 3, color)
        p.end()
        return QIcon(pm)

    def update_text_icons(self):
        if hasattr(self, "text_btn"):
            self.text_btn.setIcon(self.glyph_icon("A", self.text_color))
            self.hl_btn.setIcon(self.glyph_icon("\u270E", self.hl_color))

    def apply_size_text(self, text):
        try:
            pt = float(text)
        except ValueError:
            return
        self.editor.set_size(max(1.0, min(pt, 400.0)))

    def sync_format_ui(self):
        if self.kind != "doc" or self.qdoc is None:
            return
        ed = self.editor
        f = ed.currentCharFormat()
        c = ed.textCursor()
        pairs = (("bold", f.fontWeight() >= 600, self.act_bold), ("italic", f.fontItalic(), self.act_italic),
                 ("underline", f.fontUnderline(), self.act_underline), ("strike", f.fontStrikeOut(), self.act_strike))
        for key, val, act in pairs:
            self.tb_btns[key].setChecked(val)
            act.setChecked(val)
        va = f.verticalAlignment()
        self.act_sup.setChecked(va == QTextCharFormat.VerticalAlignment.AlignSuperScript)
        self.act_sub.setChecked(va == QTextCharFormat.VerticalAlignment.AlignSubScript)
        fams = f.fontFamilies()
        fam = fams[0] if fams else ed.document().defaultFont().family()
        self.font_box.blockSignals(True)
        self.font_box.setCurrentFont(QFont(fam))
        self.font_box.blockSignals(False)
        pt = f.fontPointSize() or ed.document().defaultFont().pointSizeF()
        self.size_box.blockSignals(True)
        self.size_box.setEditText(f"{pt:g}")
        self.size_box.blockSignals(False)
        self.style_box.blockSignals(True)
        self.style_box.setCurrentText(ed.current_style_name())
        self.style_box.blockSignals(False)
        al = ed.alignment()
        A = Qt.AlignmentFlag
        akey = ("center" if al & A.AlignHCenter else "right" if al & A.AlignRight
                else "justify" if al & A.AlignJustify else "left")
        for key in ("left", "center", "right", "justify"):
            self.tb_btns["al_" + key].setChecked(key == akey)
        self.align_acts[akey].setChecked(True)
        lst = c.currentList()
        lstyle = lst.format().style() if lst is not None else None
        is_num = lstyle == QTextListFormat.Style.ListDecimal
        is_bul = lstyle == QTextListFormat.Style.ListDisc
        self.tb_btns["number"].setChecked(is_num)
        self.tb_btns["bullet"].setChecked(is_bul)
        self.act_num.setChecked(is_num)
        self.act_bul.setChecked(is_bul)

    def on_editor_cursor(self):
        self.sync_format_ui()
        self.update_nav()

    def on_editor_text(self):
        self.words_timer.start()
        self.outline_timer.start()
        self.update_nav()

    def on_outline_timer(self):
        if self.kind == "doc" and self.left_dock.isVisible():
            self.rebuild_outline()

    def update_word_count(self):
        if self.kind == "doc" and self.qdoc is not None:
            words, _chars = self.editor.word_count()
            self.words_label.setText(f"{words} words    ")
        else:
            self.words_label.setText("")

    def refresh_doc_ui(self):
        self.update_status()
        self.sync_format_ui()
        self.update_word_count()
        self.rebuild_outline()

    def doc_page_info(self):
        total = self.editor.document().size().height()
        pages = max(1, int(-(-(total - 2 * MARGIN) // CONTENT_H)))
        y = self.editor.cursorRect().center().y() + self.editor.verticalScrollBar().value()
        page = min(pages, max(1, int((y - MARGIN) // CONTENT_H) + 1))
        return page, pages

    def on_outline_click(self, item, _col):
        v = item.data(0, Qt.ItemDataRole.UserRole)
        if self.kind == "doc":
            c = QTextCursor(self.editor.document())
            c.setPosition(int(v))
            self.editor.setTextCursor(c)
            self.editor.ensureCursorVisible()
            self.editor.setFocus()
        else:
            self.goto(int(v) - 1)

    def toggle_left_dock(self):
        show = not self.left_dock.isVisible()
        self.left_dock.setVisible(show)
        if show:
            self.rebuild_outline()

    def set_md_auto(self, on):
        self.editor.md_auto = on

    def set_show_breaks(self, on):
        self.editor.show_breaks = on
        self.editor.viewport().update()

    def insert_image_dialog(self):
        if self.kind != "doc":
            self.set_tool("image")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Insert image", "",
                                                "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)")
        for p in paths:
            img = QImage(p)
            if not img.isNull():
                self.editor.insert_image(img)

    def insert_table_dialog(self):
        text, ok = QInputDialog.getText(self, "Insert table", "Size (rows x columns):", text="3x3")
        m = re.match(r"\s*(\d+)\s*[xX]\s*(\d+)\s*$", text or "")
        if ok and m:
            rows, cols = int(m.group(1)), int(m.group(2))
            if 1 <= rows <= 50 and 1 <= cols <= 12:
                self.editor.insert_table(rows, cols)

    def insert_hr(self):
        self.editor.textCursor().insertHtml("<hr>")

    def word_count_dialog(self):
        words, chars = self.editor.word_count()
        _p, pages = self.doc_page_info()
        QMessageBox.information(self, "Word count",
                                f"Words: {words}\nCharacters: {chars}\nPages (approx.): {pages}")

    def run_search_doc(self, q):
        ed = self.editor
        doc = ed.document()
        if q != self.last_query:
            self.last_query = q
            sels = []
            c = QTextCursor(doc)
            while True:
                c = doc.find(q, c)
                if c.isNull():
                    break
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(255, 230, 0, 150))
                sel = QTextEdit.ExtraSelection()
                sel.cursor = c
                sel.format = fmt
                sels.append(sel)
            ed.setExtraSelections(sels)
            self.matches = sels
            self.match_i = -1
        if not self.matches:
            self.statusBar().showMessage("No matches.", 4000)
            return
        if not ed.find(q):
            ed.moveCursor(QTextCursor.MoveOperation.Start)
            ed.find(q)
        ed.ensureCursorVisible()
        self.statusBar().showMessage(f"{len(self.matches)} match(es)", 4000)

    def startup(self):
        self.offer_recovery()
        if not self.states:
            self.new_document()


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("PDFStudio")
    app.setApplicationName("PDFStudio")
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow()
    win.show()
    for arg in sys.argv[1:]:  # "Open with" support
        if os.path.isfile(arg) and arg.lower().endswith(".pdf"):
            win.open_path(arg)
    QTimer.singleShot(300, win.startup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
