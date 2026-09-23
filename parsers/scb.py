"""
================================================================================
渣打銀行 (Standard Chartered Bank) 帳單解析器 (OCR 與向量文字)
================================================================================
"""

import re
from .base_parser import BaseBankParser, KNOWN_CURRENCIES

try:
    import winocr
    HAS_WINOCR = True
except ImportError:
    HAS_WINOCR = False

class SCBParser(BaseBankParser):
    def __init__(self):
        super().__init__(bank_name="渣打銀行")

    def parse(self, lines, year, filename, pdf=None, *args, **kwargs):
        rows = []
        
        # 渣打帳單文字層常有自訂 CID 編碼問題，若有 winocr 則優先透過 OCR 解析第 2 頁消費明細
        if pdf is not None and HAS_WINOCR:
            try:
                rows = self._parse_with_ocr(pdf, year, filename)
                if rows:
                    return rows
            except Exception as e:
                print(f"    ⚠️ 渣打 OCR 解析異常: {e}")

        # Fallback: 文字行正則解析
        current_card_last4 = ""
        for line in lines:
            line = line.strip()
            m_card = re.search(r"(\d{4}-X+[ -]+X+-(\d{4}))", line)
            if m_card:
                current_card_last4 = m_card.group(2)
                continue
            m = re.match(r"^(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+)$", line)
            if not m: continue
            tx_d = self.parse_date_to_yyyy_mm_dd(m.group(1), year)
            post_d = self.parse_date_to_yyyy_mm_dd(m.group(2), year)
            rest = m.group(3).strip()
            tokens = rest.split()
            if not tokens or not tokens[-1].replace(",", "").replace("-", "").isdigit():
                continue
            amt = self.clean_amount(tokens[-1])
            merchant = " ".join(tokens[:-1])
            if self.is_payment_or_autopay(merchant, amt):
                continue
            rows.append([False, tx_d, post_d, self.bank_name, current_card_last4, merchant, "", amt, filename])

        return rows

    def _parse_with_ocr(self, pdf, year, filename):
        rows = []
        for page_idx, page in enumerate(pdf.pages):
            if page_idx == 0: continue # 渣打第 1 頁為摘要，第 2 頁為明細

            pil_img = page.to_image(resolution=150).original
            ocr_res = winocr.recognize_pil_sync(pil_img, lang='zh-Hant-TW')
            
            current_card_last4 = ""
            for line in ocr_res.get('lines', []):
                t = line.get('text', '').strip()
                if not t: continue
                
                # 取得卡號 (例如: 5523-XXXX-XXXX-6120)
                m_card = re.search(r"(\d{4})[ -]+[X\u4e49\u3128]+[ -]+[X\u4e49\u3128]+[ -]+(\d{4})", t)
                if m_card:
                    current_card_last4 = m_card.group(2)
                    continue

                # 取得交易行 (例如: 03 / 06 03 / 06 新-Surve TAIPEI 9,000)
                m_tx = re.match(r"^(\d{2}\s*/\s*\d{2})\s+(\d{2}\s*/\s*\d{2})\s+(.+)$", t)
                if not m_tx: continue

                tx_d_raw = m_tx.group(1).replace(" ", "")
                post_d_raw = m_tx.group(2).replace(" ", "")
                tx_d = self.parse_date_to_yyyy_mm_dd(tx_d_raw, year)
                post_d = self.parse_date_to_yyyy_mm_dd(post_d_raw, year)

                rest = m_tx.group(3).strip()
                tokens = rest.split()
                if not tokens: continue

                # 最後一個 token 為金額
                amt_str = tokens[-1].replace(" ", "")
                if not amt_str.replace(",", "").replace("-", "").isdigit():
                    continue

                amt = self.clean_amount(amt_str)
                merchant = " ".join(tokens[:-1]).strip()

                if "上期月結單" in merchant or self.is_payment_or_autopay(merchant, amt):
                    continue

                rows.append([False, tx_d, post_d, self.bank_name, current_card_last4, merchant, "", amt, filename])

        return rows
