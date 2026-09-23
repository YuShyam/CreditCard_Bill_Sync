"""
================================================================================
星展銀行 (DBS Bank) 帳單解析器
================================================================================
"""

import re
from .base_parser import BaseBankParser, KNOWN_CURRENCIES

class DBSParser(BaseBankParser):
    def __init__(self):
        super().__init__(bank_name="星展銀行")

    def parse(self, lines, year, filename, *args, **kwargs):
        rows = []
        current_card_last4 = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 檢查卡號標頭 (例如: 以下為星展傳說對決聯名卡-特爾安娜絲 正卡（卡號末4碼 1099）新增消費小計)
            m_card = re.search(r"卡號末[4４]碼\s*(\d{4})", line)
            if m_card:
                current_card_last4 = m_card.group(1)
                continue

            # 檢查交易行 (例如: 2026/04/06 2026/04/10 連加＊？亭停車事業股份 / TW Taipei 120)
            m = re.match(r"^(\d{4}/\d{2}/\d{2})\s+(\d{4}/\d{2}/\d{2})\s+(.+)$", line)
            if not m:
                continue

            tx_d = self.parse_date_to_yyyy_mm_dd(m.group(1), year)
            post_d = self.parse_date_to_yyyy_mm_dd(m.group(2), year)
            rest = m.group(3).strip()

            tokens = rest.split()
            if not tokens:
                continue

            # 最後一個 token 必須是數字金額
            amt_token = tokens[-1]
            if not amt_token.replace(",", "").replace("-", "").isdigit():
                continue

            amount = self.clean_amount(amt_token)
            merchant_raw = " ".join(tokens[:-1]).strip()

            # 檢查外幣或消費地標籤 (例如: / TW Taipei 或 / SG SINGAPORE)
            foreign_curr = ""
            merchant = merchant_raw
            if " / " in merchant_raw:
                parts = merchant_raw.split(" / ")
                merchant = parts[0].strip()
                region_info = parts[1].strip()
                # 若非台灣地區則記錄為外幣交易地
                if not any(k in region_info for k in ["TW", "Taipei", "TAIPEI"]):
                    foreign_curr = region_info

            # 排除自動繳款或 ATM 沖銷行 (例如: ATM/網路繳款/本行帳戶繳款 -6,939)
            if self.is_payment_or_autopay(merchant, amount):
                continue

            rows.append([False, tx_d, post_d, self.bank_name, current_card_last4, merchant, foreign_curr, amount, filename])

        return rows
