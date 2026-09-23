"""
================================================================================
永豐銀行 (Bank SinoPac) 帳單解析器
================================================================================
"""

import re
from .base_parser import BaseBankParser, KNOWN_CURRENCIES

class SinopacParser(BaseBankParser):
    def __init__(self):
        super().__init__(bank_name="永豐銀行")

    def parse(self, lines, year, filename, *args, **kwargs):
        rows = []
        for line in lines:
            line = line.strip()
            m = re.match(r"^(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+)$", line)
            if not m: continue
            
            tx_d = self.parse_date_to_yyyy_mm_dd(m.group(1), year)
            post_d = self.parse_date_to_yyyy_mm_dd(m.group(2), year)
            rest = m.group(3).strip()
            
            tokens = rest.split()
            if not tokens: continue
            
            card_last4 = ""
            if tokens[0].isdigit() and len(tokens[0]) == 4:
                card_last4 = tokens[0]
                tokens = tokens[1:]
                
            if not tokens: continue
            
            foreign_curr = ""
            amount = 0.0
            merchant = ""
            
            # 檢查末尾是否有外幣金額，例如: MYR20.60 或 USD 4.22
            m_fc = re.search(rf"({KNOWN_CURRENCIES}\s*[\d,]+(?:\.\d+)?)$", " ".join(tokens))
            if m_fc:
                foreign_raw = m_fc.group(1)
                # 規範化貨幣格式 (如 MYR20.60 -> MYR 20.60)
                m_curr_code = re.match(rf"^({KNOWN_CURRENCIES})\s*([\d,.]+)$", foreign_raw)
                if m_curr_code:
                    foreign_curr = f"{m_curr_code.group(1)} {m_curr_code.group(2)}"
                else:
                    foreign_curr = foreign_raw
                    
                before_fc = (" ".join(tokens))[:m_fc.start()].strip()
                b_tokens = before_fc.split()
                
                # 排除末尾的國外交易日 (如 06/27, 06/30)
                if b_tokens and re.match(r"^\d{2}/\d{2}$", b_tokens[-1]):
                    b_tokens = b_tokens[:-1]
                    
                # 前一個 token 即為台幣金額
                if b_tokens and b_tokens[-1].replace(",", "").replace("-", "").isdigit():
                    amount = self.clean_amount(b_tokens[-1])
                    merchant = " ".join(b_tokens[:-1])
                else:
                    merchant = " ".join(b_tokens)
            else:
                # 國內交易：最後一個 token 為台幣金額
                amount = self.clean_amount(tokens[-1])
                merchant = " ".join(tokens[:-1])
                
            if not merchant:
                merchant = "信用卡消費"
                
            # 過濾上期繳款 / 自扣已入帳沖銷
            if self.is_payment_or_autopay(merchant, amount):
                continue
                
            # 過濾 0 元之大戶/大大回饋入帳戶通知
            if amount == 0 and ("回饋入帳戶" in merchant or "回饋金" in merchant):
                continue
                
            rows.append([False, tx_d, post_d, self.bank_name, card_last4, merchant, foreign_curr, amount, filename])
        return rows
