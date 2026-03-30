"""ReportLab PDF出力 - サンプルと同一レイアウトの見積書PDF生成"""
import os
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak,
    Image, Frame, PageTemplate, BaseDocTemplate, KeepTogether,
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

# 使用可能な幅
CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN

# 色定義
HEADER_BG = colors.Color(0.78, 0.88, 0.97)  # 水色ヘッダー（タイトルバー用）
LIGHT_GRAY = colors.Color(0.93, 0.93, 0.93)  # テーブルヘッダー用薄グレー
WHITE = colors.white
BLACK = colors.black
RED = colors.Color(0.8, 0.1, 0.1)
BLUE_TEXT = colors.Color(0.2, 0.4, 0.7)

# 罫線の太さ
LINE_WIDTH = 0.5


def _register_fonts():
    """日本語フォントを登録"""
    if os.path.exists(str(FONT_REGULAR)):
        pdfmetrics.registerFont(TTFont('NotoSansJP', str(FONT_REGULAR)))
    if os.path.exists(str(FONT_BOLD)):
        pdfmetrics.registerFont(TTFont('NotoSansJP-Bold', str(FONT_BOLD)))

    # フォントが存在しない場合のフォールバック
    if 'NotoSansJP' not in pdfmetrics.getRegisteredFontNames():
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont('HeiseiKakuGo-W5'))
        return 'HeiseiKakuGo-W5', 'HeiseiKakuGo-W5'

    return 'NotoSansJP', 'NotoSansJP-Bold'


def _fmt(value) -> str:
    """金額を3桁カンマ区切りでフォーマット。0やNoneは'0'を返す"""
    if value is None:
        return "0"
    if isinstance(value, (int, float)):
        return f"{int(value):,}"
    return str(value)


class _PageCountDocTemplate(BaseDocTemplate):
    """総ページ数を取得するためのカスタムDocTemplate"""

    def __init__(self, *args, **kwargs):
        BaseDocTemplate.__init__(self, *args, **kwargs)
        self._page_count = 0

    def afterFlowable(self, flowable):
        """各フローアブルの後に呼ばれる"""
        pass

    def afterPage(self):
        """各ページの後に呼ばれる"""
        self._page_count = max(self._page_count, self.page)


def generate_pdf(estimate: EstimateData) -> bytes:
    """見積書PDFを生成

    Args:
        estimate: 見積データ

    Returns:
        bytes: PDF バイナリデータ
    """
    font_normal, font_bold = _register_fonts()

    # 1パス目: ページ数をカウント
    count_buffer = BytesIO()
    count_doc = _PageCountDocTemplate(
        count_buffer, pagesize=A4,
        leftMargin=LEFT_MARGIN, rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
    )
    frame = Frame(
        LEFT_MARGIN, BOTTOM_MARGIN,
        CONTENT_WIDTH, PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN,
        id='normal',
    )

    def _noop_header(c, d):
        pass

    count_template = PageTemplate(id='main', frames=[frame], onPage=_noop_header)
    count_doc.addPageTemplates([count_template])
    styles = _create_styles(font_normal, font_bold)
    elements = _build_all_elements(estimate, styles, font_normal, font_bold)
    count_doc.build(elements)
    total_pages = count_doc._page_count
    count_buffer.close()

    # 2パス目: 実際のPDF生成（ページ番号付き）
    buffer = BytesIO()
    doc = _PageCountDocTemplate(
        buffer, pagesize=A4,
        leftMargin=LEFT_MARGIN, rightMargin=RIGHT_MARGIN,
        topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
    )

    frame2 = Frame(
        LEFT_MARGIN, BOTTOM_MARGIN,
        CONTENT_WIDTH, PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN,
        id='normal',
    )

    def _header_footer(canvas_obj, doc_obj):
        """ヘッダー・フッター描画"""
        canvas_obj.saveState()
        # ヘッダー左: 見積ID
        canvas_obj.setFont(font_normal, 8)
        canvas_obj.drawString(LEFT_MARGIN, PAGE_HEIGHT - 12 * mm,
                              f"見積ID {estimate.cover.estimate_id}")
        # ヘッダー右: 発行日
        canvas_obj.drawRightString(PAGE_WIDTH - RIGHT_MARGIN, PAGE_HEIGHT - 12 * mm,
                                   f"発行日 {estimate.cover.issue_date}")
        # フッター右下: ページ X / Y
        canvas_obj.drawRightString(
            PAGE_WIDTH - RIGHT_MARGIN, 8 * mm,
            f"{doc_obj.page}"
        )
        canvas_obj.restoreState()

    template = PageTemplate(id='main', frames=[frame2], onPage=_header_footer)
    doc.addPageTemplates([template])

    styles = _create_styles(font_normal, font_bold)
    elements = _build_all_elements(estimate, styles, font_normal, font_bold)
    doc.build(elements)
    pdf_data = buffer.getvalue()
    buffer.close()
    return pdf_data


def _build_all_elements(estimate: EstimateData, styles: dict,
                         font_normal: str, font_bold: str) -> list:
    """全ページの要素を構築"""
    elements = []

    # Page 1: 御見積書表紙
    elements.extend(_build_cover_page(estimate, styles, font_normal, font_bold))

    # Page 2: 見積内訳書
    elements.append(PageBreak())
    elements.extend(_build_summary_page(estimate, styles, font_normal, font_bold))

    # Pages 3+: 各カテゴリの明細（カテゴリごとに新ページ）
    for cat in estimate.summary.categories:
        if cat.category == CategoryType.SPECIAL_NOTES and not cat.items:
            continue  # 特記事項が空なら省略
        elements.append(PageBreak())
        elements.extend(_build_detail_page(cat, styles, font_normal, font_bold))

    return elements


def _create_styles(font_normal: str, font_bold: str) -> dict:
    """各種スタイルを定義"""
    return {
        'title': ParagraphStyle(
            'Title', fontName=font_bold, fontSize=20,
            alignment=TA_CENTER, textColor=BLUE_TEXT,
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
        'normal_right_small': ParagraphStyle(
            'NormalRightSmall', fontName=font_normal, fontSize=8,
            leading=11, alignment=TA_RIGHT,
        ),
        'bold': ParagraphStyle(
            'Bold', fontName=font_bold, fontSize=9,
            leading=13,
        ),
        'bold_small': ParagraphStyle(
            'BoldSmall', fontName=font_bold, fontSize=8,
            leading=11,
        ),
        'right': ParagraphStyle(
            'Right', fontName=font_normal, fontSize=9,
            alignment=TA_RIGHT,
        ),
        'amount_large': ParagraphStyle(
            'AmountLarge', fontName=font_bold, fontSize=18,
            alignment=TA_RIGHT, textColor=RED,
        ),
        'company_name': ParagraphStyle(
            'CompanyName', fontName=font_bold, fontSize=9,
            leading=13,
        ),
        'company_name_large': ParagraphStyle(
            'CompanyNameLarge', fontName=font_bold, fontSize=11,
            leading=15, alignment=TA_RIGHT,
        ),
        'cell_normal': ParagraphStyle(
            'CellNormal', fontName=font_normal, fontSize=8,
            leading=11, wordWrap='CJK',
        ),
        'cell_right': ParagraphStyle(
            'CellRight', fontName=font_normal, fontSize=8,
            leading=11, alignment=TA_RIGHT, wordWrap='CJK',
        ),
        'cell_center': ParagraphStyle(
            'CellCenter', fontName=font_normal, fontSize=8,
            leading=11, alignment=TA_CENTER, wordWrap='CJK',
        ),
        'cell_bold': ParagraphStyle(
            'CellBold', fontName=font_bold, fontSize=8,
            leading=11, wordWrap='CJK',
        ),
        'cell_bold_right': ParagraphStyle(
            'CellBoldRight', fontName=font_bold, fontSize=8,
            leading=11, alignment=TA_RIGHT, wordWrap='CJK',
        ),
    }


def _build_cover_page(estimate: EstimateData, styles: dict,
                       font_normal: str, font_bold: str) -> list:
    """Page 1: 御見積書表紙を構築（サンプルPDFに準拠したレイアウト）"""
    elements = []

    # === タイトルバー（水色背景、青文字） ===
    title_data = [['御　見　積　書']]
    title_table = Table(title_data, colWidths=[CONTENT_WIDTH])
    title_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), HEADER_BG),
        ('TEXTCOLOR', (0, 0), (-1, -1), BLUE_TEXT),
        ('FONTNAME', (0, 0), (-1, -1), font_bold),
        ('FONTSIZE', (0, 0), (-1, -1), 20),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(title_table)
    elements.append(Spacer(1, 15 * mm))

    # === 宛先（左寄せ、下線付き） ===
    client_name = estimate.cover.client_name if estimate.cover.client_name else "　　　　　　"
    client_text = f"{client_name}　御中"
    elements.append(Paragraph(client_text, ParagraphStyle(
        'Client', fontName=font_bold, fontSize=16,
    )))
    # 下線（手動で描画する代わりにSpacerと別テーブルで表現）
    underline_table = Table([['']],
                            colWidths=[90 * mm],
                            rowHeights=[0.5 * mm])
    underline_table.setStyle(TableStyle([
        ('LINEBELOW', (0, 0), (-1, -1), 1, BLACK),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(underline_table)
    elements.append(Spacer(1, 2 * mm))
    elements.append(Paragraph("下記の通り、御見積申し上げます。", styles['normal']))
    elements.append(Spacer(1, 25 * mm))

    # === 金額テーブル + 会社情報を横並び（サンプルPDF準拠） ===
    total_str = f"¥{_fmt(estimate.cover.total_with_tax)}.-"
    tax_excl_str = f"¥{_fmt(estimate.cover.total_before_tax)}.-"
    tax_str = f"¥{_fmt(estimate.cover.tax)}.-"

    from config import COMPANY_INFO

    # --- 左列: 金額ブロック ---
    # サンプルPDF準拠: 御見積金額ボックス + その下に税抜/消費税
    # 金額ボックスは左列幅いっぱい(98mm)に配置
    amt_left_w = 98 * mm
    amount_data = [
        ['御見積金額', Paragraph(total_str, styles['amount_large'])],
    ]
    amount_table = Table(amount_data, colWidths=[28 * mm, amt_left_w - 28 * mm])
    amount_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, 0), font_bold),
        ('FONTSIZE', (0, 0), (0, 0), 12),
        ('ALIGN', (0, 0), (0, 0), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 1, BLACK),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (0, 0), 8),
        ('RIGHTPADDING', (1, 0), (1, 0), 8),
    ]))

    # 税抜合計・消費税は金額ボックスの右端に合わせて配置
    tax_detail_data = [
        [f'税抜合計', tax_excl_str],
        [f'消費税', tax_str],
    ]
    tax_detail_table = Table(tax_detail_data, colWidths=[40 * mm, 45 * mm])
    tax_detail_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_normal),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('LINEBELOW', (0, 0), (-1, 0), 0.5, BLACK),
        ('LINEBELOW', (0, 1), (-1, 1), 0.5, BLACK),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (1, 0), (1, -1), 0),
    ]))

    # 税抜/消費税を右寄せにするため、左にスペーサーを入れたテーブルで包む
    tax_wrapper_data = [['', tax_detail_table]]
    tax_wrapper = Table(tax_wrapper_data,
                        colWidths=[amt_left_w - 85 * mm, 85 * mm])
    tax_wrapper.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))

    amount_block = []
    amount_block.append(amount_table)
    amount_block.append(Spacer(1, 2 * mm))
    amount_block.append(tax_wrapper)

    # --- 右列: 会社情報ブロック ---
    right_block = []
    right_block.append(Paragraph(
        COMPANY_INFO['slogan'],
        ParagraphStyle('Slogan', fontName=font_normal, fontSize=8,
                       leading=10, alignment=TA_RIGHT)
    ))
    right_block.append(Spacer(1, 4 * mm))
    # SANEIロゴ風テキスト
    right_block.append(Paragraph(
        '<b>SANEI</b>',
        ParagraphStyle('LogoText', fontName=font_bold, fontSize=18,
                       leading=22, alignment=TA_CENTER,
                       textColor=BLUE_TEXT)
    ))
    right_block.append(Paragraph(
        '<b>株式会社 サンエー</b>',
        ParagraphStyle('LogoSub', fontName=font_bold, fontSize=9,
                       leading=12, alignment=TA_CENTER,
                       textColor=BLUE_TEXT)
    ))
    right_block.append(Spacer(1, 4 * mm))
    right_block.append(Paragraph(
        f"<b>{COMPANY_INFO['name']}</b>",
        ParagraphStyle('CompName', fontName=font_bold, fontSize=9,
                       leading=13, alignment=TA_LEFT)
    ))
    right_block.append(Paragraph(COMPANY_INFO['postal_code'],
                                  ParagraphStyle('Addr', fontName=font_normal,
                                                 fontSize=8, leading=12)))
    right_block.append(Paragraph(COMPANY_INFO['address'],
                                  ParagraphStyle('Addr2', fontName=font_normal,
                                                 fontSize=8, leading=12)))
    right_block.append(Paragraph(COMPANY_INFO['tel'],
                                  ParagraphStyle('Tel', fontName=font_normal,
                                                 fontSize=8, leading=12)))
    right_block.append(Paragraph(COMPANY_INFO['fax'],
                                  ParagraphStyle('Fax', fontName=font_normal,
                                                 fontSize=8, leading=12)))
    right_block.append(Spacer(1, 3 * mm))

    # 社印エリア（3つの空ボックス）
    stamp_data = [['', '', '']]
    stamp_table = Table(stamp_data, colWidths=[17 * mm, 17 * mm, 17 * mm],
                        rowHeights=[17 * mm])
    stamp_table.setStyle(TableStyle([
        ('BOX', (0, 0), (0, 0), 0.5, BLACK),
        ('BOX', (1, 0), (1, 0), 0.5, BLACK),
        ('BOX', (2, 0), (2, 0), 0.5, BLACK),
    ]))
    right_block.append(stamp_table)
    right_block.append(Spacer(1, 3 * mm))
    right_block.append(Paragraph(
        f"担当者：{estimate.cover.representative}",
        ParagraphStyle('Rep', fontName=font_normal, fontSize=8,
                       leading=11, alignment=TA_RIGHT)
    ))

    # 金額ブロック(左) + 会社情報(右)を横並び
    # 左列98mm + 右列82mm = 180mm = CONTENT_WIDTH
    layout_table = Table(
        [[amount_block, right_block]],
        colWidths=[98 * mm, CONTENT_WIDTH - 98 * mm],
    )
    layout_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(layout_table)
    elements.append(Spacer(1, 10 * mm))

    # === 工事情報 + 空右列（サンプルPDFでは左下に配置） ===
    info_items = [
        ("工事名", estimate.cover.project_name),
        ("工事場所", estimate.cover.project_location),
        ("工事期間", estimate.cover.project_period),
        ("有効期限", estimate.cover.validity_period),
        ("備考", estimate.cover.notes),
    ]
    info_data = []
    for label, value in info_items:
        info_data.append([label, Paragraph(value or '', styles['normal'])])

    info_table = Table(info_data, colWidths=[25 * mm, 80 * mm])
    info_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), font_bold),
        ('FONTSIZE', (0, 0), (0, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (0, -1), 0),
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
            _fmt(cat.total) if cat.total else "0",
        ])

    # 小計行
    data.append(['', '小計', _fmt(estimate.summary.subtotal)])
    # 値引き行
    data.append(['', 'お値引き', _fmt(estimate.summary.discount)])
    # 税抜合計行
    data.append(['', '税抜合計', _fmt(estimate.summary.total_before_tax)])

    # 分類列を大きく取る（サンプルPDFに準拠）
    col_widths = [15 * mm, CONTENT_WIDTH - 15 * mm - 35 * mm, 35 * mm]
    table = Table(data, colWidths=col_widths)

    num_cats = len(estimate.summary.categories)
    summary_start = 1 + num_cats  # ヘッダー(1) + カテゴリ行数

    style_cmds = [
        # ヘッダー行: 薄グレー背景
        ('BACKGROUND', (0, 0), (-1, 0), LIGHT_GRAY),
        ('FONTNAME', (0, 0), (-1, 0), font_bold),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('FONTNAME', (0, 1), (-1, -1), font_normal),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
        # 罫線: 全セル細い罫線
        ('GRID', (0, 0), (-1, -1), LINE_WIDTH, BLACK),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]

    # 小計・値引き・税抜合計行のスタイル
    for i in range(3):
        row = summary_start + i
        style_cmds.append(('FONTNAME', (1, row), (2, row), font_bold))
        style_cmds.append(('ALIGN', (1, row), (1, row), 'RIGHT'))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    return elements


def _build_detail_page(category: CategorySection, styles: dict,
                        font_normal: str, font_bold: str) -> list:
    """明細ページを構築（サンプルPDFに準拠: 2行レイアウト）"""
    elements = []

    # タイトル
    elements.append(Paragraph("見積明細書", styles['subtitle']))
    elements.append(Paragraph(
        f"（{category.category_number}. {category.category.value}）",
        ParagraphStyle('CatTitle', fontName=font_normal, fontSize=11,
                       alignment=TA_CENTER, spaceAfter=5 * mm)
    ))
    elements.append(Spacer(1, 3 * mm))

    # 列幅定義（サンプルPDFに準拠: No./摘要/備考/数量/見積単価/見積額）
    col_widths = [12 * mm, 50 * mm, 45 * mm, 18 * mm, 23 * mm, 23 * mm]

    # === テーブルデータ構築 ===
    # ヘッダー行
    data = [['No.', '摘要', '備考', '数量', '見積単価', '見積額']]

    # カテゴリ名行
    data.append([category.category.value, '', '', '', '', ''])

    # 各明細項目: サンプルPDFでは1項目=2行（備考が上下で分かれる）
    for item in category.items:
        remarks_parts = item.remarks.split('\n') if item.remarks else ['']
        remark_line1 = remarks_parts[0] if len(remarks_parts) > 0 else ''
        remark_line2 = remarks_parts[1] if len(remarks_parts) > 1 else ''

        unit_price_str = _fmt(item.unit_price) if item.unit_price else ""
        amount_str = _fmt(item.amount) if item.amount else ""

        # 行1: No. / 摘要 / 備考1行目 / （数量・単価・金額は行1-2で結合）
        data.append([
            str(item.no),
            Paragraph(item.description, styles['cell_normal']),
            Paragraph(remark_line1, styles['cell_normal']),
            item.quantity,
            unit_price_str,
            amount_str,
        ])
        # 行2: 空 / 空 / 備考2行目 / 空 / 空 / 空
        data.append([
            '',
            '',
            Paragraph(remark_line2, styles['cell_normal']),
            '',
            '',
            '',
        ])

    # 小計行
    data.append(['', '', '', f'{category.category.value} 小計', '', _fmt(category.subtotal)])
    # 合計行
    data.append(['', '', '', f'{category.category.value} 合計', '', _fmt(category.total)])

    table = Table(data, colWidths=col_widths, repeatRows=1)

    # スタイル
    style_cmds = [
        # ヘッダー行: 薄グレー背景
        ('BACKGROUND', (0, 0), (-1, 0), LIGHT_GRAY),
        ('FONTNAME', (0, 0), (-1, 0), font_bold),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        # カテゴリ名行（行1）: 太字、全列結合
        ('FONTNAME', (0, 1), (-1, 1), font_bold),
        ('FONTSIZE', (0, 1), (-1, 1), 9),
        ('SPAN', (0, 1), (-1, 1)),
        # 全体フォント
        ('FONTNAME', (0, 2), (-1, -1), font_normal),
        ('FONTSIZE', (0, 2), (-1, -1), 8),
        # アライメント
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),   # No.列: 中央
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),    # 数量: 右寄せ
        ('ALIGN', (4, 0), (4, -1), 'RIGHT'),    # 見積単価: 右寄せ
        ('ALIGN', (5, 0), (5, -1), 'RIGHT'),    # 見積額: 右寄せ
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        # 罫線: 外枠 + 列間の縦線
        ('BOX', (0, 0), (-1, -1), LINE_WIDTH, BLACK),
        # ヘッダー下の横線
        ('LINEBELOW', (0, 0), (-1, 0), LINE_WIDTH, BLACK),
        # カテゴリ名行の下の横線
        ('LINEBELOW', (0, 1), (-1, 1), LINE_WIDTH, BLACK),
        # パディング
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ]

    # 列間の縦罫線（全行）
    for col in range(1, 6):
        style_cmds.append(('LINEAFTER', (col - 1, 0), (col - 1, -1), LINE_WIDTH, BLACK))

    # 各明細項目の2行ペアのスタイル
    row_idx = 2  # ヘッダー(0) + カテゴリ名(1) の後から
    for item_idx, item in enumerate(category.items):
        row1 = row_idx + item_idx * 2
        row2 = row1 + 1

        # No.列: 行1-2を結合
        style_cmds.append(('SPAN', (0, row1), (0, row2)))
        # 摘要列: 行1-2を結合
        style_cmds.append(('SPAN', (1, row1), (1, row2)))
        # 数量列: 行1-2を結合
        style_cmds.append(('SPAN', (3, row1), (3, row2)))
        # 見積単価列: 行1-2を結合
        style_cmds.append(('SPAN', (4, row1), (4, row2)))
        # 見積額列: 行1-2を結合
        style_cmds.append(('SPAN', (5, row1), (5, row2)))

        # 備考列の行1-行2の間に区切り線
        style_cmds.append(('LINEBELOW', (2, row1), (2, row1), LINE_WIDTH * 0.5, BLACK))

        # 項目ペアの下に横線（全列）
        style_cmds.append(('LINEBELOW', (0, row2), (-1, row2), LINE_WIDTH, BLACK))

    # 小計・合計行のスタイル
    total_rows_start = 2 + len(category.items) * 2
    for i in range(2):
        row = total_rows_start + i
        style_cmds.append(('SPAN', (0, row), (2, row)))   # No.~備考を結合
        style_cmds.append(('SPAN', (3, row), (4, row)))   # 数量~見積単価を結合（ラベル用）
        style_cmds.append(('FONTNAME', (3, row), (5, row), font_bold))
        style_cmds.append(('ALIGN', (3, row), (4, row), 'RIGHT'))
        style_cmds.append(('LINEBELOW', (0, row), (-1, row), LINE_WIDTH, BLACK))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    return elements
