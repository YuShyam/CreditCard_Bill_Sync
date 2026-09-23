"""
================================================================================
第一銀行 (First Bank) 帳單解析器
================================================================================
"""

import re
from .base_parser import BaseBankParser, KNOWN_CURRENCIES

class FirstBankParser(BaseBankParser):
    def __init__(self):
        super().__init__(bank_name="第一銀行")

    def parse(self, lines, year, filename, *args, **kwargs):
        rows = []
        date_pat = re.compile(r"^(\d{2}/\d{2})")
        
        for line in lines:
            line = line.strip()
            if not date_pat.match(line): continue
            
            # 過濾上期繳款沖銷 (如 04/16 感謝您上期本行繳款已收到 -87)
            if "感謝您上期" in line or "繳款已收到" in line:
                continue
                
            m_two_dates = re.match(r"^(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+)$", line)
            if m_two_dates:
                tx_d = self.parse_date_to_yyyy_mm_dd(m_two_dates.group(1), year)
                post_d = self.parse_date_to_yyyy_mm_dd(m_two_dates.group(2), year)
                rest = m_two_dates.group(3).strip()
                
                # 抽取外幣
                foreign_curr = ""
                m_fc = re.search(rf"\b({KNOWN_CURRENCIES}\s+[\d,]+(?:\.\d+)?)\b", rest)
                if m_fc:
                    foreign_curr = m_fc.group(1)
                    rest = (rest[:m_fc.start()] + " " + rest[m_fc.end():]).strip()
                
                # 去除末尾的日期或國別標記 (如 03/18, US, TW)
                rest = re.sub(r"\s+\d{2}/\d{2}$", "", rest)
                rest = re.sub(r"\s+[A-Z]{2}$", "", rest)
                
                tokens = rest.split()
                card_last4 = ""
                amount = 0.0
                merchant = ""
                
                if len(tokens) >= 2 and tokens[-1].isdigit() and len(tokens[-1]) == 4:
                    card_last4 = tokens[-1]
                    if len(tokens) >= 3 and tokens[-2].replace(",", "").replace("-", "").isdigit():
                        amount = self.clean_amount(tokens[-2])
                        merchant = " ".join(tokens[:-2])
                    else:
                        merchant = " ".join(tokens[:-1])
                elif len(tokens) >= 2 and tokens[-1].replace(",", "").replace("-", "").isdigit():
                    amount = self.clean_amount(tokens[-1])
                    merchant = " ".join(tokens[:-1])
                else:
                    merchant = rest
                    
                if self.is_payment_or_autopay(merchant, amount):
                    continue
                    
                rows.append([False, tx_d, post_d, self.bank_name, card_last4, merchant.strip(), foreign_curr, amount, filename])
        return rows
