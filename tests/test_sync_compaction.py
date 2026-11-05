"""
================================================================================
分頁空行自癒與動態格式化單元測試 (Unit Tests for Compaction & Dynamic Formatting)
================================================================================
"""

import unittest
from unittest.mock import MagicMock, call
import sys
import os

# 將專案根目錄加入 sys.path
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from bill_sync_master import (
    TABLE_HEADERS,
    compact_bank_sheet_if_needed,
    append_bank_transactions,
    format_bank_sheet,
)

class TestSyncCompaction(unittest.TestCase):
    """測試銀行分頁空行自癒、壓縮重組與動態列數格式化"""

    def setUp(self):
        self.mock_spreadsheet = MagicMock()
        self.mock_sheet = MagicMock()
        self.mock_sheet.id = 12345
        self.mock_sheet.title = "中國信託"

    def test_compact_bank_sheet_with_gap(self):
        """測試分頁中存在 900+ 空白行斷層時，能自動自癒壓縮並保留對帳狀態"""
        # 模擬試算表狀態：表頭 + 2 筆歷史資料 (其中一筆已勾選對帳) + 5 個空行 + 2 筆第二個月資料
        header = TABLE_HEADERS
        row1 = ["TRUE", "2026/08/15", "2026/08/16", "中國信託", "1234", "超市消費", "NT$ 500", "500", "ctbc_08.pdf"]
        row2 = ["FALSE", "2026/08/20", "2026/08/21", "中國信託", "1234", "餐廳消費", "NT$ 1,200", "1200", "ctbc_08.pdf"]
        empty_rows = [["", "", "", "", "", "", "", "", ""] for _ in range(5)]
        row3 = ["FALSE", "2026/09/10", "2026/09/11", "中國信託", "1234", "加油站", "NT$ 800", "800", "ctbc_09.pdf"]
        row4 = ["FALSE", "2026/09/15", "2026/09/16", "中國信託", "1234", "書店", "NT$ 300", "300", "ctbc_09.pdf"]

        all_mock_values = [header, row1, row2] + empty_rows + [row3, row4]
        self.mock_sheet.get_all_values.return_value = all_mock_values

        total_rows = compact_bank_sheet_if_needed(self.mock_spreadsheet, self.mock_sheet)

        # 驗證總行數為 1 (表頭) + 4 (筆有效資料) = 5
        self.assertEqual(total_rows, 5)
        # 驗證有呼叫 clear 與 update
        self.mock_sheet.clear.assert_called_once()
        self.mock_sheet.update.assert_called_once()

        # 驗證寫入的資料是連續的，且完全沒有空行
        args, kwargs = self.mock_sheet.update.call_args
        written_values = kwargs.get("values", args[1] if len(args) > 1 else None)
        self.assertEqual(len(written_values), 5)
        self.assertEqual(written_values[0], TABLE_HEADERS)
        self.assertEqual(written_values[1][0], "TRUE")  # 已對帳狀態保留
        self.assertEqual(written_values[1][1], "2026/08/15")
        self.assertEqual(written_values[2][1], "2026/08/20")
        self.assertEqual(written_values[3][1], "2026/09/10")
        self.assertEqual(written_values[4][1], "2026/09/15")

    def test_append_bank_transactions_contiguous(self):
        """測試新增交易時能緊接在有效歷史資料後，絕不產生斷層"""
        # 現有 1 筆歷史資料
        header = TABLE_HEADERS
        row1 = ["FALSE", "2026/08/15", "2026/08/16", "中國信託", "1234", "超市消費", "NT$ 500", "500", "ctbc_08.pdf"]
        self.mock_sheet.get_all_values.return_value = [header, row1]

        # 新增 2 筆交易
        new_tx = [
            [False, "2026/09/01", "2026/09/02", "中國信託", "1234", "線上購物", "NT$ 1,000", "1000", "ctbc_09.pdf"],
            [False, "2026/09/05", "2026/09/06", "中國信託", "1234", "影城門票", "NT$ 600", "600", "ctbc_09.pdf"],
        ]

        total_rows = append_bank_transactions(self.mock_sheet, new_tx)

        self.assertEqual(total_rows, 4)  # 1 header + 1 old + 2 new = 4
        self.mock_sheet.clear.assert_called_once()
        self.mock_sheet.update.assert_called_once()

        args, kwargs = self.mock_sheet.update.call_args
        written_values = kwargs.get("values", args[1] if len(args) > 1 else None)
        self.assertEqual(len(written_values), 4)
        self.assertEqual(written_values[3][5], "影城門票")

    def test_format_bank_sheet_uses_dynamic_end_row(self):
        """測試格式化 API 下發之 endRowIndex 嚴格依據有效列數，絕不寫死 1000"""
        self.mock_sheet.col_values.return_value = ["消費日期", "2026/08/01", "2026/08/02"]  # 3 列 (含表頭)

        format_bank_sheet(self.mock_spreadsheet, self.mock_sheet, "中國信託", total_rows=3)

        self.mock_spreadsheet.batch_update.assert_called_once()
        batch_args, _ = self.mock_spreadsheet.batch_update.call_args
        requests = batch_args[0]["requests"]

        # 檢驗資料格式化 (repeatCell 與 setDataValidation) 的 endRowIndex 皆為 3，而非寫死 1000
        for req in requests:
            if "repeatCell" in req and req["repeatCell"]["range"].get("startRowIndex") == 1:
                self.assertEqual(req["repeatCell"]["range"]["endRowIndex"], 3)
            elif "setDataValidation" in req:
                self.assertEqual(req["setDataValidation"]["range"]["endRowIndex"], 3)
            elif "addConditionalFormatRule" in req:
                ranges = req["addConditionalFormatRule"]["rule"]["ranges"]
                for r in ranges:
                    self.assertEqual(r["endRowIndex"], 3)

if __name__ == "__main__":
    unittest.main()

