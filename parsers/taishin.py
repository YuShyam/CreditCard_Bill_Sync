"""
================================================================================
台新銀行 (Taishin Bank) 帳單解析器
================================================================================
"""

import re
from .base_parser import BaseBankParser, KNOWN_CURRENCIES, COUNTRY_CODES

class TaishinParser(BaseBankParser):
    def __init__(self):
        super().__init__(bank_name="台新銀行")

    def parse(self, lines, year, filename, *args, **kwargs):
        rows = []
        current_card_last4 = ""
        prev_line = ""
        
        for line in lines:
            line = line.strip()
            if not line: continue
            
            # 偵測卡號群組標頭：如 "Richart商務御璽卡 (卡號末四碼:0703)" 或 "末四碼: 1407" (支援 4, ４, 四)
            m_card = re.search(r"(?:卡號末[４4四]碼|末[４4四]碼)\s*[:：]?\s*(\d{4})", line)
            if m_card:
                current_card_last4 = m_card.group(1)
                prev_line = ""
                continue
                
            m = re.match(r"^(\d{2,3}/\d{2}/\d{2})\s+(\d{2,3}/\d{2}/\d{2})\s+(.+)$", line)
            if not m:
                # 若不是交易明細行且不是系統標籤，記錄為上一行 (潛在商家名稱)
                if not any(k in line for k in ["新臺幣金額", "消費日", "分期", "繳款", "本年度", "貼 心", "自動扣款"]):
                    prev_line = line
                continue
            
            tx_d = self.parse_date_to_yyyy_mm_dd(m.group(1), year)
            post_d = self.parse_date_to_yyyy_mm_dd(m.group(2), year)
            rest = m.group(3).strip()
            
            # 自動扣繳繳款行 (如 115/09/02 115/09/02 -11,490) -> 自動過濾
            if re.match(r"^[-,]?\d[\d,]*$", rest):
                amt = self.clean_amount(rest)
                if self.is_payment_or_autopay("自動轉帳扣繳", amt):
                    prev_line = ""
                    continue
                rows.append([False, tx_d, post_d, self.bank_name, current_card_last4, "一般消費", "", amt, filename])
                prev_line = ""
                continue
                
            foreign_curr = ""
            m_fc = re.search(rf"\b({KNOWN_CURRENCIES}\s+[\d,]+(?:\.\d+)?)\b", rest)
            if m_fc:
                foreign_curr = m_fc.group(1)
                before_fc = rest[:m_fc.start()].strip()
                b_tokens = before_fc.split()
                amount = 0.0
                merchant_parts = []
                
                for t in b_tokens:
                    if t.replace(",", "").isdigit() and not re.match(r"^\d{4}$", t):
                        amount = self.clean_amount(t)
                    elif not re.match(r"^\d{4}$", t) and t not in COUNTRY_CODES:
                        merchant_parts.append(t)
                        
                merchant = " ".join(merchant_parts) if merchant_parts else (prev_line or before_fc)
                
                if self.is_payment_or_autopay(merchant, amount):
                    prev_line = ""
                    continue
                    
                rows.append([False, tx_d, post_d, self.bank_name, current_card_last4, merchant, foreign_curr, amount, filename])
            else:
                tokens = rest.split()
                amount = 0.0
                merchant = ""
                if tokens[-1] in COUNTRY_CODES and len(tokens) >= 2:
                    amount = self.clean_amount(tokens[-2])
                    merchant = " ".join(tokens[:-2])
                elif tokens[-1].replace(",", "").replace("-", "").isdigit():
                    amount = self.clean_amount(tokens[-1])
                    merchant = " ".join(tokens[:-1])
                else:
                    merchant = rest
                    
                if not merchant and prev_line:
                    merchant = prev_line
                elif not merchant:
                    merchant = "一般消費" if amount >= 0 else "扣繳/退款"
                    
                if self.is_payment_or_autopay(merchant, amount):
                    prev_line = ""
                    continue
                    
                rows.append([False, tx_d, post_d, self.bank_name, current_card_last4, merchant, foreign_curr, amount, filename])
            
            prev_line = ""
        return rows
