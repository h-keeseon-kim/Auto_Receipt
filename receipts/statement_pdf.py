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
UNMATCHED_BG = colors.HexColor("#FFD8D4")
REVIEW_BG = colors.HexColor("#FFF0C7")
INFERRED_BG = colors.HexColor("#FFE4BD")
MATCHED_BG = colors.HexColor("#F0FAF2")
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


def _is_unmatched(item: CardStatementItem) -> bool:
    return bool(item.receipt_required and item.match_status == StatementMatchStatus.UNMATCHED)


def _is_review(item: CardStatementItem) -> bool:
    return bool(item.receipt_required and item.match_status == StatementMatchStatus.NEEDS_REVIEW)


def _is_inferred(item: CardStatementItem) -> bool:
    return bool(item.receipt_required and item.match_status == StatementMatchStatus.INFERRED)


def _evidence_text(item: CardStatementItem) -> str:
    evidences = list(item.receipt_evidences.all())
    if not evidences:
        inference = getattr(item, "plan_change_inference", None)
        if inference is not None:
            parts = [
                f"契約変更: {inference.change_filename_snapshot}",
                f"過去実績: {inference.historical_filename_snapshot}",
                f"{inference.previous_plan} → {inference.new_plan or '新プラン未抽出'}",
                f"旧プラン終了: {inference.previous_plan_end:%Y-%m-%d}",
                f"過去請求: {inference.historical_receipt_date:%Y-%m-%d} / "
                f"{_decimal_text(inference.amount)} {inference.currency}",
            ]
            return "\n".join(parts)
        return "-"
    filenames = list(dict.fromkeys(evidence.filename_snapshot for evidence in evidences if evidence.filename_snapshot))
    text = " / ".join(filenames) or "-"
    if len(evidences) > 1 and item.evidence_calculation:
        text += f"\n純額: {item.evidence_calculation}"
    return text


def _row_background(item: CardStatementItem):
    if _is_unmatched(item):
        return UNMATCHED_BG
    if _is_inferred(item):
        return INFERRED_BG
    if _is_review(item):
        return REVIEW_BG
    if item.match_status == StatementMatchStatus.MATCHED:
        return MATCHED_BG
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
            _paragraph("ご利用代金明細月", styles["small"]),
            _paragraph(statement.period_month.strftime("%Y年%m月"), styles["body"]),
            _paragraph("領収書発行月", styles["small"]),
            _paragraph(statement.target_receipt_month.strftime("%Y年%m月"), styles["body"]),
            _paragraph("解析ステータス", styles["small"]),
            _paragraph(statement.get_status_display(), styles["body"]),
        ],
        [
            _paragraph("AI判定明細月", styles["small"]),
            _paragraph(statement.statement_period or "-", styles["body"]),
            _paragraph("支払日", styles["small"]),
            _paragraph(statement.payment_date.strftime("%Y-%m-%d") if statement.payment_date else "-", styles["body"]),
            _paragraph("カード番号", styles["small"]),
            _paragraph(card_label, styles["body"]),
        ],
        [
            _paragraph("元ファイル", styles["small"]),
            _paragraph(statement.original_filename or "-", styles["body"]),
            _paragraph("アップロード", styles["small"]),
            _paragraph(local_uploaded.strftime("%Y-%m-%d %H:%M") if local_uploaded else "-", styles["body"]),
            _paragraph("解析 / 最終照合", styles["small"]),
            _paragraph(
                f"{local_processed.strftime('%Y-%m-%d %H:%M') if local_processed else '-'} / "
                f"{local_reconciled.strftime('%Y-%m-%d %H:%M') if local_reconciled else '-'}",
                styles["body"],
            ),
        ],
    ]
    table = Table(data, colWidths=[24 * mm, 48 * mm, 24 * mm, 43 * mm, 25 * mm, 94 * mm])
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


def _action_items_table(items: list[CardStatementItem], styles: dict[str, ParagraphStyle]) -> Table | Paragraph:
    if not items:
        return Paragraph("未一致・推定対応・解析要確認の明細行はありません。", styles["notice"])

    rows = [[
        _paragraph("No.", styles["header"]),
        _paragraph("利用日", styles["header"]),
        _paragraph("ご利用先", styles["header"]),
        _paragraph("金額", styles["header"]),
        _paragraph("状態", styles["header"]),
        _paragraph("提出証拠 / 判定メモ", styles["header"]),
    ]]
    for item in items:
        evidence_and_memo = _evidence_text(item)
        if item.match_memo:
            evidence_and_memo += "\n" + _short_text(item.match_memo)
        rows.append([
            _paragraph(item.line_reference or item.sequence, styles["small"]),
            _paragraph(item.transaction_date.strftime("%Y-%m-%d") if item.transaction_date else "-", styles["small"]),
            _paragraph(item.merchant_name, styles["small"]),
            _paragraph(_amount_text(item), styles["small"]),
            _paragraph(item.get_match_status_display(), styles["small"]),
            _paragraph(evidence_and_memo, styles["tiny"]),
        ])
    table = Table(
        rows,
        colWidths=[14 * mm, 23 * mm, 62 * mm, 30 * mm, 27 * mm, 102 * mm],
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
        commands.append(("BACKGROUND", (0, row_index), (-1, row_index), _row_background(item)))
    table.setStyle(TableStyle(commands))
    return table



def _unused_receipts_table(entries: list[dict], styles: dict[str, ParagraphStyle]) -> Table | Paragraph:
    if not entries:
        return Paragraph("明細に紐づかなかった提出書類はありません。", styles["notice"])

    rows = [[
        _paragraph("提出ファイル", styles["header"]),
        _paragraph("書類日 / 金額", styles["header"]),
        _paragraph("サービス / 販売者", styles["header"]),
        _paragraph("最も近い明細", styles["header"]),
        _paragraph("明細に使用されなかった理由", styles["header"]),
    ]]
    for entry in entries:
        amount = "-"
        if entry.get("amount"):
            amount = f"{entry.get('amount')} {entry.get('currency') or ''}".strip()
        date_amount = f"{entry.get('event_date') or '-'}\n{amount}"
        service_payee = entry.get("service_label") or entry.get("service") or "-"
        payee = entry.get("payee") or ""
        if payee and payee != service_payee:
            service_payee += f"\n販売者: {payee}"
        closest_reference = entry.get("closest_line_reference") or entry.get("closest_line_sequence") or "-"
        closest = str(closest_reference)
        if entry.get("closest_statement_date"):
            closest += f" / {entry.get('closest_statement_date')}"
        if entry.get("closest_statement_amount"):
            closest += (
                f"\n{entry.get('closest_statement_amount')} "
                f"{entry.get('closest_statement_currency') or ''}"
            ).rstrip()
        filename = entry.get("filename") or entry.get("original_filename") or "-"
        if entry.get("user"):
            filename += f"\n{entry.get('user')}"
        rows.append([
            _paragraph(filename, styles["small"]),
            _paragraph(date_amount, styles["small"]),
            _paragraph(service_payee, styles["small"]),
            _paragraph(closest, styles["small"]),
            _paragraph(_short_text(entry.get("reason") or "-", max_chars=230), styles["tiny"]),
        ])

    table = Table(
        rows,
        colWidths=[58 * mm, 31 * mm, 52 * mm, 42 * mm, 75 * mm],
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
    for row_index, entry in enumerate(entries, start=1):
        background = UNMATCHED_BG if entry.get("reason_code") == "amount_mismatch" else REVIEW_BG
        commands.append(("BACKGROUND", (0, row_index), (-1, row_index), background))
    table.setStyle(TableStyle(commands))
    return table

def _all_items_table(items: list[CardStatementItem], styles: dict[str, ParagraphStyle]) -> Table:
    rows = [[
        _paragraph("No.", styles["header"]),
        _paragraph("利用日", styles["header"]),
        _paragraph("ご利用先", styles["header"]),
        _paragraph("金額", styles["header"]),
        _paragraph("照合状態", styles["header"]),
        _paragraph("対応領収書", styles["header"]),
        _paragraph("判定メモ", styles["header"]),
    ]]
    for item in items:
        receipt_name = _evidence_text(item)
        rows.append([
            _paragraph(item.line_reference or item.sequence, styles["small"]),
            _paragraph(item.transaction_date.strftime("%Y-%m-%d") if item.transaction_date else "-", styles["small"]),
            _paragraph(item.merchant_name, styles["small"]),
            _paragraph(_amount_text(item), styles["small"]),
            _paragraph(item.get_match_status_display(), styles["small"]),
            _paragraph(receipt_name, styles["small"]),
            _paragraph(_short_text(item.match_memo or "-", max_chars=120), styles["tiny"]),
        ])
    table = Table(
        rows,
        colWidths=[13 * mm, 22 * mm, 58 * mm, 28 * mm, 28 * mm, 56 * mm, 53 * mm],
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
    """Build a Slack-shareable PDF focused on submitted vs. unmatched statement lines."""

    _register_fonts()
    styles = _styles()
    items = list(statement.items.all())
    action_items = [item for item in items if _is_unmatched(item) or _is_inferred(item) or _is_review(item)]

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
        Paragraph(
            f"{statement.period_month:%Y年%m月}明細 / 領収書発行月 {statement.target_receipt_month:%Y年%m月}",
            styles["subtitle"],
        ),
        _metadata_table(statement, styles),
        Spacer(1, 3 * mm),
        Paragraph("未一致・推定対応・解析要確認", styles["section"]),
        _action_items_table(action_items, styles),
    ]
    unused_entries = list(statement.unmatched_receipt_components or [])
    if unused_entries:
        story.extend([
            Spacer(1, 3 * mm),
            Paragraph("明細に紐づかなかった提出書類", styles["section"]),
            Paragraph(
                "提出済みですが、カード明細のどの行にも使用されなかった書類です。カード明細側の金額誤記・別取引・重複提出・解析不足の確認に使用します。",
                styles["subtitle"],
            ),
            _unused_receipts_table(unused_entries, styles),
        ])
    story.extend([
        PageBreak(),
        Paragraph("全明細照合結果", styles["section"]),
        _all_items_table(items, styles),
    ])

    document.build(story, onFirstPage=_page_callback, onLaterPages=_page_callback)
    return buffer.getvalue()


def reconciliation_report_filename(statement: CardStatement) -> str:
    return f"ReceiptHub_{statement.period_month:%Y-%m}_ご利用代金明細照合結果_{statement.pk}.pdf"
