"""ReportLab PDF出力 - サンプルと同一レイアウトの見積書PDF生成"""
import os
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
    Image, Frame, PageTemplate, BaseDocTemplate,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from models.estimate_data import EstimateData, CategorySection, CategoryType
from config import FONT_REGULAR, FONT_BOLD, ASSETS_DIR

# ページサイズ
PAGE_WIDTH, PAGE_HEIGHT = A4  # 210mm x 297mm

# マージン
LEFT_MARGIN = 15 * mm
RIGHT_MARGIN = 15 * mm
TOP_MARGIN = 20 * mm
BOTTOM_MARGIN = 15 * mm

# 色定義
HEADER_BG = colors.Color(0.78, 0.88, 0.97)  # 水色ヘッダー
WHITE = colors.white
BLACK = colors.black
LIGHT_GRAY = colors.Color(0.95, 0.95, 0.95)


def _register_fonts():
    """日本語フォントを登録"""
    if os.path.exists(str(FONT_REGULAR)):
        pdfmetrics.registerFont(TTFont('NotoSansJP', str(FONT_REGULAR)))
    if os.path.exists(str(FONT_BOLD)):
        pdfmetrics.registerFont(TTFont('NotoSansJP-Bold', str(FONT_BOLD)))

    # フォントが存在しない場合のフォールバック
    if 'NotoSansJP' not in pdfmetrics.getRegisteredFontNames():
        # HeiseiKakuGo-W5はReportLabに組み込みの日本語フォント
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
        return 'HeiseiKakuGo-W5', 'HeiseiKakuGo-W5'

    return 'NotoSansJP', 'NotoSansJP-Bold'


def generate_pdf(estimate: EstimateData) -> bytes:
    """見積書PDFを生成

    Args:
        estimate: 見積データ

    Returns:
        bytes: PDF バイナリデータ
    """
    font_normal, font_bold = _register_fonts()
    buffer = BytesIO()

    # カスタムDocTemplateを使用
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
    )

    # ページテンプレート
    frame = Frame(
        LEFT_MARGIN, BOTTOM_MARGIN,
        PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN,
        PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN,
        id='normal',
    )

    def _header_footer(canvas_obj, doc_obj):
        """ヘッダー・フッター描画"""
        canvas_obj.saveState()
        # ヘッダー：見積ID / 発行日
        canvas_obj.setFont(font_normal, 8)
        canvas_obj.drawString(LEFT_MARGIN, PAGE_HEIGHT - 12 * mm,
                              f"見積ID {estimate.cover.estimate_id}")
        canvas_obj.drawRightString(PAGE_WIDTH - RIGHT_MARGIN, PAGE_HEIGHT - 12 * mm,
                                   f"発行日 {estimate.cover.issue_date}")
        # フッター：ページ番号
        canvas_obj.drawRightString(PAGE_WIDTH - RIGHT_MARGIN, 10 * mm,
                                   str(doc_obj.page))
        canvas_obj.restoreState()

    template = PageTemplate(id='main', frames=[frame], onPage=_header_footer)
    doc.addPageTemplates([template])

    # スタイル定義
    styles = _create_styles(font_normal, font_bold)

    # ドキュメント要素を構築
    elements = []

    # Page 1: 御見積書表紙
    elements.extend(_build_cover_page(estimate, styles, font_normal, font_bold))

    # Page 2: 見積内訳書
    elements.append(PageBreak())
    elements.extend(_build_summary_page(estimate, styles, font_normal, font_bold))

    # Pages 3+: 各カテゴリの明細
    for cat in estimate.summary.categories:
        if cat.category == CategoryType.SPECIAL_NOTES and not cat.items:
            continue  # 特記事項が空なら省略
        elements.append(PageBreak())
        elements.extend(_build_detail_page(cat, styles, font_normal, font_bold))

    doc.build(elements)
    pdf_data = buffer.getvalue()
    buffer.close()
    return pdf_data


def _create_styles(font_normal: str, font_bold: str) -> dict:
    """各種スタイルを定義"""
    return {
        'title': ParagraphStyle(
            'Title', fontName=font_bold, fontSize=20,
            alignment=TA_CENTER, textColor=colors.Color(0.2, 0.4, 0.7),
            spaceAfter=10 * mm,
        ),
        'subtitle': ParagraphStyle(
            'Subtitle', fontName=font_bold, fontSize=14,
            alignment=TA_CENTER, spaceAfter=5 * mm,
        ),
        'normal': ParagraphStyle(
            'Normal', fontName=font_normal, fontSize=9,
            leading=13,
        ),
        'normal_small': ParagraphStyle(
            'NormalSmall', fontName=font_normal, fontSize=8,
            leading=11,
        ),
        'bold': ParagraphStyle(
            'Bold', fontName=font_bold, fontSize=9,
            leading=13,
        ),
        'right': ParagraphStyle(
            'Right', fontName=font_normal, fontSize=9,
            alignment=TA_RIGHT,
        ),
        'amount_large': ParagraphStyle(
            'AmountLarge', fontName=font_bold, fontSize=18,
            alignment=TA_RIGHT, textColor=colors.Color(0.8, 0.2, 0.2),
        ),
        'company_name': ParagraphStyle(
            'CompanyName', fontName=font_bold, fontSize=9,
            leading=13,
        ),
    }


def _build_cover_page(estimate: EstimateData, styles: dict,
                       font_normal: str, font_bold: str) -> list:
    """Page 1: 御見積書表紙を構築"""
    elements = []

    # タイトルバー
    title_data = [['御　見　積　書']]
    title_table = Table(title_data, colWidths=[PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN])
    title_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), HEADER_BG),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.Color(0.2, 0.4, 0.7)),
        ('FONTNAME', (0, 0), (-1, -1), font_bold),
        ('FONTSIZE', (0, 0), (-1, -1), 20),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(title_table)
    elements.append(Spacer(1, 15 * mm))

    # 宛先
    client_text = f"{estimate.cover.client_name}　御中" if estimate.cover.client_name else "　　　　　　御中"
    elements.append(Paragraph(client_text, ParagraphStyle(
        'Client', fontName=font_bold, fontSize=16, underlineWidth=1,
    )))
    elements.append(Spacer(1, 3 * mm))
    elements.append(Paragraph("下記の通り、御見積申し上げます。", styles['normal']))
    elements.append(Spacer(1, 20 * mm))

    # 金額テーブル（中央に配置）
    total_str = f"¥{estimate.cover.total_with_tax:,}.-"
    tax_excl_str = f"¥{estimate.cover.total_before_tax:,}.-"
    tax_str = f"¥{estimate.cover.tax:,}.-"

    amount_data = [
        ['御見積金額', total_str],
        ['', f'税抜合計　　　　　{tax_excl_str}'],
        ['', f'消費税　　　　　　{tax_str}'],
    ]
    amount_table = Table(amount_data, colWidths=[80 * mm, 80 * mm])
    amount_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, 0), font_bold),
        ('FONTSIZE', (0, 0), (0, 0), 12),
        ('FONTNAME', (1, 0), (1, 0), font_bold),
        ('FONTSIZE', (1, 0), (1, 0), 18),
        ('TEXTCOLOR', (1, 0), (1, 0), colors.Color(0.8, 0.1, 0.1)),
        ('FONTNAME', (1, 1), (1, 2), font_normal),
        ('FONTSIZE', (1, 1), (1, 2), 10),
        ('ALIGN', (0, 0), (0, -1), 'LEFT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, 0), 1, BLACK),
        ('LINEBELOW', (1, 1), (1, 1), 0.5, BLACK),
        ('LINEBELOW', (1, 2), (1, 2), 0.5, BLACK),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(amount_table)
    elements.append(Spacer(1, 20 * mm))

    # 工事情報と会社情報を横並び
    left_col = []
    info_items = [
        ("工事名", estimate.cover.project_name),
        ("工事場所", estimate.cover.project_location),
        ("工事期間", estimate.cover.project_period),
        ("有効期限", estimate.cover.validity_period),
        ("備考", estimate.cover.notes),
    ]
    for label, value in info_items:
        left_col.append(f"<b>{label}</b>　　　{value}")

    right_col = []
    from config import COMPANY_INFO
    right_col.append(COMPANY_INFO['slogan'])
    right_col.append("")
    right_col.append(f"<b>{COMPANY_INFO['name']}</b>")
    right_col.append(COMPANY_INFO['postal_code'])
    right_col.append(COMPANY_INFO['address'])
    right_col.append(COMPANY_INFO['tel'])
    right_col.append(COMPANY_INFO['fax'])
    right_col.append("")
    right_col.append(f"担当者：{estimate.cover.representative}")

    info_data = [[
        '\n'.join([f'<para>{l}</para>' for l in left_col]),
        '\n'.join(right_col),
    ]]

    # 左列パラグラフ
    left_paras = []
    for label, value in info_items:
        left_paras.append(Paragraph(
            f"<b>{label}</b>　　　{value}", styles['normal']
        ))
        left_paras.append(Spacer(1, 3 * mm))

    right_paras = []
    right_paras.append(Paragraph(COMPANY_INFO['slogan'], styles['normal_small']))
    right_paras.append(Spacer(1, 3 * mm))
    right_paras.append(Paragraph(f"<b>{COMPANY_INFO['name']}</b>", styles['company_name']))
    right_paras.append(Paragraph(COMPANY_INFO['postal_code'], styles['normal_small']))
    right_paras.append(Paragraph(COMPANY_INFO['address'], styles['normal_small']))
    right_paras.append(Paragraph(COMPANY_INFO['tel'], styles['normal_small']))
    right_paras.append(Paragraph(COMPANY_INFO['fax'], styles['normal_small']))
    right_paras.append(Spacer(1, 5 * mm))
    right_paras.append(Paragraph(f"担当者：{estimate.cover.representative}", styles['normal_small']))

    info_table = Table(
        [[left_paras, right_paras]],
        colWidths=[90 * mm, 80 * mm],
    )
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(info_table)

    return elements


def _build_summary_page(estimate: EstimateData, styles: dict,
                         font_normal: str, font_bold: str) -> list:
    """Page 2: 見積内訳書を構築"""
    elements = []

    # タイトル
    elements.append(Paragraph("見積内訳書", styles['subtitle']))
    elements.append(Spacer(1, 5 * mm))

    # 内訳テーブル
    header = ['No.', '分類', '見積額']
    data = [header]

    for cat in estimate.summary.categories:
        data.append([
            str(cat.category_number),
            cat.category.value,
            f"{cat.total:,}" if cat.total else "0",
        ])

    # 小計行
    data.append(['', '小計', f"{estimate.summary.subtotal:,}"])
    # 値引き行
    data.append(['', 'お値引き', f"{estimate.summary.discount:,}"])
    # 税抜合計行
    data.append(['', '税抜合計', f"{estimate.summary.total_before_tax:,}"])

    col_widths = [15 * mm, 120 * mm, 35 * mm]
    table = Table(data, colWidths=col_widths)

    style_cmds = [
        # ヘッダー
        ('BACKGROUND', (0, 0), (-1, 0), LIGHT_GRAY),
        ('FONTNAME', (0, 0), (-1, 0), font_bold),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('FONTNAME', (0, 1), (-1, -1), font_normal),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        ('GRID', (0, 0), (-1, -1), 0.5, BLACK),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]

    # 小計・値引き・税抜合計行のスタイル
    summary_start = len(data) - 3
    for i in range(3):
        row = summary_start + i
        style_cmds.append(('FONTNAME', (1, row), (2, row), font_bold))
        style_cmds.append(('ALIGN', (1, row), (1, row), 'RIGHT'))
        if i == 2:  # 税抜合計行
            style_cmds.append(('BACKGROUND', (0, row), (-1, row), LIGHT_GRAY))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    return elements


def _build_detail_page(category: CategorySection, styles: dict,
                        font_normal: str, font_bold: str) -> list:
    """明細ページを構築"""
    elements = []

    # タイトル
    elements.append(Paragraph("見積明細書", styles['subtitle']))
    elements.append(Paragraph(
        f"（{category.category_number}. {category.category.value}）",
        ParagraphStyle('CatTitle', fontName=font_normal, fontSize=11,
                       alignment=TA_CENTER, spaceAfter=5 * mm)
    ))
    elements.append(Spacer(1, 3 * mm))

    # テーブルヘッダー
    header = ['No.', '摘要', '備考', '数量', '見積単価', '見積額']
    col_widths = [12 * mm, 45 * mm, 45 * mm, 20 * mm, 25 * mm, 25 * mm]

    data = [header]

    # カテゴリ名の行
    data.append([category.category.value, '', '', '', '', ''])

    # 各項目
    for item in category.items:
        # 備考が改行を含む場合（例: "LR7-72HVH-660M\n御支給品"）
        remarks_parts = item.remarks.split('\n') if item.remarks else ['']
        main_remarks = remarks_parts[0]
        sub_remarks = remarks_parts[1] if len(remarks_parts) > 1 else ""

        unit_price_str = f"{item.unit_price:,}" if item.unit_price else ""
        amount_str = f"{item.amount:,}" if item.amount else ""

        if sub_remarks:
            # 2行の項目
            data.append([
                str(item.no),
                Paragraph(item.description, styles['normal_small']),
                Paragraph(f"{main_remarks}<br/>{sub_remarks}", styles['normal_small']),
                item.quantity,
                unit_price_str,
                amount_str,
            ])
        else:
            data.append([
                str(item.no),
                Paragraph(item.description, styles['normal_small']),
                Paragraph(main_remarks, styles['normal_small']),
                item.quantity,
                unit_price_str,
                amount_str,
            ])

    # 小計行
    data.append(['', '', '', f'{category.category.value} 小計', '', f"{category.subtotal:,}"])
    # 合計行
    data.append(['', '', '', f'{category.category.value} 合計', '', f"{category.total:,}"])

    table = Table(data, colWidths=col_widths, repeatRows=1)

    style_cmds = [
        # ヘッダー
        ('BACKGROUND', (0, 0), (-1, 0), LIGHT_GRAY),
        ('FONTNAME', (0, 0), (-1, 0), font_bold),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        # カテゴリ名行
        ('FONTNAME', (0, 1), (-1, 1), font_bold),
        ('FONTSIZE', (0, 1), (-1, 1), 9),
        ('SPAN', (0, 1), (-1, 1)),
        # 全体
        ('FONTNAME', (0, 2), (-1, -1), font_normal),
        ('FONTSIZE', (0, 2), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
        ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
        ('ALIGN', (5, 0), (5, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, BLACK),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]

    # 小計・合計行
    summary_start = len(data) - 2
    for i in range(2):
        row = summary_start + i
        style_cmds.append(('FONTNAME', (3, row), (5, row), font_bold))
        style_cmds.append(('SPAN', (0, row), (2, row)))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    return elements
