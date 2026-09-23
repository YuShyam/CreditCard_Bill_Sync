"""
================================================================================
中國信託 (CTBC Bank) 帳單解析器 (OCR 與向量文字混合引擎)
================================================================================
"""

import re
import sys
from .base_parser import BaseBankParser, KNOWN_CURRENCIES

try:
    import winocr
    HAS_WINOCR = True
except ImportError:
    HAS_WINOCR = False

PAY_MAP = {
    "S": "Samsung Pay 刷卡消費",
    "A": "Apple Pay 刷卡消費",
    "G": "Google Pay 刷卡消費",
    "H": "Hami Pay 刷卡消費",
    "T": "台灣 Pay 刷卡消費",
    "M": "Garmin Pay 刷卡消費",
    "F": "Google Pay SE 刷卡消費",
    "Q": "TWQR 跨機構購物",
}

class CTBCParser(BaseBankParser):
    def __init__(self):
        super().__init__(bank_name="中國信託")

    def clean_ocr_merchant(self, name: str) -> str:
        """清洗 OCR 辨識出來的商家名稱"""
        if not name:
            return "國內刷卡消費"
        name = re.sub(r"^[『「\s,*-]+", "", name)
        name = re.sub(r"[』」\s,]+$", "", name)
        name = name.replace("PCH0ME", "PCHOME")
        name = re.sub(r"連加\*L[|I]NE[禮物][EE]", "連加*LINE禮物", name)
        name = re.sub(r"連加\*m[。.]m[。.]賭網", "連加*momo購物", name)
        name = re.sub(r"台麥當勞S0?K", "台灣麥當勞", name)
        name = re.sub(r"新世紀資股份有限公司", "新世紀資通股份有限公司", name)
        return name.strip() or "國內刷卡消費"

    def parse(self, lines, year, filename, pdf=None):
        """
        中信帳單解析：
        若有 pdf 實例且系統支援 winocr，則使用「文字層座標 + 圖像層繁體中文 OCR」精準萃取中文特店名稱；
        否則退回文字層快速解析模式。
        """
        if pdf is not None and HAS_WINOCR:
            try:
                rows = self._parse_with_ocr(pdf, year, filename)
                if rows:
                    return rows
            except Exception as e:
                print(f"    ⚠️ 中信 OCR 解析異常，退回純文字模式: {e}")

        # Fallback: 純文字解析
        return self._parse_text_only(lines, year, filename)

    def _parse_with_ocr(self, pdf, year, filename):
        rows = []
        for page_idx, page in enumerate(pdf.pages):
            if page_idx == 0:
                continue # 首頁通常為摘要

            # 1. OCR 抽取頁面中特店欄位之文字與 Y 座標 (繁體中文)
            pil_img = page.to_image(resolution=150).original
            ocr_res = winocr.recognize_pil_sync(pil_img, lang='zh-Hant-TW')

            ocr_words = []
            for line in ocr_res.get('lines', []):
                for w in line.get('words', []):
                    rect = w.get('bounding_rect', {})
                    ocr_words.append({
                        'text': w.get('text', ''),
                        'x': rect.get('x', 0),
                        'y': rect.get('y', 0),
                    })

            # 篩選特店欄位 (240 <= x < 540, 150 <= y <= 650)
            merchant_ocr_items = [w for w in ocr_words if 240 <= w['x'] < 540 and 150 <= w['y'] <= 650]
            ocr_lines = []
            for w in sorted(merchant_ocr_items, key=lambda w: (w['y'], w['x'])):
                placed = False
                for grp in ocr_lines:
                    avg_y = sum(item['y'] for item in grp) / len(grp)
                    if abs(w['y'] - avg_y) <= 8:
                        grp.append(w)
                        placed = True
                        break
                if not placed:
                    ocr_lines.append([w])

            ocr_merchant_lookup = []
            for grp in ocr_lines:
                grp.sort(key=lambda w: w['x'])
                avg_y = sum(w['y'] for w in grp) / len(grp)
                m_text = "".join(w['text'] for w in grp).strip()
                if m_text and not any(k in m_text for k in ["消日", "入帳", "消費暨收", "循環信用", "依契約", "款項已收到", "國泰銀", "注意事項"]):
                    ocr_merchant_lookup.append((avg_y, m_text))

            # 2. 用 pdfplumber 抽取帶有 top 座標的單詞並組合成行
            words_pdf = page.extract_words()
            pdf_lines = []
            for w in sorted(words_pdf, key=lambda x: (x['top'], x['x0'])):
                placed = False
                for line in pdf_lines:
                    avg_top = sum(item['top'] for item in line) / len(line)
                    if abs(w['top'] - avg_top) <= 3.5:
                        line.append(w)
                        placed = True
                        break
                if not placed:
                    pdf_lines.append([w])

            for pline in pdf_lines:
                pline.sort(key=lambda x: x['x0'])
                avg_top = sum(w['top'] for w in pline) / len(pline)
                expected_ocr_y = avg_top * (150.0 / 72.0) - 14.0

                line_text = " ".join(w['text'] for w in pline).strip()

                # 檢查交易日期 (如 115/08/21 115/08/24)
                m = re.search(r"(\d{2,3}/\d{2}/\d{2})\s+(\d{2,3}/\d{2}/\d{2})", line_text)
                if not m:
                    continue

                tx_d = self.parse_date_to_yyyy_mm_dd(m.group(1), year)
                post_d = self.parse_date_to_yyyy_mm_dd(m.group(2), year)

                rest = line_text[m.end():].strip()
                tokens = rest.split()
                if not tokens:
                    rest = line_text[:m.start()].strip()
                    tokens = rest.split()
                if not tokens:
                    continue

                # 情況 A: 國外交易手續費 (如 18 9159)
                if len(tokens) == 2 and tokens[0].isdigit() and tokens[1].isdigit() and len(tokens[1]) == 4:
                    amt = self.clean_amount(tokens[0])
                    card_last4 = tokens[1]
                    rows.append([False, tx_d, post_d, self.bank_name, card_last4, "國外交易手續費", "", amt, filename])
                    continue

                # 情況 B: 外幣消費 (如 EXIMBAY*wowpass 1,201 9159 KR 08/28 KRW 52,000.00)
                foreign_curr = ""
                m_fc = re.search(rf"\b({KNOWN_CURRENCIES}\s+[\d,]+(?:\.\d+)?)\b", line_text)
                if m_fc:
                    foreign_curr = m_fc.group(1)
                    before_fc = line_text[:m_fc.start()].strip()
                    before_fc = re.sub(r"\d{2,3}/\d{2}/\d{2}", "", before_fc).strip()
                    b_tokens = before_fc.split()
                    card_last4 = ""
                    amount = 0.0
                    merchant = ""
                    for i, t in enumerate(b_tokens):
                        if t.isdigit() and len(t) == 4 and i > 0:
                            card_last4 = t
                            amount = self.clean_amount(b_tokens[i-1])
                            merchant = " ".join(b_tokens[:i-1])
                            break
                    if not merchant:
                        merchant = before_fc
                    if self.is_payment_or_autopay(merchant, amount):
                        continue
                    rows.append([False, tx_d, post_d, self.bank_name, card_last4, merchant, foreign_curr, amount, filename])
                else:
                    # 情況 C: 國內消費 (中文特店圖片)
                    card_last4 = ""
                    amount = 0.0
                    for i, t in enumerate(tokens):
                        if t.isdigit() and len(t) == 4 and i > 0:
                            card_last4 = t
                            amount = self.clean_amount(tokens[i-1])
                            break
                    if amount == 0.0:
                        for t in tokens:
                            if t.replace(",", "").replace("-", "").isdigit() and len(t) != 4:
                                amount = self.clean_amount(t)
                            elif t.isdigit() and len(t) == 4:
                                card_last4 = t

                    # 依據 Y 座標精準匹配 OCR 商家名稱
                    best_match = None
                    min_diff = 999
                    for ocr_y, ocr_text in ocr_merchant_lookup:
                        diff = abs(ocr_y - expected_ocr_y)
                        if diff < min_diff and diff <= 10:
                            min_diff = diff
                            best_match = ocr_text

                    merchant = self.clean_ocr_merchant(best_match)

                    if "國泰銀" in merchant or self.is_payment_or_autopay(merchant, amount):
                        continue

                    rows.append([False, tx_d, post_d, self.bank_name, card_last4, merchant, "", amount, filename])

        return rows

    def _parse_text_only(self, lines, year, filename):
        rows = []
        for line in lines:
            line = line.strip()
            m = re.match(r"^(\d{2,3}/\d{2}/\d{2})\s+(\d{2,3}/\d{2}/\d{2})\s+(.+)$", line)
            if not m: continue

            tx_d = self.parse_date_to_yyyy_mm_dd(m.group(1), year)
            post_d = self.parse_date_to_yyyy_mm_dd(m.group(2), year)
            rest = m.group(3).strip()
            tokens = rest.split()
            if not tokens: continue

            if len(tokens) == 2 and tokens[0].isdigit() and tokens[1].isdigit() and len(tokens[1]) == 4:
                amt = self.clean_amount(tokens[0])
                card_last4 = tokens[1]
                rows.append([False, tx_d, post_d, self.bank_name, card_last4, "國外交易手續費", "", amt, filename])
                continue

            foreign_curr = ""
            m_fc = re.search(rf"\b({KNOWN_CURRENCIES}\s+[\d,]+(?:\.\d+)?)\b", rest)
            if m_fc:
                foreign_curr = m_fc.group(1)
                before_fc = rest[:m_fc.start()].strip()
                b_tokens = before_fc.split()
                card_last4 = ""
                amount = 0.0
                merchant = ""
                for i, t in enumerate(b_tokens):
                    if t.isdigit() and len(t) == 4 and i > 0:
                        card_last4 = t
                        amount = self.clean_amount(b_tokens[i-1])
                        merchant = " ".join(b_tokens[:i-1])
                        break
                if not merchant:
                    merchant = before_fc
                if self.is_payment_or_autopay(merchant, amount):
                    continue
                rows.append([False, tx_d, post_d, self.bank_name, card_last4, merchant, foreign_curr, amount, filename])
            else:
                card_last4 = ""
                amount = 0.0
                if tokens[-1] == "TW" and len(tokens) >= 3 and tokens[-2].isdigit() and len(tokens[-2]) == 4:
                    card_last4 = tokens[-2]
                    amount = self.clean_amount(tokens[-3])
                    raw_m = " ".join(tokens[:-3]) if len(tokens) > 3 else ""
                    merchant = PAY_MAP.get(raw_m, raw_m or "國內刷卡消費")
                elif tokens[-1].isdigit() and len(tokens[-1]) == 4 and len(tokens) >= 2:
                    card_last4 = tokens[-1]
                    amount = self.clean_amount(tokens[-2])
                    raw_m = " ".join(tokens[:-2]) if len(tokens) > 2 else ""
                    merchant = PAY_MAP.get(raw_m, raw_m or "國內刷卡消費")
                else:
                    amount = self.clean_amount(tokens[-1])
                    merchant = rest

                if not merchant or merchant == "TW":
                    merchant = "國內刷卡消費"
                if self.is_payment_or_autopay(merchant, amount):
                    continue
                rows.append([False, tx_d, post_d, self.bank_name, card_last4, merchant, foreign_curr, amount, filename])
        return rows
