from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import CardStatement, CardStatementItem, StatementMatchStatus


FONT_GOTHIC = "HeiseiKakuGo-W5"
FONT_MINCHO = "HeiseiMin-W3"

INK = colors.HexColor("#182033")
MUTED = colors.HexColor("#64748B")
BRAND = colors.HexColor("#2457D6")
BORDER = colors.HexColor("#D7DFEA")
HEADER_BG = colors.HexColor("#EEF3FF")
MISSING_BG = colors.HexColor("#FFF3BF")
REVIEW_BG = colors.HexColor("#FFE4C7")
NEUTRAL_BG = colors.HexColor("#F5F7FA")
WHITE = colors.white


def _register_fonts() -> None:
    for font_name in (FONT_GOTHIC, FONT_MINCHO):
        try:
            pdfmetrics.getFont(font_name)
        except KeyError:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))


def _safe_text(value) -> str:
    return escape("" if value is None else str(value)).replace("\n", "<br/>")


def _paragraph(value, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_safe_text(value) or "-", style)


def _short_text(value, max_chars: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _decimal_text(value: Decimal | None, *, places: int | None = None) -> str:
    if value is None:
        return "-"
    if places is not None:
        return f"{value:,.{places}f}"
    text = format(value, "f").rstrip("0").rstrip(".")
    whole, dot, fraction = text.partition(".")
    try:
        whole = f"{int(whole):,}"
    except ValueError:
        pass
    return whole + (dot + fraction if dot else "")


def _amount_text(item: CardStatementItem) -> str:
    values: list[str] = []
    if item.amount_jpy is not None:
        values.append(f"¥{_decimal_text(item.amount_jpy, places=0)}")
    if item.original_amount is not None:
        values.append(f"{_decimal_text(item.original_amount)} {item.original_currency or ''}".strip())
    return "\n".join(values) if values else "-"


def _receipt_available(item: CardStatementItem) -> bool:
    return bool(item.matched_receipt_id and item.matched_receipt and item.matched_receipt.file_available)


def _manual_review(item: CardStatementItem) -> bool:
    return bool(
        item.match_status in {StatementMatchStatus.AMBIGUOUS, StatementMatchStatus.UNMATCHED}
        or (item.receipt_required and item.matched_user_id is None)
    )


def _action_status(item: CardStatementItem) -> str:
    labels: list[str] = []
    if item.receipt_required and not _receipt_available(item):
        labels.append("領収書未提出")
    if _manual_review(item):
        labels.append("管理者確認")
    return " / ".join(dict.fromkeys(labels)) or "確認済み"


def _row_background(item: CardStatementItem):
    if item.receipt_required and not _receipt_available(item):
        return MISSING_BG
    if _manual_review(item):
        return REVIEW_BG
    if item.match_status == StatementMatchStatus.IGNORED:
        return NEUTRAL_BG
    return WHITE


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title",
            fontName=FONT_GOTHIC,
            fontSize=18,
            leading=23,
            textColor=INK,
            spaceAfter=3 * mm,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            fontName=FONT_GOTHIC,
            fontSize=8.5,
            leading=12,
            textColor=MUTED,
            spaceAfter=4 * mm,
        ),
        "section": ParagraphStyle(
            "section",
            fontName=FONT_GOTHIC,
            fontSize=11.5,
            leading=15,
            textColor=INK,
            spaceBefore=3 * mm,
            spaceAfter=2 * mm,
        ),
        "body": ParagraphStyle(
            "body",
            fontName=FONT_GOTHIC,
            fontSize=8,
            leading=11,
            textColor=INK,
            alignment=TA_LEFT,
        ),
        "small": ParagraphStyle(
            "small",
            fontName=FONT_GOTHIC,
            fontSize=6.8,
            leading=9,
            textColor=INK,
            alignment=TA_LEFT,
        ),
        "tiny": ParagraphStyle(
            "tiny",
            fontName=FONT_GOTHIC,
            fontSize=6.1,
            leading=8,
            textColor=INK,
            alignment=TA_LEFT,
        ),
        "header": ParagraphStyle(
            "header",
            fontName=FONT_GOTHIC,
            fontSize=7,
            leading=9,
            textColor=WHITE,
            alignment=TA_CENTER,
        ),
        "notice": ParagraphStyle(
            "notice",
            fontName=FONT_GOTHIC,
            fontSize=7.2,
            leading=10,
            textColor=INK,
            borderColor=BORDER,
            borderWidth=0.5,
            borderPadding=5,
            backColor=HEADER_BG,
        ),
    }


def _metadata_table(statement: CardStatement, styles: dict[str, ParagraphStyle]) -> Table:
    local_uploaded = timezone.localtime(statement.uploaded_at) if statement.uploaded_at else None
    local_processed = timezone.localtime(statement.processed_at) if statement.processed_at else None
    local_reconciled = timezone.localtime(statement.reconciled_at) if statement.reconciled_at else None
    card_label = f"****-{statement.card_last4}" if statement.card_last4 else "-"
    data = [
        [
            _paragraph("対象月", styles["small"]),
            _paragraph(statement.period_month.strftime("%Y年%m月"), styles["body"]),
            _paragraph("解析ステータス", styles["small"]),
            _paragraph(statement.get_status_display(), styles["body"]),
            _paragraph("カード番号", styles["small"]),
            _paragraph(card_label, styles["body"]),
        ],
        [
            _paragraph("AI判定対象月", styles["small"]),
            _paragraph(statement.statement_period or "-", styles["body"]),
            _paragraph("支払日", styles["small"]),
            _paragraph(statement.payment_date.strftime("%Y-%m-%d") if statement.payment_date else "-", styles["body"]),
            _paragraph("元ファイル", styles["small"]),
            _paragraph(statement.original_filename or "-", styles["body"]),
        ],
        [
            _paragraph("アップロード", styles["small"]),
            _paragraph(local_uploaded.strftime("%Y-%m-%d %H:%M") if local_uploaded else "-", styles["body"]),
            _paragraph("解析完了", styles["small"]),
            _paragraph(local_processed.strftime("%Y-%m-%d %H:%M") if local_processed else "-", styles["body"]),
            _paragraph("最終照合", styles["small"]),
            _paragraph(local_reconciled.strftime("%Y-%m-%d %H:%M") if local_reconciled else "-", styles["body"]),
        ],
    ]
    table = Table(data, colWidths=[22 * mm, 40 * mm, 24 * mm, 35 * mm, 22 * mm, 115 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), WHITE),
                ("GRID", (0, 0), (-1, -1), 0.35, BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), NEUTRAL_BG),
                ("BACKGROUND", (2, 0), (2, -1), NEUTRAL_BG),
                ("BACKGROUND", (4, 0), (4, -1), NEUTRAL_BG),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _action_items_table(action_items: list[CardStatementItem], styles: dict[str, ParagraphStyle]) -> Table | Paragraph:
    if not action_items:
        return Paragraph("未提出・確認対象の明細行はありません。", styles["notice"])

    rows = [
        [
            _paragraph("No.", styles["header"]),
            _paragraph("利用日", styles["header"]),
            _paragraph("ご利用先", styles["header"]),
            _paragraph("金額", styles["header"]),
            _paragraph("ユーザー", styles["header"]),
            _paragraph("サービス", styles["header"]),
            _paragraph("状態", styles["header"]),
            _paragraph("確認メモ", styles["header"]),
        ]
    ]
    for item in action_items:
        rows.append(
            [
                _paragraph(item.line_reference or item.sequence, styles["small"]),
                _paragraph(item.transaction_date.strftime("%Y-%m-%d") if item.transaction_date else "-", styles["small"]),
                _paragraph(item.merchant_name, styles["small"]),
                _paragraph(_amount_text(item), styles["small"]),
                _paragraph(item.matched_user_label, styles["small"]),
                _paragraph(item.matched_service_label, styles["small"]),
                _paragraph(_action_status(item), styles["small"]),
                _paragraph(_short_text(item.match_memo or "-"), styles["tiny"]),
            ]
        )
    table = Table(
        rows,
        colWidths=[13 * mm, 20 * mm, 43 * mm, 25 * mm, 38 * mm, 43 * mm, 28 * mm, 48 * mm],
        repeatRows=1,
        splitByRow=1,
    )
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for row_index, item in enumerate(action_items, start=1):
        commands.append(("BACKGROUND", (0, row_index), (-1, row_index), _row_background(item)))
    table.setStyle(TableStyle(commands))
    return table


def _all_items_table(items: list[CardStatementItem], styles: dict[str, ParagraphStyle]) -> Table:
    rows = [
        [
            _paragraph("No.", styles["header"]),
            _paragraph("利用日", styles["header"]),
            _paragraph("ご利用先", styles["header"]),
            _paragraph("金額", styles["header"]),
            _paragraph("ユーザー", styles["header"]),
            _paragraph("サービス", styles["header"]),
            _paragraph("AI一致", styles["header"]),
            _paragraph("領収書", styles["header"]),
        ]
    ]
    for item in items:
        rows.append(
            [
                _paragraph(item.line_reference or item.sequence, styles["small"]),
                _paragraph(item.transaction_date.strftime("%Y-%m-%d") if item.transaction_date else "-", styles["small"]),
                _paragraph(item.merchant_name, styles["small"]),
                _paragraph(_amount_text(item), styles["small"]),
                _paragraph(item.matched_user_label, styles["small"]),
                _paragraph(item.matched_service_label, styles["small"]),
                _paragraph(item.get_match_status_display(), styles["small"]),
                _paragraph(item.receipt_status_label, styles["small"]),
            ]
        )
    table = Table(
        rows,
        colWidths=[13 * mm, 20 * mm, 53 * mm, 26 * mm, 39 * mm, 46 * mm, 28 * mm, 33 * mm],
        repeatRows=1,
        splitByRow=1,
    )
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for row_index, item in enumerate(items, start=1):
        background = _row_background(item)
        if background != WHITE:
            commands.append(("BACKGROUND", (0, row_index), (-1, row_index), background))
    table.setStyle(TableStyle(commands))
    return table


def _page_callback(canvas, doc) -> None:
    width, _ = landscape(A4)
    canvas.saveState()
    canvas.setTitle("ReceiptHub ご利用代金明細照合結果")
    canvas.setAuthor(getattr(settings, "APP_NAME", "ReceiptHub"))
    canvas.setFont(FONT_GOTHIC, 6.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(12 * mm, 7 * mm, f"{getattr(settings, 'APP_NAME', 'ReceiptHub')} - ご利用代金明細照合結果")
    canvas.drawRightString(width - 12 * mm, 7 * mm, f"{doc.page} ページ")
    canvas.restoreState()


def build_card_statement_reconciliation_pdf(statement: CardStatement) -> bytes:
    """Build a Slack-shareable PDF summary of one statement reconciliation result."""

    _register_fonts()
    styles = _styles()
    items = list(statement.items.all())
    action_items = [
        item
        for item in items
        if (item.receipt_required and not _receipt_available(item)) or _manual_review(item)
    ]

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=11 * mm,
        bottomMargin=14 * mm,
        title="ReceiptHub ご利用代金明細照合結果",
        author=getattr(settings, "APP_NAME", "ReceiptHub"),
        subject=f"{statement.period_month:%Y-%m} ご利用代金明細照合結果",
    )

    story: list = [
        Paragraph("ご利用代金明細 照合結果", styles["title"]),
        Paragraph(f"{statement.period_month:%Y年%m月}分", styles["subtitle"]),
        _metadata_table(statement, styles),
        Spacer(1, 3 * mm),
        Paragraph("未提出・確認対象", styles["section"]),
        _action_items_table(action_items, styles),
        PageBreak(),
        Paragraph("全明細照合結果", styles["section"]),
        _all_items_table(items, styles),
    ]

    document.build(story, onFirstPage=_page_callback, onLaterPages=_page_callback)
    return buffer.getvalue()


def reconciliation_report_filename(statement: CardStatement) -> str:
    return f"ReceiptHub_{statement.period_month:%Y-%m}_ご利用代金明細照合結果_{statement.pk}.pdf"
