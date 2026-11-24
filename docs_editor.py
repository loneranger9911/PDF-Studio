"""Google-Docs-style typed page for PDF Studio.

You type on one white page. Documents are exported as PDF, and the editable
source is embedded inside that PDF, so reopening it in PDF Studio gives you the
page back (not just a picture of it).
"""
import base64
import html as htmlmod
import json
import re
import statistics
import uuid

import fitz  # PyMuPDF
from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QRect, QSizeF, Qt, QUrl
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QIcon, QImage, QPageLayout, QPageSize,
                           QPainter, QPdfWriter, QPen, QPixmap, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextDocument, QTextDocumentFragment, QTextImageFormat,
                           QTextLength, QTextListFormat, QTextTableFormat)
from PySide6.QtWidgets import QColorDialog, QFrame, QInputDialog, QMenu, QTextEdit

# US Letter at 96 dpi, 1-inch margins
SHEET_W, SHEET_H, MARGIN = 816, 1056, 96
CONTENT_W, CONTENT_H = SHEET_W - 2 * MARGIN, SHEET_H - 2 * MARGIN
EMBED_NAME = "pdfstudio-source.json"
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
IMAGE_RES = QTextDocument.ResourceType.ImageResource

# name -> (heading level, size pt, bold, italic, color, space before, space after)
STYLES = {
    "Normal text": (0, 11, False, False, "#000000", 0, 6),
    "Title": (0, 26, False, False, "#000000", 0, 6),
    "Subtitle": (0, 15, False, False, "#666666", 0, 10),
    "Heading 1": (1, 20, False, False, "#000000", 18, 6),
    "Heading 2": (2, 16, False, False, "#000000", 14, 4),
    "Heading 3": (3, 14, False, False, "#434343", 12, 4),
}
TEXT_COLORS = [("Black", "#000000"), ("Dark gray", "#434343"), ("Gray", "#999999"), ("White", "#ffffff"),
               ("Red", "#d93025"), ("Orange", "#e8710a"), ("Yellow", "#f9ab00"), ("Green", "#188038"),
               ("Cyan", "#129eaf"), ("Blue", "#1a73e8"), ("Purple", "#9334e6"), ("Magenta", "#d01884")]
TABLE_COLORS = [("White", "#ffffff"), ("Light gray", "#f1f3f4"), ("Beige", "#f0ede8"), ("Navy", "#2f4560"),
                ("Blue", "#1a73e8"), ("Light blue", "#d2e3fc"), ("Green", "#ceead6"), ("Yellow", "#fef7cb"),
                ("Orange", "#fdd9b5"), ("Red", "#f6c9c4"), ("Purple", "#e9d2fd"), ("Black", "#000000")]
HL_COLORS = [("Yellow", "#fff475"), ("Green", "#ccff90"), ("Cyan", "#a7ffeb"), ("Blue", "#aecbfa"),
             ("Pink", "#fdcfe8"), ("Orange", "#fbbc04"), ("Gray", "#e8eaed")]


def new_qdoc() -> QTextDocument:
    d = QTextDocument()
    d.setDefaultFont(QFont("Arial", 11))
    d.setDocumentMargin(MARGIN)
    bf = QTextBlockFormat()
    bf.setLineHeight(115.0, 1)  # 1 = proportional
    bf.setBottomMargin(6)
    QTextCursor(d).setBlockFormat(bf)
    return d


def looks_like_markdown(text: str) -> bool:
    return "\n" in text and bool(re.search(r"^(#{1,6} |[-*+] |\d+\. |> |```)", text, re.M))


def _image_from_resource(res):
    img = None
    if isinstance(res, QImage):
        img = res
    elif isinstance(res, QPixmap):
        img = res.toImage()
    elif res is not None:
        try:
            img = QImage.fromData(bytes(res))
        except Exception:
            img = None
    return img if (img is not None and not img.isNull()) else None


def _png_b64(img: QImage) -> str:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return base64.b64encode(bytes(buf.data())).decode("ascii")


def _image_names(html: str):
    return set(re.findall(r'<img[^>]*?\ssrc="([^"]+)"', html))


def serialize_doc(qdoc: QTextDocument, title: str = "") -> bytes:
    html = qdoc.toHtml()
    images = {}
    for name in _image_names(html):
        img = _image_from_resource(qdoc.resource(IMAGE_RES, QUrl(name)))
        if img is not None:
            images[name] = _png_b64(img)
    return json.dumps({"v": 1, "title": title, "html": html, "images": images}).encode("utf-8")


def load_doc_payload(payload: dict) -> QTextDocument:
    q = new_qdoc()
    for name, b64 in payload.get("images", {}).items():
        q.addResource(IMAGE_RES, QUrl(name), QImage.fromData(base64.b64decode(b64)))
    q.setHtml(payload["html"])
    return q


def export_pdf_bytes(qdoc: QTextDocument, title: str = "", embed: bool = True) -> bytes:
    """Lay the page out as US Letter (1-inch margins on every page) and return PDF bytes."""
    clone = qdoc.clone()
    clone.setPageSize(QSizeF(SHEET_W, SHEET_H))  # paginated; margin (96px) applies to each page
    for name in _image_names(qdoc.toHtml()):
        res = qdoc.resource(IMAGE_RES, QUrl(name))
        if res is not None:
            clone.addResource(IMAGE_RES, QUrl(name), res)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buf)
    writer.setResolution(96)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Inch)
    if title:
        writer.setTitle(title)
    writer.setCreator("PDF Studio")
    clone.print_(writer)
    del writer  # finishes the PDF stream
    buf.close()
    pdf = fitz.open(stream=bytes(buf.data()), filetype="pdf")
    if embed:
        pdf.embfile_add(EMBED_NAME, serialize_doc(qdoc, title), filename=EMBED_NAME,
                        desc="PDF Studio editable source")
    out = pdf.tobytes(garbage=3, deflate=True)
    pdf.close()
    return out


def _is_bold(s):
    return bool(s["flags"] & 16) or "bold" in s["font"].lower()


def _is_italic(s):
    f = s["font"].lower()
    return bool(s["flags"] & 2) or "italic" in f or "oblique" in f


def _span_html(s, scale):
    t = s["text"]
    if not t:
        return ""
    css = [f"font-size:{s['size'] * scale:.1f}pt"]
    color = "#%06x" % s["color"]
    if color != "#000000":
        css.append(f"color:{color}")
    if _is_bold(s):
        css.append("font-weight:bold")
    if _is_italic(s):
        css.append("font-style:italic")
    return f'<span style="{";".join(css)}">{htmlmod.escape(t)}</span>'


def _snap_edges(vals, tol=2.5):
    out = []
    for v in sorted(vals):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def _nearest(edges, v):
    return min(range(len(edges)), key=lambda i: abs(edges[i] - v))


def _find_tables(page):
    try:
        return [t for t in page.find_tables().tables if len(t.cells) >= 4]
    except Exception:
        return []


def _table_html(page, tab, scale):
    """Rebuild a PDF table as an HTML table: merged cells, fills, text styles, alignment, widths."""
    bbox = fitz.Rect(tab.bbox)
    cells = [fitz.Rect(c) for c in tab.cells if c]
    xs = _snap_edges([x for c in cells for x in (c.x0, c.x1)])
    ys = _snap_edges([y for c in cells for y in (c.y0, c.y1)])
    try:
        pix = page.get_pixmap(clip=bbox, dpi=72, alpha=False)
    except Exception:
        pix = None
    grid = {}
    for c in cells:
        r0, r1 = _nearest(ys, c.y0), _nearest(ys, c.y1)
        c0, c1 = _nearest(xs, c.x0), _nearest(xs, c.x1)
        grid.setdefault(r0, []).append((c0, c, max(1, r1 - r0), max(1, c1 - c0)))
    total_w = max(bbox.width, 1.0)
    rows_html = []
    for r in range(len(ys) - 1):
        tds = []
        for _c0, rect, rspan, cspan in sorted(grid.get(r, []), key=lambda t: t[0]):
            inner = fitz.Rect(rect.x0 + 1, rect.y0 + 1, rect.x1 - 1, rect.y1 - 1)
            paras, boxes = [], []
            for b in page.get_text("dict", clip=inner)["blocks"]:
                if b["type"] != 0:
                    continue
                lines = []
                for line in b["lines"]:
                    sp = "".join(_span_html(s, scale) for s in line["spans"])
                    if sp.strip():
                        lines.append(sp)
                    for s in line["spans"]:
                        if s["text"].strip():
                            boxes.append(fitz.Rect(s["bbox"]))
                if lines:
                    paras.append(" ".join(lines))
            align = "left"
            if boxes:
                tb = boxes[0]
                for bx in boxes[1:]:
                    tb = tb | bx
                mid = (rect.x0 + rect.x1) / 2
                if abs((tb.x0 + tb.x1) / 2 - mid) < rect.width * 0.08:
                    align = "center"
                elif rect.x1 - tb.x1 < rect.width * 0.12 and tb.x0 - rect.x0 > rect.width * 0.3:
                    align = "right"
            bg = ""
            if pix is not None:
                px = int(min(max(rect.x0 - bbox.x0 + 2, 0), pix.width - 1))
                py = int(min(max(rect.y0 - bbox.y0 + 2, 0), pix.height - 1))
                rgb = tuple(pix.pixel(px, py)[:3])
                if min(rgb) < 250:
                    bg = ' bgcolor="#%02x%02x%02x"' % rgb
            attrs = f' align="{align}" valign="middle"{bg}'
            if rspan > 1:
                attrs += f' rowspan="{rspan}"'
            if cspan > 1:
                attrs += f' colspan="{cspan}"'
            if r == 0 and cspan == 1:
                attrs += f' width="{rect.width / total_w * 100:.1f}%"'
            tds.append(f"<td{attrs}>{'<br>'.join(paras)}</td>")
        if tds:
            rows_html.append("<tr>" + "".join(tds) + "</tr>")
    width_pct = min(100.0, bbox.width / 468.0 * 100.0)
    return (f'<table border="1" cellspacing="0" cellpadding="6" width="{width_pct:.0f}%" '
            f'style="border-style:solid;border-color:#c7c7c7;">' + "".join(rows_html) + "</table>")


def _text_block_html(b, scale, median):
    spans, biggest, bold_chars, chars = [], 0.0, 0, 0
    for line in b["lines"]:
        for s in line["spans"]:
            t = s["text"]
            if not t:
                continue
            spans.append(_span_html(s, scale))
            if t.strip():
                biggest = max(biggest, s["size"])
                chars += len(t)
                bold_chars += len(t) if _is_bold(s) else 0
        spans.append(" ")
    content = "".join(spans).strip()
    if not content:
        return None
    ratio = biggest / median if median else 1
    if ratio >= 1.6:
        tag = "h1"
    elif ratio >= 1.35:
        tag = "h2"
    elif ratio >= 1.15 and chars and bold_chars / chars > 0.6:
        tag = "h3"
    else:
        tag = "p"
    return f"<{tag}%BRK%>{content}</{tag}>"


def _image_html(b, images):
    try:
        pix = fitz.Pixmap(b["image"])
        if pix.colorspace is not None and pix.colorspace.n >= 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        data = pix.tobytes("png")
    except Exception:
        return None
    name = f"pdfimg-{len(images) + 1}"
    images[name] = data
    x0, _y0, x1, _y1 = b["bbox"]
    w = max(24, min(int((x1 - x0) * 96 / 72), CONTENT_W))
    return f'<p%BRK%><img src="{name}" width="{w}"></p>'


def pdf_to_html(doc):
    """Approximate an ordinary PDF as flowing, editable HTML: text styles, images and real tables."""
    pages, sizes = [], []
    for page in doc:
        d = page.get_text("dict", sort=True)
        pages.append((d, _find_tables(page), page))
        for b in d["blocks"]:
            if b["type"] == 0:
                for line in b["lines"]:
                    for s in line["spans"]:
                        n = len(s["text"].strip())
                        if n:
                            sizes.extend([s["size"]] * min(n, 40))
    median = statistics.median(sizes) if sizes else 11.0
    scale = 11.0 / median if median else 1.0
    parts, images = [], {}
    for pi, (d, tabs, page) in enumerate(pages):
        tboxes = [fitz.Rect(t.bbox) for t in tabs]
        items = []
        for b in d["blocks"]:
            r = fitz.Rect(b["bbox"])
            ctr = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
            if any(tb.contains(ctr) for tb in tboxes):
                continue  # this text lives inside a table
            item = _text_block_html(b, scale, median) if b["type"] == 0 else (
                _image_html(b, images) if b["type"] == 1 else None)
            if item:
                items.append((r.y0, item))
        for tab, tb in zip(tabs, tboxes):
            idx = next((i for i, it in enumerate(items) if it[0] >= tb.y0 - 1), len(items))
            items.insert(idx, (tb.y0, _table_html(page, tab, scale)))
        for k, (_y, h) in enumerate(items):
            brk = ' style="page-break-before:always"' if (pi > 0 and k == 0) else ""
            if h.startswith("<table"):
                parts.append(('<p style="page-break-before:always"></p>' if brk else "") + h)
            else:
                parts.append(h.replace("%BRK%", brk, 1))
    body = "".join(parts) or "<p></p>"
    return f'<html><body style="font-family:Arial;font-size:11pt">{body}</body></html>', images


INLINE_RULES = [
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*$"), "bold"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~$"), "strike"),
    (re.compile(r"(?<![*\w])\*(?=[^\s*])([^*]+?)(?<=[^\s*])\*$"), "italic"),
    (re.compile(r"(?<![_\w])_(?=\S)([^_]+?)(?<=\S)_$"), "italic"),
    (re.compile(r"`([^`]+)`$"), "code"),
]


class PageEditor(QTextEdit):
    """One white US-Letter-wide page you type on, with Docs-style formatting commands."""

    GRIP = 10  # thickness of the row/column drag grips

    def __init__(self):
        super().__init__()
        self.setObjectName("sheet")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedWidth(SHEET_W + self.verticalScrollBar().sizeHint().width())
        self.setPlaceholderText("Start typing...  (try '# ' for a heading, '- ' for a bullet list, "
                                "or paste an image)")
        self.md_auto = True
        self.show_breaks = True
        self.notify = lambda msg: None  # the main window hooks this to the status bar
        self._drag = None
        self._hover = None
        self.viewport().setMouseTracking(True)

    # ---------- page-break guides ----------
    def paintEvent(self, e):
        super().paintEvent(e)
        vp = self.viewport()
        p = QPainter(vp)
        if self.show_breaks:
            p.setPen(QPen(QColor("#bdc1c6"), 1, Qt.PenStyle.DashLine))
            f = QFont("Segoe UI")
            f.setPixelSize(10)
            p.setFont(f)
            total = self.document().size().height()
            off = self.verticalScrollBar().value()
            k = 1
            while MARGIN + CONTENT_H * k < total - MARGIN:
                y = int(MARGIN + CONTENT_H * k - off)
                if 0 <= y <= vp.height():
                    p.drawLine(0, y, vp.width(), y)
                    p.drawText(vp.width() - 90, y - 4, f"page {k + 1} starts")
                k += 1
        self._paint_table_overlay(p)
        p.end()

    # ---------- helpers ----------
    def _blocks(self):
        c = self.textCursor()
        doc = self.document()
        end_pos = c.selectionEnd()
        if c.hasSelection() and doc.findBlock(end_pos).position() == end_pos:
            end_pos -= 1
        blk = doc.findBlock(c.selectionStart())
        last = doc.findBlock(end_pos).blockNumber()
        out = []
        while blk.isValid() and blk.blockNumber() <= last:
            out.append(blk)
            blk = blk.next()
        return out

    def _merge(self, fmt):
        self.mergeCurrentCharFormat(fmt)
        self.setFocus()

    # ---------- character formatting ----------
    def bold(self, on):
        f = QTextCharFormat()
        f.setFontWeight(700 if on else 400)
        self._merge(f)

    def italic(self, on):
        f = QTextCharFormat()
        f.setFontItalic(on)
        self._merge(f)

    def underline(self, on):
        f = QTextCharFormat()
        f.setFontUnderline(on)
        self._merge(f)

    def strike(self, on):
        f = QTextCharFormat()
        f.setFontStrikeOut(on)
        self._merge(f)

    def script(self, kind):
        f = QTextCharFormat()
        va = {"sup": QTextCharFormat.VerticalAlignment.AlignSuperScript,
              "sub": QTextCharFormat.VerticalAlignment.AlignSubScript}.get(
            kind, QTextCharFormat.VerticalAlignment.AlignNormal)
        f.setVerticalAlignment(va)
        self._merge(f)

    def set_family(self, family):
        f = QTextCharFormat()
        f.setFontFamilies([family])
        self._merge(f)

    def set_size(self, pt):
        f = QTextCharFormat()
        f.setFontPointSize(float(pt))
        self._merge(f)

    def set_color(self, color):
        f = QTextCharFormat()
        f.setForeground(QBrush(color))
        self._merge(f)

    def set_highlight(self, color):
        f = QTextCharFormat()
        f.setBackground(QBrush(color) if color is not None else QBrush(Qt.BrushStyle.NoBrush))
        self._merge(f)

    def clear_formatting(self):
        c = self.textCursor()
        if not c.hasSelection():
            c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            c.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        f = QTextCharFormat()
        f.setFont(self.document().defaultFont())
        c.setCharFormat(f)
        self.setFocus()

    # ---------- paragraph formatting ----------
    def current_style_name(self):
        c = self.textCursor()
        lvl = c.blockFormat().headingLevel()
        if 1 <= lvl <= 3:
            return f"Heading {lvl}"
        cf = c.charFormat()
        size = cf.fontPointSize() or self.document().defaultFont().pointSizeF()
        if abs(size - 26) < 0.6:
            return "Title"
        if abs(size - 15) < 0.6 and cf.foreground().color().name().lower() == "#666666":
            return "Subtitle"
        return "Normal text"

    def apply_style(self, name):
        lvl, size, bold, italic, color, top, bottom = STYLES[name]
        cur = self.textCursor()
        cur.beginEditBlock()
        f = QTextCharFormat()
        f.setFontPointSize(size)
        f.setFontWeight(700 if bold else 400)
        f.setFontItalic(italic)
        f.setForeground(QBrush(QColor(color)))
        for blk in self._blocks():
            bc = QTextCursor(blk)
            bf = blk.blockFormat()
            bf.setHeadingLevel(lvl)
            bf.setTopMargin(top)
            bf.setBottomMargin(bottom)
            bf.setLeftMargin(0)
            bf.setLineHeight(115.0, 1)
            bc.setBlockFormat(bf)
            bc.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            bc.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            bc.mergeCharFormat(f)
            bc.setBlockCharFormat(f)
        cur.endEditBlock()
        self.mergeCurrentCharFormat(f)
        self.setFocus()

    def apply_quote(self):
        cur = self.textCursor()
        cur.beginEditBlock()
        f = QTextCharFormat()
        f.setFontItalic(True)
        f.setForeground(QBrush(QColor("#5f6368")))
        for blk in self._blocks():
            bc = QTextCursor(blk)
            bf = blk.blockFormat()
            bf.setLeftMargin(28)
            bc.setBlockFormat(bf)
            bc.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            bc.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            bc.mergeCharFormat(f)
            bc.setBlockCharFormat(f)
        cur.endEditBlock()
        self.mergeCurrentCharFormat(f)

    def set_line_spacing(self, factor):
        cur = self.textCursor()
        cur.beginEditBlock()
        for blk in self._blocks():
            bf = blk.blockFormat()
            bf.setLineHeight(factor * 100.0, 1)
            QTextCursor(blk).setBlockFormat(bf)
        cur.endEditBlock()

    def toggle_list(self, style):
        c = self.textCursor()
        lst = c.currentList()
        c.beginEditBlock()
        if lst is not None and lst.format().style() == style:
            for blk in self._blocks():
                tl = blk.textList()
                if tl is not None:
                    tl.remove(blk)
                bc = QTextCursor(blk)
                bf = bc.blockFormat()
                bf.setIndent(0)
                bc.setBlockFormat(bf)
        else:
            fmt = QTextListFormat()
            fmt.setStyle(style)
            fmt.setIndent(1)
            c.createList(fmt)
        c.endEditBlock()
        self.setFocus()

    def change_indent(self, delta):
        c = self.textCursor()
        c.beginEditBlock()
        for blk in self._blocks():
            lst = blk.textList()
            if lst is not None:
                lf = lst.format()
                lf.setIndent(max(1, lf.indent() + delta))
                lst.setFormat(lf)
            else:
                bf = blk.blockFormat()
                bf.setIndent(max(0, bf.indent() + delta))
                QTextCursor(blk).setBlockFormat(bf)
        c.endEditBlock()
        self.setFocus()

    # ---------- inserting things ----------
    def insert_link(self):
        c = self.textCursor()
        url, ok = QInputDialog.getText(self, "Insert link", "URL:", text="https://")
        if not ok or not url.strip():
            return
        f = QTextCharFormat()
        f.setAnchorHref(url.strip())
        f.setForeground(QBrush(QColor("#1a73e8")))
        f.setFontUnderline(True)
        if c.hasSelection():
            c.mergeCharFormat(f)
        else:
            base = QTextCharFormat(self.currentCharFormat())
            c.insertText(url.strip(), f)
            self.setTextCursor(c)
            self.setCurrentCharFormat(base)
        self.setFocus()

    def insert_image(self, image: QImage):
        name = f"img-{uuid.uuid4().hex[:12]}"
        self.document().addResource(IMAGE_RES, QUrl(name), image)
        fmt = QTextImageFormat()
        fmt.setName(name)
        w = min(image.width(), CONTENT_W)
        fmt.setWidth(w)
        fmt.setHeight(w * image.height() / max(image.width(), 1))
        self.textCursor().insertImage(fmt)
        self.setFocus()

    def insert_table(self, rows, cols):
        f = QTextTableFormat()
        f.setBorder(1)
        f.setCellPadding(4)
        f.setCellSpacing(0)
        f.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
        self.textCursor().insertTable(rows, cols, f)
        self.setFocus()

    def insert_markdown(self, text):
        tmp = QTextDocument()
        tmp.setDefaultFont(self.document().defaultFont())
        tmp.setMarkdown(text)
        self.textCursor().insertFragment(QTextDocumentFragment(tmp))

    def selection_markdown(self):
        c = self.textCursor()
        if not c.hasSelection():
            return ""
        tmp = QTextDocument()
        QTextCursor(tmp).insertFragment(QTextDocumentFragment(c))
        return tmp.toMarkdown()

    def headings(self):
        out, blk = [], self.document().begin()
        while blk.isValid():
            lvl = blk.blockFormat().headingLevel()
            if 1 <= lvl <= 3 and blk.text().strip():
                out.append((lvl, blk.text().strip(), blk.position()))
            blk = blk.next()
        return out

    def word_count(self):
        t = self.toPlainText()
        return len(t.split()), len(t)

    # ---------- clipboard: images and Markdown ----------
    def canInsertFromMimeData(self, source):
        return source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QPixmap):
                img = img.toImage()
            if isinstance(img, QImage) and not img.isNull():
                self.insert_image(img)
                return
        if source.hasUrls():
            handled = False
            for u in source.urls():
                p = u.toLocalFile()
                if p.lower().endswith(IMG_EXT):
                    img = QImage(p)
                    if not img.isNull():
                        self.insert_image(img)
                        handled = True
            if handled:
                return
        if self.md_auto and source.hasText() and not source.hasHtml() and looks_like_markdown(source.text()):
            self.insert_markdown(source.text())
            return
        super().insertFromMimeData(source)

    # ---------- mouse ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            hit = self._hit_grip(e.position().toPoint())
            if hit:
                self._drag = {"kind": hit[0], "idx": hit[1], "table": hit[2], "drop": None,
                              "moved": False, "start": e.position().toPoint()}
                self.viewport().update()
                return
            if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
                href = self.anchorAt(e.position().toPoint())
                if href:
                    QDesktopServices.openUrl(QUrl(href))
                    return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        d = self._drag
        if d:
            if (pos - d["start"]).manhattanLength() > 6:
                d["moved"] = True
            xs, ys, _rows, _cols = self._grips(d["table"])
            edges, v = (ys, pos.y()) if d["kind"] == "row" else (xs, pos.x())
            d["drop"] = min(range(len(edges)), key=lambda k: abs(edges[k] - v))
            self.viewport().update()
            return
        hit = self._hit_grip(pos)
        new = (hit[0], hit[1]) if hit else None
        if new != self._hover:
            self._hover = new
            self.viewport().update()
        self.viewport().setCursor(Qt.CursorShape.SizeAllCursor if hit else Qt.CursorShape.IBeamCursor)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag:
            d, self._drag = self._drag, None
            t, kind, src_i = d["table"], d["kind"], d["idx"]
            if d["moved"] and d["drop"] is not None:
                dst = d["drop"] - 1 if d["drop"] > src_i else d["drop"]
                if dst != src_i and self.table_move(t, kind, src_i, dst):
                    self.select_table_line(t, kind, dst)
                else:
                    self.select_table_line(t, kind, src_i)
            else:
                self.select_table_line(t, kind, src_i)
            self.viewport().update()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        c = self.cursorForPosition(e.position().toPoint())
        last = self.document().characterCount() - 1
        for p in (c.position() - 1, c.position()):
            if 0 <= p < last:
                sc = QTextCursor(self.document())
                sc.setPosition(p)
                sc.setPosition(p + 1, QTextCursor.MoveMode.KeepAnchor)
                f = sc.charFormat()
                if f.isImageFormat():
                    self._resize_image(sc, f.toImageFormat())
                    return
        super().mouseDoubleClickEvent(e)

    def _resize_image(self, sc, fmt):
        img = _image_from_resource(self.document().resource(IMAGE_RES, QUrl(fmt.name())))
        aspect = (img.height() / img.width()) if img is not None else (fmt.height() / max(fmt.width(), 1))
        w, ok = QInputDialog.getInt(self, "Image size", f"Width in pixels (max {CONTENT_W}):",
                                    int(fmt.width()) or 300, 20, CONTENT_W)
        if ok:
            fmt.setWidth(w)
            fmt.setHeight(w * aspect)
            sc.setCharFormat(fmt)

    # ---------- tables: rows, columns, colors, drag to reorder ----------
    def _table_ctx(self):
        c = self.textCursor()
        t = c.currentTable()
        if t is None:
            self.notify("Click inside a table first.")
            return None, None
        return t, t.cellAt(c)

    @staticmethod
    def _constraints(t):
        return list(t.format().columnWidthConstraints())

    @staticmethod
    def _set_constraints(t, lengths):
        if not lengths:
            return
        if all(ln.type() == QTextLength.Type.PercentageLength for ln in lengths):
            total = sum(ln.rawValue() for ln in lengths) or 1.0
            lengths = [QTextLength(QTextLength.Type.PercentageLength, ln.rawValue() * 100.0 / total)
                       for ln in lengths]
        fmt = t.format()
        fmt.setColumnWidthConstraints(lengths)
        t.setFormat(fmt)

    def _cell_style(self, cell):
        """Everything needed to clone a cell's look: fill, borders, alignment, font, color."""
        first = cell.firstCursorPosition()
        cf = first.blockCharFormat()
        if cell.lastPosition() > cell.firstPosition():
            probe = QTextCursor(self.document())
            probe.setPosition(cell.firstPosition() + 1)
            cf = probe.charFormat()
        return {"cell": cell.format().toTableCellFormat(), "block": first.blockFormat(), "char": cf}

    @staticmethod
    def _apply_cell_style(cell, st):
        cell.setFormat(st["cell"])
        cur = cell.firstCursorPosition()
        cur.setBlockFormat(st["block"])
        cur.setBlockCharFormat(st["char"])

    def _cell_range(self):
        t, cell = self._table_ctx()
        if t is None:
            return None
        r0, r1 = cell.row(), cell.row() + cell.rowSpan() - 1
        c0, c1 = cell.column(), cell.column() + cell.columnSpan() - 1
        cur = self.textCursor()
        if cur.hasComplexSelection():
            try:
                fr, nr, fc, nc = cur.selectedTableCells()[-4:]
                if fr >= 0:
                    r0, r1, c0, c1 = fr, fr + nr - 1, fc, fc + nc - 1
            except Exception:
                pass
        return t, r0, r1, c0, c1

    @staticmethod
    def _unique_cells(t, r0, r1, c0, c1):
        seen, out = set(), []
        for r in range(max(0, r0), min(t.rows() - 1, r1) + 1):
            for c in range(max(0, c0), min(t.columns() - 1, c1) + 1):
                cl = t.cellAt(r, c)
                key = (cl.row(), cl.column())
                if key not in seen:
                    seen.add(key)
                    out.append(cl)
        return out

    def table_insert_row(self, below=True):
        t, cell = self._table_ctx()
        if t is None:
            return
        edge = cell.row() + cell.rowSpan() - 1 if below else cell.row()
        idx = edge + 1 if below else edge
        styles = [self._cell_style(t.cellAt(edge, c)) for c in range(t.columns())]
        guard = self.textCursor()
        guard.beginEditBlock()
        t.insertRows(idx, 1)
        for c in range(t.columns()):
            self._apply_cell_style(t.cellAt(idx, c), styles[c])
        guard.endEditBlock()
        self.setFocus()

    def table_insert_col(self, right=True):
        t, cell = self._table_ctx()
        if t is None:
            return
        edge = cell.column() + cell.columnSpan() - 1 if right else cell.column()
        idx = edge + 1 if right else edge
        styles = [self._cell_style(t.cellAt(r, edge)) for r in range(t.rows())]
        cons = self._constraints(t)
        guard = self.textCursor()
        guard.beginEditBlock()
        t.insertColumns(idx, 1)
        for r in range(t.rows()):
            self._apply_cell_style(t.cellAt(r, idx), styles[r])
        if len(cons) == t.columns() - 1:
            cons.insert(idx, cons[edge])
            self._set_constraints(t, cons)
        guard.endEditBlock()
        self.setFocus()

    def table_delete_row(self):
        rng = self._cell_range()
        if rng is None:
            return
        t, r0, r1, _c0, _c1 = rng
        if r1 - r0 + 1 >= t.rows():
            self.table_delete()
            return
        t.removeRows(r0, r1 - r0 + 1)

    def table_delete_col(self):
        rng = self._cell_range()
        if rng is None:
            return
        t, _r0, _r1, c0, c1 = rng
        if c1 - c0 + 1 >= t.columns():
            self.table_delete()
            return
        cons, old = self._constraints(t), t.columns()
        t.removeColumns(c0, c1 - c0 + 1)
        if len(cons) == old:
            del cons[c0:c1 + 1]
            self._set_constraints(t, cons)

    def table_delete(self):
        t, _cell = self._table_ctx()
        if t is None:
            return
        cur = QTextCursor(self.document())
        cur.setPosition(t.firstPosition() - 1)
        cur.setPosition(t.lastPosition() + 1, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()

    def _scoped_range(self, scope):
        rng = self._cell_range()
        if rng is None:
            return None
        t, r0, r1, c0, c1 = rng
        if scope == "row":
            c0, c1 = 0, t.columns() - 1
        elif scope == "col":
            r0, r1 = 0, t.rows() - 1
        elif scope == "table":
            r0, r1, c0, c1 = 0, t.rows() - 1, 0, t.columns() - 1
        return t, r0, r1, c0, c1

    def table_color(self, scope, color):
        """Fill color for the cell(s) / row / column / whole table. color=None clears it."""
        rng = self._scoped_range(scope)
        if rng is None:
            return
        t, r0, r1, c0, c1 = rng
        guard = self.textCursor()
        guard.beginEditBlock()
        for cl in self._unique_cells(t, r0, r1, c0, c1):
            f = cl.format().toTableCellFormat()
            f.setBackground(QBrush(color) if color is not None else QBrush(Qt.BrushStyle.NoBrush))
            cl.setFormat(f)
        guard.endEditBlock()

    def table_text_color(self, scope, color):
        rng = self._scoped_range(scope)
        if rng is None or color is None:
            return
        t, r0, r1, c0, c1 = rng
        guard = self.textCursor()
        guard.beginEditBlock()
        for cl in self._unique_cells(t, r0, r1, c0, c1):
            cur = QTextCursor(self.document())
            cur.setPosition(cl.firstPosition())
            cur.setPosition(cl.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
            f = QTextCharFormat()
            f.setForeground(QBrush(color))
            cur.mergeCharFormat(f)
            first = cl.firstCursorPosition()
            bcf = first.blockCharFormat()
            bcf.setForeground(QBrush(color))
            first.setBlockCharFormat(bcf)
        guard.endEditBlock()

    def table_border(self, color=None, width=None):
        t, _cell = self._table_ctx()
        if t is None:
            return
        fmt = t.format()
        if color is not None:
            fmt.setBorderBrush(QBrush(color))
        if width is not None:
            fmt.setBorder(width)
        t.setFormat(fmt)

    def table_merge(self):
        t, _cell = self._table_ctx()
        if t is not None:
            t.mergeCells(self.textCursor())

    def table_split(self):
        t, cell = self._table_ctx()
        if t is not None:
            t.splitCell(cell.row(), cell.column(), 1, 1)

    def table_move_step(self, kind, delta):
        t, cell = self._table_ctx()
        if t is None:
            return
        src_i = cell.row() if kind == "row" else cell.column()
        dst = src_i + delta
        n = t.rows() if kind == "row" else t.columns()
        if 0 <= dst < n and self.table_move(t, kind, src_i, dst):
            nc = t.cellAt(dst, cell.column()) if kind == "row" else t.cellAt(cell.row(), dst)
            self.setTextCursor(nc.firstCursorPosition())

    def table_move(self, t, kind, src_i, dst):
        """Move row/column src_i so it ends up at index dst. Content, colors and formatting travel with it."""
        rows, cols = t.rows(), t.columns()
        for r in range(rows):
            for c in range(cols):
                cl = t.cellAt(r, c)
                if cl.rowSpan() > 1 or cl.columnSpan() > 1:
                    self.notify("Split merged cells first (Table > Split cell), then move rows or columns.")
                    return False
        n = rows if kind == "row" else cols
        if src_i == dst or not (0 <= src_i < n and 0 <= dst < n):
            return False
        order = list(range(n))
        order.insert(dst, order.pop(src_i))
        doc = self.document()
        snap = {}
        for r in range(rows):
            for c in range(cols):
                cl = t.cellAt(r, c)
                cur = QTextCursor(doc)
                cur.setPosition(cl.firstPosition())
                cur.setPosition(cl.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
                first = cl.firstCursorPosition()
                snap[(r, c)] = (QTextDocumentFragment(cur), cl.format().toTableCellFormat(),
                                first.blockFormat(), first.blockCharFormat())
        cons = self._constraints(t)
        guard = self.textCursor()
        guard.beginEditBlock()
        for r in range(rows):
            for c in range(cols):
                sr, sc = (order[r], c) if kind == "row" else (r, order[c])
                frag, cell_fmt, blk_fmt, blk_cfmt = snap[(sr, sc)]
                cl = t.cellAt(r, c)
                cur = QTextCursor(doc)
                cur.setPosition(cl.firstPosition())
                cur.setPosition(cl.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
                cur.removeSelectedText()
                cl = t.cellAt(r, c)
                dest = cl.firstCursorPosition()
                if not frag.isEmpty():
                    dest.insertFragment(frag)
                first = cl.firstCursorPosition()
                first.setBlockFormat(blk_fmt)
                first.setBlockCharFormat(blk_cfmt)
                cl.setFormat(cell_fmt)
        if kind == "col" and len(cons) == cols:
            self._set_constraints(t, [cons[i] for i in order])
        guard.endEditBlock()
        return True

    def select_table_line(self, t, kind, idx):
        try:
            if kind == "row":
                a, b = t.cellAt(idx, 0), t.cellAt(idx, t.columns() - 1)
            else:
                a, b = t.cellAt(0, idx), t.cellAt(t.rows() - 1, idx)
            cur = QTextCursor(self.document())
            cur.setPosition(a.firstPosition())
            cur.setPosition(b.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cur)
        except Exception:
            pass

    # ----- drag grips -----
    def _table_geometry(self, t):
        """Row/column edge positions in viewport coordinates (approximate, from the layout)."""
        lay = self.document().documentLayout()
        fr = lay.frameBoundingRect(t)
        dy, dx = self.verticalScrollBar().value(), self.horizontalScrollBar().value()
        fmt = t.format()
        inset = fmt.cellPadding() + fmt.border()
        rows, cols = t.rows(), t.columns()
        tops, lefts = [None] * rows, [None] * cols
        for r in range(rows):
            for c in range(cols):
                cl = t.cellAt(r, c)
                if cl.row() != r or cl.column() != c:
                    continue
                br = lay.blockBoundingRect(cl.firstCursorPosition().block())
                tops[r] = br.top() if tops[r] is None else min(tops[r], br.top())
                lefts[c] = br.left() if lefts[c] is None else min(lefts[c], br.left())
        ys = [fr.top()] + [(tops[r] - inset) if tops[r] is not None else fr.top() for r in range(1, rows)] \
            + [fr.bottom()]
        xs = [fr.left()] + [(lefts[c] - inset) if lefts[c] is not None else fr.left() for c in range(1, cols)] \
            + [fr.right()]
        return [x - dx for x in xs], [y - dy for y in ys]

    def _grips(self, t):
        xs, ys = self._table_geometry(t)
        g = self.GRIP
        rows = [QRect(int(xs[0]) - g - 6, int(ys[r]) + 2, g, max(6, int(ys[r + 1] - ys[r]) - 4))
                for r in range(len(ys) - 1)]
        cols = [QRect(int(xs[c]) + 2, int(ys[0]) - g - 6, max(6, int(xs[c + 1] - xs[c]) - 4), g)
                for c in range(len(xs) - 1)]
        return xs, ys, rows, cols

    def _hit_grip(self, pos):
        t = self._drag["table"] if self._drag else self.textCursor().currentTable()
        if t is None:
            return None
        try:
            _xs, _ys, rows, cols = self._grips(t)
        except Exception:
            return None
        for i, r in enumerate(rows):
            if r.adjusted(-3, 0, 3, 0).contains(pos):
                return ("row", i, t)
        for i, r in enumerate(cols):
            if r.adjusted(0, -3, 0, 3).contains(pos):
                return ("col", i, t)
        return None

    def _paint_table_overlay(self, p):
        d = self._drag
        t = d["table"] if d else self.textCursor().currentTable()
        if t is None:
            return
        try:
            xs, ys, rows, cols = self._grips(t)
        except Exception:
            return
        p.setPen(Qt.PenStyle.NoPen)
        for kind, rects in (("row", rows), ("col", cols)):
            for i, r in enumerate(rects):
                hot = self._hover == (kind, i) or (d is not None and d["kind"] == kind and d["idx"] == i)
                p.setBrush(QColor("#1a73e8") if hot else QColor("#c7cbd1"))
                p.drawRoundedRect(r, 3, 3)
        if d and d["moved"]:
            kind, idx = d["kind"], d["idx"]
            p.setBrush(QColor(26, 115, 232, 45))
            if kind == "row":
                p.drawRect(QRect(int(xs[0]), int(ys[idx]), int(xs[-1] - xs[0]), int(ys[idx + 1] - ys[idx])))
            else:
                p.drawRect(QRect(int(xs[idx]), int(ys[0]), int(xs[idx + 1] - xs[idx]), int(ys[-1] - ys[0])))
            if d["drop"] is not None:
                p.setPen(QPen(QColor("#1a73e8"), 3))
                if kind == "row":
                    y = int(ys[d["drop"]])
                    p.drawLine(int(xs[0]), y, int(xs[-1]), y)
                else:
                    x = int(xs[d["drop"]])
                    p.drawLine(x, int(ys[0]), x, int(ys[-1]))

    # ----- table menu (right-click, Table menu, toolbar button) -----
    def _custom_then(self, fn):
        c = QColorDialog.getColor(QColor("#ffffff"), self, "Pick a color")
        if c.isValid():
            fn(c)

    def populate_table_menu(self, menu):
        def act(text, fn):
            menu.addAction(text).triggered.connect(lambda _=False: fn())

        def colors(title, fn, none_label="No color"):
            sub = menu.addMenu(title)
            for name, hexv in TABLE_COLORS:
                pm = QPixmap(14, 14)
                pm.fill(QColor(hexv))
                sub.addAction(QIcon(pm), name).triggered.connect(lambda _=False, h=hexv: fn(QColor(h)))
            sub.addAction(none_label).triggered.connect(lambda _=False: fn(None))
            sub.addAction("Custom...").triggered.connect(lambda _=False: self._custom_then(fn))

        act("Insert row above", lambda: self.table_insert_row(False))
        act("Insert row below", lambda: self.table_insert_row(True))
        act("Insert column left", lambda: self.table_insert_col(False))
        act("Insert column right", lambda: self.table_insert_col(True))
        menu.addSeparator()
        act("Delete row", self.table_delete_row)
        act("Delete column", self.table_delete_col)
        act("Delete table", self.table_delete)
        menu.addSeparator()
        colors("Cell color", lambda c: self.table_color("cell", c))
        colors("Row color", lambda c: self.table_color("row", c))
        colors("Column color", lambda c: self.table_color("col", c))
        colors("Whole table color", lambda c: self.table_color("table", c))
        colors("Text color (selected cells)", lambda c: self.table_text_color("cell", c))
        border = menu.addMenu("Borders")
        border.addAction("Border color...").triggered.connect(
            lambda _=False: self._custom_then(lambda c: self.table_border(color=c)))
        for label, w in (("No border", 0), ("Thin (1 px)", 1), ("Medium (2 px)", 2), ("Thick (3 px)", 3)):
            border.addAction(label).triggered.connect(lambda _=False, w=w: self.table_border(width=w))
        menu.addSeparator()
        act("Merge selected cells", self.table_merge)
        act("Split cell", self.table_split)
        menu.addSeparator()
        act("Move row up", lambda: self.table_move_step("row", -1))
        act("Move row down", lambda: self.table_move_step("row", 1))
        act("Move column left", lambda: self.table_move_step("col", -1))
        act("Move column right", lambda: self.table_move_step("col", 1))

    def contextMenuEvent(self, e):
        if not self.textCursor().hasSelection():
            self.setTextCursor(self.cursorForPosition(e.pos()))
        if self.textCursor().currentTable() is None:
            super().contextMenuEvent(e)
            return
        menu = QMenu(self)
        self.populate_table_menu(menu)
        menu.exec(e.globalPos())

    # ---------- Markdown auto-format while typing ----------
    def keyPressEvent(self, e):
        super().keyPressEvent(e)
        if not self.md_auto:
            return
        if e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                            | Qt.KeyboardModifier.MetaModifier):
            return
        t = e.text()
        if t == " ":
            self._md_block()
        elif t in ("*", "`", "~", "_"):
            self._md_inline()

    def _md_block(self):
        c = self.textCursor()
        if c.hasSelection():
            return
        before = c.block().text()[:c.positionInBlock()]
        action = None
        m = re.fullmatch(r"(#{1,6}) ", before)
        if m:
            action = ("style", f"Heading {min(len(m.group(1)), 3)}")
        elif before in ("- ", "* ", "+ "):
            action = ("list", QTextListFormat.Style.ListDisc)
        elif re.fullmatch(r"\d+\. ", before):
            action = ("list", QTextListFormat.Style.ListDecimal)
        elif before == "> ":
            action = ("quote", None)
        if not action:
            return
        c.beginEditBlock()
        start = c.block().position()
        c.setPosition(start)
        c.setPosition(start + len(before), QTextCursor.MoveMode.KeepAnchor)
        c.removeSelectedText()
        self.setTextCursor(c)
        if action[0] == "style":
            self.apply_style(action[1])
        elif action[0] == "list":
            self.toggle_list(action[1])
        else:
            self.apply_quote()
        c.endEditBlock()

    def _md_inline(self):
        c = self.textCursor()
        if c.hasSelection():
            return
        blk = c.block()
        pos = c.positionInBlock()
        before = blk.text()[:pos]
        for rx, kind in INLINE_RULES:
            m = rx.search(before)
            if not m:
                continue
            base = QTextCharFormat(self.currentCharFormat())
            f = QTextCharFormat(base)
            if kind == "bold":
                f.setFontWeight(700)
            elif kind == "italic":
                f.setFontItalic(True)
            elif kind == "strike":
                f.setFontStrikeOut(True)
            else:
                f.setFontFamilies(["Consolas"])
                f.setBackground(QBrush(QColor("#f1f3f4")))
            c.beginEditBlock()
            c.setPosition(blk.position() + m.start())
            c.setPosition(blk.position() + pos, QTextCursor.MoveMode.KeepAnchor)
            c.removeSelectedText()
            c.insertText(m.group(1), f)
            c.endEditBlock()
            self.setTextCursor(c)
            self.setCurrentCharFormat(base)
            return
