"""
================================================================================
通用銀行帳單解析器 (Generic Bank Parser - Fallback)
================================================================================
"""

import re
from .base_parser import BaseBankParser

class GenericBankParser(BaseBankParser):
    def __init__(self, bank_name="其他銀行"):
        super().__init__(bank_name=bank_name)

    def parse(self, lines, year, filename, *args, **kwargs):
        rows = []
        date_pattern = re.compile(r"^(\d{2,4}[/-]\d{1,2}[/-]\d{1,2}|\d{2}[/-]\d{2})")
        amount_pattern = re.compile(r"([-,]?\d{1,3}(?:,\d{3})*(?:\.\d{2})?)$")

        for line in lines:
            line = line.strip()
            if not line or "總額" in line or "應繳" in line or "前期餘額" in line:
                continue
            
            match_date = date_pattern.search(line)
            if match_date:
                parts = line.split()
                if len(parts) >= 3:
                    tx_date = self.parse_date_to_yyyy_mm_dd(parts[0], year)
                    post_date = self.parse_date_to_yyyy_mm_dd(parts[1], year) if len(parts) > 3 and date_pattern.match(parts[1]) else tx_date
                    
                    match_amt = amount_pattern.search(parts[-1])
                    if match_amt:
                        amount = self.clean_amount(parts[-1])
                        merchant = " ".join(parts[2:-1]) if len(parts) > 3 else parts[1]
                        if merchant and not merchant.isdigit():
                            rows.append([
                                False, tx_date, post_date, self.bank_name, "", merchant, "", amount, filename
                            ])
        return rows

GenericParser = GenericBankParser
