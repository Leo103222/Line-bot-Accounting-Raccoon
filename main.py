import os
import logging
import re
import json
import gspread
import google.generativeai as genai
import random
from flask import Flask, request, abort
from linebot import WebhookHandler, LineBotApi
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from string import Template

# === 時區設定（可用環境變數 APP_TZ 覆蓋，預設 Asia/Taipei） ===
APP_TZ = os.getenv('APP_TZ', 'Asia/Taipei')
TIMEZONE = ZoneInfo(APP_TZ)

# === (NEW) 步驟一：定義預設類別 (全域) ===
DEFAULT_CATEGORIES = ['餐飲', '飲料', '交通', '娛樂', '購物', '日用品', '雜項', '收入']

# === 配置日誌 ===
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === 刪除預覽狀態暫存 (用於「確認刪除」功能) ===
delete_preview_cache = {}

# === 載入環境變數 ===
load_dotenv()

# === 從環境變數讀取金鑰 ===
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", '記帳小浣熊資料庫')
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# === 驗證金鑰是否已載入 ===
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY, GOOGLE_SHEET_ID]):
    logger.error("!!! 關鍵金鑰載入失敗 !!!")
    raise ValueError("金鑰未配置，請檢查 .env 檔案")
else:
    logger.debug("所有金鑰已成功從 .env 載入。")

# === 初始化 Flask 應用程式 ===
app = Flask(__name__)
logger.info("Flask application initialized successfully.")

# === 配置 LINE 與 Gemini API 客戶端 ===
try:
    if not LINE_CHANNEL_ACCESS_TOKEN or not re.match(r'^[A-Za-z0-9+/=]+$', LINE_CHANNEL_ACCESS_TOKEN):
        logger.error("LINE_CHANNEL_ACCESS_TOKEN 格式無效")
        raise ValueError("LINE_CHANNEL_ACCESS_TOKEN 格式無效")
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-2.5-flash-lite')
    
    logger.debug("LINE 和 Gemini API 客戶端初始化成功")
except Exception as e:
    logger.error(f"API 客戶端初始化失敗: {e}", exc_info=True)
    raise

# === Google Sheets 初始化 ===
def get_sheets_workbook():
    """
    初始化 Google Sheets 客戶端並返回工作簿 (Workbook) 物件
    """
    logger.debug("正在初始化 Google Sheets 憑證...")
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json:
            logger.error("GOOGLE_CREDENTIALS 未設置或為空")
            raise ValueError("GOOGLE_CREDENTIALS 未設置或為空")
        
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)
        
        logger.debug(f"成功授權，嘗試開啟試算表 ID：{GOOGLE_SHEET_ID}")
        workbook = client.open_by_key(GOOGLE_SHEET_ID)
        return workbook
        
    except Exception as e:
        logger.error(f"Google Sheets 初始化失敗：{e}", exc_info=True)
        raise

def ensure_worksheets(workbook):
    """
    (MODIFIED) 確保 Google Sheet 中存在 Transactions, Budgets, Categories 工作表
    """
    logger.debug("檢查並確保 Transactions, Budgets, Categories 工作表存在...")
    try:
        # --- Transactions Sheet ---
        try:
            trx_sheet = workbook.worksheet('Transactions')
            logger.debug("找到 Transactions 工作表")
            header = trx_sheet.row_values(1)
            if not header:
                 logger.debug("Transactions 工作表為空，正在寫入標頭...")
                 trx_sheet.append_row(['日期', '類別', '金額', '使用者ID', '使用者名稱', '備註'])
                 
        except gspread.exceptions.WorksheetNotFound:
            logger.debug("未找到 Transactions 工作表，正在創建...")
            trx_sheet = workbook.add_worksheet(title='Transactions', rows=1000, cols=10)
            trx_sheet.append_row(['日期', '類別', '金額', '使用者ID', '使用者名稱', '備註'])

        # --- Budgets Sheet ---
        try:
            budget_sheet = workbook.worksheet('Budgets')
            logger.debug("找到 Budgets 工作表")
            header_budget = budget_sheet.row_values(1)
            if not header_budget:
                logger.debug("Budgets 工作表為空，正在寫入標頭...")
                budget_sheet.append_row(['使用者ID', '類別', '限額'])
                
        except gspread.exceptions.WorksheetNotFound:
            logger.debug("未找到 Budgets 工作表，正在創建...")
            budget_sheet = workbook.add_worksheet(title='Budgets', rows=100, cols=5)
            budget_sheet.append_row(['使用者ID', '類別', '限額'])

        # --- (NEW) Categories Sheet ---
        try:
            cat_sheet = workbook.worksheet('Categories')
            logger.debug("找到 Categories 工作表")
            header_cat = cat_sheet.row_values(1)
            if not header_cat:
                logger.debug("Categories 工作表為空，正在寫入標頭...")
                cat_sheet.append_row(['使用者ID', '類別'])
                
        except gspread.exceptions.WorksheetNotFound:
            logger.debug("未找到 Categories 工作表，正在創建...")
            cat_sheet = workbook.add_worksheet(title='Categories', rows=100, cols=5)
            cat_sheet.append_row(['使用者ID', '類別'])

        return trx_sheet, budget_sheet, cat_sheet # (MODIFIED) 回傳三個工作表
        
    except Exception as e:
        logger.error(f"創建或檢查工作表失敗：{e}", exc_info=True)
        return None, None, None

def get_user_profile_name(user_id):
    """
    透過 LINE API 獲取使用者名稱
    """
    logger.debug(f"獲取使用者 {user_id} 的個人資料...")
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except LineBotApiError as e:
        logger.error(f"無法獲取使用者 {user_id} 的個人資料：{e}", exc_info=True)
        return "未知用戶"

# === (NEW) 步驟三：新增類別管理相關函式 ===

def get_user_categories(cat_sheet, user_id):
    """
    (新) 輔助函式：獲取使用者的完整類別列表 (預設 + 自訂)
    """
    logger.debug(f"正在獲取 {user_id} 的自訂類別...")
    try:
        all_cats_records = cat_sheet.get_all_records()
        custom_cats = []
        for r in all_cats_records:
            if r.get('使用者ID') == user_id and r.get('類別'):
                custom_cats.append(r.get('類別'))
        
        # 合併預設與自訂，並用 dict.fromkeys 技巧去除重複 (同時保持順序)
        full_list = list(dict.fromkeys(DEFAULT_CATEGORIES + custom_cats))
        
        logger.debug(f"使用者 {user_id} 的完整類別：{full_list}")
        return full_list
    except Exception as e:
        logger.error(f"獲取 {user_id} 的自訂類別失敗：{e}", exc_info=True)
        return DEFAULT_CATEGORIES # 發生錯誤時，退回僅使用預設類別

def handle_list_categories(cat_sheet, user_id):
    """
    (新) 處理「我的類別」指令
    """
    logger.debug(f"處理 '我的類別'，user_id: {user_id}")
    user_cats = get_user_categories(cat_sheet, user_id)
    custom_cats = [c for c in user_cats if c not in DEFAULT_CATEGORIES]
    
    reply = "🦝 **您的類別清單**：\n\n"
    reply += "--- 預設類別 ---\n"
    reply += " ".join(f"• {c}" for c in DEFAULT_CATEGORIES) + "\n\n"
    
    if custom_cats:
        reply += "--- 您的自訂類別 ---\n"
        reply += " ".join(f"• {c}" for c in custom_cats) + "\n\n"
    else:
        reply += "--- 您的自訂類別 ---\n(您尚未新增任何自訂類別)\n\n"
    
    reply += "💡 您可以使用「新增類別 [名稱]」來增加喔！\n💡 「刪除類別 [名稱]」可移除自訂類別。"
    return reply
# === *** (MODIFIED) 步驟三-B: 升級 `handle_search_records_nlp` (修復 Bug #3) *** ===
# === *** (UPDATED 11-12) 重構為 get_all_values *** ===
def handle_search_records_nlp(sheet, user_id, full_text, event_time):
    """
    處理關鍵字和日期區間查詢 (使用 NLP)
    (已升級，支援收入/支出過濾)
    (MODIFIED 11-12) 重構為 get_all_values
    """
    logger.debug(f"處理 'NLP 查詢'，user_id: {user_id}, query: {full_text}")

    try:
        parsed_query = call_search_nlp(full_text, event_time)
        if parsed_query.get('status') == 'failure':
            return parsed_query.get('message', "🦝 查詢失敗，我不太懂您的意思。")

        keyword = parsed_query.get('keyword')
        start_date = parsed_query.get('start_date')
        end_date = parsed_query.get('end_date')
        # (FIX #3) 獲取新的 'type' 欄位
        query_type = parsed_query.get('type', 'all') 
        nlp_message = parsed_query.get('message', f"關於「{full_text}」")
            
    except Exception as e:
        logger.error(f"查詢的 NLP 解析失敗：{e}", exc_info=True)
        return f"查詢失敗：AI 分析器出錯：{str(e)}"
        
    logger.debug(f"NLP 解析查詢結果：Keyword: {keyword}, Start: {start_date}, End: {end_date}, Type: {query_type}")

    # (NEW 11-12) 效能優化
    all_values = sheet.get_all_values()
    if not all_values or len(all_values) < 2:
        return f"🦝 找不到關於「{nlp_message}」的任何記錄喔 (帳本是空的)！"
        
    header = all_values[0]
    try:
        idx_uid = header.index('使用者ID')
        idx_time_new = header.get('日期', -1)
        idx_time_old = header.get('時間', -1)
        idx_amount = header.index('金額')
        idx_cat = header.index('類別')
        idx_note = header.index('備註')
    except (ValueError, KeyError) as e:
        logger.error(f"GSheet 標頭錯誤 (handle_search_records_nlp): {e}")
        return "查詢失敗：GSheet 標頭欄位缺失。"

    matches = []
    
    try:
        start_dt = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None
        end_dt = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None
    except ValueError as e:
        return f"AI 回傳的日期格式錯誤 ({start_date}, {end_date})。"

    # (NEW 11-12) 遍歷原始 list
    for r in all_values[1:]:
        if len(r) <= max(idx_uid, idx_time_new, idx_time_old, idx_amount, idx_cat, idx_note):
            continue
            
        if r[idx_uid] != user_id:
            continue
        
        keyword_match = True
        date_match = True
        type_match = True # (FIX #3) 新增類型比對
        
        # 1. 關鍵字比對
        category_val = r[idx_cat] if r[idx_cat] else ''
        notes_val = r[idx_note] if r[idx_note] else ''
        if keyword:
            keyword_match = (keyword in category_val) or (keyword in notes_val)
        
        # 2. 日期比對
        record_time_str = ""
        if idx_time_new != -1 and len(r) > idx_time_new and r[idx_time_new]:
            record_time_str = r[idx_time_new]
        elif idx_time_old != -1 and len(r) > idx_time_old and r[idx_time_old]:
            record_time_str = r[idx_time_old]
            
        if (start_dt or end_dt) and record_time_str:
            try:
                record_dt = datetime.strptime(record_time_str[:10], '%Y-%m-%d').date()
                if start_dt and record_dt < start_dt: date_match = False
                if end_dt and record_dt > end_dt: date_match = False
            except ValueError:
                date_match = False 
        
        # 3. (FIX #3) 類型比對 (收入/支出)
        try:
            amount = float(r[idx_amount])
            if query_type == 'income' and amount <= 0: # 收入 (必須 > 0)
                type_match = False
            if query_type == 'expense' and amount >= 0: # 支出 (必須 < 0)
                type_match = False
        except (ValueError, TypeError):
            type_match = False # 金額格式錯誤，過濾掉
        
        # 必須全部符合
        if keyword_match and date_match and type_match:
            # (NEW 11-12) 儲存原始的 row (list)，並附上時間戳 (用於排序)
            matches.append((record_time_str, r))
    
    if not matches:
        return f"🦝 找不到關於「{nlp_message}」的任何記錄喔！"
    
    reply = f"🔎 {nlp_message} (共 {len(matches)} 筆)：\n\n"
    limit = 20 
    
    # (NEW 11-12) 排序
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)
    
    total_amount_all_matches = 0.0
    
    for (date_str, r) in sorted_matches:
         try:
            amount = float(r[idx_amount])
            total_amount_all_matches += amount
            
            if len(reply.split('\n')) <= limit + 5: 
                category = r[idx_cat]
                notes = r[idx_note]
                
                if not date_str:
                     display_date = "N/A"
                else:
                    try:
                        if len(date_str) > 10:
                            display_date = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
                        else:
                            display_date = datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y-%m-%d')
                    except ValueError:
                        display_date = date_str 
                
                reply += f"• {display_date} {notes} ({category}) {amount:.0f} 元\n"
                
         except (ValueError, TypeError):
            continue
    
    reply += f"\n--------------------\n"
    reply += f"📈 查詢總計：{total_amount_all_matches:.0f} 元\n"
    
    if len(matches) > limit:
        reply += f"(僅顯示最近 {limit} 筆記錄)"
        
    return reply

def handle_add_category(cat_sheet, user_id, text):
    """
    (新) 處理「新增類別」指令
    (MODIFIED) 使用 re.search 進行模糊比對
    """
    logger.debug(f"處理 '新增類別'，user_id: {user_id}, text: {text}")
    # (MODIFIED) 1. \s+ 改為 \s* (允許沒有空格)
    # (MODIFIED) 2. re.match 改為 re.search (允許指令在句子中)
    match = re.search(r'(新增類別|增加類別)\s*(.+)', text)
    if not match:
        return "格式錯誤！請包含「新增類別 [名稱]」\n例如：「我想 新增類別 寵物」"
    
    # (MODIFIED) 3. 移除前後括號，例如 [交際應酬] -> 交際應酬
    new_cat = match.group(2).strip()
    new_cat = re.sub(r'^[\[【(](.+?)[\]】)]$', r'\1', new_cat).strip()
    
    if not new_cat:
        return "類別名稱不可為空喔！"
    if len(new_cat) > 10:
        return "🦝 類別名稱太長了（最多10個字）！"
    if new_cat in DEFAULT_CATEGORIES:
        return f"🦝 「{new_cat}」是預設類別，不用新增喔！"
    
    try:
        # 檢查是否已存在
        all_cats_records = cat_sheet.get_all_records()
        for r in all_cats_records:
            if r.get('使用者ID') == user_id and r.get('類別') == new_cat:
                return f"🦝 嘿！「{new_cat}」已經在您的類別中了～"
        
        # 新增
        cat_sheet.append_row([user_id, new_cat])
        logger.info(f"使用者 {user_id} 成功新增類別：{new_cat}")
        return f"✅ 成功新增類別：「{new_cat}」！"
    except Exception as e:
        logger.error(f"新增類別失敗：{e}", exc_info=True)
        return f"新增類別時發生錯誤：{str(e)}"
# (MODIFIED 11-12) 重構為 get_all_values
def handle_total_analysis(sheet, user_id):
    """
    處理 '總收支分析' 指令 (現在也包含了 '查帳')
    """
    logger.debug(f"處理 '總收支分析 / 查帳' (新版)，user_id: {user_id}")
    try:
        # (NEW 11-12) 效能優化
        all_values = sheet.get_all_values()
        if not all_values or len(all_values) < 2:
            return "您目前沒有任何記帳記錄喔！"

        header = all_values[0]
        # 1. 先定義欄位索引
        try:
            idx_uid = header.index('使用者ID')
            idx_amount = header.index('金額')
            idx_cat = header.index('類別')
        except (ValueError, KeyError) as e:
            logger.error(f"GSheet 標頭錯誤 (handle_total_analysis): {e}")
            return "分析失敗：GSheet 標頭欄位缺失。"

        total_income = 0.0
        total_expense = 0.0
        category_spending = {}

        # 2. 遍歷原始列表 (跳過標頭)
        for r in all_values[1:]:
            # 確保欄位足夠且是這位使用者
            if len(r) > max(idx_uid, idx_amount, idx_cat) and r[idx_uid] == user_id:
                try:
                    amount = float(r[idx_amount])
                    if amount > 0:
                        total_income += amount
                    else:
                        expense = abs(amount)
                        total_expense += expense
                        category = r[idx_cat] if r[idx_cat] else '雜項'
                        category_spending[category] = category_spending.get(category, 0) + expense
                except (ValueError, TypeError):
                    continue
        
        if total_income == 0 and total_expense == 0:
             return "您目前沒有任何記帳記錄喔！"
             
        reply = f"📈 **您的總收支分析** (從開始記帳至今)：\n\n"
        reply += f"💰 總收入：{total_income:.0f} 元\n"
        reply += f"💸 總支出：{total_expense:.0f} 元\n"
        reply += f"📊 淨餘額：{total_income - total_expense:.0f} 元\n"
        
        if category_spending:
            reply += "\n--- 總支出類別分析 (花費最多) ---\n"
            sorted_spending = sorted(category_spending.items(), key=lambda item: item[1], reverse=True)
            
            for i, (category, amount) in enumerate(sorted_spending):
                percentage = (amount / total_expense) * 100 if total_expense > 0 else 0
                icon = ["🥇", "🥈", "🥉"]
                prefix = icon[i] if i < 3 else "🔹"
                reply += f"{prefix} {category}: {amount:.0f} 元 (佔 {percentage:.1f}%)\n"
        
        return reply
    except Exception as e:
        logger.error(f"總收支分析失败：{e}", exc_info=True)
        return f"總收支分析報表產生失败：{str(e)}"
    
def handle_delete_category(cat_sheet, user_id, text):
    """
    (新) 處理「刪除類別」指令
    (MODIFIED) 使用 re.search 進行模糊比對
    """
    logger.debug(f"處理 '刪除類別'，user_id: {user_id}, text: {text}")
    # (MODIFIED) 1. \s+ 改為 \s* (允許沒有空格)
    # (MODIFIED) 2. re.match 改為 re.search (允許指令在句子中)
    match = re.search(r'(刪除類別|移除類別)\s*(.+)', text)
    if not match:
        return "格式錯誤！請包含「刪除類別 [名稱]」\n例如：「幫我 刪除類別 寵物」"
    
    # (MODIFIED) 3. 移除前後括號
    cat_to_delete = match.group(2).strip()
    cat_to_delete = re.sub(r'^[\[【(](.+?)[\]】)]$', r'\1', cat_to_delete).strip()

    if cat_to_delete in DEFAULT_CATEGORIES:
        return f"🦝 「{cat_to_delete}」是預設類別，不可以刪除喔！"
    
    try:
        all_values = cat_sheet.get_all_values()
        row_to_delete_index = -1
        # 從後面開始找，確保找到最新的 (雖然理論上不該重複)
        for i in range(len(all_values) - 1, 0, -1): 
            row = all_values[i]
            # 確保欄位存在
            if len(row) > 1 and row[0] == user_id and row[1] == cat_to_delete:
                row_to_delete_index = i + 1 # GSheet row index is 1-based
                break
        
        if row_to_delete_index != -1:
            cat_sheet.delete_rows(row_to_delete_index)
            logger.info(f"使用者 {user_id} 成功刪除類別：{cat_to_delete}")
            return f"🗑️ 已刪除您的自訂類別：「{cat_to_delete}」"
        else:
            return f"🦝 找不到您的自訂類別：「{cat_to_delete}」"
    except Exception as e:
        logger.error(f"刪除類別失敗：{e}", exc_info=True)
        return f"刪除類別時發生錯誤：{str(e)}"

# === (MODIFIED) 意圖分類器 (強化模糊比對) ===
# *** (UPDATED 11-02) ***
# === (MODIFIED) 意圖分類器 (強化模糊比對) ===
# *** (UPDATED 11-05 v4) ***
def get_user_intent(text, event_time):
    """
    使用 Gemini 判斷使用者的 "主要意圖"
    """
    logger.debug(f"正在分類意圖: {text}")
    
    today_str = event_time.strftime('%Y-%m-%d')
    date_context = f"今天是 {today_str} (星期{event_time.weekday()})."

    prompt_raw = """
    你是一個記帳機器人的「意圖分類總管」。
    使用者的輸入是：「$TEXT」
    $DATE_CTX

    你的*唯一*任務是判斷使用者的主要意圖。請嚴格回傳以下 JSON 格式：
    {
      "intent": "RECORD" | "DELETE" | "UPDATE" | "QUERY_DATA" | "QUERY_REPORT" | "QUERY_ADVICE" | "MANAGE_BUDGET" | "MANAGE_CATEGORIES" | "NEW_FEATURE_EXCHANGE_RATE" | "HELP" | "CHAT" | "UNKNOWN"
    }

    判斷規則：
    - RECORD: 明顯的記帳 (例如 "雞排 80", "收入 5000", "午餐100 晚餐200", "目前收入 39020 支出 45229" -> 這也是 RECORD)
    - DELETE: 明顯的刪除 (例如 "刪除 雞排", "刪掉 昨天", "幫我把早上的麵包刪掉")
    - UPDATE: 明顯的修改 (例如 "香蕉能改為餐飲嗎", "把昨天的 100 元改成 120")
    - QUERY_DATA: 查詢*特定資料* (例如 "查詢 雞排", "查詢今天", "查詢這禮拜的餐飲")
    - QUERY_REPORT: 查詢*匯總報表* (例如 "查帳", "月結", "本週重點", "總收支分析")
    - QUERY_ADVICE: 詢問*建議* (例如 "我本月花太多嗎？", "有什麼建議")
    - MANAGE_BUDGET: 設定或查看預算 (例如 "設置預算", "查看預算", "我還剩多少預算？")
    - MANAGE_CATEGORIES: (新) 新增、刪除或查詢類別 (例如 "新增類別 寵物", "我的類別", "有哪些類別？", "類別", "目前類別")
    - NEW_FEATURE_EXCHANGE_RATE: 詢問金融功能，特別是匯率 (例如 "美金匯率", "100 USD = ? TWD")
    - HELP: 請求幫助 (例如 "幫助", "你會幹嘛", "說明", "help")
    - CHAT: 閒聊 (例如 "你好", "謝謝", "你是誰")
    - UNKNOWN: 無法分類

    範例：
    輸入: "刪掉早上的草莓麵包$$55" -> {"intent": "DELETE"}
    輸入: "查詢今天" -> {"intent": "QUERY_DATA"}
    輸入: "有什麼建議" -> {"intent": "QUERY_ADVICE"}
    輸入: "美金匯率" -> {"intent": "NEW_FEATURE_EXCHANGE_RATE"}
    輸入: "月結" -> {"intent": "QUERY_REPORT"}
    輸入: "我還剩多少預算？" -> {"intent": "MANAGE_BUDGET"}
    輸入: "我的餐飲預算 3000" -> {"intent": "MANAGE_BUDGET"}
    輸入: "預算" -> {"intent": "MANAGE_BUDGET"}
    輸入: "我想加個類別 叫 寵物" -> {"intent": "MANAGE_CATEGORIES"}
    輸入: "我想刪掉 寵物 這個類別" -> {"intent": "MANAGE_CATEGORIES"}
    輸入: "我的類別" -> {"intent": "MANAGE_CATEGORIES"}
    輸入: "有哪些類別？" -> {"intent": "MANAGE_CATEGORIES"}
    輸入: "類別" -> {"intent": "MANAGE_CATEGORIES"}
    輸入: "help" -> {"intent": "HELP"}
    # === (NEW) (修改點) 解決 Bug 6 & 7 (閒聊判斷) ===
    輸入: "我今天晚餐吃了烤肉沒花錢快樂" -> {"intent": "CHAT"}
    輸入: "朋友請我吃火鍋" -> {"intent": "CHAT"}
    
    # === (*** NEW 11-05 v4 ***) 解決無意義輸入的幻覺 Bug ===
    # 告訴 AI 這些都是閒聊或無法處理
    輸入: "..." -> {"intent": "CHAT"}
    輸入: "…" -> {"intent": "CHAT"}
    輸入: "？？？" -> {"intent": "CHAT"}
    輸入: "：" -> {"intent": "CHAT"}
    輸入: "///" -> {"intent": "CHAT"}
    # 或是完全無法辨識的
    輸入: "asdfg" -> {"intent": "UNKNOWN"}
    """
    prompt = Template(prompt_raw).substitute(
        TEXT=text,
        DATE_CTX=date_context
    )

    try:
        response = gemini_model.generate_content(prompt)
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        logger.debug(f"Gemini Intent response: {clean_response}")
        data = json.loads(clean_response)
        return data.get('intent', 'UNKNOWN')
    except Exception as e:
        logger.error(f"Gemini Intent API 呼叫失敗：{e}", exc_info=True)
        return "UNKNOWN"

# === *** (NEW) 步驟一：新增 `handle_chat_nlp` (強化聊天) *** ===
def handle_chat_nlp(text):
    """
    (新功能) 使用 Gemini 處理閒聊意圖，提供動態回應
    """
    logger.debug(f"Handling NLP chat: {text}")
    prompt = f"""
    你是一個記帳機器人「小浣熊🦝」，你正在和使用者聊天。
    請用可愛、友善、有點俏皮的口吻回覆使用者的話。
    保持回覆簡短（一到兩句話）。
    如果問你的主人或開發者或創造的人是誰之類的請可愛的回應是黃瀚葳
    使用者的話：「{text}」

    你的回覆：
    """
    try:
        response = gemini_model.generate_content(prompt)
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        # 避免 AI 回傳空訊息
        if not clean_response:
            return "🦝 嘻嘻！"
        return clean_response
    except Exception as e:
        logger.error(f"Chat NLP failed: {e}")
        return "🦝 呃... 小浣熊剛剛有點分心了，你可以試試其他的"


# === Webhook 處理 (LINE 訊息的入口) ===
@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.debug(f"Received webhook request, body (前100字): {body[:100]}...")
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError as e:
        logger.error(f"Invalid signature: {e}. Check LINE_CHANNEL_SECRET.", exc_info=True)
        abort(400)
    except Exception as e:
        logger.error(f"Webhook 處理失敗: {e}", exc_info=True)
        return 'Internal Server Error', 500
    
    return 'OK'

# === (REWRITE) `handle_message` (主路由器) ===
# *** (UPDATED 11-02) ***
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    line_timestamp_ms = event.timestamp
    event_time = datetime.fromtimestamp(line_timestamp_ms / 1000.0, tz=TIMEZONE)
    
    logger.debug(f"Received message: '{text}' from user '{user_id}' at {event_time}")
    
    # 1. 幫助指令 (優先)
    # (MODIFIED) 增加模糊比對 (說明, help) 且要求完全匹配
    if re.search(r'^(幫助|說明|你會幹嘛|help)$', text, re.I): # re.I = 忽略大小寫
        # (MODIFIED) 動態產生預設類別列表
        default_cat_str = " ".join(f"• {c}" for c in DEFAULT_CATEGORIES)
        
        # (MODIFIED) 使用 f-string 插入 default_cat_str
        reply_text = (
            f"📌 **記帳小浣熊使用說明🦝**：\n\n"
            "💸 **自然記帳** (AI會幫你分析)：\n"
            "   - 「今天中午吃了雞排80」\n"
            "   - 「昨天喝飲料 50」\n"
            "   - 「午餐100 晚餐200」\n\n"
            "📊 **分析查詢** (推薦使用圖文選單)：\n"
            "   - 「總收支分析」：分析所有時間\n"
            "   - 「月結」：分析這個月\n"
            "   - 「本週重點」：分析本週\n\n"
            "🔎 **自然語言查詢**：\n"
            "   - 「查詢 雞排」\n"
            "   - 「查詢 這禮拜的餐飲」\n"
            "   - 「查詢 上個月的收入」\n"
            "   - 「我本月花太多嗎？」\n\n"
            "🗑️ **刪除**：\n"
            "   - 「刪除」：(安全) 移除您最近一筆記錄\n"
            "   - 「刪除 雞排」：預覽將刪除的記錄\n"
            "   - 「確認刪除」：確認執行全部刪除\n"
            "   - 「確認刪除 1」：(新) 確認刪除單筆\n\n"
            "💡 **預算**：\n"
            "   - 「設定預算 餐飲 3000」\n"
            "   - 「預算 餐飲 5000」 (可省略設定)\n"
            "   - 「查看預算」：檢查本月預算使用情況\n\n"
            "✨ **類別管理**：\n"
            f"   --- 預設類別 ---\n   {default_cat_str}\n\n"
            "   --- 自訂功能 ---\n"
            "   - 「我的類別」：查看所有(含自訂)類別\n"
            "   - 「新增類別 [名稱]」 (例如: 新增類別 寵物)\n"
            "   - 「刪除類別 [名稱]」 (僅限自訂類別)"
        )
        
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
            return
        except LineBotApiError as e:
            logger.error(f"回覆 '幫助' 訊息失敗：{e}", exc_info=True)
            return

    # 2. 獲取 Google Sheets 工作簿 (所有後續指令都需要)
    try:
        workbook = get_sheets_workbook()
        if not workbook:
            raise ValueError("Google Sheets 工作簿為 None")
    except Exception as e:
        logger.error(f"初始化 Google Sheets 失敗：{e}", exc_info=True)
        reply_text = f"糟糕！小浣熊的帳本連接失敗：{str(e)}"
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        except LineBotApiError as e_reply:
            logger.error(f"回覆 Google Sheets 錯誤訊息失敗：{e_reply}", exc_info=True)
        return

    # (MODIFIED) 確保工作表存在 (現在有 3 個)
    trx_sheet, budget_sheet, cat_sheet = ensure_worksheets(workbook)
    if not trx_sheet or not budget_sheet or not cat_sheet:
        reply_text = "糟糕！無法創建或存取 'Transactions', 'Budgets' 或 'Categories' 工作表。"
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        except LineBotApiError as e:
            logger.error(f"回覆工作表錯誤訊息失敗：{e}", exc_info=True)
        return
    
    # === (NEW) 步驟 2.1：(修改點) 提早獲取 user_name ===
    # 移到這裡，讓所有意圖都能使用
    user_name = get_user_profile_name(user_id) 
            
    # === 3. (新) AI 意圖分類器 ===
    user_intent = get_user_intent(text, event_time)
    logger.info(f"使用者 '{user_id}' 的意圖被分類為: {user_intent}")

    # === 4. (新) 意圖路由器 (Router) ===
    try:
        if user_intent == "HELP":
            # (理論上在步驟 1 就被攔截了，但以防萬一)
            # (MODIFIED) 再次呼叫幫助訊息
            default_cat_str = " ".join(f"• {c}" for c in DEFAULT_CATEGORIES)
            reply_text = (
                f"📌 **記帳小浣熊使用說明🦝**：\n\n"
                "💸 **自然記帳** (AI會幫你分析)：\n"
                "   - 「今天中午吃了雞排80」\n"
                "   - 「昨天喝飲料 50」\n"
                "   - 「午餐100 晚餐200」\n\n"
                "📊 **分析查詢** (推薦使用圖文選單)：\n"
                "   - 「總收支分析」：分析所有時間\n"
                "   - 「月結」：分析這個月\n"
                "   - 「本週重點」：分析本週\n\n"
                "🔎 **自然語言查詢**：\n"
                "   - 「查詢 雞排」\n"
                "   - 「查詢 這禮拜的餐飲」\n"
                "   - 「查詢 上個月的收入」\n"
                "   - 「我本月花太多嗎？」\n\n"
                "🗑️ **刪除**：\n"
                "   - 「刪除」：(安全) 移除您最近一筆記錄\n"
                "   - 「刪除 雞排」：預覽將刪除的記錄\n"
                "   - 「確認刪除」：確認執行全部刪除\n"
                "   - 「確認刪除 1」：(新) 確認刪除單筆\n\n"
                "💡 **預算**：\n"
                "   - 「設定預算 餐飲 3000」\n"
                "   - 「預算 餐飲 5000」 (可省略設定)\n"
                "   - 「查看預算」：檢查本月預算使用情況\n\n"
                "✨ **類別管理**：\n"
                f"   --- 預設類別 ---\n   {default_cat_str}\n\n"
                "   --- 自訂功能 ---\n"
                "   - 「我的類別」：查看所有(含自訂)類別\n"
                "   - 「新增類別 [名稱]」 (例如: 新增類別 寵物)\n"
                "   - 「刪除類別 [名稱]」 (僅限自訂類別)"
            )

        # --- 報表查詢 (QUERY_REPORT) ---
        elif user_intent == "QUERY_REPORT":
            logger.debug("意圖：QUERY_REPORT (查詢報表)")
            if "查帳" in text or "總收支" in text or "總分析" in text:
                reply_text = handle_total_analysis(trx_sheet, user_id)
            elif "月結" in text:
                reply_text = handle_monthly_report(trx_sheet, user_id, event_time)
            elif "週" in text or "周" in text: 
                reply_text = handle_weekly_report(trx_sheet, user_id, event_time)
            else: 
                reply_text = handle_search_records_nlp(trx_sheet, user_id, text, event_time)
        
        # --- 預算管理 (MANAGE_BUDGET) ---
        elif user_intent == "MANAGE_BUDGET":
            logger.debug("意圖：MANAGE_BUDGET (預算管理)")
            # (MODIFIED) 增加模糊比對
            # 1. 檢查是否包含 "設置" 或 "設定" 關鍵字
            # 2. 檢查是否像 "預算 [某東西] [數字]" (例如 "預算 餐飲 3000")
            # (MODIFIED v2) 這裡的 re.search 條件放寬 (稍後 handle_set_budget 會用更精確的 re.search)
            if "設置" in text or "設定" in text or re.search(r'預算.*?\d+', text):
                # (MODIFIED) 傳入 cat_sheet
                reply_text = handle_set_budget(budget_sheet, cat_sheet, text, user_id)
            else: 
                # 其他 (例如 "查看預算", "預算")
                reply_text = handle_view_budget(trx_sheet, budget_sheet, user_id, event_time)

        # --- (NEW) 類別管理 (MANAGE_CATEGORIES) ---
        elif user_intent == "MANAGE_CATEGORIES":
            logger.debug("意圖：MANAGE_CATEGORIES (類別管理)")
            if "新增" in text or "增加" in text:
                reply_text = handle_add_category(cat_sheet, user_id, text)
            elif "刪除" in text or "移除" in text:
                # (MODIFIED) 確保 "刪除類別" 不會被 "刪除" 指令攔截
                # 這裡的 "刪除" 關鍵字比 "DELETE" 意圖中的 "刪除" 關鍵字更特定 (因為意圖是 MANAGE_CATEGORIES)
                reply_text = handle_delete_category(cat_sheet, user_id, text)
            else: # "我的類別", "有哪些類別", "類別", "目前類別" etc.
                reply_text = handle_list_categories(cat_sheet, user_id)

        # --- 刪除 (DELETE) ---
        elif user_intent == "DELETE":
            logger.debug("意圖：DELETE (刪除)")
            if "確認刪除" in text or ("確認" in text and "刪除" in text):
                # (MODIFIED) 傳入 text 參數
                reply_text = handle_confirm_delete(trx_sheet, user_id, event_time, text)
            elif text == "刪除": 
                reply_text = handle_delete_last_record(trx_sheet, user_id)
            else:
                reply_text = handle_advanced_delete_nlp(trx_sheet, user_id, text, event_time) 

        # --- 查詢資料 (QUERY_DATA) ---
        elif user_intent == "QUERY_DATA":
            logger.debug("意圖：QUERY_DATA (查詢資料)")
            reply_text = handle_search_records_nlp(trx_sheet, user_id, text, event_time) 

        # --- 詢問建議 (QUERY_ADVICE) ---
        elif user_intent == "QUERY_ADVICE":
            logger.debug("意圖：QUERY_ADVICE (詢問建議)")
            # (修改點) 傳入 user_name
            reply_text = handle_conversational_query_advice(trx_sheet, budget_sheet, text, user_id, user_name, event_time)
        
        # --- 修改 (UPDATE) ---
        elif user_intent == "UPDATE":
            logger.debug("意圖：UPDATE (修改)")
            reply_text = handle_update_record_nlp(trx_sheet, user_id, text, event_time) 

        # --- 新功能 (NEW_FEATURE) ---
        elif user_intent == "NEW_FEATURE_EXCHANGE_RATE":
            logger.debug("意圖：NEW_FEATURE (匯率)")
            reply_text = handle_exchange_rate_query(text)
            
        # --- 記帳 (RECORD) ---
        elif user_intent == "RECORD":
            logger.debug("意圖：RECORD (記帳)")
            # (修改點) user_name 已經在上面獲取了
            reply_text = handle_nlp_record(trx_sheet, budget_sheet, cat_sheet, text, user_id, user_name, event_time)
        
        # --- 聊天 (CHAT) ---
        elif user_intent == "CHAT":
            logger.debug("意圖：CHAT (聊天)")
            reply_text = handle_chat_nlp(text)
        
        else: # UNKNOWN 
            logger.warning(f"未知的意圖 '{user_intent}'，當作聊天或記帳處理。")
            # (修改點) user_name 已經在上面獲取了
            reply_text = handle_nlp_record(trx_sheet, budget_sheet, cat_sheet, text, user_id, user_name, event_time)

    except Exception as e:
        logger.error(f"處理意圖 '{user_intent}' 失敗：{e}", exc_info=True)
        reply_text = f"糟糕！小浣熊處理您的指令時出錯了：{str(e)}"

    # 最終回覆
    if not isinstance(reply_text, str):
        reply_text = str(reply_text)

    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except LineBotApiError as e:
        logger.error(f"回覆訊息失敗：{e}", exc_info=True)

# === 核心功能函式 (Helper Functions) ===

def get_datetime_from_record(r):
    """
    相容性輔助函式：
    優先嘗試讀取 '日期' (新)，如果沒有，再讀取 '時間' (舊)
    """
    return r.get('日期', r.get('時間', ''))

def get_cute_reply(category):
    """
    根據類別返回客製化的可愛回應 (隨機)
    """
    replies = {
        "餐飲": ["好好吃飯，才有力氣！ 🍜 (⁎⁍̴̛ᴗ⁍̴̛⁎)", "吃飽飽，心情好！ 😋", "這餐看起來真不錯！ 🍔"],
        "飲料": ["是全糖嗎？ 🧋 快樂水 get daze！", "乾杯！ 🥂", "喝點飲料，放鬆一下～ 🥤"],
        "交通": ["嗶嗶！出門平安 🚗 目的地就在前方！", "出發！ 🚀", "路上小心喔！ 🚌"],
        "娛樂": ["哇！聽起來好好玩！ 🎮 (≧▽≦)", "Happy time! 🥳", "這錢花得值得！ 🎬"],
        "購物": ["又要拆包裹啦！📦 快樂就是這麼樸實無華！", "買！都買！ 🛍️", "錢沒有不見，只是變成你喜歡的樣子！ 💸"],
        "日用品": ["生活小物補貨完成～ 🧻", "家裡又多了一點安全感 ✨", "補貨行動成功！🧴"],
        "雜項": ["嗯... 這筆花費有點神秘喔 🧐", "生活總有些意想不到的開銷～ 🤷", "筆記筆記... 📝"],
        "收入": ["太棒了！💰 距離財富自由又近了一步！", "發財啦！ 🤑", "努力有回報！ 💪"]
    }
    default_replies = ["✅ 記錄完成！", "OK！記好囉！ ✍️", "小浣熊收到！ 🦝"]
    
    # (MODIFIED) 如果是自訂類別，但 AI 還是回傳了「娛樂」的可愛回應 (例如 Bug B)，
    # 這裡做一個保險，如果是收入，強制蓋過。
    if category == "收入":
        return random.choice(replies["收入"])
        
    category_replies = replies.get(category, default_replies)
    return random.choice(category_replies)

def check_budget_warning(trx_sheet, budget_sheet, user_id, category, event_time):
    """
    檢查特定類別的預算，如果接近或超過則回傳警告訊息
    """
    if category == "收入":
        return ""

    logger.debug(f"正在為 {user_id} 檢查 {category} 的預算...")
    try:
        budgets_records = budget_sheet.get_all_records()
        user_budget_limit = 0.0
        for b in budgets_records:
            if b.get('使用者ID') == user_id and b.get('類別') == category:
                user_budget_limit = float(b.get('限額', 0))
                break
        
        if user_budget_limit <= 0:
            return "" # 未設定預算

        transactions_records = trx_sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        spent = 0.0
        for r in transactions_records:
            try:
                amount = float(r.get('金額', 0))
                record_time_str = get_datetime_from_record(r)
                
                if (r.get('使用者ID') == user_id and
                    record_time_str.startswith(current_month_str) and
                    r.get('類別') == category and
                    amount < 0):
                    spent += abs(amount)
            except (ValueError, TypeError):
                continue
        
        percentage = (spent / user_budget_limit) * 100
        
        if percentage >= 100:
            return f"\n\n🚨 警告！ {category} 預算已超支 {spent - user_budget_limit:.0f} 元！ 😱"
        elif percentage >= 90:
            remaining = user_budget_limit - spent
            return f"\n\n🔔 注意！ {category} 預算只剩下 {remaining:.0f} 元囉！ (已用 {percentage:.0f}%)"
        
        return ""
    
    except Exception as e:
        logger.error(f"檢查預算警告失敗：{e}", exc_info=True)
        return "\n(檢查預算時發生錯誤)"

# === 加法/乘法 表達式解析與合併（本地保險機制） ===
def _parse_amount_expr(expr: str):
    """
    嘗試解析簡單的金額運算字串
    """
    try:
        expr_std = expr.replace('x', '*').replace('X', '*').replace('＋', '+').replace('－', '-').replace('＊', '*')
        if re.fullmatch(r"[0-9\.\+\-\*\s]+", expr_std):
            return eval(expr_std, {"__builtins__": {}}, {})
    except Exception:
        pass
    return None

def _try_collapse_add_expr_from_text(original_text: str, records: list):
    """
    嘗試合併像「晚餐180+60+135」這類被誤拆的多筆紀錄
    """
    text = original_text.strip()
    m = re.search(r"\d", text)
    if not m:
        return records, False

    prefix = text[:m.start()].strip()
    tail = text[m.start():]

    val = _parse_amount_expr(tail)
    if val is None:
        return records, False
    if len(records) < 2:
        return records, False

    cats = [r.get("category", "") for r in records]
    same_cat = len(set(cats)) == 1
    if not same_cat:
        return records, False

    signs = [1 if float(r.get("amount", 0)) > 0 else -1 for r in records]
    sign = 1 if signs.count(1) > signs.count(-1) else -1

    collapsed = [{
        "datetime": records[0].get("datetime"),
        "category": records[0].get("category"),
        "amount": float(val) * sign,
        "notes": prefix or records[0].get("notes", "")
    }]
    return collapsed, True

# === (MODIFIED) `handle_nlp_record` (記帳) (動態類別 + Bug 修正) ===
# *** (UPDATED 11-04) ***
# === (MODIFIED) `handle_nlp_record` (記帳) (動態類別 + Bug 修正) ===
# *** (UPDATED 11-05 v3) ***
# === (MODIFIED) `handle_nlp_record` (記帳) (動態類別 + Bug 修正) ===
# *** (UPDATED 11-12 v4) ***
def handle_nlp_record(sheet, budget_sheet, cat_sheet, text, user_id, user_name, event_time):
    """
    (MODIFIED) 使用 Gemini NLP 處理自然語言記帳 (記帳、聊天、查詢、系統問題)
    """
    logger.debug(f"處理自然語言記帳指令：{text}")

    # === 步驟一：動態獲取使用者的類別 ===
    try:
        user_categories = get_user_categories(cat_sheet, user_id)
        user_categories_list_str = ", ".join(f'"{c}"' for c in user_categories)
        user_categories_pipe_str = " | ".join(f'"{c}"' for c in user_categories)
    except Exception as e:
        logger.error(f"獲取動態類別失敗: {e}，將退回預設類別")
        user_categories = DEFAULT_CATEGORIES
        user_categories_list_str = ", ".join(f'"{c}"' for c in user_categories)
        user_categories_pipe_str = " | ".join(f'"{c}"' for c in user_categories)
    
    current_time_str = event_time.strftime('%Y-%m-%d %H:%M:%S')
    today = event_time.date() # (NEW) 取得 date 物件
    today_str = today.strftime('%Y-%m-%d')
    
    # === (*** NEW 11-05 v3 ***) 建立詳細的日期上下文 ===
    date_context_lines = [
        f"今天是 {today_str} (星期{today.weekday()}) (註：星期一是0, 星期日是6)。",
        f"使用者傳送時間是: {event_time.strftime('%H:%M:%S')}",
        "",
        "--- 最近日期對照表 (用於解析 '星期幾' 或 '前天') ---"
    ]
    # (NEW) 迴圈產生過去 7 天的日期
    weekdays_zh = ["一", "二", "三", "四", "五", "六", "日"]
    for i in range(7): # 產生今天 + 過去 6 天
        day = today - timedelta(days=i)
        day_str = day.strftime('%Y-%m-%d')
        weekday_str = weekdays_zh[day.weekday()]
        
        prefix = ""
        if i == 0:
            prefix = "(今天)"
        elif i == 1:
            prefix = "(昨天)"
        elif i == 2:
            prefix = "(前天)"
        
        date_context_lines.append(
            f"  - {day_str} (星期{weekday_str}) {prefix}"
        )
    
    date_context = "\n".join(date_context_lines)
    # === (*** END 11-05 v3 ***) ===
    
    # === (*** MODIFIED 11-12 v4 ***) ===
    prompt_raw = """
    你是一個記帳機器人的 AI 助手，你的名字是「記帳小浣熊🦝」。
    使用者的輸入是：「$TEXT」

    目前的日期時間上下文如下：
    $DATE_CTX

    **使用者的「傳送時間」是 $CURRENT_TIME**。

    請嚴格按照以下 JSON 格式回傳，不要有任何其他文字或 "```json" 標記：
    {
      "status": "success" | "failure" | "chat" | "query" | "system_query",
      "data": [
        {
          "datetime": "YYYY-MM-DD HH:MM:SS",
          "category": $USER_CATEGORIES_PIPE,
          "amount": <number>,
          "notes": "<string>"
        }
      ] | null,
      "message": "<string>"
    }

    ---
    ### (優先規則) 解析規則 (失敗、聊天、查詢)
    ---
    
    2. status "chat": 如果使用者只是在閒聊 (例如 "你好", "你是誰", "謝謝")。
       - (規則 2.1) 如果使用者提到「請客」、「沒花錢」，或只是分享事件*而沒有明確金額*，這*不是*記帳，應視為 "chat"。

    3. status "query": 如果使用者在 "詢問" 關於他帳務的問題 (例如 "我本月花太多嗎？")。
    4. status "system_query": 如果使用者在詢問 "系統功能" 或 "有哪些類別"。
    
    5. status "failure": (高優先級) 如果輸入無法處理。
       - (規則 5.1) 缺少關鍵資訊 (例如 "雞排" (沒說金額))。
       - (規則 5.2) 嚴格禁止在沒有明确金額時*猜測*一個數字。
       - (規則 5.3) **無意義輸入**: 如果使用者的輸入*完全*由標點符號、亂碼或單一表情符號組成 (例如 "...", "???", "////", "：：：", "…", "😅")，這*不是*記帳，應視為 "failure"。

    ---
    ### (次要規則) 解析規則 (記帳成功)
    ---
    
    1. status "success": (僅當上述規則皆不符合時) 如果成功解析為記帳 (包含一筆或多筆)。
       - data: 必須是一個 "列表" (List)，包含一或多個記帳物件。
       - **多筆記帳**: 如果使用者一次輸入多筆 (例如 "午餐100 晚餐200", "杏仁茶30+茶葉蛋10")，"data" 列表中必須包含 *多個* 物件。
       - **時間規則 (非常重要！請嚴格遵守！)**:
           - (規則 1.1) **顯式時間 (最高優先)**: 如果使用者 "明確" 提到 "日期" (例如 "昨天", "10/25", "星期一", "前天") 或 "時間" (例如 "16:22", "晚上7點")，**必須** 優先解析並使用該時間。
           - (規則 1.2) **(新!) 如何使用日期對照表**: 你必須使用「最近日期對照表」來找出「星期一」或「前天」對應的 `YYYY-MM-DD`。
           - (規則 1.3) **預設為傳送時間 (次高優先)**: 如果 "規則 1.1" 不適用 (即使用者 "沒有" 提到明確日期或時間，例如輸入 "雞排 80", "零食 50")，**必須** 使用使用者的「傳送時間」，即 **$CURRENT_TIME**。
           - (規則 1.4) **時段關鍵字 (僅供參考)**:
               - 如果使用者輸入 "早餐 50"，且「傳送時間」是 09:30，則判斷為補記帳，使用 $TODAY 08:00:00。
               - 如果使用者輸入 "午餐 100"，且「傳送時間」是 14:00，則判斷為補記帳，使用 $TODAY 12:00:00。
               - (新規則 1.4.1) 如果使用者輸入的「備註」*同時包含*品項和時段 (例如 "麥當勞早餐 80", "宵夜雞排 90")，請*優先*套用時段時間 (例如 "麥當L早餐 80" -> `datetime: "$TODAY 08:00:00"`, `category: "餐飲"`, `notes: "麥當勞早餐"`)。

       - category: (動態) 必須是 [ $USER_CATEGORIES_LIST ] 之一。
       - amount: 支出必須為負數 (-)，收入必須為正數 (+)。
       - **(v4 修改點)** notes: 盡可能擷取出花費的「品項」與「重要描述」(例如 '信用卡買書')。
       - message: "記錄成功"

    ⚠️ 規則補充：
    - (收入判斷): 如果使用者明確提到 "贏"、"賺"、"撿到"、"收到"，*必須* 歸類為 `"category": "收入"` 且 `amount` 為*正數* (+)。

    ---
    ### 範例
    ---
    # (Failure 範例 - 規則 5)
    輸入: "..." (規則 5.3) -> {"status": "failure", "data": null, "message": "🦝？ 請問需要我做什麼嗎？"}
    輸入: "????" (規則 5.3) -> {"status": "failure", "data": null, "message": "🦝？"}
    輸入: "…" (規則 5.3) -> {"status": "failure", "data": null, "message": "🦝？"}
    輸入: "宵夜" (規則 5.1) -> {"status": "failure", "data": null, "message": "🦝？ 宵夜吃了什麼？花了多少錢呢？"}
    
    # (Chat 範例 - 規則 2)
    輸入: "我今天晚餐吃了烤肉沒花錢" (規則 2.1) -> {"status": "chat", "data": null, "message": "哇！真幸運！🦝"}
    輸入: "你好" -> {"status": "chat", "data": null, "message": "哈囉！我是記帳小浣熊🦝 需要幫忙記帳嗎？"}
    
    # (Query 範例 - 規則 3, 4)
    輸入: "我本月花太多嗎？" -> {"status": "query", "data": null, "message": "我本月花太多嗎？"}
    輸入: "目前有什麼項目?" -> {"status": "system_query", "data": null, "message": "請問您是指「我的類別」嗎？ 🦝 您可以輸入「我的類別」來查看喔！"}

    # (Success 範例 - 規則 1)
    (假設今天是 2025-11-12 星期三)
    輸入: "星期一 吃了雞排80" (規則 1.2)
    輸出: {"status": "success", "data": [{"datetime": "2025-11-10 12:00:00", "category": "餐飲", "amount": -80, "notes": "雞排"}], "message": "記錄成功"}

    輸入: "前天 喝飲料 50" (規則 1.2)
    輸出: {"status": "success", "data": [{"datetime": "2025-11-10 16:00:00", "category": "飲料", "amount": -50, "notes": "喝飲料"}], "message": "記錄成功"}

    輸入: "昨天 晚餐 200" (規則 1.1)
    輸出: {"status": "success", "data": [{"datetime": "2025-11-11 18:00:00", "category": "餐飲", "amount": -200, "notes": "晚餐"}], "message": "記錄成功"}

    輸入: "香蕉 20" (規則 1.3) -> {"status": "success", "data": [{"datetime": "$CURRENT_TIME", "category": "餐飲", "amount": -20, "notes": "香蕉"}], "message": "記錄成功"}
    
    # (v4 新增範例)
    輸入: "今天16:30用信用卡購買書籍350元"
    輸出: {"status": "success", "data": [{"datetime": "2025-11-12 16:30:00", "category": "購物", "amount": -350, "notes": "信用卡購買書籍"}], "message": "記錄成功"}

    輸入: "午餐100 晚餐200" (規則 1) -> {"status": "success", "data": [{"datetime": "$TODAY 12:00:00", "category": "餐飲", "amount": -100, "notes": "午餐"}, {"datetime": "$TODAY 18:00:00", "category": "餐飲", "amount": -200, "notes": "晚餐"}], "message": "記錄成功"}
    """
    prompt = Template(prompt_raw).substitute(
        CURRENT_TIME=current_time_str,
        TODAY=today_str, # (NEW) $TODAY 變數仍然保留
        TEXT=text,
        DATE_CTX=date_context, # (NEW) 傳入全新的、更豐富的上下文
        USER_CATEGORIES_LIST=user_categories_list_str,
        USER_CATEGORIES_PIPE=user_categories_pipe_str
    )
    
    try:
        logger.debug("發送 prompt 至 Gemini API")
        response = gemini_model.generate_content(prompt)
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        logger.debug(f"Gemini NLP response: {clean_response}")
        
        data = json.loads(clean_response)
        status = data.get('status')
        message = data.get('message')

        if status == 'success':
            records = data.get('data', [])

            try:
                records, _did = _try_collapse_add_expr_from_text(text, records)
            except Exception as _e:
                logger.warning(f"合併加法表達式失敗：{_e}")
            if not records:
                return "🦝？ AI 分析成功，但沒有返回任何記錄。"
            
            reply_summary_lines = []
            last_category = "雜項" 
            
            for record in records:
                datetime_str = record.get('datetime', current_time_str)
                category = record.get('category', '雜項')
                amount_str = record.get('amount', 0)
                notes = record.get('notes', text)
                
                if category not in user_categories:
                    logger.warning(f"AI 回傳了不在列表中的類別：'{category}'，已強制修正為 '雜項'")
                    notes = f"({category}) {notes}"
                    category = "雜項"
                
                try:
                    amount = float(amount_str)
                    if amount == 0:
                        reply_summary_lines.append(f"• {notes} ({category}) 金額為 0，已跳過。")
                        continue
                except (ValueError, TypeError):
                    reply_summary_lines.append(f"• {notes} ({category}) 金額 '{amount_str}' 格式錯誤，已跳過。")
                    continue

                sheet.append_row([datetime_str, category, amount, user_id, user_name, notes])
                logger.debug(f"成功寫入 Google Sheet 記錄: {datetime_str}, {category}, {amount}, {notes}")
                
                try:
                    display_time = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
                except ValueError:
                    display_time = datetime_str 
                
                reply_summary_lines.append(f"• {display_time} {notes} ({category}) {abs(amount):.0f} 元")
                last_category = category
            
            logger.debug("所有記錄寫入完畢")

            if any(float(r.get('amount', 0)) > 0 for r in records):
                last_category = "收入"
                
            cute_reply = get_cute_reply(last_category)
            
            # === (*** MODIFIED 11-05 v2 ***) ===
            # (高危險) 為了避免 429 錯誤，"記帳" 功能 "不應該" 觸發任何 "讀取"
            # (已刪除) warning_message = ...
            # (已刪除) all_records = ...
            
            summary_text = "\n".join(reply_summary_lines)
            
            # (*** MODIFIED 11-05 v2 ***)
            # 簡化回傳，完全移除 "讀取" 相關的訊息
            return (
                f"{cute_reply}\n\n"
                f"📝 **摘要 (共 {len(reply_summary_lines)} 筆)**：\n"
                f"{summary_text}"
            )

        elif status == 'chat':
            if not message:
                return handle_chat_nlp(text)
            return message
        
        elif status == 'system_query':
            return message or "請問您是指「我的類別」嗎？ 🦝"
        
        elif status == 'query':
            logger.debug(f"NLP 偵測到聊天式查詢 '{text}'，轉交至 handle_conversational_query_advice")
            return handle_conversational_query_advice(sheet, budget_sheet, text, user_id, user_name, event_time)
        
        else: # status == 'failure'
            return message or "🦝？ 抱歉，我聽不懂喔..."

    except json.JSONDecodeError as e:
        logger.error(f"Gemini NLP JSON 解析失敗: {clean_response}")
        return f"糟糕！AI 分析器暫時罷工了 (JSON解析失敗)：{clean_response}"
    except Exception as e:
        logger.error(f"Gemini API 呼叫或 GSheet 寫入失敗：{e}", exc_info=True)
        return f"目前我無法處理這個請求：{str(e)}"
    
# === *** (DELETED) `handle_check_balance` 已被刪除 *** ===
# (因為 handle_total_analysis 更好)

# (MODIFIED 11-12) 重構為 get_all_values
def handle_monthly_report(sheet, user_id, event_time):
    """
    處理 '月結' 指令
    """
    logger.debug(f"處理 '月結' 指令，user_id: {user_id}")
    try:
        # (NEW 11-12) 效能優化
        all_values = sheet.get_all_values()
        if not all_values or len(all_values) < 2:
            return "您的帳本是空的，沒有記錄可分析。"
            
        header = all_values[0]
        try:
            idx_uid = header.index('使用者ID')
            idx_time_new = header.get('日期', -1)
            idx_time_old = header.get('時間', -1)
            idx_amount = header.index('金額')
            idx_cat = header.index('類別')
        except (ValueError, KeyError) as e:
            logger.error(f"GSheet 標頭錯誤 (handle_monthly_report): {e}")
            return "月結失敗：GSheet 標頭欄位缺失。"

        current_month_str = event_time.strftime('%Y-%m')
        
        total_income = 0.0
        total_expense = 0.0
        category_spending = {}

        # (NEW 11-12) 遍歷原始 list
        for r in all_values[1:]:
            if len(r) <= max(idx_uid, idx_time_new, idx_time_old, idx_amount, idx_cat):
                continue
                
            if r[idx_uid] != user_id:
                continue
                
            # (NEW) 優先讀取 '日期'，再讀取 '時間'
            record_time_str = ""
            if idx_time_new != -1 and len(r) > idx_time_new and r[idx_time_new]:
                record_time_str = r[idx_time_new]
            elif idx_time_old != -1 and len(r) > idx_time_old and r[idx_time_old]:
                record_time_str = r[idx_time_old]

            if record_time_str.startswith(current_month_str):
                try:
                    amount = float(r[idx_amount])
                    if amount > 0:
                        total_income += amount
                    else:
                        total_expense += amount
                        category = r[idx_cat] if r[idx_cat] else '雜項'
                        category_spending[category] = category_spending.get(category, 0) + abs(amount)
                except (ValueError, TypeError):
                    continue
        
        if total_income == 0 and total_expense == 0:
            return f"📅 {current_month_str} 月報表：\n您這個月還沒有任何記錄喔！"

        reply = f"📅 **{current_month_str} 月結報表**：\n\n"
        reply += f"💰 本月收入：{total_income:.0f} 元\n"
        reply += f"💸 本月支出：{abs(total_expense):.0f} 元\n"
        reply += f"📈 本月淨利：{total_income + total_expense:.0f} 元\n"
        
        if category_spending:
            reply += "\n--- 支出分析 (花費最多) ---\n"
            sorted_spending = sorted(category_spending.items(), key=lambda item: item[1], reverse=True)
            
            for i, (category, amount) in enumerate(sorted_spending):
                icon = ["🥇", "🥈", "🥉"]
                prefix = icon[i] if i < 3 else "🔹"
                reply += f"{prefix} {category}: {amount:.0f} 元\n"
        
        return reply
    except Exception as e:
        logger.error(f"月結失敗：{e}", exc_info=True)
        return f"月結報表產生失敗：{str(e)}"

# (MODIFIED 11-12) 重構為 get_all_values
def handle_weekly_report(sheet, user_id, event_time):
    """
    處理 '本週重點' 指令
    """
    logger.debug(f"處理 '本週重點' 指令，user_id: {user_id}")
    try:
        # (NEW 11-12) 效能優化
        all_values = sheet.get_all_values()
        if not all_values or len(all_values) < 2:
            return "您的帳本是空的，沒有記錄可分析。"
            
        header = all_values[0]
        try:
            idx_uid = header.index('使用者ID')
            idx_time_new = header.get('日期', -1)
            idx_time_old = header.get('時間', -1)
            idx_amount = header.index('金額')
            idx_cat = header.index('類別')
        except (ValueError, KeyError) as e:
            logger.error(f"GSheet 標頭錯誤 (handle_weekly_report): {e}")
            return "週報表失敗：GSheet 標頭欄位缺失。"

        today = event_time.date()
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)
        
        start_of_week_str = start_of_week.strftime('%Y-%m-%d')
        end_of_week_str = end_of_week.strftime('%Y-%m-%d')

        logger.debug(f"本週區間：{start_of_week_str} 到 {end_of_week_str}")

        total_expense = 0.0
        category_spending = {}
        day_spending = {} 

        # (NEW 11-12) 遍歷原始 list
        for r in all_values[1:]:
            if len(r) <= max(idx_uid, idx_time_new, idx_time_old, idx_amount, idx_cat):
                continue
                
            if r[idx_uid] != user_id:
                continue
            
            # (NEW) 優先讀取 '日期'，再讀取 '時間'
            record_time_str = ""
            if idx_time_new != -1 and len(r) > idx_time_new and r[idx_time_new]:
                record_time_str = r[idx_time_new]
            elif idx_time_old != -1 and len(r) > idx_time_old and r[idx_time_old]:
                record_time_str = r[idx_time_old]
                
            if not record_time_str:
                continue
            
            try:
                record_date = datetime.strptime(record_time_str[:10], '%Y-%m-%d').date()
                if start_of_week <= record_date <= end_of_week:
                    amount = float(r[idx_amount])
                    if amount < 0:
                        expense = abs(amount)
                        total_expense += expense
                        
                        category = r[idx_cat] if r[idx_cat] else '雜項'
                        category_spending[category] = category_spending.get(category, 0) + expense
                        
                        record_date_str = record_time_str[:10]
                        day_spending[record_date_str] = day_spending.get(record_date_str, 0) + expense
            except (ValueError, TypeError):
                continue
        
        if total_expense == 0:
            return f"📊 本週摘要 ({start_of_week_str} ~ {end_of_week_str})：\n您這週還沒有任何記錄喔！"

        reply = f"📊 **本週花費摘要** ({start_of_week_str} ~ {end_of_week_str})：\n"
        reply += f"💸 本週總支出：{total_expense:.0f} 元\n\n"
        
        if category_spending:
            reply += "--- 支出類別 ---\n"
            sorted_spending = sorted(category_spending.items(), key=lambda item: item[1], reverse=True)
            
            for category, amount in sorted_spending:
                percentage = (amount / total_expense) * 100 if total_expense > 0 else 0
                reply += f"• {category}：{amount:.0f} 元 (佔 {percentage:.0f}%)\n"
        
        if day_spending:
            reply += "\n--- 每日花費 ---\n"
            most_spent_day = max(day_spending, key=day_spending.get)
            most_spent_amount = day_spending[most_spent_day]
            
            try:
                display_date = datetime.strptime(most_spent_day, '%Y-%m-%d').strftime('%m/%d')
            except ValueError:
                display_date = most_spent_day
                
            reply += f"📉 花最多的一天：{display_date} (共 {most_spent_amount:.0f} 元)\n"
            
        return reply
    except Exception as e:
        logger.error(f"本週重點失敗：{e}", exc_info=True)
        return f"本週重點報表產生失敗：{str(e)}"

def handle_delete_last_record(sheet, user_id):
    """
    處理 '刪除' 指令，刪除使用者的 "最後一筆" 記錄
    """
    logger.debug(f"處理 '刪除' (最後一筆) 指令，user_id: {user_id}")
    try:
        all_values = sheet.get_all_values()
        
        if not all_values:
            return "您的帳本是空的，沒有記錄可刪除。"
            
        header = all_values[0]
        try:
            user_id_col_index = header.index('使用者ID')
        except ValueError:
            logger.warning("找不到 '使用者ID' 欄位，預設為 3 (D欄)")
            user_id_col_index = 3 
        
        for row_index in range(len(all_values) - 1, 0, -1): 
            row = all_values[row_index]
            if len(row) > user_id_col_index and row[user_id_col_index] == user_id:
                row_to_delete = row_index + 1
                
                try:
                    amount_val = float(row[2])
                    deleted_desc = f"{row[0]} {row[1]} {amount_val:.0f} 元"
                except (ValueError, TypeError, IndexError):
                    deleted_desc = f"第 {row_to_delete} 行的記錄"
                
                sheet.delete_rows(row_to_delete)
                return f"🗑️ 已刪除：{deleted_desc}"
        
        return "找不到您的記帳記錄可供刪除。"
    except Exception as e:
        logger.error(f"刪除失敗：{e}", exc_info=True)
        return f"刪除記錄失敗：{str(e)}"

# === (MODIFIED) 替換 handle_advanced_delete_nlp 函式 ===
def handle_advanced_delete_nlp(sheet, user_id, full_text, event_time):
    """
    (MODIFIED) 預覽刪除功能：使用 NLP 解析 full_text (例如 "刪掉早上的草莓麵包")
    (支援序號顯示與快取)
    """
    logger.debug(f"處理 'NLP 預覽刪除'，user_id: {user_id}, text: {full_text}")
    
    try:
        parsed_query = call_search_nlp(full_text, event_time)
        if parsed_query.get('status') == 'failure':
            return parsed_query.get('message', "🦝 刪除失敗，我不太懂您的意思。")

        keyword = parsed_query.get('keyword')
        start_date = parsed_query.get('start_date')
        end_date = parsed_query.get('end_date')
        
        if not keyword and not start_date and not end_date:
            logger.warning(f"NLP 無法從 '{full_text}' 解析出刪除條件。")
            temp_keyword = full_text.replace("刪掉", "").replace("刪除", "").replace("幫我把", "").strip()
            temp_keyword = re.sub(r'[\d$]+元?', '', temp_keyword).strip()
            
            if not temp_keyword:
                 return f"🦝 刪除失敗：AI 無法解析您的條件「{full_text}」。"
            keyword = temp_keyword
            
        nlp_message = parsed_query.get('message', f"關於「{keyword or full_text}」")
            
    except Exception as e:
        logger.error(f"預覽刪除的 NLP 解析失敗：{e}", exc_info=True)
        return f"刪除失敗：AI 分析器出錯：{str(e)}"
        
    logger.debug(f"NLP 解析刪除條件：Keyword: {keyword}, Start: {start_date}, End: {end_date}")

    # --- (GSheet 搜尋邏輯) ---
    try:
        all_values = sheet.get_all_values()
        
        if not all_values:
            return "🦝 您的帳本是空的，找不到記錄可刪除。"
            
        header = all_values[0]
        
        try:
            idx_uid = header.index('使用者ID')
            try:
                idx_time = header.index('日期')
            except ValueError:
                idx_time = header.index('時間')
            idx_cat = header.index('類別')
            idx_note = header.index('備註')
            idx_amount = header.index('金額')
        except ValueError as e:
            logger.error(f"預覽刪除失敗：GSheet 標頭欄位名稱錯誤或缺失: {e}")
            return "刪除失敗：找不到必要的 GSheet 欄位。請檢查 GSheet 標頭是否正確。"
        
        # (MODIFIED) 儲存所有符合的記錄 (包含 GSheet 行號)
        matches_found = [] 
        
        start_dt = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None
        end_dt = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None
        
        logger.debug("開始遍歷 GSheet Values 尋找刪除目標...")
        
        for row_index in range(1, len(all_values)):
            row = all_values[row_index]
            
            if len(row) <= max(idx_uid, idx_time, idx_cat, idx_note, idx_amount):
                continue
            
            if row[idx_uid] != user_id:
                continue
            
            keyword_match = True
            date_match = True
            
            if keyword:
                keyword_match = (keyword in row[idx_cat]) or (keyword in row[idx_note])
            
            record_datetime_str = row[idx_time]
            if (start_dt or end_dt) and record_datetime_str:
                try:
                    record_dt = datetime.strptime(record_datetime_str[:10], '%Y-%m-%d').date()
                    if start_dt and record_dt < start_dt: date_match = False
                    if end_dt and record_dt > end_dt: date_match = False
                except ValueError:
                    date_match = False
            
            if keyword_match and date_match:
                # (MODIFIED) 儲存 GSheet 行號 (1-based) 和資訊
                info_dict = {
                    'gsheet_row': row_index + 1, 
                    'date': record_datetime_str[:10] if record_datetime_str else 'N/A',
                    'category': row[idx_cat] if len(row) > idx_cat else 'N/A',
                    'amount': row[idx_amount] if len(row) > idx_amount else '0',
                    'notes': row[idx_note] if len(row) > idx_note else 'N/A'
                }
                matches_found.append(info_dict)
        
        if not matches_found:
            return f"🦝 嘿～找不到符合「{nlp_message}」的記錄呢～\n請確認一下條件是否有誤喔！"
        
        total_count = len(matches_found)
        
        warning_msg = ""
        if total_count > 30:
            warning_msg = f"\n\n⚠️ 警告！您即將刪除 {total_count} 筆記錄，數量較多，請確認無誤！"
        
        preview_msg = f"🗑️ **刪除預覽** - 「{nlp_message}」\n\n"
        preview_msg += f"📊 小浣熊找到 {total_count} 筆記錄囉～\n\n"
        
        display_count = min(5, total_count)
        
        # (NEW) 建立 cache 用的 mapping
        cache_mapping = {}
        all_gsheet_rows = [m['gsheet_row'] for m in matches_found]

        for i in range(display_count):
            info = matches_found[i]
            serial_num = i + 1 # 序號 (1, 2, 3...)
            gsheet_row = info['gsheet_row']
            
            cache_mapping[serial_num] = gsheet_row # 儲存 {1: gsheet_row_10, 2: gsheet_row_15}
            
            try:
                amount_val = float(info['amount']) if info['amount'] else 0
                # (NEW) 在訊息中加入序號
                preview_msg += f"  {serial_num}. {info['date']} {info['notes']} ({info['category']}) {abs(amount_val):.0f} 元\n"
            except (ValueError, TypeError):
                preview_msg += f"  {serial_num}. {info['date']} {info['notes']} ({info['category']})\n"
        
        if total_count > 5:
            preview_msg += f"\n    ... (還有 {total_count - 5} 筆未顯示，僅能操作前 {display_count} 筆) ...\n"
        
        preview_msg += warning_msg
        # (MODIFIED) 更新提示訊息
        preview_msg += f"\n\n💡 請輸入：「確認刪除」 (刪除 *全部* {total_count} 筆)\n"
        preview_msg += f"💡 或輸入：「確認刪除 [序號]」 (例如：確認刪除 1)"
        
        # (MODIFIED) 儲存新的 cache 結構
        delete_preview_cache[user_id] = {
            'timestamp': event_time,
            'message': preview_msg,
            'mapping': cache_mapping,    # 存入序號對應 {1: 10, 2: 15}
            'all_rows': all_gsheet_rows  # 存入所有 GSheet 行號 [10, 15]
        }
        
        logger.info(f"預覽刪除：找到 {total_count} 筆記錄，已暫存至 cache (含 mapping)")
        
        return preview_msg
        
    except Exception as e:
        logger.error(f"預覽刪除失敗：{e}", exc_info=True)
        return f"預覽刪除失敗：{str(e)}"

# === (MODIFIED) 替換 handle_confirm_delete 函式 ===
def handle_confirm_delete(sheet, user_id, event_time, text):
    """
    (MODIFIED) 確認刪除功能：支援「確認刪除」 (全部) 或 「確認刪除 [序號]」 (單筆)
    """
    logger.debug(f"處理 '確認刪除' 指令，user_id: {user_id}, text: {text}")
    
    if user_id not in delete_preview_cache:
        return "🦝 嘿～您還沒有預覽任何記錄呢！\n請先使用「刪除」指令查看要刪除的內容喔～"
    
    cache_data = delete_preview_cache[user_id]
    cache_time = cache_data['timestamp']
    
    time_diff = event_time - cache_time
    if time_diff.total_seconds() > 300:  # 5 分鐘 = 300 秒
        del delete_preview_cache[user_id]
        return "⏰ 哎呀！您的預覽已經過期囉（超過 5 分鐘）\n請重新使用「刪除」指令預覽～～ 🦝"
    
    # (NEW) 判斷是「全部刪除」還是「刪除單筆」
    rows_to_delete_gsheet_indices = []
    delete_message_suffix = ""

    # (NEW) 嘗試匹配「確認刪除 1」或「刪除 1」 (允許空格或沒空格)
    match = re.search(r'(確認刪除|刪除)\s*(\d+)', text)
    
    if match:
        try:
            serial_num = int(match.group(2))
            mapping = cache_data.get('mapping', {})
            
            if serial_num in mapping:
                gsheet_row = mapping[serial_num]
                rows_to_delete_gsheet_indices = [gsheet_row]
                delete_message_suffix = f" 1 筆記錄 (序號 {serial_num})"
                logger.debug(f"確認刪除：偵測到單筆刪除，序號 {serial_num}，GSheet 行號 {gsheet_row}")
            else:
                return f"🦝 咦？我找不到「序號 {serial_num}」喔～\n請檢查您預覽中的序號 (最多只支援到 5 喔！)"
        except ValueError:
            # 應該不會發生，但以防萬一
            rows_to_delete_gsheet_indices = cache_data.get('all_rows', [])
            delete_message_suffix = f" {len(rows_to_delete_gsheet_indices)} 筆記錄 (全部)"
            logger.debug(f"確認刪除：解析序號失敗，退回全部刪除")
    
    else:
        # (舊邏輯) 刪除全部
        rows_to_delete_gsheet_indices = cache_data.get('all_rows', [])
        delete_message_suffix = f" {len(rows_to_delete_gsheet_indices)} 筆記錄 (全部)"
        logger.debug(f"確認刪除：偵測到全部刪除，共 {len(rows_to_delete_gsheet_indices)} 筆")

    
    if not rows_to_delete_gsheet_indices:
        del delete_preview_cache[user_id]
        return "🦝 嗯...暫存中沒有記錄可以刪除耶～"
    
    try:
        deleted_count = 0
        # (MODIFIED) 使用新的 gsheet indices 列表
        for row_num in sorted(rows_to_delete_gsheet_indices, reverse=True):
            try:
                sheet.delete_rows(row_num)
                deleted_count += 1
            except Exception as e:
                logger.error(f"刪除第 {row_num} 行失敗: {e}")
        
        del delete_preview_cache[user_id]
        logger.info(f"確認刪除成功：共刪除 {deleted_count} 筆記錄")
        # (MODIFIED) 回傳動態訊息
        return f"✅ **刪除完成！** ✨\n\n小浣熊已經幫您刪除了{delete_message_suffix}喔～ 🦝"
        
    except Exception as e:
        logger.error(f"確認刪除失敗：{e}", exc_info=True)
        if user_id in delete_preview_cache:
            del delete_preview_cache[user_id]
        return f"刪除記錄時發生錯誤：{str(e)}"

def handle_set_budget(sheet, cat_sheet, text, user_id):
    """
    (MODIFIED) 處理 '設置預算' 指令 (使用動態類別 + re.search 模糊比對)
    (MODIFIED v2) 提高人性化，允許類別和金額之間沒有空格
    """
    logger.debug(f"處理 '設置預算' 指令，user_id: {user_id}, text: {text}")
    # (MODIFIED) 1. 使用 re.search
    # (MODIFIED) 2. 允許 "設置預算", "設定預算", "預算"
    # (MODIFIED v2) 3. 將 \s+ (強制空格) 改為 \s* (允許無空格)
    match = re.search(r'(設置預算|設定預算|預算)\s*([\u4e00-\u9fa5a-zA-Z0-9]+)\s*(\d+)', text)
    if not match:
        return "格式錯誤！請包含「(設置/設定)預算 [類別] [限額]」\n例如：「設定預算 餐飲 3000」\n或：「預算 餐飲 3000」\n或：「預算飲料635」"
    
    category = match.group(2).strip()
    limit = int(match.group(3)) 
    
    # (MODIFIED) 獲取使用者的動態類別列表
    valid_categories = get_user_categories(cat_sheet, user_id)
    
    # 不能為「收入」設定預算
    if category == "收入":
        return "🦝 不能為「收入」設定支出預算喔！"
        
    if category not in valid_categories:
        return f"無效類別！「{category}」不在您的類別清單中。\n請先使用「新增類別 {category}」"

    try:
        cell_list = sheet.findall(user_id)
        found_row = -1
        
        for cell in cell_list:
            row_values = sheet.row_values(cell.row)
            if len(row_values) > 1 and row_values[1] == category:
                found_row = cell.row
                break
        
        if found_row != -1:
            sheet.update_cell(found_row, 3, limit) 
            return f"✅ 已更新預算：{category} {limit} 元" 
        else:
            sheet.append_row([user_id, category, limit]) 
            return f"✅ 已設置預算：{category} {limit} 元" 
    except Exception as e:
        logger.error(f"設置預算失敗：{e}", exc_info=True)
        return f"設置預算失敗：{str(e)}"

# (MODIFIED 11-12) 重構 trx_sheet 的讀取
def handle_view_budget(trx_sheet, budget_sheet, user_id, event_time):
    """
    處理 '查看預算' 指令
    """
    logger.debug(f"處理 '查看預算' 指令，user_id: {user_id}")
    try:
        budgets_records = budget_sheet.get_all_records()
        user_budgets = [b for b in budgets_records if b.get('使用者ID') == user_id]
        
        if not user_budgets:
            return "您尚未設置任何預算。請輸入「設置預算 [類別] [限額]」"

        # (NEW 11-12) 效能優化
        all_values = trx_sheet.get_all_values()
        
        header = []
        trx_data_rows = []
        if all_values and len(all_values) >= 2:
            header = all_values[0]
            trx_data_rows = all_values[1:]
        else:
            logger.warning("查看預算時，Transactions GSheet 為空")
            
        header_map = {name: i for i, name in enumerate(header)}
        
        # 必須的欄位
        if '使用者ID' not in header_map or '金額' not in header_map or '類別' not in header_map:
             logger.error("GSheet 標頭錯誤 (handle_view_budget): 關鍵欄位缺失")
             # 即使 GSheet 為空，也回傳預算限額
        
        idx_uid = header_map.get('使用者ID', -1)
        idx_time_new = header_map.get('日期', -1)
        idx_time_old = header_map.get('時間', -1)
        idx_amount = header_map.get('金額', -1)
        idx_cat = header_map.get('類別', -1)

        current_month_str = event_time.strftime('%Y-%m')
        
        # (NEW 11-12) 預先計算本月花費 (快)
        monthly_spending = {}
        if idx_uid != -1: # 只有在 GSheet 欄位都正常時才計算
            for r in trx_data_rows:
                if len(r) <= max(idx_uid, idx_time_new, idx_time_old, idx_amount, idx_cat):
                    continue
                if r[idx_uid] != user_id:
                    continue
                    
                record_time_str = ""
                if idx_time_new != -1 and len(r) > idx_time_new and r[idx_time_new]:
                    record_time_str = r[idx_time_new]
                elif idx_time_old != -1 and len(r) > idx_time_old and r[idx_time_old]:
                    record_time_str = r[idx_time_old]

                if record_time_str.startswith(current_month_str):
                    try:
                        amount = float(r[idx_amount])
                        if amount < 0:
                            category = r[idx_cat] if r[idx_cat] else '雜項'
                            monthly_spending[category] = monthly_spending.get(category, 0) + abs(amount)
                    except (ValueError, TypeError):
                        continue

        reply = f"📊 **{current_month_str} 預算狀態**：\n"
        total_spent = 0.0
        total_limit = 0.0
        
        for budget in user_budgets:
            category = budget.get('類別')
            limit = float(budget.get('限額', 0))
            if limit <= 0:
                continue
                
            total_limit += limit
            # (NEW 11-12) 從預先計算的 dict 中取值
            spent = monthly_spending.get(category, 0)
            total_spent += spent
            
            remaining = limit - spent
            percentage = (spent / limit) * 100 if limit > 0 else 0
            
            bar_fill = '■' * int(percentage / 10)
            bar_empty = '□' * (10 - int(percentage / 10))
            if percentage > 100:
                bar_fill = '■' * 10
                bar_empty = ''
            elif percentage < 0:
                 bar_fill = ''
                 bar_empty = '□' * 10
                 
            status_icon = "🟢" if remaining >= 0 else "🔴"
            reply += f"\n{category} (限額 {limit:.0f} 元)\n"
            reply += f"   {status_icon} 已花費：{spent:.0f} 元\n"
            reply += f"   [{bar_fill}{bar_empty}] {percentage:.0f}%\n"
            reply += f"   剩餘：{remaining:.0f} 元\n"

        reply += "\n--------------------\n"
        if total_limit > 0:
            total_remaining = total_limit - total_spent
            total_percentage = (total_spent / total_limit) * 100 if total_spent > 0 else 0
            status_icon = "🟢" if total_remaining >= 0 else "🔴"
            
            reply += f"總預算： {total_limit:.0f} 元\n"
            reply += f"總花費： {total_spent:.0f} 元\n"
            reply += f"{status_icon} 總剩餘：{total_remaining:.0f} 元 ({total_percentage:.0f}%)"
        else:
            reply += "總預算尚未設定或設定為 0。"
        
        return reply
    except Exception as e:
        logger.error(f"查看預算失敗：{e}", exc_info=True)
        return f"查看預算失敗：{str(e)}"

# *** (UPDATED 11-02) ***
# (修改點) 增加 user_name 參數
# *** (UPDATED 11-02) ***
# (修改點) 增加 user_name 參數
# *** (UPDATED 11-12) ***
# (修改點) 增加 user_name 參數
# (重構) 改用 get_all_values
def handle_conversational_query_advice(trx_sheet, budget_sheet, text, user_id, user_name, event_time):
    """
    (新功能) 處理 "詢問建議" (例如 "我花太多嗎", "有什麼建議")
    (MODIFIED 11-12) 重構為 get_all_values
    """
    logger.debug(f"處理 '聊天式建議'，user_id: {user_id}, text: {text}")

    try:
        # === (NEW 11-12) 步驟 0: 效能優化 (主要修復點) ===
        # 1. 一次性讀取所有 GSheet 原始值
        logger.debug("Optimizing: 正在讀取所有交易紀錄 (get_all_values)...")
        all_values = trx_sheet.get_all_values()
        
        if not all_values or len(all_values) < 2:
            logger.warning("GSheet (Transactions) 為空或只有標頭")
            return "🦝 您的帳本還是空的，沒辦法給建議喔～"
            
        header = all_values[0]
        # (NEW) 建立標頭索引地圖
        header_map = {name: i for i, name in enumerate(header)}
        # (NEW) 傳遞 (標頭之後) 的所有資料
        trx_data_rows = all_values[1:]
        # ==========================================

        # 1. 取得本月資料 (使用你的輔助函式)
        this_month_date = event_time.date()
        # (MODIFIED 11-12) 傳入 (values, map, user_id, ...)
        this_month_data = get_spending_data_for_month(trx_data_rows, header_map, user_id, this_month_date.year, this_month_date.month)
        
        # 2. 取得上月資料
        last_month_end_date = this_month_date.replace(day=1) - timedelta(days=1)
        # (MODIFIED 11-12) 重複使用 (values, map)
        last_month_data = get_spending_data_for_month(trx_data_rows, header_map, user_id, last_month_end_date.year, last_month_end_date.month)
        
        this_month_total = this_month_data['total']
        last_month_total = last_month_data['total']
        
        # 3. 取得預算資料 (這個 OK，budget_sheet 通常很小)
        budgets_records = budget_sheet.get_all_records()
        user_budgets = [b for b in budgets_records if b.get('使用者ID') == user_id]
        total_limit = sum(float(b.get('限額', 0)) for b in user_budgets)
        
        # === (新) AI 分析 Prompt ===
        analysis_data = f"""
        - 使用者名稱：{user_name}
        - 詢問："{text}"
        - 本月 ({this_month_date.month}月) 目前支出：{this_month_total:.0f} 元
        - 上月 ({last_month_end_date.month}月) 總支出：{last_month_total:.0f} 元
        - 本月總預算：{total_limit:.0f} 元
        - 本月支出細項 (JSON)：{json.dumps(this_month_data['categories'])}
        """
        
        prompt_raw = """
        你是一個友善且專業的記帳分析師「小浣熊🦝」。
        請根據以下數據，用 "可愛且專業" 的口吻，回答使用者的問題。

        數據：
        $ANALYSIS_DATA

        請直接回覆分析結果 (不要說 "根據數據...")，口氣要像小浣熊：

        # === (NEW) (修改點) 解決 Bug 2 & 3 ===
        - (稱呼規則) 請用「{user_name}」來稱呼使用者，例如「哈囉～{user_name}！」，而不是用 ID。
        - (預算規則) 如果「本月總預算」為 0 元，請*不要*說「超出預期」，而應該說「您本月尚未設定總預算喔！」
        
        - 優先比較「本月支出」和「上月支出」，給出結論 (例如 "花費增加/減少了 X%")。
        - 接著比較「本月支出」和「本月總預算」，判斷是否在控制內 (請遵守 預算規則)。
        - 最後，從「本月支出細項」中找出花費*最多*的類別，並給予*具體*的建議。
        - 保持簡潔有力。
        """
        # (修改點) 傳入 user_name
        prompt = Template(prompt_raw).substitute(
            ANALYSIS_DATA=analysis_data,
            user_name=user_name 
        )
        
        # (MODIFIED) 
        # 經過優化後，現在執行到這裡會快很多
        # 程式有足夠的時間等待 Gemini 回應
        response = gemini_model.generate_content(prompt)
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        
        return clean_response

    except Exception as e:
        logger.error(f"聊天式建議失敗：{e}", exc_info=True)
        return f"糟糕！小浣熊分析時打結了：{str(e)}"

# === (MODIFIED 11-12) 
# 1. 移除 sheet, user_id 參數
# 2. 改為接收 (all_values, header_map, user_id, year, month)
def get_spending_data_for_month(all_trx_values, header_map, user_id, year, month):
    """
    (MODIFIED 11-12) 獲取特定年/月，某使用者的總支出和分類支出 (從傳入的 values list)
    """
    # 3. 更新 log
    logger.debug(f"輔助函式：分析 {year}-{month} 的資料 (從 {len(all_trx_values)} 筆記錄中)")
    month_str = f"{year}-{month:02d}"
    
    total_expense = 0.0
    category_spending = {}
    
    # 4. (NEW) 獲取欄位索引
    try:
        idx_uid = header_map['使用者ID']
        idx_time_new = header_map.get('日期', -1) # 新
        idx_time_old = header_map.get('時間', -1) # 舊
        idx_amount = header_map['金額']
        idx_cat = header_map['類別']
    except KeyError as e:
        logger.error(f"GSheet 標頭錯誤 (get_spending_data_for_month): {e}")
        return {"total": 0, "categories": {}}

    # 5. (MODIFIED) 
    for r in all_trx_values: # 傳進來的 all_trx_values 應該已經不含 header
        if len(r) <= max(idx_uid, idx_time_new, idx_time_old, idx_amount, idx_cat):
            continue # 跳過不完整的行
            
        # 6. (MODIFIED) 
        if r[idx_uid] != user_id:
            continue
            
        # (NEW) 優先讀取 '日期'，再讀取 '時間'
        record_time_str = ""
        if idx_time_new != -1 and len(r) > idx_time_new and r[idx_time_new]:
            record_time_str = r[idx_time_new]
        elif idx_time_old != -1 and len(r) > idx_time_old and r[idx_time_old]:
            record_time_str = r[idx_time_old]

        if record_time_str.startswith(month_str):
            try:
                amount = float(r[idx_amount])
                if amount < 0:
                    expense = abs(amount)
                    total_expense += expense
                    category = r[idx_cat] if r[idx_cat] else '雜項'
                    category_spending[category] = category_spending.get(category, 0) + expense
            except (ValueError, TypeError):
                continue
                
    return {"total": total_expense, "categories": category_spending}

# === *** (MODIFIED) 步驟三-A: 升級 `call_search_nlp` (修復 Bug #3) *** ===
# === *** (UPDATED 11-12 v4) *** ===
def call_search_nlp(query_text, event_time):
    """
    (升級) 呼叫 Gemini NLP 來解析 "查詢" 或 "刪除" 的條件
    (已升級，支援收入/支出 type 欄位)
    """
    today = event_time.date()
    today_str = today.strftime('%Y-%m-%d')
    yesterday_str = (today - timedelta(days=1)).strftime('%Y-%m-%d')
    
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    start_of_last_week = start_of_week - timedelta(days=7)
    end_of_last_week = start_of_week - timedelta(days=1)
    
    start_of_month = today.replace(day=1)
    
    last_month_end_date = start_of_month - timedelta(days=1)
    start_of_last_month = last_month_end_date.replace(day=1)

    date_context_lines = [
        f"今天是 {today_str} (星期{today.weekday()})。",
        f"昨天: {yesterday_str}",
        f"本週 (週一到週日): {start_of_week.strftime('%Y-%m-%d')} 到 {end_of_week.strftime('%Y-%m-%d')}",
        f"上週 (週一到週日): {start_of_last_week.strftime('%Y-%m-%d')} 到 {end_of_last_week.strftime('%Y-%m-%d')}",
        f"本月: {start_of_month.strftime('%Y-%m-%d')} 到 {today_str}",
        f"上個月: {start_of_last_month.strftime('%Y-%m-%d')} 到 {last_month_end_date.strftime('%Y-%m-%d')}",
    ]
    date_context = "\n".join(date_context_lines)

    # (v4 修改點)
    prompt_raw = """
    你是一個查詢/刪除的「條件解析器」。
    使用者的輸入是：「$QUERY_TEXT」

    目前的日期上下文：
    $DATE_CTX

    請依照下列規則回覆一段 JSON（不要輸出多餘文字與 Markdown 標記）：
    {
      "status": "success" | "failure",
      "keyword": "<若能抽出查詢關鍵字(例如 品項、類別)，填入字串；否則為空字串>",
      "start_date": "YYYY-MM-DD 或空字串",
      "end_date": "YYYY-MM-DD 或空字串",
      "type": "all" | "income" | "expense",
      "message": "<用一句話總結查詢條件>"
    }

    規則補充：
    - 你的任務是 "拆解" 條件，不是回答問題。
    - 如果只有時間 (例如 "今天", "這禮拜")，keyword 必須為空字串。
    - 如果只有關鍵字 (例如 "雞排")，日期必須為空字串。
    - 刪除的語句 (例如 "刪掉", "移除") *不是* 關鍵字，真正的關鍵字是 "品項"。
    - (新規則) 如果查詢包含 "收入" 或 "賺"，"type" 應為 "income"。
    - (新規則) 如果查詢包含 "支出" 或 "花費"，"type" 應為 "expense"。
    - (新規則) 如果兩者都沒有，"type" 應為 "all"。
    - (新規則) "收入" 和 "支出" *不應* 被當作 "keyword" (關鍵字)。
    - (v4 新規則) 如果關鍵字包含 "消費" 或 "金額"，請移除它們，只保留核心品項。例如 "信用卡消費" -> "信用卡"。

    範例：
    輸入: "查詢今天"
    輸出: {"status": "success", "keyword": "", "start_date": "$TODAY_STR", "end_date": "$TODAY_STR", "type": "all", "message": "今天"}

    輸入: "查詢這禮拜的餐飲"
    輸出: {"status": "success", "keyword": "餐飲", "start_date": "$START_OF_WEEK", "end_date": "$END_OF_WEEK", "type": "all", "message": "本週的 餐飲"}

    輸入: "查詢 雞排"
    輸出: {"status": "success", "keyword": "雞排", "start_date": "", "end_date": "", "type": "all", "message": "關於「雞排」"}
    
    輸入: "刪掉早上的草莓麵包"
    輸出: {"status": "success", "keyword": "草莓麵包", "start_date": "$TODAY_STR", "end_date": "$TODAY_STR", "type": "all", "message": "今天早上的「草莓麵包」"}
    
    # (FIX #3) 新增 type 範例
    輸入: "查詢昨日支出"
    輸出: {"status": "success", "keyword": "", "start_date": "$YESTERDAY_STR", "end_date": "$YESTERDAY_STR", "type": "expense", "message": "昨天的支出"}
    
    輸入: "查詢昨日收入"
    輸出: {"status": "success", "keyword": "", "start_date": "$YESTERDAY_STR", "end_date": "$YESTERDAY_STR", "type": "income", "message": "昨天的收入"}

    輸入: "查詢這禮拜的餐飲支出"
    輸出: {"status": "success", "keyword": "餐飲", "start_date": "$START_OF_WEEK", "end_date": "$END_OF_WEEK", "type": "expense", "message": "本週的 餐飲 支出"}

    # (v4 新增範例)
    輸入: "請問我11月信用卡消費金額"
    輸出: {"status": "success", "keyword": "信用卡", "start_date": "$START_OF_MONTH", "end_date": "$TODAY_STR", "type": "expense", "message": "本月的 信用卡 支出"}
    """
    
    prompt = Template(prompt_raw).substitute(
        QUERY_TEXT=query_text,
        DATE_CTX=date_context,
        TODAY_STR=today_str,
        YESTERDAY_STR=yesterday_str,
        START_OF_WEEK=start_of_week.strftime('%Y-%m-%d'),
        END_OF_WEEK=end_of_week.strftime('%Y-%m-%d'),
        START_OF_MONTH=start_of_month.strftime('%Y-%m-%d'), # (v4) 補上範例所需變數
    )

    try:
        logger.debug("發送 search prompt 至 Gemini API")
        response = gemini_model.generate_content(prompt)
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        logger.debug(f"Gemini Search response: {clean_response}")
        
        parsed_query = json.loads(clean_response)
        return parsed_query
        
    except json.JSONDecodeError as e:
        logger.error(f"Gemini Search JSON 解析失敗: {clean_response}")
        return {"status": "failure", "message": f"AI 分析器 JSON 解析失敗: {e}"}
    except Exception as e:
        logger.error(f"Gemini Search API 呼叫失敗：{e}", exc_info=True)
        return {"status": "failure", "message": f"AI 分析器 API 呼叫失敗: {e}"}
    
# === (NEW) `handle_update_record_nlp` (佔位) ===
def handle_update_record_nlp(sheet, user_id, text, event_time):
    """
    (新功能) 處理 "修改" 意圖
    """
    logger.debug(f"處理 'NLP 修改'，user_id: {user_id}, text: {text}")
    return "🦝 哎呀！小浣熊還在學習如何「修改」記錄... 😅\n\n目前這個功能還在開發中。您可以先使用「刪除」指令 (例如 '刪除 香蕉')，然後再重新記一筆喔！"

# === (NEW) `handle_exchange_rate_query` (佔位) ===
def handle_exchange_rate_query(text):
    """
    (新功能) 處理匯率查詢
    """
    logger.debug(f"處理 '匯率查詢'，text: {text}")
    return "🦝 匯率查詢... 嗎？\n小浣熊還在學習如何連接到銀行... 🏦\n這個功能未來會開放喔！敬請期待！"

# === 主程式入口 ===
if __name__ == "__main__":
    logger.info("Starting Flask server locally...")
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
