"""
================================================================================
銀行帳單解析器工廠模組 (Bank Parser Factory Router)
================================================================================
"""

from .base_parser import BaseBankParser
from .taishin import TaishinParser
from .fubon import FubonParser
from .ctbc import CTBCParser
from .esun import EsunParser
from .sinopac import SinopacParser
from .first_bank import FirstBankParser
from .dbs import DBSParser
from .scb import SCBParser
from .generic import GenericParser

# 銀行關鍵字與專屬解析器映射表
PARSER_REGISTRY = {
    "台新": TaishinParser,
    "富邦": FubonParser,
    "中國信託": CTBCParser,
    "中信": CTBCParser,
    "玉山": EsunParser,
    "永豐": SinopacParser,
    "第一": FirstBankParser,
    "星展": DBSParser,
    "渣打": SCBParser,
}

def get_parser(bank_name: str) -> BaseBankParser:
    """
    根據銀行名稱自動匹配並回傳對應的專屬解析器實例
    :param bank_name: 銀行名稱 (如 '富邦銀行', '台新銀行')
    :return: 實作 BaseBankParser 的專屬解析器
    """
    if not bank_name:
        return GenericParser(bank_name="未知銀行")
        
    for keyword, parser_cls in PARSER_REGISTRY.items():
        if keyword in bank_name:
            return parser_cls()
            
    return GenericParser(bank_name=bank_name)

__all__ = [
    "BaseBankParser",
    "get_parser",
    "TaishinParser",
    "FubonParser",
    "CTBCParser",
    "EsunParser",
    "SinopacParser",
    "FirstBankParser",
    "DBSParser",
    "SCBParser",
    "GenericParser",
]
