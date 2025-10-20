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
from linebot.v3.messaging import TextMessage, MessagingApi, ReplyMessageRequest
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from dotenv import load_dotenv

# === 步驟 1：載入環境變數 ===
load_dotenv()

# === 步驟 2：從環境變數讀取金鑰 ===
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", '記帳小浣熊資料庫')

# === 配置日誌 ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === 步驟 3：驗證金鑰是否已載入 ===
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY]):
    logger.error("!!! 關鍵金鑰載入失敗 !!!")
    logger.error("請檢查：")
    logger.error("1. 專案資料夾中是否有 .env 檔案？")
    logger.error("2. .env 檔案中是否正確填寫了 LINE_... 和 GEMINI_...？")
    raise ValueError("金鑰未配置，請檢查 .env 檔案")
else:
    logger.info("所有金鑰已成功從 .env 載入。")
    logger.info(f"LINE_CHANNEL_ACCESS_TOKEN (前10字): {LINE_CHANNEL_ACCESS_TOKEN[:10]}...")
    logger.info(f"GOOGLE_SHEET_NAME: {GOOGLE_SHEET_NAME}")

# === 初始化 Flask 應用程式 ===
app = Flask(__name__)
logger.info("Flask application initialized successfully.")

# === 配置 LINE 與 Gemini API 客戶端 ===
try:
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    messaging_api = MessagingApi(LINE_CHANNEL_ACCESS_TOKEN)
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    # 驗證 messaging_api 初始化
    if not isinstance(messaging_api, MessagingApi):
        logger.error(f"MessagingApi 初始化失敗，LINE_CHANNEL_ACCESS_TOKEN: {LINE_CHANNEL_ACCESS_TOKEN[:10]}...")
        raise ValueError("MessagingApi 初始化失敗，可能是 LINE_CHANNEL_ACCESS_TOKEN 無效")
except Exception as e:
    logger.error(f"API 客戶端初始化失敗: {e}", exc_info=True)
    raise

# === Google Sheets 初始化 ===
def get_sheets_workbook():
    """
    初始化 Google Sheets 客戶端並返回工作簿 (Workbook) 物件
    優先使用環境變數 GOOGLE_CREDENTIALS，適配 Render 雲端環境
    """
    logger.info("正在初始化 Google Sheets 憑證...")
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if "GOOGLE_CREDENTIALS" in os.environ:
            logger.info("使用環境變數 GOOGLE_CREDENTIALS 建立憑證。")
            creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
            logger.info(f"GOOGLE_CREDENTIALS project_id: {creds_info.get('project_id', '未找到')}")
            creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        else:
            logger.info("GOOGLE_CREDENTIALS 未設置，嘗試使用本地檔案 service_account.json")
            creds = Credentials.from_service_account_file(
                "service_account.json", scopes=scopes
            )
        
        client = gspread.authorize(creds)
        sheet_name = os.getenv("GOOGLE_SHEET_NAME", "記帳小浣熊資料庫")
        logger.info(f"成功授權，正在開啟試算表：{sheet_name}")
        return client.open(sheet_name)

    except json.JSONDecodeError as e:
        logger.error(f"GOOGLE_CREDENTIALS JSON 格式錯誤：{e}")
        logger.error(f"GOOGLE_CREDENTIALS 內容（前50字）：{os.getenv('GOOGLE_CREDENTIALS')[:50]}...")
        return None
    except FileNotFoundError:
        logger.error("找不到 service_account.json 檔案，且未設置 GOOGLE_CREDENTIALS。")
        return None
    except gspread.exceptions.APIError as e:
        logger.error(f"Google Sheets API 錯誤：{e}")
        return None
    except Exception as e:
        logger.error(f"Google Sheets 初始化失敗：{e}", exc_info=True)
        return None

def ensure_worksheets(workbook):
    """
    確保 Google Sheet 中存在 Transactions 和 Budgets 工作表，若不存在則創建
    """
    try:
        # 檢查 Transactions 工作表
        try:
            trx_sheet = workbook.worksheet('Transactions')
        except gspread.exceptions.WorksheetNotFound:
            logger.info("未找到 Transactions 工作表，正在創建...")
            trx_sheet = workbook.add_worksheet(title='Transactions', rows=1000, cols=10)
            # 設置標頭
            trx_sheet.append_row(['日期', '類別', '金額', '使用者ID', '使用者名稱', '備註'])
            logger.info("Transactions 工作表創建成功")

        # 檢查 Budgets 工作表
        try:
            budget_sheet = workbook.worksheet('Budgets')
        except gspread.exceptions.WorksheetNotFound:
            logger.info("未找到 Budgets 工作表，正在創建...")
            budget_sheet = workbook.add_worksheet(title='Budgets', rows=100, cols=5)
            # 設置標頭
            budget_sheet.append_row(['使用者ID', '類別', '限額'])
            logger.info("Budgets 工作表創建成功")

        return trx_sheet, budget_sheet

    except Exception as e:
        logger.error(f"創建或檢查工作表失敗：{e}", exc_info=True)
        return None, None

def get_user_profile_name(user_id):
    try:
        profile = messaging_api.get_profile(user_id)
        return profile.display_name
    except Exception as e:
        logger.error(f"無法獲取使用者 {user_id} 的個人資料：{e}")
        return "未知用戶"

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
    
    return 'OK'

# === 訊息總機 (核心邏輯) ===
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    
    line_timestamp_ms = event.timestamp
    event_time = datetime.fromtimestamp(line_timestamp_ms / 1000.0)
    
    logger.info(f"Received message: '{text}' from user '{user_id}'")

    reply_text = "🦝？我不太明白您的意思，請輸入「幫助」來查看指令。"
    
    # === 獲取 Google Sheets 工作簿 ===
    workbook = get_sheets_workbook()
    if not workbook:
        reply_text = "糟糕！小浣熊的帳本(Google Sheet)連接失敗了 😵 請檢查憑證設置或 Google Sheets API 權限。"
        try:
            messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            logger.info("Reply sent successfully")
        except Exception as e:
            logger.error(f"回覆訊息失敗：{e}", exc_info=True)
        return

    # === 確保工作表存在 ===
    trx_sheet, budget_sheet = ensure_worksheets(workbook)
    if not trx_sheet or not budget_sheet:
        reply_text = "糟糕！無法創建或存取 'Transactions' 或 'Budgets' 工作表，請檢查 Google Sheet 設定。"
        try:
            messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            logger.info("Reply sent successfully")
        except Exception as e:
            logger.error(f"回覆訊息失敗：{e}", exc_info=True)
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
            reply_text = handle_check_balance(trx_sheet, user_id)
            
        elif text == "月結":
            reply_text = handle_monthly_report(trx_sheet, user_id, event_time)
            
        elif text == "刪除":
            reply_text = handle_delete_record(trx_sheet, user_id)
            
        elif text.startswith("設置預算"):
            reply_text = handle_set_budget(budget_sheet, text, user_id)
            
        elif text == "查看預算":
            reply_text = handle_view_budget(trx_sheet, budget_sheet, user_id, event_time)
            
        else:
            user_name = get_user_profile_name(user_id)
            reply_text = handle_nlp_record(trx_sheet, text, user_id, user_name, event_time)

    except Exception as e:
        logger.error(f"處理指令 '{text}' 失敗：{e}", exc_info=True)
        reply_text = "糟糕！小浣熊處理您的指令時出錯了 😥"

    # === 最終回覆 ===
    if not isinstance(reply_text, str):
        reply_text = str(reply_text)

    logger.info(f"Final Reply:\n{reply_text}")
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )
        logger.info("Reply sent successfully")
    except Exception as e:
        logger.error(f"回覆訊息失敗：{e}", exc_info=True)

# === 核心功能函式 (Helper Functions) ===
def handle_nlp_record(sheet, text, user_id, user_name, event_time):
    today = event_time.date()
    today_str = today.strftime('%Y-%m-%d')
    
    date_context_lines = [
        f"今天是 {today_str} (星期{today.weekday()})。",
        "日期參考：",
        f"- 昨天: {(today - timedelta(days=1)).strftime('%Y-%m-%d')}"
    ]
    for i in range(1, 8):
        day = today - timedelta(days=i)
        if day.weekday() == 0: date_context_lines.append(f"- 上週一: {day.strftime('%Y-%m-%d')}")
        if day.weekday() == 2: date_context_lines.append(f"- 上週三: {day.strftime('%Y-%m-%d')}")
        if day.weekday() == 4: date_context_lines.append(f"- 上週五: {day.strftime('%Y-%m-%d')}")

    date_context = "\n".join(date_context_lines)
    
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
    1. 如果成功解析為記帳：
        - status: "success"
        - date: 必須是 YYYY-MM-DD 格式。如果沒提日期，預設為今天 ({today_str})。
        - category: 必須是 [餐飲, 飲料, 交通, 娛樂, 購物, 雜項, 收入] 之一。
        - amount: 必須是數字。如果是「收入」，必須為正數 (+)。如果是「支出」(吃、喝、買等)，必須為負數 (-)。
        - notes: 盡可能擷取出花費的項目，例如「雞排」。
    2. 如果使用者只是在閒聊 (例如 "你好", "你是誰", "謝謝")：
        - status: "chat"
        - data: null
        - message: (請用「記帳小浣熊🦝」的語氣友善回覆)
    3. 如果看起來像記帳，但缺少關鍵資訊 (例如 "我吃了東西" 或 "雞排" (沒說金額))：
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
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        
        logger.info(f"Gemini NLP response: {clean_response}")
        
        data = json.loads(clean_response)
        status = data.get('status')
        message = data.get('message')

        if status == 'success':
            record = data.get('data', {})
            date = record.get('date', today_str)
            category = record.get('category', '雜項')
            amount = record.get('amount', 0)
            notes = record.get('notes', text)
            
            if amount == 0:
                return "🦝？ 金額不能是 0 喔！"

            sheet.append_row([date, category, amount, user_id, user_name, notes])
            logger.info("Successfully appended row to Google Sheet.")

            all_records = sheet.get_all_records()
            user_balance = sum(float(r.get('金額', 0)) for r in all_records if r.get('使用者ID') == user_id and isinstance(r.get('金額', 0), (int, float, str)) and str(r.get('金額', 0)).replace('.', '', 1).replace('-', '', 1).isdigit())

            return f"✅ 已記錄：{date}\n{notes} ({category}) {abs(amount)} 元\n📈 {user_name} 的目前總餘額：{user_balance} 元"

        elif status == 'chat':
            return message or "你好！我是記帳小浣熊 🦝"
        
        else:
            return message or "🦝？ 抱歉，我聽不懂..."

    except json.JSONDecodeError:
        logger.error(f"Gemini NLP JSON 解析失敗: {clean_response}")
        return "糟糕！AI 分析器暫時罷工了 (JSON解析失敗)... 請稍後再試。"
    except Exception as e:
        logger.error(f"Gemini API 呼叫或 GSheet 寫入失敗：{e}", exc_info=True)
        return "目前我無法處理這個請求，請稍後再試。"

def handle_check_balance(sheet, user_id):
    logger.info(f"Handling '查帳' for user {user_id}")
    try:
        records = sheet.get_all_records()
        user_records = [r for r in records if r.get('使用者ID') == user_id]
        
        if not user_records:
            return "您目前沒有任何記帳記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        
        for r in user_records:
            amount_str = r.get('金額')
            try:
                amount = float(amount_str)
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += amount
            except (ValueError, TypeError):
                logger.warning(f"Skipping invalid amount '{amount_str}' in sheet for user {user_id}")
                continue

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
    logger.info(f"Handling '月結' for user {user_id}")
    try:
        records = sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        user_month_records = [
            r for r in records 
            if r.get('使用者ID') == user_id 
            and r.get('日期', '').startswith(current_month_str)
        ]
        
        if not user_month_records:
            return f"📅 {current_month_str} 月報表：\n您這個月還沒有任何記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        category_spending = {}

        for r in user_month_records:
            amount_str = r.get('金額')
            try:
                amount = float(amount_str)
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += amount
                    category = r.get('類別', '雜項')
                    category_spending[category] = category_spending.get(category, 0) + abs(amount)
            except (ValueError, TypeError):
                continue

        reply = f"📅 **{current_month_str} 月結報表**：\n\n"
        reply += f"💰 本月收入：{total_income} 元\n"
        reply += f"💸 本月支出：{abs(total_expense)} 元\n"
        reply += f"📈 本月淨利：{total_income + total_expense} 元\n"
        
        if category_spending:
            reply += "\n--- 支出分析 (花費最多) ---\n"
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
    logger.info(f"Handling '刪除' for user {user_id}")
    try:
        all_values = sheet.get_all_values()
        user_id_col_index = 3 
        
        for row_index in range(len(all_values) - 1, 0, -1):
            row = all_values[row_index]
            if len(row) > user_id_col_index and row[user_id_col_index] == user_id:
                row_to_delete = row_index + 1
                sheet.delete_rows(row_to_delete)
                deleted_desc = f"{row[0]} {row[1]} {row[2]} 元"
                return f"🗑️ 已刪除：{deleted_desc}"
        
        return "找不到您的記帳記錄可供刪除。"
            
    except Exception as e:
        logger.error(f"刪除失敗：{e}", exc_info=True)
        return "刪除記錄失敗。"

def handle_set_budget(sheet, text, user_id):
    logger.info(f"Handling '設置預算' for user {user_id}")
    match = re.match(r'設置預算\s+([\u4e00-\u9fa5]+)\s+(\d+)', text)
    if not match:
        return "格式錯誤！請輸入「設置預算 [類別] [限額]」，例如：「設置預算 餐飲 3000」"
    
    category = match.group(1).strip()
    limit = int(match.group(2))
    
    valid_categories = ['餐飲', '飲料', '交通', '娛樂', '購物', '雜項']
    if category not in valid_categories:
        return f"無效類別，請使用：{', '.join(valid_categories)}"

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
        return "設置預算失敗。"

def handle_view_budget(trx_sheet, budget_sheet, user_id, event_time):
    logger.info(f"Handling '查看預算' for user {user_id}")
    try:
        budgets_records = budget_sheet.get_all_records()
        user_budgets = [b for b in budgets_records if b.get('使用者ID') == user_id]
        
        if not user_budgets:
            return "您尚未設置任何預算。請輸入「設置預算 [類別] [限額]」"

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
                continue

        reply = f"📊 **{current_month_str} 預算狀態**：\n"
        total_spent = 0.0
        total_limit = 0.0
        
        for budget in user_budgets:
            category = budget.get('類別')
            limit = float(budget.get('限額', 0))
            if limit <= 0: continue
                
            total_limit += limit
            spent = sum(abs(float(r.get('金額', 0))) for r in user_month_expenses if r.get('類別') == category)
            total_spent += spent
            remaining = limit - spent
            percentage = (spent / limit) * 100
            bar_fill = '■' * int(percentage / 10)
            bar_empty = '□' * (10 - int(percentage / 10))
            if percentage > 100:
                 bar_fill = '■' * 10
                 bar_empty = ''
                 
            status_icon = "🟢" if remaining >= 0 else "🔴"

            reply += f"\n{category} (限額 {limit} 元)\n"
            reply += f"   {status_icon} 已花費：{spent} 元\n"
            reply += f"   [{bar_fill}{bar_empty}] {percentage:.0f}%\n"
            reply += f"   剩餘：{remaining} 元\n"

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
    logger.info("Starting Flask server locally...")
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)