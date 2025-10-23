import os
import logging
import re
import json
import gspread
import google.generativeai as genai
from flask import Flask, request, abort
from linebot import WebhookHandler, LineBotApi
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from dotenv import load_dotenv

# === 配置日誌 ===
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === 步驟 1：載入環境變數 ===
load_dotenv()

# === 步驟 2：從環境變數讀取金鑰 ===
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", '記帳小浣熊資料庫')
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# === 步驟 3：驗證金鑰是否已載入 ===
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, GEMINI_API_KEY, GOOGLE_SHEET_ID]):
    logger.error("!!! 關鍵金鑰載入失敗 !!!")
    logger.error("請檢查：")
    logger.error("1. 專案資料夾中是否有 .env 檔案？")
    logger.error("2. .env 檔案中是否正確填寫了 LINE_..., GEMINI_..., GOOGLE_SHEET_ID？")
    raise ValueError("金鑰未配置，請檢查 .env 檔案")
else:
    logger.debug("所有金鑰已成功從 .env 載入。")
    logger.debug(f"LINE_CHANNEL_ACCESS_TOKEN (前10字): {LINE_CHANNEL_ACCESS_TOKEN[:10] if LINE_CHANNEL_ACCESS_TOKEN else '未設置'}...")
    logger.debug(f"LINE_CHANNEL_SECRET (前10字): {LINE_CHANNEL_SECRET[:10] if LINE_CHANNEL_SECRET else '未設置'}...")
    logger.debug(f"GOOGLE_SHEET_NAME: {GOOGLE_SHEET_NAME}")
    logger.debug(f"GOOGLE_SHEET_ID: {GOOGLE_SHEET_ID}")

# === 初始化 Flask 應用程式 ===
app = Flask(__name__)
logger.info("Flask application initialized successfully.")

# === 配置 LINE 與 Gemini API 客戶端 ===
try:
    if not LINE_CHANNEL_ACCESS_TOKEN or not re.match(r'^[A-Za-z0-9+/=]+$', LINE_CHANNEL_ACCESS_TOKEN):
        logger.error("LINE_CHANNEL_ACCESS_TOKEN 格式無效，可能包含空格或無效字符")
        raise ValueError("LINE_CHANNEL_ACCESS_TOKEN 格式無效")
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    
    # === 2. 修改 Gemini API 初始化 ===
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-2.5-flash-lite')
    # ===
    
    logger.debug("LINE 和 Gemini API 客戶端初始化成功")
except Exception as e:
    logger.error(f"API 客戶端初始化失敗: {e}", exc_info=True)
    raise

# === Google Sheets 初始化 ===
def get_sheets_workbook():
    """
    初始化 Google Sheets 客戶端並返回工作簿 (Workbook) 物件
    使用 GOOGLE_SHEET_ID 存取試算表
    """
    logger.debug("正在初始化 Google Sheets 憑證...")
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json:
            logger.error("GOOGLE_CREDENTIALS 未設置或為空")
            raise ValueError("GOOGLE_CREDENTIALS 未設置或為空")
        
        logger.debug(f"GOOGLE_CREDENTIALS 內容（前100字）：{creds_json[:100]}...")
        try:
            creds_info = json.loads(creds_json)
            logger.debug(f"GOOGLE_CREDENTIALS project_id: {creds_info.get('project_id', '未找到')}")
            logger.debug(f"GOOGLE_CREDENTIALS client_email: {creds_info.get('client_email', '未找到')}")
        except json.JSONDecodeError as e:
            logger.error(f"GOOGLE_CREDENTIALS JSON 解析錯誤：{e}")
            logger.error(f"GOOGLE_CREDENTIALS 內容（前100字）：{creds_json[:100]}...")
            raise ValueError(f"GOOGLE_CREDENTIALS 格式無效：{str(e)}")
        
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)
        logger.debug(f"成功授權，嘗試開啟試算表 ID：{GOOGLE_SHEET_ID}")
        
        try:
            workbook = client.open_by_key(GOOGLE_SHEET_ID)
            logger.debug(f"成功開啟試算表 ID：{GOOGLE_SHEET_ID}")
            return workbook
        except gspread.exceptions.SpreadsheetNotFound as e:
            logger.error(f"找不到試算表 ID '{GOOGLE_SHEET_ID}'：{e}")
            raise ValueError(f"試算表 ID '{GOOGLE_SHEET_ID}' 不存在或未共享給服務帳戶")
        except gspread.exceptions.APIError as e:
            logger.error(f"Google Sheets API 錯誤：{e}")
            raise ValueError(f"Google Sheets API 權限錯誤：{e}")
    except Exception as e:
        logger.error(f"Google Sheets 初始化失敗：{e}", exc_info=True)
        raise

def ensure_worksheets(workbook):
    """
    確保 Google Sheet 中存在 Transactions 和 Budgets 工作表，若不存在則創建
    """
    logger.debug("檢查並確保 Transactions 和 Budgets 工作表存在...")
    try:
        try:
            trx_sheet = workbook.worksheet('Transactions')
            logger.debug("找到 Transactions 工作表")
        except gspread.exceptions.WorksheetNotFound:
            logger.debug("未找到 Transactions 工作表，正在創建...")
            trx_sheet = workbook.add_worksheet(title='Transactions', rows=1000, cols=10)
            trx_sheet.append_row(['日期', '類別', '金額', '使用者ID', '使用者名稱', '備註'])
            logger.debug("Transactions 工作表創建成功")

        try:
            budget_sheet = workbook.worksheet('Budgets')
            logger.debug("找到 Budgets 工作表")
        except gspread.exceptions.WorksheetNotFound:
            logger.debug("未找到 Budgets 工作表，正在創建...")
            budget_sheet = workbook.add_worksheet(title='Budgets', rows=100, cols=5)
            budget_sheet.append_row(['使用者ID', '類別', '限額'])
            logger.debug("Budgets 工作表創建成功")

        return trx_sheet, budget_sheet
    except Exception as e:
        logger.error(f"創建或檢查工作表失敗：{e}", exc_info=True)
        return None, None

def get_user_profile_name(user_id):
    logger.debug(f"獲取使用者 {user_id} 的個人資料...")
    try:
        profile = line_bot_api.get_profile(user_id)
        logger.debug(f"成功獲取使用者 {user_id} 的顯示名稱：{profile.display_name}")
        return profile.display_name
    except LineBotApiError as e:
        logger.error(f"無法獲取使用者 {user_id} 的個人資料：{e}", exc_info=True)
        return "未知用戶"

# === Webhook 處理 (LINE 訊息的入口) ===
@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.debug(f"Received webhook request, body (前100字): {body[:100]}...")
    logger.debug(f"X-Line-Signature: {signature}")
    
    try:
        handler.handle(body, signature)
        logger.debug("Webhook 處理成功")
    except InvalidSignatureError as e:
        logger.error(f"Invalid signature: {e}. Check LINE_CHANNEL_SECRET.", exc_info=True)
        abort(400)
    except Exception as e:
        logger.error(f"Webhook 處理失敗: {e}", exc_info=True)
        return 'Internal Server Error', 500
    
    return 'OK'

# === 訊息總機 (核心邏輯) ===
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    line_timestamp_ms = event.timestamp
    event_time = datetime.fromtimestamp(line_timestamp_ms / 1000.0)
    
    logger.debug(f"Received message: '{text}' from user '{user_id}' at {event_time}")
    
    # === 特殊處理：僅「幫助」指令不需資料庫 ===
    if text == "幫助":
        # === 修改：步驟一，新增「查詢」說明 ===
        reply_text = (
            "📌 **記帳小浣熊使用說明🦝**：\n\n"
            "💸 **自然記帳** (AI會幫你分析)：\n"
            "   - 「今天中午吃了雞排80」\n"
            "   - 「昨天喝飲料 50」\n"
            "   - 「上禮拜三收入 1000 獎金」\n"
            "   - 「5/10 交通費 120」\n\n"
            "📊 **查帳**：\n"
            "   - 「查帳」：查看總支出、收入和淨餘額\n\n"
            "🔎 **查詢**：\n"
            "   - 「查詢 [關鍵字]」：搜尋相關記錄\n"
            "     (例如: 查詢 雞排)\n\n"
            "📅 **月結**：\n"
            "   - 「月結」：分析這個月的收支總結\n\n"
            "🗑️ **刪除**：\n"
            "   - 「刪除」：移除您最近一筆記錄\n\n"
            "💡 **預算**：\n"
            "   - 「設置預算 餐飲 3000」\n"
            "   - 「查看預算」：檢查本月預算使用情況\n"
            " 類別: 🍽️ 餐飲 🥤 飲料 🚌 交通 🎬 娛樂 🛍️ 購物 💡 雜項💰 收入"
        )
        # === 修改結束 ===
        
        logger.debug("處理 '幫助' 指令，準備回覆")
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
            logger.debug("成功回覆 '幫助' 指令")
            return
        except LineBotApiError as e:
            logger.error(f"回覆 '幫助' 訊息失敗：{e}", exc_info=True)
            return

    # === 獲取 Google Sheets 工作簿 ===
    logger.debug("嘗試初始化 Google Sheets 工作簿")
    try:
        workbook = get_sheets_workbook()
        if not workbook:
            logger.error("Google Sheets 工作簿為 None")
            reply_text = "糟糕！小浣熊的帳本(Google Sheet)連接失敗了 😵 請檢查憑證設置或 Google Sheets API 權限。"
            try:
                line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
                logger.debug("成功回覆 Google Sheets 初始化失敗訊息")
            except LineBotApiError as e:
                logger.error(f"回覆 Google Sheets 失敗訊息時出錯：{e}", exc_info=True)
            return
    except Exception as e:
        logger.error(f"初始化 Google Sheets 失敗：{e}", exc_info=True)
        reply_text = f"糟糕！小浣熊的帳本連接失敗：{str(e)}"
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
            logger.debug("成功回覆 Google Sheets 初始化錯誤訊息")
        except LineBotApiError as e:
            logger.error(f"回覆 Google Sheets 錯誤訊息失敗：{e}", exc_info=True)
        return

    # === 確保工作表存在 ===
    logger.debug("檢查 Google Sheets 工作表")
    trx_sheet, budget_sheet = ensure_worksheets(workbook)
    if not trx_sheet or not budget_sheet:
        reply_text = "糟糕！無法創建或存取 'Transactions' 或 'Budgets' 工作表，請檢查 Google Sheet 設定。"
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
            logger.debug("成功回覆工作表錯誤訊息")
        except LineBotApiError as e:
            logger.error(f"回覆工作表錯誤訊息失敗：{e}", exc_info=True)
        return
        
    # === 指令路由器 (Router) ===
    try:
        if text == "查帳":
            reply_text = handle_check_balance(trx_sheet, user_id)
        elif text == "月結":
            reply_text = handle_monthly_report(trx_sheet, user_id, event_time)
        elif text == "刪除":
            reply_text = handle_delete_record(trx_sheet, user_id)
        elif text.startswith("設置預算"):
            reply_text = handle_set_budget(budget_sheet, text, user_id)
        elif text == "查看預算":
            reply_text = handle_view_budget(trx_sheet, budget_sheet, user_id, event_time)
        
        # === 新增：步驟二，加入「查詢」路由 ===
        elif text.startswith("查詢"):
            keyword = text[2:].strip() # 取得「查詢」後面的所有文字並去除空白
            if not keyword:
                reply_text = "請輸入您想查詢的關鍵字喔！\n例如：「查詢 雞排」"
            else:
                reply_text = handle_search_records(trx_sheet, user_id, keyword)
        # === 新增結束 ===
                
        else:
            user_name = get_user_profile_name(user_id)
            reply_text = handle_nlp_record(trx_sheet, budget_sheet, text, user_id, user_name, event_time)

    except Exception as e:
        logger.error(f"處理指令 '{text}' 失敗：{e}", exc_info=True)
        reply_text = f"糟糕！小浣熊處理您的指令時出錯了：{str(e)}"

    # === 最終回覆 ===
    if not isinstance(reply_text, str):
        reply_text = str(reply_text)

    logger.debug(f"準備回覆訊息：{reply_text[:100]}...")
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        logger.debug("成功回覆訊息")
    except LineBotApiError as e:
        logger.error(f"回覆訊息失敗：{e}", exc_info=True)

# === 核心功能函式 (Helper Functions) ===

def get_cute_reply(category):
    """
    根據類別返回客製化的可愛回應
    """
    replies = {
        "餐飲": "好好吃飯，才有力氣！ 🍜 (⁎⁍̴̛ᴗ⁍̴̛⁎)",
        "飲料": "是全糖嗎？ 🧋 快樂水 get daze！",
        "交通": "嗶嗶！出門平安 🚗 目的地就在前方！",
        "娛樂": "哇！聽起來好好玩！ 🎮 (≧▽≦)",
        "購物": "又要拆包裹啦！📦 快樂就是這麼樸實無華！",
        "雜項": "嗯... 這筆花費有點神秘喔 🧐",
        "收入": "太棒了！💰 距離財富自由又近了一步！"
    }
    # 如果找不到類別，就回傳一個通用的
    return replies.get(category, "✅ 記錄完成！")

def check_budget_warning(trx_sheet, budget_sheet, user_id, category, event_time):
    """
    檢查特定類別的預算，如果接近或超過則回傳警告訊息
    """
    # 收入不需要檢查預算
    if category == "收入":
        return ""

    logger.debug(f"正在為 {user_id} 檢查 {category} 的預算...")
    try:
        # 1. 找到這個類別的預算
        budgets_records = budget_sheet.get_all_records()
        user_budget_limit = 0.0
        for b in budgets_records:
            if b.get('使用者ID') == user_id and b.get('類別') == category:
                user_budget_limit = float(b.get('限額', 0))
                break
        
        # 如果沒有設定這個類別的預算，或預算為 0，就不用警告
        if user_budget_limit <= 0:
            logger.debug(f"使用者 {user_id} 未設定 {category} 預算，跳過警告。")
            return ""

        # 2. 計算這個類別的本月總花費
        transactions_records = trx_sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        spent = 0.0
        for r in transactions_records:
            try:
                amount = float(r.get('金額', 0))
                if (r.get('使用者ID') == user_id and
                    r.get('日期', '').startswith(current_month_str) and
                    r.get('類別') == category and
                    amount < 0): # 確保是支出
                    spent += abs(amount)
            except (ValueError, TypeError):
                continue
        
        logger.debug(f"{category} 預算 {user_budget_limit}, 本月已花 {spent}")
        
        # 3. 判斷是否警告
        percentage = (spent / user_budget_limit) * 100
        
        if percentage >= 100:
            # 修改：格式化為 .0f (無小數點)
            return f"\n\n🚨 警告！ {category} 預算已超支 {spent - user_budget_limit:.0f} 元！ 😱"
        elif percentage >= 90:
            remaining = user_budget_limit - spent
            # 修改：格式化為 .0f (無小數點)
            return f"\n\n🔔 注意！ {category} 預算只剩下 {remaining:.0f} 元囉！ (已用 {percentage:.0f}%)"
        
        return "" # 還在安全範圍
    
    except Exception as e:
        logger.error(f"檢查預算警告失敗：{e}", exc_info=True)
        # 即使檢查失敗，也不該讓主程式崩潰
        return "\n(檢查預算時發生錯誤)"

def handle_nlp_record(sheet, budget_sheet, text, user_id, user_name, event_time):
    logger.debug(f"處理自然語言記帳指令：{text}")
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
        logger.debug("發送 prompt 至 Gemini API")
        
        response = gemini_model.generate_content(prompt)
        
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        
        logger.debug(f"Gemini NLP response: {clean_response}")
        
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

            # 寫入 GSheet
            sheet.append_row([date, category, amount, user_id, user_name, notes])
            logger.debug("成功寫入 Google Sheet 記錄")
                        
            # 1. 獲取可愛回應
            cute_reply = get_cute_reply(category)
            
            # 2. 檢查預算警告
            warning_message = check_budget_warning(sheet, budget_sheet, user_id, category, event_time)
            
            # 3. 計算總餘額
            all_records = sheet.get_all_records()
            user_balance = 0.0
            for r in all_records:
                if r.get('使用者ID') == user_id:
                    try:
                        amount_val = float(r.get('金額', 0)) # 避免變數名稱衝突
                        user_balance += amount_val
                    except (ValueError, TypeError):
                        continue
            
            # 4. 組合最終回覆
            # 修改：格式化 amount 和 user_balance 為 .0f (無小數點)
            return (
                f"{cute_reply}\n\n"
                f"📝 摘要：{date} {notes} ({category}) {abs(amount):.0f} 元\n"
                f"📈 {user_name} 目前總餘額：{user_balance:.0f} 元"
                f"{warning_message}" # 這個字串本身就包含 \n\n (如果有的話)
            )

        elif status == 'chat':
            return message or "你好！我是記帳小浣熊 🦝"
        
        else:
            return message or "🦝？ 抱歉，我聽不懂..."

    except json.JSONDecodeError:
        logger.error(f"Gemini NLP JSON 解析失敗: {clean_response}")
        return "糟糕！AI 分析器暫時罷工了 (JSON解析失敗)... 請稍後再試。"
    except Exception as e:
        logger.error(f"Gemini API 呼叫或 GSheet 寫入失敗：{e}", exc_info=True)
        return f"目前我無法處理這個請求：{str(e)}"

def handle_check_balance(sheet, user_id):
    logger.debug(f"處理 '查帳' 指令，user_id: {user_id}")
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
                logger.warning(f"跳過無效金額 '{amount_str}' for user {user_id}")
                continue

        total_balance = total_income + total_expense
        
        # 修改：格式化所有金額為 .0f (無小數點)
        return (
            f"📊 **您的財務總覽**：\n\n"
            f"💰 總收入：{total_income:.0f} 元\n"
            f"💸 總支出：{abs(total_expense):.0f} 元\n"
            f"--------------------\n"
            f"📈 淨餘額：{total_balance:.0f} 元"
        )
    except Exception as e:
        logger.error(f"查帳失敗：{e}", exc_info=True)
        return f"查帳失敗：無法讀取試算表：{str(e)}"

def handle_monthly_report(sheet, user_id, event_time):
    logger.debug(f"處理 '月結' 指令，user_id: {user_id}")
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
        
        # 修改：格式化總結金額為 .0f (無小數點)
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
                # 修改：格式化分類金額為 .0f (無小數點)
                reply += f"{prefix} {category}: {amount:.0f} 元\n"
        
        return reply
    except Exception as e:
        logger.error(f"月結失敗：{e}", exc_info=True)
        return f"月結報表產生失敗：{str(e)}"

def handle_delete_record(sheet, user_id):
    logger.debug(f"處理 '刪除' 指令，user_id: {user_id}")
    try:
        all_values = sheet.get_all_values()
        user_id_col_index = 3 
        
        for row_index in range(len(all_values) - 1, 0, -1):
            row = all_values[row_index]
            if len(row) > user_id_col_index and row[user_id_col_index] == user_id:
                row_to_delete = row_index + 1
                
                # 修改：格式化刪除訊息中的金額為 .0f (無小數點)
                try:
                    amount_val = float(row[2]) # row[2] 是金額欄位
                    deleted_desc = f"{row[0]} {row[1]} {amount_val:.0f} 元"
                except (ValueError, TypeError):
                    deleted_desc = f"{row[0]} {row[1]} {row[2]} 元" # 轉換失敗時的備案
                
                sheet.delete_rows(row_to_delete)
                return f"🗑️ 已刪除：{deleted_desc}"
        
        return "找不到您的記帳記錄可供刪除。"
    except Exception as e:
        logger.error(f"刪除失敗：{e}", exc_info=True)
        return f"刪除記錄失敗：{str(e)}"

def handle_set_budget(sheet, text, user_id):
    logger.debug(f"處理 '設置預算' 指令，user_id: {user_id}, text: {text}")
    match = re.match(r'設置預算\s+([\u4e00-\u9fa5]+)\s+(\d+)', text)
    if not match:
        return "格式錯誤！請輸入「設置預算 [類別] [限額]」，例如：「設置預算 餐飲 3000」"
    
    category = match.group(1).strip()
    limit = int(match.group(2)) # 這裡已是 int，不需修改
    
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
            return f"✅ 已更新預算：{category} {limit} 元" # limit 已是 int
        else:
            sheet.append_row([user_id, category, limit])
            return f"✅ 已設置預算：{category} {limit} 元" # limit 已是 int
    except Exception as e:
        logger.error(f"設置預算失敗：{e}", exc_info=True)
        return f"設置預算失敗：{str(e)}"

def handle_view_budget(trx_sheet, budget_sheet, user_id, event_time):
    logger.debug(f"處理 '查看預算' 指令，user_id: {user_id}")
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
            if limit <= 0:
                continue
                
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

            # 修改：格式化 limit, spent, remaining 為 .0f (無小數點)
            reply += f"\n{category} (限額 {limit:.0f} 元)\n"
            reply += f"   {status_icon} 已花費：{spent:.0f} 元\n"
            reply += f"   [{bar_fill}{bar_empty}] {percentage:.0f}%\n"
            reply += f"   剩餘：{remaining:.0f} 元\n"

        reply += "\n--------------------\n"
        if total_limit > 0:
            total_remaining = total_limit - total_spent
            total_percentage = (total_spent / total_limit) * 100
            status_icon = "🟢" if total_remaining >= 0 else "🔴"
            
            # 修改：格式化 total_limit, total_spent, total_remaining 為 .0f (無小數點)
            reply += f"總預算： {total_limit:.0f} 元\n"
            reply += f"總花費： {total_spent:.0f} 元\n"
            reply += f"{status_icon} 總剩餘：{total_remaining:.0f} 元 ({total_percentage:.0f}%)"
        else:
            reply += "總預算尚未設定或設定為 0。"
        
        return reply
    except Exception as e:
        logger.error(f"查看預算失敗：{e}", exc_info=True)
        return f"查看預算失敗：{str(e)}"

# === 新增：步驟三，加入「查詢」核心函式 ===
def handle_search_records(sheet, user_id, keyword):
    """
    處理關鍵字查詢
    """
    logger.debug(f"處理 '查詢' 指令，user_id: {user_id}, keyword: {keyword}")
    try:
        records = sheet.get_all_records()
        matches = []
        
        # 篩選符合 user_id 和 keyword 的記錄
        for r in records:
            if r.get('使用者ID') == user_id:
                # 檢查「類別」或「備註」欄位是否包含關鍵字
                if keyword in r.get('類別', '') or keyword in r.get('備註', ''):
                    matches.append(r)
        
        if not matches:
            return f"🦝 找不到關於「{keyword}」的任何記錄喔！"
        
        # 格式化回覆訊息
        reply = f"🔎 關鍵字「{keyword}」的搜尋結果 (共 {len(matches)} 筆)：\n\n"
        total_amount = 0.0
        limit = 15 # 最多顯示 15 筆，避免訊息過長
        
        # 為了排序，我們先處理所有匹配的記錄
        sorted_matches = sorted(matches, key=lambda x: x.get('日期', ''), reverse=True)
        
        for r in sorted_matches[:limit]:
            try:
                date = r.get('日期', 'N/A')
                category = r.get('類別', 'N/A')
                notes = r.get('備註', 'N/A')
                amount = float(r.get('金額', 0))
                
                total_amount += amount # 計算前 limit 筆的總和 (或所有?) 
                                    # 這裡改為計算所有匹配的總和
                
                # 格式化單筆記錄
                reply += f"• {date} {notes} ({category}) {amount:.0f} 元\n"
                
            except (ValueError, TypeError):
                continue
        
        # 計算所有匹配項的總和 (而不是只有前15筆)
        total_amount_all_matches = 0.0
        for r in matches:
             try:
                total_amount_all_matches += float(r.get('金額', 0))
             except (ValueError, TypeError):
                continue
        
        reply += f"\n--------------------\n"
        reply += f"📈 查詢總計：{total_amount_all_matches:.0f} 元\n"
        
        if len(matches) > limit:
            reply += f"(只顯示最近 {limit} 筆記錄)"
            
        return reply
        
    except Exception as e:
        logger.error(f"查詢記錄失敗：{e}", exc_info=True)
        return f"查詢失敗：{str(e)}"
# === 新增結束 ===


# === 主程式入口 ===
if __name__ == "__main__":
    logger.info("Starting Flask server locally...")
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)