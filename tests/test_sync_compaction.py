"""
================================================================================
分頁空行自癒、倒序排序與動態格式化單元測試 (Unit Tests for Compaction, Sorting & Formatting)
================================================================================
"""

import unittest
from unittest.mock import MagicMock
import sys
import os

# 將專案根目錄加入 sys.path
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from bill_sync_master import (
    TABLE_HEADERS,
    sort_tx_rows,
    compact_bank_sheet_if_needed,
    append_bank_transactions,
    format_bank_sheet,
)

class TestSyncCompaction(unittest.TestCase):
    """測試銀行分頁空行自癒、消費日期倒序排序與動態列數格式化"""

    def setUp(self):
        self.mock_spreadsheet = MagicMock()
        self.mock_sheet = MagicMock()
        self.mock_sheet.id = 12345
        self.mock_sheet.title = "永豐銀行"

    def test_sort_tx_rows_descending_user_scenario(self):
        """測試真實用戶案例：8月與7月交錯混亂的帳目能精確依消費日期倒序排列"""
        disordered_rows = [
            ["FALSE", "2026/08/11", "2026/08/13", "永豐銀行", "8701", "保險費A", "NT$ 100", "100", "f1.pdf"],
            ["FALSE", "2026/08/18", "2026/08/21", "永豐銀行", "8701", "加油站A", "NT$ 200", "200", "f1.pdf"],
            ["TRUE",  "2026/08/22", "2026/08/25", "永豐銀行", "8701", "街口支付", "NT$ 300", "300", "f1.pdf"],
            ["FALSE", "2026/07/02", "2026/07/07", "永豐銀行", "8701", "加油站B", "NT$ 400", "400", "f2.pdf"],
            ["FALSE", "2026/07/14", "2026/07/17", "永豐銀行", "8701", "加油站C", "NT$ 500", "500", "f2.pdf"],
        ]

        sorted_result = sort_tx_rows(disordered_rows)

        # 檢驗排序：2026/08/22 > 2026/08/18 > 2026/08/11 > 2026/07/14 > 2026/07/02
        expected_dates = ["2026/08/22", "2026/08/18", "2026/08/11", "2026/07/14", "2026/07/02"]
        actual_dates = [r[1] for r in sorted_result]
        self.assertEqual(actual_dates, expected_dates)

        # 檢驗 08/22 的對帳狀態 TRUE 依然牢固綁定
        self.assertEqual(sorted_result[0][0], "TRUE")
        self.assertEqual(sorted_result[0][5], "街口支付")

    def test_compact_bank_sheet_with_gap_and_reorder(self):
        """測試分頁中存在空白行斷層且順序混亂時，能自動自癒壓縮並依日期倒序排列"""
        header = TABLE_HEADERS
        row1 = ["TRUE",  "2026/08/15", "2026/08/16", "永豐銀行", "8701", "超市消費", "NT$ 500", "500", "f1.pdf"]
        row2 = ["FALSE", "2026/08/20", "2026/08/21", "永豐銀行", "8701", "餐廳消費", "NT$ 1,200", "1200", "f1.pdf"]
        empty_rows = [["", "", "", "", "", "", "", "", ""] for _ in range(5)]
        row3 = ["FALSE", "2026/09/10", "2026/09/11", "永豐銀行", "8701", "加油站", "NT$ 800", "800", "f2.pdf"]
        row4 = ["FALSE", "2026/09/15", "2026/09/16", "永豐銀行", "8701", "書店", "NT$ 300", "300", "f2.pdf"]

        all_mock_values = [header, row1, row2] + empty_rows + [row3, row4]
        self.mock_sheet.get_all_values.return_value = all_mock_values

        total_rows = compact_bank_sheet_if_needed(self.mock_spreadsheet, self.mock_sheet)

        # 驗證總行數為 1 (表頭) + 4 (有效資料) = 5
        self.assertEqual(total_rows, 5)
        self.mock_sheet.clear.assert_called_once()
        self.mock_sheet.update.assert_called_once()

        # 驗證寫入的資料為倒序排列 (9/15 -> 9/10 -> 8/20 -> 8/15)
        args, kwargs = self.mock_sheet.update.call_args
        written_values = kwargs.get("values", args[1] if len(args) > 1 else None)
        self.assertEqual(len(written_values), 5)
        self.assertEqual(written_values[0], TABLE_HEADERS)
        self.assertEqual(written_values[1][1], "2026/09/15")
        self.assertEqual(written_values[2][1], "2026/09/10")
        self.assertEqual(written_values[3][1], "2026/08/20")
        self.assertEqual(written_values[4][1], "2026/08/15")
        self.assertEqual(written_values[4][0], "TRUE")  # 8/15 的對帳狀態維持 TRUE

    def test_append_bank_transactions_descending(self):
        """測試新增交易時能與歷史資料融合並依日期倒序排列，最新在最上方"""
        header = TABLE_HEADERS
        row1 = ["FALSE", "2026/08/15", "2026/08/16", "永豐銀行", "8701", "舊交易", "NT$ 500", "500", "f1.pdf"]
        self.mock_sheet.get_all_values.return_value = [header, row1]

        # 新增 2 筆交易 (一筆較新，一筆較舊)
        new_tx = [
            [False, "2026/09/01", "2026/09/02", "永豐銀行", "8701", "9月交易", "NT$ 1,000", "1000", "f2.pdf"],
            [False, "2026/07/20", "2026/07/21", "永豐銀行", "8701", "7月補登", "NT$ 200", "200", "f0.pdf"],
        ]

        total_rows = append_bank_transactions(self.mock_sheet, new_tx)

        self.assertEqual(total_rows, 4)  # 1 header + 3 tx = 4
        self.mock_sheet.clear.assert_called_once()
        self.mock_sheet.update.assert_called_once()

        args, kwargs = self.mock_sheet.update.call_args
        written_values = kwargs.get("values", args[1] if len(args) > 1 else None)
        self.assertEqual(len(written_values), 4)
        # 排序驗證：09/01 > 08/15 > 07/20
        self.assertEqual(written_values[1][1], "2026/09/01")
        self.assertEqual(written_values[2][1], "2026/08/15")
        self.assertEqual(written_values[3][1], "2026/07/20")

    def test_format_bank_sheet_uses_dynamic_end_row(self):
        """測試格式化 API 下發之 endRowIndex 嚴格依據有效列數，絕不寫死 1000"""
        self.mock_sheet.col_values.return_value = ["消費日期", "2026/08/02", "2026/08/01"]

        format_bank_sheet(self.mock_spreadsheet, self.mock_sheet, "永豐銀行", total_rows=3)

        self.mock_spreadsheet.batch_update.assert_called_once()
        batch_args, _ = self.mock_spreadsheet.batch_update.call_args
        requests = batch_args[0]["requests"]

        # 檢驗資料格式化的 endRowIndex 皆為 3，而非寫死 1000
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
