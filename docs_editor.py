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
from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QSizeF, Qt, QUrl
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QImage, QPageLayout, QPageSize,
                           QPainter, QPdfWriter, QPen, QPixmap, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextDocument, QTextDocumentFragment, QTextImageFormat,
                           QTextLength, QTextListFormat, QTextTableFormat)
from PySide6.QtWidgets import QFrame, QInputDialog, QTextEdit

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


def pdf_to_html(doc):
    """Approximate an ordinary PDF as flowing, editable HTML (text styles + images)."""
    pages, sizes = [], []
    for page in doc:
        d = page.get_text("dict", sort=True)
        pages.append(d)
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
    for pi, d in enumerate(pages):
        need_break = pi > 0
        for b in d["blocks"]:
            style = ' style="page-break-before:always"' if need_break else ""
            if b["type"] == 0:
                spans, biggest, bold_chars, chars = [], 0.0, 0, 0
                for line in b["lines"]:
                    for s in line["spans"]:
                        t = s["text"]
                        if not t:
                            continue
                        bold = bool(s["flags"] & 16) or "bold" in s["font"].lower()
                        italic = bool(s["flags"] & 2) or "italic" in s["font"].lower()
                        css = [f"font-size:{s['size'] * scale:.1f}pt"]
                        color = "#%06x" % s["color"]
                        if color != "#000000":
                            css.append(f"color:{color}")
                        if bold:
                            css.append("font-weight:bold")
                        if italic:
                            css.append("font-style:italic")
                        spans.append(f'<span style="{";".join(css)}">{htmlmod.escape(t)}</span>')
                        if t.strip():
                            biggest = max(biggest, s["size"])
                            chars += len(t)
                            bold_chars += len(t) if bold else 0
                    spans.append(" ")
                content = "".join(spans).strip()
                if not content:
                    continue
                ratio = biggest / median if median else 1
                if ratio >= 1.6:
                    tag = "h1"
                elif ratio >= 1.35:
                    tag = "h2"
                elif ratio >= 1.15 and chars and bold_chars / chars > 0.6:
                    tag = "h3"
                else:
                    tag = "p"
                parts.append(f"<{tag}{style}>{content}</{tag}>")
                need_break = False
            elif b["type"] == 1:
                try:
                    pix = fitz.Pixmap(b["image"])
                    if pix.colorspace is not None and pix.colorspace.n >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    data = pix.tobytes("png")
                except Exception:
                    continue
                name = f"pdfimg-{len(images) + 1}"
                images[name] = data
                x0, _y0, x1, _y1 = b["bbox"]
                w = max(24, min(int((x1 - x0) * 96 / 72), CONTENT_W))
                parts.append(f'<p{style}><img src="{name}" width="{w}"></p>')
                need_break = False
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

    # ---------- page-break guides ----------
    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.show_breaks:
            return
        vp = self.viewport()
        p = QPainter(vp)
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
        if e.button() == Qt.MouseButton.LeftButton and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            href = self.anchorAt(e.position().toPoint())
            if href:
                QDesktopServices.openUrl(QUrl(href))
                return
        super().mousePressEvent(e)

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
