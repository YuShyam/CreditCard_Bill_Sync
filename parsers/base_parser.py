"""
================================================================================
銀行帳單解析抽象基類與通用工具 (Base Bank Parser)
================================================================================
"""

import re
from datetime import datetime

# 常用標準貨幣代碼正則
KNOWN_CURRENCIES = r"(?:USD|KRW|JPY|CNY|EUR|GBP|HKD|SGD|MYR|AUD|CAD|THB|TWD|VND|PHP|NZD|CHF|SEK|RMB)"
COUNTRY_CODES = {"TW", "HK", "KR", "NL", "JP", "US", "GB", "SG", "MY", "CN", "TH", "VN"}

class BaseBankParser:
    """所有銀行解析器的抽象基類"""
    
    def __init__(self, bank_name="未知銀行"):
        self.bank_name = bank_name

    def parse(self, lines, year, filename, pdf=None):
        """
        解析帳單純文字行清單
        :param lines: List[str] 帳單每行文字
        :param year: str 帳單所屬年份 (YYYY)
        :param filename: str PDF 來源檔名
        :return: List[List] 標準明細清單：
                 [對帳(False), 消費日期, 入帳日期, 銀行名稱, 卡號末四碼, 消費商家/項目, 原幣金額/幣別, 台幣金額, 來源檔名]
        """
        raise NotImplementedError("各銀行子類別必須實作 parse 方法")

    @staticmethod
    def parse_date_to_yyyy_mm_dd(date_str, fallback_year):
        """
        將民國年 (115/08/25) 或月日 (08/25) 統一轉換為西元 YYYY/MM/DD
        """
        if not date_str:
            return ""
        date_str = str(date_str).strip().replace(".", "/").replace("-", "/")
        
        # 格式 1: 民國年 115/08/25 或 115/8/25
        m_tw = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", date_str)
        if m_tw:
            y = int(m_tw.group(1)) + 1911
            m = int(m_tw.group(2))
            d = int(m_tw.group(3))
            return f"{y:04d}/{m:02d}/{d:02d}"
        
        # 格式 2: 西元年 2026/08/25
        m_west = re.match(r"^(20\d{2})/(\d{1,2})/(\d{1,2})$", date_str)
        if m_west:
            y = int(m_west.group(1))
            m = int(m_west.group(2))
            d = int(m_west.group(3))
            return f"{y:04d}/{m:02d}/{d:02d}"
            
        # 格式 3: 月日 08/25
        m_md = re.match(r"^(\d{1,2})/(\d{1,2})$", date_str)
        if m_md:
            m = int(m_md.group(1))
            d = int(m_md.group(2))
            y = int(fallback_year) if fallback_year else datetime.now().year
            return f"{y:04d}/{m:02d}/{d:02d}"
            
        return date_str

    @staticmethod
    def clean_amount(amt_str):
        """清理金額字串為 float"""
        if amt_str is None or amt_str == "":
            return 0.0
        s = str(amt_str).replace(",", "").replace("$", "").replace("NT", "").strip()
        try:
            return float(s)
        except Exception:
            return 0.0

    @staticmethod
    def is_payment_or_autopay(merchant: str, amount: float) -> bool:
        """
        判定是否為「上期繳款/自扣沖銷/還款」紀錄 (非真實消費或退貨)
        """
        if not merchant:
            return amount < 0
        desc = merchant.strip()
        
        # 退款/退貨屬於真實消費沖銷，不應被當作繳款過濾
        if "退貨" in desc or "退款" in desc or "取消" in desc or "折抵" in desc:
            return False
            
        payment_keywords = [
            "感謝您", "自動扣繳", "自動轉帳", "自扣已入帳", "ACH", "繳款", "扣繳", 
            "已收到", "本行繳款", "轉帳繳款", "自動扣款"
        ]
        if any(kw in desc for kw in payment_keywords) and amount <= 0:
            return True
            
        return False

