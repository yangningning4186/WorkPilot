from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "authorized" / "Atlas-Reading-Sample.pdf"
REGULAR_FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
BOLD_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

pdfmetrics.registerFont(TTFont("AtlasArial", REGULAR_FONT))
pdfmetrics.registerFont(TTFont("AtlasArial-Bold", BOLD_FONT))


def page_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D7DEE5"))
    canvas.setLineWidth(0.6)
    canvas.line(0.75 * inch, 0.58 * inch, 7.75 * inch, 0.58 * inch)
    canvas.setFillColor(colors.HexColor("#667085"))
    canvas.setFont("AtlasArial", 8.5)
    canvas.drawString(0.75 * inch, 0.38 * inch, "ATLAS DESKTOP PILOT - MANUAL TEST FIXTURE")
    canvas.drawRightString(7.75 * inch, 0.38 * inch, f"Page {doc.page}")
    canvas.restoreState()


def build_pdf() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.72 * inch,
        bottomMargin=0.78 * inch,
        title="Atlas Desktop Pilot - Reading and Citation Fixture",
        author="WorkPilot Manual QA",
        subject="Synthetic test fixture for PDF reading and citation validation",
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "AtlasTitle",
        parent=styles["Title"],
        fontName="AtlasArial-Bold",
        fontSize=24,
        leading=29,
        textColor=colors.HexColor("#17324D"),
        alignment=TA_LEFT,
        spaceAfter=14,
    )
    kicker = ParagraphStyle(
        "Kicker",
        parent=styles["Normal"],
        fontName="AtlasArial-Bold",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#147D64"),
        tracking=0.8,
        spaceAfter=8,
    )
    heading = ParagraphStyle(
        "Heading",
        parent=styles["Heading2"],
        fontName="AtlasArial-Bold",
        fontSize=15,
        leading=19,
        textColor=colors.HexColor("#17324D"),
        spaceBefore=7,
        spaceAfter=8,
    )
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontName="AtlasArial",
        fontSize=10.5,
        leading=15.5,
        textColor=colors.HexColor("#263645"),
        alignment=TA_LEFT,
        spaceAfter=8,
    )
    callout = ParagraphStyle(
        "Callout",
        parent=body,
        fontName="AtlasArial-Bold",
        fontSize=11,
        leading=16,
        textColor=colors.HexColor("#0F5F4C"),
        spaceAfter=0,
    )
    table_header = ParagraphStyle(
        "TableHeader",
        parent=body,
        fontName="AtlasArial-Bold",
        fontSize=9.5,
        leading=12,
        textColor=colors.white,
        alignment=TA_CENTER,
        spaceAfter=0,
    )
    table_body = ParagraphStyle(
        "TableBody",
        parent=body,
        fontSize=9.5,
        leading=12,
        alignment=TA_LEFT,
        spaceAfter=0,
    )

    story = []

    # Page 1
    story.extend(
        [
            Paragraph("CONTROLLED TEST DOCUMENT", kicker),
            Paragraph("Atlas Desktop Pilot", title),
            Paragraph(
                "This synthetic document is intentionally self-contained. It is used to test PDF reading, page-level locators, citation links, and unsupported-question behavior in WorkPilot.",
                body,
            ),
            Spacer(1, 8),
            Paragraph("Project snapshot", heading),
        ]
    )

    snapshot = [
        [Paragraph("Field", table_header), Paragraph("Controlled value", table_header)],
        [Paragraph("Program", table_body), Paragraph("Atlas Desktop Pilot", table_body)],
        [Paragraph("Owner", table_body), Paragraph("Mina Zhao", table_body)],
        [Paragraph("Region", table_body), Paragraph("East China", table_body)],
        [Paragraph("Pilot cohort", table_body), Paragraph("12 customers", table_body)],
        [Paragraph("Primary metric", table_body), Paragraph("At least 85% workflow completion", table_body)],
    ]
    snapshot_table = Table(snapshot, colWidths=[1.65 * inch, 4.75 * inch], repeatRows=1)
    snapshot_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17324D")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5DF")),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F7F9FB")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.extend(
        [
            snapshot_table,
            Spacer(1, 18),
            Paragraph("Decision principle", heading),
            KeepTogether(
                Table(
                    [[Paragraph("Reliability comes before pilot expansion. The cohort remains at 12 customers until permission boundaries and restart recovery both pass.", callout)]],
                    colWidths=[6.4 * inch],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#E9F5F1")),
                            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#77B6A5")),
                            ("LEFTPADDING", (0, 0), (-1, -1), 14),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                            ("TOPPADDING", (0, 0), (-1, -1), 13),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
                        ]
                    ),
                )
            ),
            Spacer(1, 14),
            Paragraph(
                "No phone number, email address, or external support contact is stated anywhere in this document. A correct answer to a request for a phone number must say that the information is not provided.",
                body,
            ),
            PageBreak(),
        ]
    )

    # Page 2
    story.extend(
        [
            Paragraph("RELEASE CONTROL", kicker),
            Paragraph("Three gates control release", title),
            Paragraph(
                "All three gates are mandatory. A release is blocked if any single gate is incomplete.",
                body,
            ),
            Spacer(1, 10),
        ]
    )
    gate_rows = [
        [Paragraph("Gate", table_header), Paragraph("Pass condition", table_header), Paragraph("Evidence owner", table_header)],
        [Paragraph("1. Smoke suite", table_body), Paragraph("100% of required smoke checks pass", table_body), Paragraph("Test Lead", table_body)],
        [Paragraph("2. Rollback drill", table_body), Paragraph("Rollback completes in 12 minutes or less", table_body), Paragraph("Ops Lead", table_body)],
        [Paragraph("3. Citation integrity", table_body), Paragraph("Every release claim has a valid locator", table_body), Paragraph("Evidence Reviewer", table_body)],
    ]
    gates = Table(gate_rows, colWidths=[1.55 * inch, 3.35 * inch, 1.5 * inch], repeatRows=1)
    gates.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17324D")),
                ("GRID", (0, 0), (-1, -1), 0.55, colors.HexColor("#BCC9D5")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FA")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.extend(
        [
            gates,
            Spacer(1, 20),
            Paragraph("Interpretation", heading),
            Paragraph(
                "The smoke result is binary: 99% is not a pass. The rollback threshold is inclusive: exactly 12 minutes passes. Citation integrity applies to claims presented as release evidence, not to casual conversation.",
                body,
            ),
            Spacer(1, 10),
            KeepTogether(
                Table(
                    [[Paragraph("Release rule: 3 of 3 gates must pass before pilot expansion.", callout)]],
                    colWidths=[6.4 * inch],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF4DD")),
                            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#D9A441")),
                            ("LEFTPADDING", (0, 0), (-1, -1), 14),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                            ("TOPPADDING", (0, 0), (-1, -1), 13),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
                        ]
                    ),
                )
            ),
            PageBreak(),
        ]
    )

    # Page 3
    story.extend(
        [
            Paragraph("INCIDENT OPERATIONS", kicker),
            Paragraph("P0 escalation clock", title),
            Paragraph(
                "The clock starts when the incident is declared P0. The named owner for the incident process is the Ops Lead.",
                body,
            ),
            Spacer(1, 10),
        ]
    )
    incident_rows = [
        [Paragraph("Milestone", table_header), Paragraph("Deadline", table_header), Paragraph("Required action", table_header)],
        [Paragraph("Acknowledge", table_body), Paragraph("Within 15 minutes", table_body), Paragraph("Confirm ownership and open the incident channel", table_body)],
        [Paragraph("Status update", table_body), Paragraph("Every 30 minutes", table_body), Paragraph("Publish impact, mitigation, and next checkpoint", table_body)],
        [Paragraph("RCA", table_body), Paragraph("Within two business days", table_body), Paragraph("Publish root cause, corrective actions, and owners", table_body)],
    ]
    incidents = Table(incident_rows, colWidths=[1.45 * inch, 1.55 * inch, 3.4 * inch], repeatRows=1)
    incidents.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17324D")),
                ("GRID", (0, 0), (-1, -1), 0.55, colors.HexColor("#BCC9D5")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FA")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.extend(
        [
            incidents,
            Spacer(1, 20),
            Paragraph("Closure condition", heading),
            Paragraph(
                "A P0 incident is not complete when service first recovers. Closure requires verified recovery, a named corrective-action owner, and an RCA delivered within two business days.",
                body,
            ),
            Spacer(1, 10),
            KeepTogether(
                Table(
                    [[Paragraph("Controlled answer: owner = Ops Lead; acknowledgement = 15 minutes; RCA = two business days.", callout)]],
                    colWidths=[6.4 * inch],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#E9F5F1")),
                            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#77B6A5")),
                            ("LEFTPADDING", (0, 0), (-1, -1), 14),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                            ("TOPPADDING", (0, 0), (-1, -1), 13),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
                        ]
                    ),
                )
            ),
        ]
    )

    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)


if __name__ == "__main__":
    build_pdf()
