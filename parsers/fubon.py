"""
================================================================================
富邦銀行 (Taipei Fubon Bank) 帳單解析器
================================================================================
"""

import re
from .base_parser import BaseBankParser, KNOWN_CURRENCIES

class FubonParser(BaseBankParser):
    def __init__(self):
        super().__init__(bank_name="富邦銀行")

    def parse(self, lines, year, filename, *args, **kwargs):
        rows = []
        current_card_last4 = ""
        
        for line in lines:
            line = line.strip()
            
            # 偵測卡號標頭：如 "MASTER鈦金正卡末４碼8617" 或 "正卡末4碼1234"
            m_card = re.search(r"(?:卡末[４4]碼|末[４4]碼)\s*(\d{4})", line)
            if m_card:
                current_card_last4 = m_card.group(1)
                
            m_head = re.match(r"^(\d{2,3}/\d{2}/\d{2})\s+(.+)$", line)
            if not m_head: continue
            
            tx_d_raw = m_head.group(1)
            rest = m_head.group(2).strip()
            tx_d = self.parse_date_to_yyyy_mm_dd(tx_d_raw, year)
            
            m_post = re.search(r"(\d{2,3}/\d{2}/\d{2})", rest)
            if m_post:
                post_d_raw = m_post.group(1)
                post_d = self.parse_date_to_yyyy_mm_dd(post_d_raw, year)
                merchant = rest[:m_post.start()].strip()
                after_post = rest[m_post.end():].strip()
                
                foreign_curr = ""
                m_fc = re.search(rf"\b({KNOWN_CURRENCIES}\s+[\d,]+(?:\.\d+)?)\b", after_post)
                if m_fc:
                    foreign_curr = m_fc.group(1)
                    
                tokens = after_post.split()
                amount = self.clean_amount(tokens[-1]) if tokens else 0.0
                
                if not merchant:
                    merchant = "自動轉帳扣繳/繳款" if amount < 0 else "信用卡消費"
                elif re.match(r"^[\d,]+\s*\)", merchant):
                    merchant = f"國外交易手續費 ({merchant})"
                    
                # 過濾上期繳款 / ACH 自動扣繳
                if self.is_payment_or_autopay(merchant, amount):
                    continue
                    
                rows.append([False, tx_d, post_d, self.bank_name, current_card_last4, merchant, foreign_curr, amount, filename])
        return rows
