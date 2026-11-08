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


def build_pptx(path: Path) -> dict:
    """PPTX with known layout/format; returns the ground truth."""
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.dml.color import RGBColor as PRGB
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.util import Inches as PIn, Pt as PPt

    prs = Presentation()
    prs.slide_width, prs.slide_height = PIn(13.333), PIn(7.5)  # 16:9

    master = prs.slide_masters[0]
    theme = master.part.part_related_by(RT.THEME)
    import re

    blob = re.sub(rb'(<a:majorFont>\s*<a:latin typeface=")[^"]*', rb"\1Georgia", theme.blob)
    blob = re.sub(rb'(<a:minorFont>\s*<a:latin typeface=")[^"]*', rb"\1Verdana", blob)
    assert blob != theme.blob, "theme font patch had no effect"
    theme._blob = blob

    from pptx.oxml.shapes.picture import CT_Picture

    def add_pic(owner, blob, x, y, w, h, name):
        """python-pptx only exposes add_picture on slides; masters/layouts need the part-level route."""
        _, rid = owner.part.get_or_add_image_part(BytesIO(blob))
        owner.shapes._spTree.append(
            CT_Picture.new_pic(owner.shapes._next_shape_id, name, name, rid, PIn(x), PIn(y), PIn(w), PIn(h)))

    add_pic(master, png_bytes((180, 20, 20), (200, 100)), 11.5, 0.3, 1.2, 0.6, "Master Logo")
    layout_title_only = next(l for l in prs.slide_layouts if l.name == "Title Only")
    add_pic(layout_title_only, png_bytes((20, 20, 180), (100, 100)), 0.4, 6.8, 0.5, 0.5, "Layout Logo")

    s1 = prs.slides.add_slide(next(l for l in prs.slide_layouts if l.name == "Title Slide"))
    s1.shapes.title.text = "Annual Review"
    s1.placeholders[1].text = "FY2024"

    s2 = prs.slides.add_slide(next(l for l in prs.slide_layouts if l.name == "Title and Content"))
    s2.shapes.title.text = "Highlights"
    s2.placeholders[1].text = "Revenue up 12%"
    tbl = s2.shapes.add_table(3, 2, PIn(1), PIn(4), PIn(5), PIn(1.5)).table
    tbl.cell(0, 0).text, tbl.cell(0, 1).text = "Metric", "Value"
    s2.shapes.add_picture(BytesIO(png_bytes((0, 150, 0), (160, 80))), PIn(8), PIn(3), width=PIn(3), height=PIn(1.5))
    s2.notes_slide.notes_text_frame.text = "Speaker notes here"

    s3 = prs.slides.add_slide(layout_title_only)
    s3.shapes.title.text = "Chart"
    tb = s3.shapes.add_textbox(PIn(1), PIn(1.5), PIn(6), PIn(1))
    run = tb.text_frame.paragraphs[0].add_run()
    run.text = "Styled text"
    run.font.name, run.font.size, run.font.bold = "Arial", PPt(24), True
    run.font.color.rgb = PRGB(0xC0, 0x00, 0x00)
    cd = CategoryChartData()
    cd.categories = ["A", "B"]
    cd.add_series("S1", (1, 2))
    s3.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, PIn(1), PIn(3), PIn(6), PIn(3.5), cd)
    s3.background.fill.solid()
    s3.background.fill.fore_color.rgb = PRGB(0xF2, 0xF2, 0xF2)
    prs.save(path)
    return {
        "size_in": (13.333, 7.5), "major": "Georgia", "minor": "Verdana", "n_slides": 3,
        "master_logo": {"x": 11.5, "y": 0.3, "w": 1.2},
        "layout_logo": {"layout": "Title Only", "x": 0.4, "y": 6.8, "w": 0.5, "h": 0.5},
        "s2_picture": {"x": 8.0, "y": 3.0, "w": 3.0, "h": 1.5}, "s2_table": {"x": 1.0, "y": 4.0, "w": 5.0, "rows": 3, "cols": 2},
        "s3_text": {"font": "Arial", "size": 24.0, "bold": True, "color": "C00000"}, "s3_bg": "F2F2F2",
        "s3_chart": {"x": 1.0, "y": 3.0, "w": 6.0, "h": 3.5},
    }


def build_xlsx(path: Path) -> dict:
    """XLSX with known formatting; returns the ground truth."""
    from datetime import date

    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.worksheet.table import Table, TableStyleInfo

    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.sheet_properties.tabColor = "FF6600"
    ws["A1"] = "Quarterly Sales"
    ws.merge_cells("A1:D1")
    ws["A1"].font = Font(name="Calibri", size=18, bold=True, color="1F4E79")
    ws["A1"].alignment = Alignment(horizontal="center")
    for i, h in enumerate(["Region", "Revenue", "Margin", "Date"], 1):
        c = ws.cell(row=3, column=i, value=h)
        c.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E79")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = Border(bottom=Side(style="medium", color="000000"))
    rows = [("North", 1234.5, 0.123, date(2024, 1, 15)), ("South", 99.0, 0.5, date(2024, 2, 1)), ("East", 250.25, 0.08, date(2024, 3, 9))]
    for r, row in enumerate(rows, 4):
        for ci, v in enumerate(row, 1):
            ws.cell(row=r, column=ci, value=v)
        ws.cell(row=r, column=2).number_format = "#,##0.00"
        ws.cell(row=r, column=3).number_format = "0.0%"
        ws.cell(row=r, column=4).number_format = "yyyy-mm-dd"
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 14
    ws.row_dimensions[3].height = 28
    ws.freeze_panes = "A4"
    ws.conditional_formatting.add("B4:B6", CellIsRule(operator="greaterThan", formula=["100"], fill=PatternFill("solid", bgColor="FFC7CE")))
    dv = DataValidation(type="list", formula1='"North,South,East,West"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add("A4:A6")
    t = Table(displayName="Sales", ref="A3:D6")
    t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(t)
    img = XlImage(BytesIO(png_bytes((20, 160, 60), (96, 48))))
    ws.add_image(img, "F2")
    ws.page_setup.orientation = "landscape"
    ws.oddFooter.center.text = "Confidential"
    ws2 = wb.create_sheet("Hidden")
    ws2.sheet_state = "hidden"
    ws2["A1"] = "x"
    wb.save(path)
    return {"title_font": ("Calibri", 18.0, "1F4E79"), "header_fill": "1F4E79", "header_font": ("Arial", 11.0),
            "merged": "A1:D1", "col_widths": {"A": 24.0, "B": 14.0}, "row3_height": 28.0, "freeze": "A4",
            "tab": "FF6600", "image_from": "F2", "table": ("Sales", "A3:D6"), "orientation": "landscape",
            "formats": {"#,##0.00", "0.0%", "yyyy-mm-dd"}}


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
