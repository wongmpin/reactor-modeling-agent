"""Build the two-page report using the bundled document runtime (reportlab)."""
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT

root = Path(__file__).resolve().parents[1]
pdfmetrics.registerFont(TTFont("ReportCJK", r"C:\Windows\Fonts\simhei.ttf"))
source = (root / "docs/technical-report.md").read_text(encoding="utf-8")
style = ParagraphStyle("body", fontName="ReportCJK", fontSize=10.2, leading=16.2, wordWrap="CJK", spaceAfter=9)
heading = ParagraphStyle("heading", parent=style, fontSize=13, leading=19, textColor=colors.HexColor("#14536a"), spaceBefore=10, spaceAfter=7)
title = ParagraphStyle("title", parent=style, fontSize=23, leading=30, spaceAfter=10, textColor=colors.HexColor("#123c50"))
story = []
for block in source.split("\n\n"):
    if block.startswith("## 实测结果"):
        story.append(PageBreak())
    if block.startswith("# "):
        story.append(Paragraph(escape(block[2:]), title))
    elif block.startswith("## "):
        story.append(Paragraph(escape(block[3:]), heading))
    else:
        story.append(Paragraph(escape(block).replace("\n", "<br/>"), style))

def footer(canvas, doc):
    canvas.setStrokeColor(colors.HexColor("#b5c7ce"))
    canvas.line(42, 34, 553, 34)
    canvas.setFont("ReportCJK", 8)
    canvas.setFillColor(colors.HexColor("#49636e"))
    canvas.drawString(42, 22, "本地实机验证 · 2026-10-26 · 测试假设需现场确认")
    canvas.drawRightString(553, 22, str(doc.page))

out = root / "output/pdf/technical-report.pdf"
out.parent.mkdir(parents=True, exist_ok=True)
SimpleDocTemplate(str(out), pagesize=(595.28, 841.89), leftMargin=42, rightMargin=42,
                  topMargin=38, bottomMargin=45, title="化工反应器建模 Agent 技术报告", author="Reactor Agent project").build(story, onFirstPage=footer, onLaterPages=footer)
print(out)
