import os
import logging
import re
import json
import gspread
import google.generativeai as genai
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks.models import MessageEvent, TextMessageContent
from linebot.v3.messaging import TextMessage, MessagingApi
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from dotenv import load_dotenv # 匯入 dotenv

# === 步驟 1：載入環境變數 ===
# 這行程式碼會自動去讀取 .env 檔案
load_dotenv()

# === 步驟 2：從環境變數讀取金鑰 ===
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 從環境變數讀取設定 (並提供預設值)
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", 'service_account.json')
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", '記帳小浣熊資料庫')
# === 金鑰配置結束 ===


# === 初始化 Flask 應用程式 ===
app = Flask(__name__)

# === 配置日誌 ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === 步驟 3：驗證金鑰是否已載入 ===
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY]):
    logger.error("!!! 關鍵金鑰載入失敗 !!!")
    logger.error("請檢查：")
    logger.error("1. 專案資料夾中是否有 .env 檔案？")
    logger.error("2. .env 檔案中是否正確填寫了 LINE_... 和 GEMINI_...？")
    # 如果金鑰不存在，程式在這裡停止會更安全
    # raise ValueError("金鑰未配置，請檢查 .env 檔案")
else:
    logger.info("所有金鑰已成功從 .env 載入。")

# === 配置 LINE 與 Gemini API 客戶端 ===
try:
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    messaging_api = MessagingApi(LINE_CHANNEL_ACCESS_TOKEN)
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    logger.error(f"API 客戶端初始化失敗: {e}")
    # 這通常是因為金鑰格式錯誤或為 None
    raise

# === Google Sheets 初始化 ===
import json
from google.oauth2 import service_account

def get_sheets_workbook():
    """
    初始化 Google Sheets 客戶端並返回工作簿 (Workbook) 物件
    支援兩種模式：
      1. 本地端：使用實體 service_account.json 檔案
      2. Render 雲端：使用環境變數 GOOGLE_CREDENTIALS
    """
    logger.info("正在初始化 Google Sheets 憑證...")

    try:
        # 1️⃣ Render 模式：若環境中有 GOOGLE_CREDENTIALS，就從中讀取 JSON
        if "GOOGLE_CREDENTIALS" in os.environ:
            logger.info("使用環境變數 GOOGLE_CREDENTIALS 建立憑證。")
            creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
            creds = service_account.Credentials.from_service_account_info(
                creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            # 2️⃣ 本地開發模式：從 service_account.json 讀取
            SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service_account.json")
            logger.info(f"使用本地檔案 {SERVICE_ACCOUNT_FILE} 建立憑證。")
            creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )

        # 建立 gspread 客戶端
        client = gspread.authorize(creds)
        sheet_name = os.getenv("GOOGLE_SHEET_NAME", "記帳小浣熊資料庫")
        logger.info(f"成功授權，正在開啟試算表：{sheet_name}")
        return client.open(sheet_name)

    except FileNotFoundError:
        logger.error("找不到 service_account.json 檔案，請確認檔案位置或設定 GOOGLE_CREDENTIALS。")
        return None
    except json.JSONDecodeError:
        logger.error("GOOGLE_CREDENTIALS JSON 格式錯誤，請檢查環境變數內容。")
        return None
    except Exception as e:
        logger.error(f"Google Sheets 初始化失敗：{e}", exc_info=True)
        return None


def get_user_profile_name(user_id):
    """
    使用 LINE API 獲取使用者名稱
    """
    try:
        # 呼叫 LINE API 取得用戶資料
        profile = messaging_api.get_profile(user_id)
        return profile.display_name
    except Exception as e:
        logger.error(f"Failed to get user profile for {user_id}: {e}")
        return "未知用戶" # 如果失敗，給一個預設名稱

# === Webhook 處理 (LINE 訊息的入口) ===
@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    logger.info("Received webhook request")
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature. Check your LINE_CHANNEL_SECRET.")
        abort(400)
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return 'Internal Server Error', 500
    
    return 'OK' # 必須回傳 'OK' 讓 LINE 知道你收到了

# === 訊息總機 (核心邏輯) ===
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    
    # 獲取 LINE 傳來的時間戳 (毫秒)，並轉換為 datetime 物件
    line_timestamp_ms = event.timestamp
    event_time = datetime.fromtimestamp(line_timestamp_ms / 1000.0)
    
    logger.info(f"Received message: '{text}' from user '{user_id}'")

    reply_text = "🦝？我不太明白您的意思，請輸入「幫助」來查看指令。"
    
    # === 獲取 Google Sheets 工作簿 ===
    workbook = get_sheets_workbook()
    if not workbook:
        # 如果連不上 Google Sheet，直接回覆錯誤訊息
        reply_text = "糟糕！小浣熊的帳本(Google Sheet)連接失敗了 😵 請檢查 'service_account.json' 憑證。."
        messaging_api.reply_message(reply_token=reply_token, messages=[TextMessage(text=reply_text)])
        return

    try:
        # 嘗試獲取兩個工作表
        trx_sheet = workbook.worksheet('Transactions')
        budget_sheet = workbook.worksheet('Budgets')
    except gspread.exceptions.WorksheetNotFound as e:
        logger.error(f"找不到工作表: {e}")
        reply_text = "糟糕！找不到 'Transactions' 或 'Budgets' 工作表，請檢查你的 Google Sheet 設定。"
        messaging_api.reply_message(reply_token=reply_token, messages=[TextMessage(text=reply_text)])
        return
        
    # === 指令路由器 (Router) ===
    try:
        if text == "幫助":
            reply_text = (
                "📌 **記帳小浣熊使用說明🦝**：\n\n"
                "💸 **自然記帳** (AI會幫你分析)：\n"
                "   - 「今天中午吃了雞排80」\n"
                "   - 「昨天喝飲料 50」\n"
                "   - 「上禮拜三收入 1000 獎金」\n"
                "   - 「5/10 交通費 120」\n\n"
                "📊 **查帳**：\n"
                "   - 「查帳」：查看總支出、收入和淨餘額\n\n"
                "📅 **月結**：\n"
                "   - 「月結」：分析這個月的收支總結\n\n"
                "🗑️ **刪除**：\n"
                "   - 「刪除」：移除您最近一筆記錄\n\n"
                "💡 **預算**：\n"
                "   - 「設置預算 餐飲 3000」\n"
                "   - 「查看預算」：檢查本月預算使用情況"
            )
            
        elif text == "查帳":
            # 呼叫「查帳」功能
            reply_text = handle_check_balance(trx_sheet, user_id)
            
        elif text == "月結":
            # 呼叫「月結」功能，傳入當前時間
            reply_text = handle_monthly_report(trx_sheet, user_id, event_time)
            
        elif text == "刪除":
            # 呼叫「刪除」功能
            reply_text = handle_delete_record(trx_sheet, user_id)
            
        elif text.startswith("設置預算"):
            # 呼叫「設置預算」功能
            reply_text = handle_set_budget(budget_sheet, text, user_id)
            
        elif text == "查看預算":
            # 呼叫「查看預算」功能
            reply_text = handle_view_budget(trx_sheet, budget_sheet, user_id, event_time)
            
        else:
            # === 自然語言記帳 (NLP) ===
            # 如果以上指令都不是，就交給 AI 處理
            logger.info(f"Passing to NLP handler: '{text}'")
            # 取得使用者名稱，用於存入資料庫
            user_name = get_user_profile_name(user_id)
            reply_text = handle_nlp_record(trx_sheet, text, user_id, user_name, event_time)

    except Exception as e:
        # 捕捉所有功能執行中的錯誤，避免 LINE 500 錯誤
        logger.error(f"Error handling function for text '{text}': {e}", exc_info=True)
        reply_text = "糟糕！小浣熊處理您的指令時出錯了 😥"

    # === 最終回覆 ===
    if not isinstance(reply_text, str):
        reply_text = str(reply_text) # 確保回覆是字串

    logger.info(f"Final Reply:\n{reply_text}")
    try:
        # 使用 LINE API 回覆訊息
        messaging_api.reply_message(
            reply_token=reply_token,
            messages=[TextMessage(text=reply_text)]
        )
    except Exception as e:
        logger.error(f"Error replying message: {e}", exc_info=True)


# === 核心功能函式 (Helper Functions) ===

def handle_nlp_record(sheet, text, user_id, user_name, event_time):
    """
    使用 Gemini API 解析自然語言並記帳
    """
    
    # 提供 AI 足夠的日期上下文 (Context)
    today = event_time.date()
    today_str = today.strftime('%Y-%m-%d')
    
    # 準備一個日期參考表
    date_context_lines = [
        f"今天是 {today_str} (星期{today.weekday()})。",
        "日期參考：",
        f"- 昨天: {(today - timedelta(days=1)).strftime('%Y-%m-%d')}"
    ]
    # 加上 "上週" 的參考
    for i in range(1, 8):
        day = today - timedelta(days=i)
        if day.weekday() == 0: date_context_lines.append(f"- 上週一: {day.strftime('%Y-%m-%d')}")
        if day.weekday() == 2: date_context_lines.append(f"- 上週三: {day.strftime('%Y-%m-%d')}")
        if day.weekday() == 4: date_context_lines.append(f"- 上週五: {day.strftime('%Y-%m-%d')}")

    date_context = "\n".join(date_context_lines)
    
    # 這是最關鍵的 Prompt (AI 指示稿)
    prompt = f"""
    你是一個記帳機器人的 AI 助手。
    使用者的輸入是：「{text}」
    
    目前的日期上下文如下：
    {date_context}

    請嚴格按照以下 JSON 格式回傳，不要有任何其他文字或 "```json" 標記：
    {{
      "status": "success" | "failure" | "chat",
      "data": {{
        "date": "YYYY-MM-DD",
        "category": "餐飲" | "飲料" | "交通" | "娛樂" | "購物" | "雜項" | "收入",
        "amount": <number>,
        "notes": "<string>"
      }} | null,
      "message": "<string>"
    }}

    解析規則：
    1.  如果成功解析為記帳：
        - status: "success"
        - date: 必須是 YYYY-MM-DD 格式。如果沒提日期，預設為今天 ({today_str})。
        - category: 必須是 [餐飲, 飲料, 交通, 娛樂, 購物, 雜項, 收入] 之一。
        - amount: 必須是數字。如果是「收入」，必須為正數 (+)。如果是「支出」(吃、喝、買等)，必須為負數 (-)。
        - notes: 盡可能擷取出花費的項目，例如「雞排」。
    2.  如果使用者只是在閒聊 (例如 "你好", "你是誰", "謝謝")：
        - status: "chat"
        - data: null
        - message: (請用「記帳小浣熊🦝」的語氣友善回覆)
    3.  如果看起來像記帳，但缺少關鍵資訊 (例如 "我吃了東西" 或 "雞排" (沒說金額))：
        - status: "failure"
        - data: null
        - message: "🦝？我不太確定... 麻煩請提供日期和金額喔！"
    
    範例：
    輸入: "今天中午吃了雞排80" -> {{"status": "success", "data": {{"date": "{today_str}", "category": "餐飲", "amount": -80, "notes": "雞排"}}, "message": "記錄成功"}}
    輸入: "昨天 收入 1000" -> {{"status": "success", "data": {{"date": "{(today - timedelta(days=1)).strftime('%Y-%m-%d')}", "category": "收入", "amount": 1000, "notes": "收入"}}, "message": "記錄成功"}}
    輸入: "你好" -> {{"status": "chat", "data": null, "message": "你好！我是記帳小浣熊🦝 需要幫忙記帳嗎？"}}
    """
    
    try:
        logger.info("Sending prompt to Gemini...")
        response = gemini_model.generate_content(prompt)
        # 移除 Gemini 可能回傳的 markdown 標記
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        
        logger.info(f"Gemini NLP response: {clean_response}")
        
        # 解析 AI 回傳的 JSON
        data = json.loads(clean_response)

        status = data.get('status')
        message = data.get('message')

        if status == 'success':
            record = data.get('data', {})
            date = record.get('date', today_str)
            category = record.get('category', '雜項')
            amount = record.get('amount', 0)
            notes = record.get('notes', text) # 如果沒擷取出 note，預設為原始訊息
            
            if amount == 0:
                return "🦝？ 金額不能是 0 喔！"

            # 寫入 Google Sheet
            # 欄位：[日期, 類別, 金額, 使用者ID, 使用者名稱, 備註]
            sheet.append_row([date, category, amount, user_id, user_name, notes])
            logger.info("Successfully appended row to Google Sheet.")

            # 查詢目前總餘額
            all_records = sheet.get_all_records()
            user_balance = sum(float(r.get('金額', 0)) for r in all_records if r.get('使用者ID') == user_id and isinstance(r.get('金額', 0), (int, float, str)) and str(r.get('金額', 0)).replace('.', '', 1).replace('-', '', 1).isdigit())

            return f"✅ 已記錄：{date}\n{notes} ({category}) {abs(amount)} 元\n📈 {user_name} 的目前總餘額：{user_balance} 元"

        elif status == 'chat':
            # AI 判斷為閒聊
            return message or "你好！我是記帳小浣熊 🦝"
        
        else: # failure or other status
            # AI 判斷為失敗
            return message or "🦝？ 抱歉，我聽不懂..."

    except json.JSONDecodeError:
        logger.error(f"Gemini NLP JSON 解析失敗: {clean_response}")
        return "糟糕！AI 分析器暫時罷工了 (JSON解析失敗)... 請稍後再試。"
    except Exception as e:
        logger.error(f"Gemini API 呼叫或 GSheet 寫入失敗：{e}", exc_info=True)
        return "目前我無法處理這個請求，請稍後再試。"


def handle_check_balance(sheet, user_id):
    """
    查帳：計算指定使用者的總收入、總支出、淨餘額
    """
    logger.info(f"Handling '查帳' for user {user_id}")
    try:
        records = sheet.get_all_records()
        
        # 篩選出這位使用者的所有記錄
        user_records = [r for r in records if r.get('使用者ID') == user_id]
        
        if not user_records:
            return "您目前沒有任何記帳記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        
        for r in user_records:
            amount_str = r.get('金額')
            try:
                # 確保金額是數字
                amount = float(amount_str)
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += amount
            except (ValueError, TypeError):
                logger.warning(f"Skipping invalid amount '{amount_str}' in sheet for user {user_id}")
                continue # 跳過無效的金額

        total_balance = total_income + total_expense
        
        return (
            f"📊 **您的財務總覽**：\n\n"
            f"💰 總收入：{total_income} 元\n"
            f"💸 總支出：{abs(total_expense)} 元\n"
            f"--------------------\n"
            f"📈 淨餘額：{total_balance} 元"
        )
    except Exception as e:
        logger.error(f"查帳失敗：{e}", exc_info=True)
        return "查帳失敗：無法讀取試算表。"

def handle_monthly_report(sheet, user_id, event_time):
    """
    月結：分析指定使用者「當月」的收支
    """
    logger.info(f"Handling '月結' for user {user_id}")
    try:
        records = sheet.get_all_records()
        
        # 獲取當前的 "YYYY-MM" 字串
        current_month_str = event_time.strftime('%Y-%m')
        
        # 篩選出 "這個使用者" 且 "這個月" 的記錄
        user_month_records = [
            r for r in records 
            if r.get('使用者ID') == user_id 
            and r.get('日期', '').startswith(current_month_str)
        ]
        
        if not user_month_records:
            return f"📅 {current_month_str} 月報表：\n您這個月還沒有任何記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        category_spending = {} # 用來統計各類別花費

        for r in user_month_records:
            amount_str = r.get('金額')
            try:
                amount = float(amount_str)
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += amount
                    # 統計支出類別
                    category = r.get('類別', '雜項')
                    category_spending[category] = category_spending.get(category, 0) + abs(amount)
            except (ValueError, TypeError):
                continue # 跳過無效金額

        reply = f"📅 **{current_month_str} 月結報表**：\n\n"
        reply += f"💰 本月收入：{total_income} 元\n"
        reply += f"💸 本月支出：{abs(total_expense)} 元\n"
        reply += f"📈 本月淨利：{total_income + total_expense} 元\n"
        
        if category_spending:
            reply += "\n--- 支出分析 (花費最多) ---\n"
            # 排序：從花費最多的開始
            sorted_spending = sorted(category_spending.items(), key=lambda item: item[1], reverse=True)
            
            for i, (category, amount) in enumerate(sorted_spending):
                icon = ["🥇", "🥈", "🥉"]
                prefix = icon[i] if i < 3 else "🔹"
                reply += f"{prefix} {category}: {amount} 元\n"
        
        return reply

    except Exception as e:
        logger.error(f"月結失敗：{e}", exc_info=True)
        return "月結報表產生失敗。"


def handle_delete_record(sheet, user_id):
    """
    刪除：刪除指定 user_id 的「最後一筆」記錄
    """
    logger.info(f"Handling '刪除' for user {user_id}")
    try:
        # 使用 get_all_values() 效能較好，且能拿到準確的列號
        all_values = sheet.get_all_values() # 包含標題
        
        # 假設 '使用者ID' 在 D 欄 (index 3)
        user_id_col_index = 3 
        
        # 從最後一列 (len(all_values) - 1) 往回找到 1 (略過標題 0)
        for row_index in range(len(all_values) - 1, 0, -1):
            row = all_values[row_index]
            if len(row) > user_id_col_index and row[user_id_col_index] == user_id:
                # 找到了！
                row_to_delete = row_index + 1 # GSpread 列號是 1-based
                
                # 刪除那一列
                sheet.delete_rows(row_to_delete)
                
                # 組合被刪除的訊息
                # 假設 0:日期, 1:類別, 2:金額
                deleted_desc = f"{row[0]} {row[1]} {row[2]} 元"
                return f"🗑️ 已刪除：{deleted_desc}"
        
        # 如果迴圈跑完都沒找到
        return "找不到您的記帳記錄可供刪除。"
            
    except Exception as e:
        logger.error(f"刪除失敗：{e}", exc_info=True)
        return "刪除記錄失敗。"

def handle_set_budget(sheet, text, user_id):
    """
    設置預算：寫入 'Budgets' 工作表
    """
    logger.info(f"Handling '設置預算' for user {user_id}")
    # 使用正規表示法解析 "設置預算 [類別] [金額]"
    match = re.match(r'設置預算\s+([\u4e00-\u9fa5]+)\s+(\d+)', text)
    if not match:
        return "格式錯誤！請輸入「設置預算 [類別] [限額]」，例如：「設置預算 餐飲 3000」"
    
    category = match.group(1).strip()
    limit = int(match.group(2))
    
    valid_categories = ['餐飲', '飲料', '交通', '娛樂', '購物', '雜項']
    if category not in valid_categories:
        return f"無效類別，請使用：{', '.join(valid_categories)}"

    try:
        # 尋找是否已存在該用戶的該類別預算
        # '使用者ID' 在 A 欄, '類別' 在 B 欄
        cell_list = sheet.findall(user_id) # 找到所有 user_id 的儲存格
        found_row = -1
        
        for cell in cell_list:
            # 檢查同一列的 B 欄 (index 1) 是否為我們要的類別
            row_values = sheet.row_values(cell.row)
            if len(row_values) > 1 and row_values[1] == category:
                found_row = cell.row
                break
        
        if found_row != -1:
            # 找到了，更新 C 欄 (index 3) 的限額
            sheet.update_cell(found_row, 3, limit)
            return f"✅ 已更新預算：{category} {limit} 元"
        else:
            # 沒找到，新增一列 [使用者ID, 類別, 限額]
            sheet.append_row([user_id, category, limit])
            return f"✅ 已設置預算：{category} {limit} 元"
        
    except Exception as e:
        logger.error(f"設置預算失敗：{e}", exc_info=True)
        return "設置預算失敗。"


def handle_view_budget(trx_sheet, budget_sheet, user_id, event_time):
    """
    查看預算：讀取 'Budgets' 並對比 'Transactions' 的當月花費
    """
    logger.info(f"Handling '查看預算' for user {user_id}")
    try:
        # 1. 獲取使用者的所有預算設定
        budgets_records = budget_sheet.get_all_records()
        user_budgets = [b for b in budgets_records if b.get('使用者ID') == user_id]
        
        if not user_budgets:
            return "您尚未設置任何預算。請輸入「設置預算 [類別] [限額]」"

        # 2. 獲取 "這個使用者" 且 "這個月" 的 "所有支出"
        transactions_records = trx_sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        
        user_month_expenses = []
        for r in transactions_records:
            try:
                amount = float(r.get('金額', 0))
                if (r.get('使用者ID') == user_id and
                    r.get('日期', '').startswith(current_month_str) and
                    amount < 0):
                    user_month_expenses.append(r)
            except (ValueError, TypeError):
                continue # 忽略無效金額

        reply = f"📊 **{current_month_str} 預算狀態**：\n"
        total_spent = 0.0
        total_limit = 0.0
        
        # 3. 逐一計算每個預算類別
        for budget in user_budgets:
            category = budget.get('類別')
            limit = float(budget.get('限額', 0))
            if limit <= 0: continue # 跳過無效預算
                
            total_limit += limit
            
            # 計算該類別的當月花費
            spent = sum(abs(float(r.get('金額', 0))) for r in user_month_expenses if r.get('類別') == category)
            total_spent += spent
            
            remaining = limit - spent
            
            # 製作進度條 (簡易版)
            percentage = (spent / limit) * 100
            bar_fill = '■' * int(percentage / 10)
            bar_empty = '□' * (10 - int(percentage / 10))
            if percentage > 100: # 爆表
                 bar_fill = '■' * 10
                 bar_empty = ''
                 
            status_icon = "🟢" if remaining >= 0 else "🔴"

            reply += f"\n{category} (限額 {limit} 元)\n"
            reply += f"   {status_icon} 已花費：{spent} 元\n"
            reply += f"   [{bar_fill}{bar_empty}] {percentage:.0f}%\n"
            reply += f"   剩餘：{remaining} 元\n"

        # 4. 總結
        reply += "\n--------------------\n"
        if total_limit > 0:
            total_remaining = total_limit - total_spent
            total_percentage = (total_spent / total_limit) * 100
            status_icon = "🟢" if total_remaining >= 0 else "🔴"
            
            reply += f"總預算： {total_limit} 元\n"
            reply += f"總花費： {total_spent} 元\n"
            reply += f"{status_icon} 總剩餘： {total_remaining} 元 ({total_percentage:.0f}%)"
        else:
            reply += "總預算尚未設定或設定為 0。"
        
        return reply

    except Exception as e:
        logger.error(f"查看預算失敗：{e}", exc_info=True)
        return "查看預算失敗。"


# === 主程式入口 ===
if __name__ == "__main__":
    # 啟動 Flask 伺服器
    # 你需要一個 ngrok 這樣的工具把這個網址暴露給 LINE
    logger.info("Starting Flask server...")
    port = int(os.getenv('PORT', 5000))
    # 在本地開發時，debug=True 很好用
    # 但在部署到 Google Cloud Run 時，請確保 debug=False
    app.run(host='0.0.0.0', port=port, debug=True)