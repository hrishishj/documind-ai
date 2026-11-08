"""Builders for sample/test Office templates with deliberately tricky formatting.

Used by tests and by tools/make_sample_templates.py to populate sample_templates/.
"""
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Emu, Inches, Pt, RGBColor
from PIL import Image


def png_bytes(color=(200, 30, 30), size=(120, 60)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_floating(run, x_emu: int, y_emu: int) -> None:
    """Convert the run's inline picture into a page-anchored floating picture."""
    drawing = run._r.find(qn("w:drawing"))
    inline = drawing.find(qn("wp:inline"))
    extent = inline.find(qn("wp:extent"))
    graphic = inline.find(qn("a:graphic"))
    anchor = parse_xml(
        f'<wp:anchor {nsdecls("wp", "a")} distT="0" distB="0" distL="114300" distR="114300" '
        'simplePos="0" relativeHeight="1" behindDoc="1" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="page"><wp:posOffset>{x_emu}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="page"><wp:posOffset>{y_emu}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{extent.get("cx")}" cy="{extent.get("cy")}"/>'
        '<wp:wrapNone/>'
        '<wp:docPr id="99" name="Floating logo" descr="Company watermark"/>'
        "</wp:anchor>"
    )
    anchor.append(graphic)
    drawing.replace(inline, anchor)


def build_docx(path: Path) -> dict:
    """Create a DOCX with known formatting; returns the ground-truth values used."""
    doc = Document()
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.PORTRAIT
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.left_margin, sec.right_margin = Inches(1.25), Inches(0.75)
    sec.top_margin, sec.bottom_margin = Inches(1.5), Inches(1.0)

    # custom fonts / colours on styles
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Georgia", Pt(11)
    h1 = doc.styles["Heading 1"]
    h1.font.name, h1.font.size, h1.font.bold = "Arial", Pt(20), True
    h1.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)

    # header: text + inline logo ; footer: text + PAGE field
    hp = sec.header.paragraphs[0]
    hp.text = "ACME Corp - Confidential"
    hp.add_run().add_picture(BytesIO(png_bytes((10, 90, 200))), width=Inches(0.8))
    fp = sec.footer.paragraphs[0]
    fp.text = "Page "
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fp._p.append(parse_xml(f'<w:fldSimple {nsdecls("w")} w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>'))

    doc.add_heading("Quarterly Report", level=1)
    p = doc.add_paragraph("Intro paragraph in Georgia. ")
    r = p.add_run("Bold red run.")
    r.bold, r.font.color.rgb = True, RGBColor(0xC0, 0x00, 0x00)
    doc.add_heading("Details", level=2)

    # floating logo anchored to the page
    fl = doc.add_paragraph().add_run()
    fl.add_picture(BytesIO(png_bytes()), width=Inches(1.5), height=Inches(0.75))
    _make_floating(fl, x_emu=914400, y_emu=457200)

    # table with explicit borders, header shading, merged cell
    t = doc.add_table(rows=3, cols=3)
    tblPr = t._tbl.tblPr
    tblPr.append(parse_xml(
        f'<w:tblBorders {nsdecls("w")}>'
        '<w:top w:val="single" w:sz="12" w:color="FF0000"/>'
        '<w:left w:val="single" w:sz="4" w:color="000000"/>'
        '<w:bottom w:val="double" w:sz="6" w:color="0000FF"/>'
        '<w:right w:val="single" w:sz="4" w:color="000000"/>'
        '<w:insideH w:val="dashed" w:sz="4" w:color="808080"/>'
        '<w:insideV w:val="nil"/>'
        "</w:tblBorders>"))
    for i, h in enumerate(["Region", "Q1", "Q2"]):
        c = t.rows[0].cells[i]
        c.text = h
        c._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="D9E2F3"/>'))
    t.rows[1].cells[0].text = "North"
    t.rows[1].cells[1].text = "10"
    t.rows[1].cells[2].text = "20"
    t.rows[2].cells[0].merge(t.rows[2].cells[1])
    t.rows[2].cells[0].text = "Merged total"
    t.rows[2].cells[2].text = "30"

    doc.save(path)
    return {
        "page": (8.5, 11.0),
        "margins": {"left": 1.25, "right": 0.75, "top": 1.5, "bottom": 1.0},
        "normal_font": "Georgia", "h1_font": "Arial", "h1_size": 20.0, "h1_color": "1F4E79",
        "header_text": "ACME Corp - Confidential",
        "floating": {"x_in": 1.0, "y_in": 0.5, "w_in": 1.5, "h_in": 0.75, "behind": True},
        "table_borders": {"top": ("single", "FF0000"), "bottom": ("double", "0000FF"),
                          "insideH": ("dashed", "808080"), "insideV": ("nil", None)},
        "header_fill": "D9E2F3",
    }
