"""
================================================================================
信用卡電子帳單同步工具 (Credit Card Bill Sync Master)
--------------------------------------------------------------------------------
模組化架構說明：
1. 模組化 Parser: 各銀行獨立解析模組 (parsers/)
2. 獨立銀行分頁: 各銀行帳單明細獨立存儲
3. 消費總表聚合: 主表寫入 QUERY 陣列公式，依日期降冪排序
4. 解密與年份判定: 支援多組密碼輪詢，自動處理民國與西元日期轉換
5. 增量與全量重建: 支援 _SyncLogs 增量同步與 --rebuild 全量重新同步
================================================================================
"""

import os
import sys
import re
import io
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# 強制 Windows 終端使用 UTF-8 編碼
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import pikepdf
import pdfplumber
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from parsers import get_parser, BaseBankParser

def load_env_file(filepath=".env"):
    """從本地 .env 載入環境變數 (不覆蓋已存在的系統或 CI 變數)"""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"[WARN] 載入 .env 發生異常: {e}")

# 啟動時自動載入本地 .env
load_env_file()

# ==============================================================================
# 核心設定 (由環境變數或 .env 注入)
# ==============================================================================
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
PARENT_FOLDER_ID = os.environ.get("PARENT_FOLDER_ID", "")

# 從環境變數讀取密碼清單 (由 .env 或 GitHub Secrets 提供)
raw_pwds = os.environ.get("PDF_PASSWORDS", "[]")
try:
    PASSWORD_LIST = json.loads(raw_pwds)
    if not isinstance(PASSWORD_LIST, list):
        PASSWORD_LIST = [str(PASSWORD_LIST)]
except Exception:
    PASSWORD_LIST = [p.strip() for p in raw_pwds.split(",") if p.strip()]

SA_KEY_PATH = os.environ.get("SA_KEY_PATH", "sa-key.json")
SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY", "")

MASTER_SHEET_TITLE = "消費總表"
LOGS_SHEET_TITLE = "_SyncLogs"
TABLE_HEADERS = [
    "對帳", "消費日期", "入帳日期", "銀行名稱", "卡號末四碼", 
    "消費商家 / 項目", "原幣金額 / 幣別", "台幣金額", "帳單來源檔名"
]

BANK_TAB_COLORS = {
    "中國信託": {"red": 0.0, "green": 0.5, "blue": 0.3},
    "台新銀行": {"red": 0.8, "green": 0.1, "blue": 0.15},
    "玉山銀行": {"red": 0.0, "green": 0.6, "blue": 0.45},
    "富邦銀行": {"red": 0.0, "green": 0.45, "blue": 0.8},
    "永豐銀行": {"red": 0.85, "green": 0.65, "blue": 0.0},
    "星展銀行": {"red": 0.9, "green": 0.1, "blue": 0.1},
    "第一銀行": {"red": 0.1, "green": 0.4, "blue": 0.2},
    "渣打銀行": {"red": 0.0, "green": 0.55, "blue": 0.65},
    "消費總表": {"red": 0.12, "green": 0.16, "blue": 0.23},
}

# ==============================================================================
# 🔐 Google 服務帳號與工作表初始化
# ==============================================================================
def init_google_clients():
    """初始化 Drive 與 Sheets 客戶端"""
    scopes = [
        'https://www.googleapis.com/auth/drive',
        'https://www.googleapis.com/auth/spreadsheets'
    ]
    if SA_KEY_JSON_STR:
        sa_info = json.loads(SA_KEY_JSON_STR)
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    elif os.path.exists(SA_KEY_PATH):
        creds = Credentials.from_service_account_file(SA_KEY_PATH, scopes=scopes)
    else:
        raise FileNotFoundError(f"找不到服務帳號金鑰！請確認 sa-key.json 存在或已設定 GCP_SA_KEY。")

    drive_service = build('drive', 'v3', credentials=creds)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    
    # 初始化主表 (消費總表)
    try:
        sheet_master = spreadsheet.worksheet(MASTER_SHEET_TITLE)
    except gspread.exceptions.WorksheetNotFound:
        sheet_master = spreadsheet.sheet1
        sheet_master.update_title(MASTER_SHEET_TITLE)

    # 確保 _SyncLogs 存在
    try:
        sheet_logs = spreadsheet.worksheet(LOGS_SHEET_TITLE)
    except gspread.exceptions.WorksheetNotFound:
        sheet_logs = spreadsheet.add_worksheet(title=LOGS_SHEET_TITLE, rows=1000, cols=6)
        sheet_logs.append_row(["File_ID", "檔案名稱", "銀行名稱", "處理年份", "處理時間", "匯入筆數"])

    return drive_service, spreadsheet, sheet_master, sheet_logs

def get_or_create_bank_sheet(spreadsheet, bank_name: str, rebuild: bool = False):
    """取得或建立指定銀行的專屬工作表"""
    try:
        sheet = spreadsheet.worksheet(bank_name)
        if rebuild:
            sheet.clear()
            sheet.append_row(TABLE_HEADERS)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=bank_name, rows=1000, cols=10)
        sheet.append_row(TABLE_HEADERS)
    return sheet

def format_bank_sheet(spreadsheet, sheet, bank_name: str):
    """為銀行分頁套用專業金融排版、凍結首列、Checkbox、警示條件式格式化與自動欄寬"""
    sheet_id = sheet.id
    requests = []

    # 1. 凍結第 1 列與分頁標籤顏色
    tab_color = BANK_TAB_COLORS.get(bank_name, {"red": 0.3, "green": 0.35, "blue": 0.4})
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {
                    "frozenRowCount": 1
                },
                "tabColor": tab_color
            },
            "fields": "gridProperties.frozenRowCount,tabColor"
        }
    })

    # 2. 標題列 (Row 0): 深石墨灰底 + 純白粗體 + 水平垂直置中
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": len(TABLE_HEADERS)
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.118, "green": 0.161, "blue": 0.231},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "textFormat": {
                        "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        "bold": True,
                        "fontSize": 10
                    }
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"
        }
    })

    # 3. 資料列預設垂直置中 (Row 1 ~ 1000)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 0,
                "endColumnIndex": len(TABLE_HEADERS)
            },
            "cell": {
                "userEnteredFormat": {
                    "verticalAlignment": "MIDDLE",
                    "textFormat": {
                        "fontSize": 10
                    }
                }
            },
            "fields": "userEnteredFormat(verticalAlignment,textFormat)"
        }
    })

    # 4. 金融工學欄位格式 (Cols 0 to 8)
    # Col A: 對帳 Checkbox (置中)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 0,
                "endColumnIndex": 1
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER"
                }
            },
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })
    # Col A: Checkbox DataValidation
    requests.append({
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 0,
                "endColumnIndex": 1
            },
            "rule": {
                "condition": {
                    "type": "BOOLEAN"
                },
                "showCustomUi": True
            }
        }
    })

    # Col B, C: 日期 (置中 + yyyy/mm/dd)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 1,
                "endColumnIndex": 3
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER",
                    "numberFormat": {
                        "type": "DATE",
                        "pattern": "yyyy/mm/dd"
                    }
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"
        }
    })

    # Col D, E: 銀行、卡號 (置中)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 3,
                "endColumnIndex": 5
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER",
                    "numberFormat": {
                        "type": "TEXT"
                    }
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"
        }
    })

    # Col F: 消費商家 / 項目 (靠左)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 5,
                "endColumnIndex": 6
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "LEFT"
                }
            },
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })

    # Col G: 原幣金額 / 幣別 (置中)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 6,
                "endColumnIndex": 7
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER"
                }
            },
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })

    # Col H: 台幣金額 (靠右 + #,##0)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 7,
                "endColumnIndex": 8
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "RIGHT",
                    "numberFormat": {
                        "type": "NUMBER",
                        "pattern": "#,##0"
                    }
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"
        }
    })

    # Col I: 檔名 (靠左 + 灰色次要資訊)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": 8,
                "endColumnIndex": 9
            },
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "LEFT",
                    "textFormat": {
                        "foregroundColor": {"red": 0.4, "green": 0.45, "blue": 0.5},
                        "fontSize": 9
                    }
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,textFormat)"
        }
    })

    # 5. 條件式格式化
    # 警示 1: 年費/違約金/滯納金/循環利息/利息 -> 紅底紅字警示
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": 1000,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(TABLE_HEADERS)
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": '=REGEXMATCH($F2, "年費|違約金|滯納金|循環利息|利息|掛失費|調單費")'}]
                    },
                    "format": {
                        "backgroundColor": {"red": 0.996, "green": 0.886, "blue": 0.886},
                        "textFormat": {
                            "foregroundColor": {"red": 0.725, "green": 0.110, "blue": 0.110},
                            "bold": True
                        }
                    }
                }
            },
            "index": 0
        }
    })

    # 警示 2: 已勾選對帳 ($A2=TRUE) -> 灰字 + 刪除線
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": 1000,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(TABLE_HEADERS)
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": '=$A2=TRUE'}]
                    },
                    "format": {
                        "textFormat": {
                            "foregroundColor": {"red": 0.580, "green": 0.639, "blue": 0.722},
                            "strikethrough": True
                        }
                    }
                }
            },
            "index": 1
        }
    })

    # 6. 自動欄寬
    requests.append({
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": len(TABLE_HEADERS)
            }
        }
    })

    try:
        spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"    ⚠️ 分頁格式化套用異常 ({bank_name}): {e}")

def format_and_update_master_sheet(spreadsheet, sheet_master, bank_sheet_names):
    """
    更新消費總表：
    1. 頂部 Bento 數據看板 (Rows 1~4: 本期應繳總額 / 待客訴費用 / 對帳進度 / 納管銀行)
    2. 凍結第 6 列標題
    3. 寫入動態 QUERY 陣列公式 (Row 7)
    4. 自動套用核取方塊、金融工學對齊、條件式格式化與自動欄寬
    """
    print(f"\n📊 正在更新【{MASTER_SHEET_TITLE}】的頂部數據看板與動態聚合公式...")
    sheet_id = sheet_master.id
    
    # 清空主表所有內容
    sheet_master.clear()
    
    # 寫入頂部 Bento 看板標籤與公式
    kpi_labels = [
        "💳 本期應繳總金額 (NT$)", "", 
        "🚨 待客訴費用 (年費/滯納金/利息)", "", 
        "⏳ 本期對帳進度", "", 
        "📊 納管銀行與總筆數", "", ""
    ]
    kpi_formulas = [
        '=IFERROR(SUM(H7:H), 0)', '',
        '=IFERROR(SUMIF(F7:F, "*年費*", H7:H) + SUMIF(F7:F, "*違約金*", H7:H) + SUMIF(F7:F, "*滯納金*", H7:H) + SUMIF(F7:F, "*利息*", H7:H), 0)', '',
        '=IFERROR(TEXT(COUNTIF(A7:A, TRUE), "0") & " / " & TEXT(COUNTA(B7:B), "0") & " 筆 (" & TEXT(IFERROR(COUNTIF(A7:A, TRUE)/COUNTA(B7:B), 0), "0.0%") & ")", "0 / 0 筆 (0%)")', '',
        '=COUNTA(UNIQUE(FILTER(D7:D, D7:D<>""))) & " 家銀行 ｜ " & COUNTA(B7:B) & " 筆交易"', '', ''
    ]
    blank_row = [""] * len(TABLE_HEADERS)
    
    # 組合公式：=QUERY({ IFERROR('銀行A'!A2:I, {"","","","","","","","",""}); ... }, "select * where Col2 is not null order by Col2 desc", 0)
    empty_row = '{"","","","","","","","",""}'
    ranges = [f"IFERROR('{bname}'!A2:I, {empty_row})" for bname in bank_sheet_names]
    master_query_formula = f'=QUERY({{ {"; ".join(ranges)} }}, "select * where Col2 is not null order by Col2 desc", 0)' if bank_sheet_names else ""
    
    grid_values = [
        kpi_labels,
        kpi_formulas,
        blank_row, # Row 3 (part of KPI card)
        blank_row, # Row 4 (part of KPI card)
        blank_row, # Row 5 (gap)
        TABLE_HEADERS, # Row 6 (Header)
        [master_query_formula] + [""] * (len(TABLE_HEADERS) - 1) # Row 7
    ]
    
    sheet_master.update(range_name='A1:I7', values=grid_values, raw=False)
    
    requests = []
    
    # 1. 凍結前 6 列與設定總表 Tab 色彩
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {
                    "frozenRowCount": 6
                },
                "tabColor": BANK_TAB_COLORS.get("消費總表", {"red": 0.12, "green": 0.16, "blue": 0.23})
            },
            "fields": "gridProperties.frozenRowCount,tabColor"
        }
    })
    
    # 2. 合併儲存格建立 Bento Cards (A1:B4, C1:D4, E1:F4, G1:I4)
    merge_ranges = [
        {"startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 2}, # A1:B1
        {"startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 0, "endColumnIndex": 2}, # A2:B4
        {"startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 4}, # C1:D1
        {"startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 2, "endColumnIndex": 4}, # C2:D4
        {"startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 4, "endColumnIndex": 6}, # E1:F1
        {"startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 4, "endColumnIndex": 6}, # E2:F4
        {"startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 6, "endColumnIndex": 9}, # G1:I1
        {"startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 6, "endColumnIndex": 9}, # G2:I4
    ]
    for mr in merge_ranges:
        requests.append({
            "mergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    **mr
                },
                "mergeType": "MERGE_ALL"
            }
        })
        
    # 3. Bento 看板卡片樣式
    # Card 1: 應繳總額 (藍灰調)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 2},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.945, "green": 0.961, "blue": 0.976},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": {"red": 0.28, "green": 0.35, "blue": 0.45}, "bold": True, "fontSize": 9}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"
        }
    })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 0, "endColumnIndex": 2},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.945, "green": 0.961, "blue": 0.976},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": {"red": 0.06, "green": 0.09, "blue": 0.16}, "bold": True, "fontSize": 15},
                    "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,numberFormat)"
        }
    })
    
    # Card 2: 爭議款 (淺紅調)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 2, "endColumnIndex": 4},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.996, "green": 0.949, "blue": 0.949},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": {"red": 0.75, "green": 0.15, "blue": 0.15}, "bold": True, "fontSize": 9}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"
        }
    })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 2, "endColumnIndex": 4},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.996, "green": 0.949, "blue": 0.949},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": {"red": 0.86, "green": 0.15, "blue": 0.15}, "bold": True, "fontSize": 15},
                    "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat,numberFormat)"
        }
    })
    
    # Card 3: 對帳進度 (翡翠綠調)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 4, "endColumnIndex": 6},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.925, "green": 0.992, "blue": 0.961},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": {"red": 0.02, "green": 0.45, "blue": 0.35}, "bold": True, "fontSize": 9}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"
        }
    })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 4, "endColumnIndex": 6},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.925, "green": 0.992, "blue": 0.961},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": {"red": 0.02, "green": 0.59, "blue": 0.41}, "bold": True, "fontSize": 13}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"
        }
    })
    
    # Card 4: 總筆數 (石灰灰調)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 6, "endColumnIndex": 9},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.973, "green": 0.980, "blue": 0.988},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": {"red": 0.3, "green": 0.35, "blue": 0.45}, "bold": True, "fontSize": 9}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"
        }
    })
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 4, "startColumnIndex": 6, "endColumnIndex": 9},
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.973, "green": 0.980, "blue": 0.988},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
                    "textFormat": {"foregroundColor": {"red": 0.2, "green": 0.25, "blue": 0.33}, "bold": True, "fontSize": 13}
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"
        }
    })
    
    # 4. 表頭列 (Row 5 / 6th row): 深石墨灰底 + 純白粗體 + 水平垂直置中
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 5,
                "endRowIndex": 6,
                "startColumnIndex": 0,
                "endColumnIndex": len(TABLE_HEADERS)
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.118, "green": 0.161, "blue": 0.231},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "textFormat": {
                        "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        "bold": True,
                        "fontSize": 10
                    }
                }
            },
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"
        }
    })
    
    # 5. 資料列預設垂直置中 (Row 6 ~ 1000)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 6,
                "endRowIndex": 1000,
                "startColumnIndex": 0,
                "endColumnIndex": len(TABLE_HEADERS)
            },
            "cell": {
                "userEnteredFormat": {
                    "verticalAlignment": "MIDDLE",
                    "textFormat": {
                        "fontSize": 10
                    }
                }
            },
            "fields": "userEnteredFormat(verticalAlignment,textFormat)"
        }
    })
    
    # 6. 金融工學欄位格式 (Row 6 ~ 1000)
    # Col A: Checkbox (置中 + DataValidation BOOLEAN)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 1000, "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })
    requests.append({
        "setDataValidation": {
            "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 1000, "startColumnIndex": 0, "endColumnIndex": 1},
            "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True}
        }
    })
    
    # Col B, C: 日期 (置中 + yyyy/mm/dd)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 1000, "startColumnIndex": 1, "endColumnIndex": 3},
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER",
                    "numberFormat": {"type": "DATE", "pattern": "yyyy/mm/dd"}
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"
        }
    })
    
    # Col D, E: 銀行、卡號 (置中)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 1000, "startColumnIndex": 3, "endColumnIndex": 5},
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "CENTER",
                    "numberFormat": {"type": "TEXT"}
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"
        }
    })
    
    # Col F: 消費商家 / 項目 (靠左)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 1000, "startColumnIndex": 5, "endColumnIndex": 6},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}},
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })
    
    # Col G: 原幣金額 / 幣別 (置中)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 1000, "startColumnIndex": 6, "endColumnIndex": 7},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment"
        }
    })
    
    # Col H: 台幣金額 (靠右 + #,##0)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 1000, "startColumnIndex": 7, "endColumnIndex": 8},
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "RIGHT",
                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0"}
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"
        }
    })
    
    # Col I: 檔名 (靠左 + 灰色次要資訊)
    requests.append({
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 6, "endRowIndex": 1000, "startColumnIndex": 8, "endColumnIndex": 9},
            "cell": {
                "userEnteredFormat": {
                    "horizontalAlignment": "LEFT",
                    "textFormat": {"foregroundColor": {"red": 0.4, "green": 0.45, "blue": 0.5}, "fontSize": 9}
                }
            },
            "fields": "userEnteredFormat(horizontalAlignment,textFormat)"
        }
    })
    
    # 7. 條件式格式化 (Conditional Formatting)
    # 警示 1: 年費/違約金/滯納金/循環利息/利息 -> 紅底紅字警示
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 6,
                    "endRowIndex": 1000,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(TABLE_HEADERS)
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": '=REGEXMATCH($F7, "年費|違約金|滯納金|循環利息|利息|掛失費|調單費")'}]
                    },
                    "format": {
                        "backgroundColor": {"red": 0.996, "green": 0.886, "blue": 0.886},
                        "textFormat": {
                            "foregroundColor": {"red": 0.725, "green": 0.110, "blue": 0.110},
                            "bold": True
                        }
                    }
                }
            },
            "index": 0
        }
    })
    
    # 警示 2: 已勾選對帳 ($A7=TRUE) -> 灰字 + 刪除線
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 6,
                    "endRowIndex": 1000,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(TABLE_HEADERS)
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": '=$A7=TRUE'}]
                    },
                    "format": {
                        "textFormat": {
                            "foregroundColor": {"red": 0.580, "green": 0.639, "blue": 0.722},
                            "strikethrough": True
                        }
                    }
                }
            },
            "index": 1
        }
    })
    
    # 8. 自動欄寬
    requests.append({
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": len(TABLE_HEADERS)
            }
        }
    })
    
    try:
        spreadsheet.batch_update({"requests": requests})
        print(f"  ✅ 總表看板與排版更新完成！")
    except Exception as e:
        print(f"  ⚠️ 總表格式化套用異常: {e}")

# ==============================================================================
# 輔助函式
# ==============================================================================
def get_year_from_content(text):
    if not text: return None
    match_cn = re.search(r"(\d{3,4})\s*年\s*(\d{1,2})\s*月", text)
    if match_cn:
        y = int(match_cn.group(1))
        return str(y + 1911) if y < 1900 else str(y)
    match_gen = re.search(r"(?:帳單)?結帳日\s*[:：]?\s*(\d{3,4})[\/.-](\d{1,2})[\/.-](\d{1,2})", text)
    if match_gen:
        y = int(match_gen.group(1))
        return str(y + 1911) if y < 1900 else str(y)
    return None

def get_year_from_filename(filename):
    match_west = re.search(r"(20[1-3]\d)", filename)
    if match_west: return match_west.group(1)
    match_roc = re.search(r"(11[0-9])\d{2}", filename)
    if match_roc: return str(int(match_roc.group(1)) + 1911)
    return None

# ==============================================================================
# 主執行流程
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="信用卡電子帳單同步工具")
    parser.add_argument("-r", "--rebuild", action="store_true", help="清空各銀行分頁與 Log，全量重新掃描入庫")
    args = parser.parse_args()

    print(f"\n========================================================")
    print(f"信用卡電子帳單同步程序啟動")
    if args.rebuild:
        print(f"執行模式: 全量重建 (--rebuild)")
    else:
        print(f"執行模式: 增量同步")
    print(f"========================================================")
    
    drive_service, spreadsheet, sheet_master, sheet_logs = init_google_clients()
    
    if args.rebuild:
        print(f"正在初始化 Log 記錄表...")
        sheet_logs.clear()
        sheet_logs.append_row(["File_ID", "檔案名稱", "銀行名稱", "處理年份", "處理時間", "匯入筆數"])
        processed_file_ids = set()
    else:
        processed_file_ids = set(sheet_logs.col_values(1)[1:])
        print(f"已記錄處理過的歷史檔案數: {len(processed_file_ids)} 個")

    # 取得 Parent Folder 底下的所有銀行子資料夾
    query = f"'{PARENT_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    bank_folders = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    total_new_tx = 0
    total_new_files = 0
    
    # 記錄各銀行收集到的交易行: dict[bank_name] -> List[rows]
    bank_tx_map = defaultdict(list)
    active_bank_names = set()

    for b_folder in bank_folders:
        bank_name = b_folder['name']
        bank_folder_id = b_folder['id']
        
        if bank_name.startswith("_"): continue
        
        print(f"\n正在檢查銀行資料夾: {bank_name}")

        file_query = f"'{bank_folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
        files = drive_service.files().list(q=file_query, fields="files(id, name, mimeType)").execute().get('files', [])

        for file in files:
            file_id = file['id']
            file_name = file['name']

            if file_id in processed_file_ids or file_name.startswith("!"):
                continue

            # 確保為 PDF 檔案 (支援副檔名大小寫與特殊 MIME Type)
            if not file_name.lower().endswith(".pdf"):
                continue

            # 過濾純存摺/綜合對帳單 (如彰銀數位存款、永豐綜合對帳單、星展綜合月結單、渣打存款結單)
            if "綜合對帳單" in file_name or "數位存款" in file_name or "CBGCS-MTHLYSTMT" in file_name:
                print(f"  [略過] 非信用卡帳單: {file_name}")
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet_logs.append_row([file_id, file_name, bank_name, "N/A", now_str, 0])
                processed_file_ids.add(file_id)
                continue

            if bank_name == "渣打銀行" and "statement" in file_name.lower():
                print(f"  [略過] 非信用卡理財結單: {file_name}")
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet_logs.append_row([file_id, file_name, bank_name, "N/A", now_str, 0])
                processed_file_ids.add(file_id)
                continue

            print(f"  正在處理帳單: {file_name}")

            # 下載 PDF
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            fh.seek(0)

            # --- 第一階段：解密 ---
            correct_password = None
            is_encrypted = True
            try:
                with pikepdf.open(fh) as pdf:
                    correct_password = ""
                    is_encrypted = False
            except pikepdf.PasswordError:
                for pwd in PASSWORD_LIST:
                    fh.seek(0)
                    try:
                        with pikepdf.open(fh, password=pwd) as pdf:
                            correct_password = pwd
                            break
                    except Exception:
                        continue

            if correct_password is None:
                print(f"  [錯誤] 密碼不符或無法解密: {file_name}")
                continue

            # --- 第二階段與第三階段：年份判定與模組化 Parser 抽取明細 ---
            year = None
            extracted_text_all = ""
            tx_rows = []
            parser = get_parser(bank_name)
            
            try:
                fh.seek(0)
                pwd_arg = correct_password if is_encrypted else None
                with pdfplumber.open(fh, password=pwd_arg) as pdf:
                    for p in pdf.pages:
                        extracted_text_all += (p.extract_text() or "") + "\n"
                    year = get_year_from_content(extracted_text_all)
                    if year is None:
                        year = get_year_from_filename(file_name) or datetime.now().strftime("%Y")
                    
                    # 傳入文字行與 pdf 實例 (支援 OCR 或文字層深度解析)
                    tx_rows = parser.parse(extracted_text_all.split("\n"), year, file_name, pdf=pdf)
            except Exception as e:
                print(f"  [WARN] 帳單抽取解析異常: {e}")
                if year is None:
                    year = get_year_from_filename(file_name) or datetime.now().strftime("%Y")
                tx_rows = parser.parse(extracted_text_all.split("\n"), year, file_name)
            
            if tx_rows:
                bank_tx_map[bank_name].extend(tx_rows)
                total_new_tx += len(tx_rows)
                active_bank_names.add(bank_name)
                print(f"    成功解析 {len(tx_rows)} 筆明細 ({parser.__class__.__name__})")

            # 記錄到 _SyncLogs
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet_logs.append_row([file_id, file_name, bank_name, year, now_str, len(tx_rows)])
            processed_file_ids.add(file_id)
            total_new_files += 1

    # --- 第四階段：分流寫入各銀行專屬頁籤 ---
    if bank_tx_map:
        print(f"\n正在將明細寫入各銀行分頁...")
        for b_name, rows in bank_tx_map.items():
            b_sheet = get_or_create_bank_sheet(spreadsheet, b_name, rebuild=args.rebuild)
            chunk_size = 500
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i:i + chunk_size]
                b_sheet.append_rows(chunk)
            print(f"  {b_name}: 已寫入 {len(rows)} 筆明細")

    # --- 第五階段：更新消費總表動態 QUERY 公式與看板排版 ---
    all_worksheets = spreadsheet.worksheets()
    all_bank_tabs = [
        ws.title for ws in all_worksheets 
        if ws.title not in [MASTER_SHEET_TITLE, LOGS_SHEET_TITLE] and not ws.title.startswith("_")
    ]
    
    print(f"\n正在套用各銀行分頁排版與格式化...")
    for ws in all_worksheets:
        if ws.title in all_bank_tabs:
            format_bank_sheet(spreadsheet, ws, ws.title)

    format_and_update_master_sheet(spreadsheet, sheet_master, all_bank_tabs)

    print(f"\n========================================================")
    print(f"帳單同步處理完成")
    print(f"- 處理帳單總數: {total_new_files} 份")
    print(f"- 匯入明細筆數: {total_new_tx} 筆")
    print(f"- 銀行分頁數量: {len(all_bank_tabs)} 個 ({', '.join(all_bank_tabs)})")
    print(f"- 試算表網址: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit")
    print(f"========================================================\n")

if __name__ == "__main__":
    main()
