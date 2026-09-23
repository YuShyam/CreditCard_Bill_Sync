"""
================================================================================
玉山銀行 (E.SUN Bank) 帳單解析器
================================================================================
"""

import re
from .base_parser import BaseBankParser, KNOWN_CURRENCIES

class EsunParser(BaseBankParser):
    def __init__(self):
        super().__init__(bank_name="玉山銀行")

    def parse(self, lines, year, filename, *args, **kwargs):
        rows = []
        current_card_last4 = ""
        
        for line in lines:
            line = line.strip()
            
            # 偵測卡號群組標頭：如 "卡號：4323-XXXX-XXXX-0502（Unicard－正卡）"
            m_card = re.search(r"卡號\s*[:：]\s*\d{4}[-X\d]{8,14}(\d{4})", line, re.IGNORECASE)
            if m_card:
                current_card_last4 = m_card.group(1)
                
            # 繳款單行過濾
            if "感謝您辦理自動轉帳繳款" in line or "自動轉帳繳款" in line:
                continue
                
            m_tx = re.match(r"^(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+)$", line)
            if not m_tx: continue
            
            tx_d = self.parse_date_to_yyyy_mm_dd(m_tx.group(1), year)
            post_d = self.parse_date_to_yyyy_mm_dd(m_tx.group(2), year)
            rest = m_tx.group(3).strip()
            
            m_twd = re.search(r"TWD\s+([-,]?\d[\d,]*)$", rest)
            if not m_twd: continue
            
            amount = self.clean_amount(m_twd.group(1))
            content = rest[:m_twd.start()].strip()
            
            foreign_curr = ""
            m_fc = re.search(rf"\b({KNOWN_CURRENCIES}\s+[-,]?\d[\d,]*(?:\.\d+)?)\b", content)
            if m_fc:
                foreign_curr = m_fc.group(1)
                content = content[:m_fc.start()].strip()
                
            merchant = content
            if not merchant:
                merchant = "信用卡消費"
                
            if self.is_payment_or_autopay(merchant, amount):
                continue
                
            rows.append([False, tx_d, post_d, self.bank_name, current_card_last4, merchant, foreign_curr, amount, filename])
        return rows
