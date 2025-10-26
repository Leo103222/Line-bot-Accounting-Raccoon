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
from datetime import datetime, timedelta
from dotenv import load_dotenv

# === 配置日誌 ===
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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
    確保 Google Sheet 中存在 Transactions 和 Budgets 工作表
    """
    logger.debug("檢查並確保 Transactions 和 Budgets 工作表存在...")
    try:
        try:
            trx_sheet = workbook.worksheet('Transactions')
            logger.debug("找到 Transactions 工作表")
            # 檢查標頭，如果為空(例如全新的sheet)，則寫入
            header = trx_sheet.row_values(1)
            if not header:
                 logger.debug("Transactions 工作表為空，正在寫入標頭...")
                 trx_sheet.append_row(['時間', '類別', '金額', '使用者ID', '使用者名稱', '備註'])
                 
        except gspread.exceptions.WorksheetNotFound:
            logger.debug("未找到 Transactions 工作表，正在創建...")
            trx_sheet = workbook.add_worksheet(title='Transactions', rows=1000, cols=10)
            # 統一使用 '時間' 作為標頭
            trx_sheet.append_row(['時間', '類別', '金額', '使用者ID', '使用者名稱', '備註'])

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

        return trx_sheet, budget_sheet
    except Exception as e:
        logger.error(f"創建或檢查工作表失敗：{e}", exc_info=True)
        return None, None

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

# === 訊息總機 (核心邏輯) ===
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    line_timestamp_ms = event.timestamp
    event_time = datetime.fromtimestamp(line_timestamp_ms / 1000.0)
    
    logger.debug(f"Received message: '{text}' from user '{user_id}' at {event_time}")
    
    # 特殊處理：「幫助」指令不需資料庫
    if text == "幫助":
        reply_text = (
            "📌 **記帳小浣熊使用說明🦝**：\n\n"
            "💸 **自然記帳** (AI會幫你分析)：\n"
            "   - 「今天中午吃了雞排80」\n"
            "   - 「昨天喝飲料 50」\n"
            "   - 「上禮拜三收入 1000 獎金」\n"
            "   - 「5/10 交通費 120」\n"
            "   - 「午餐100 晚餐200」 (支援多筆)\n\n"
            "📊 **查帳**：\n"
            "   - 「查帳」：查看總支出、收入和淨餘額\n\n"
            "🔎 **查詢**：\n"
            "   - 「查詢 雞排」\n"
            "   - 「查詢 這禮拜的餐飲」\n"
            "   - 「查詢 10/1~10/10 的收入」\n\n"
            "📅 **月結**：\n"
            "   - 「月結」：分析這個月的收支總結\n\n"
            "🗑️ **刪除**：\n"
            "   - 「刪除」：移除您最近一筆記錄\n\n"
            "💡 **預算**：\n"
            "   - 「設置預算 餐飲 3000」\n"
            "   - 「查看預算」：檢查本月預算使用情況\n"
            " 類別: 🍽️ 餐飲 🥤 飲料 🚌 交通 🎬 娛樂 🛍️ 購物 💡 雜項💰 收入"
        )
        
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
            return
        except LineBotApiError as e:
            logger.error(f"回覆 '幫助' 訊息失敗：{e}", exc_info=True)
            return

    # 獲取 Google Sheets 工作簿
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

    # 確保工作表存在
    trx_sheet, budget_sheet = ensure_worksheets(workbook)
    if not trx_sheet or not budget_sheet:
        reply_text = "糟糕！無法創建或存取 'Transactions' 或 'Budgets' 工作表。"
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        except LineBotApiError as e:
            logger.error(f"回覆工作表錯誤訊息失敗：{e}", exc_info=True)
        return
        
    # 指令路由器 (Router)
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
        elif text.startswith("查詢"):
            keyword = text[2:].strip()
            if not keyword:
                reply_text = "請輸入您想查詢的關鍵字喔！\n例如：「查詢 雞排」或「查詢 這禮拜」"
            else:
                reply_text = handle_search_records(trx_sheet, user_id, keyword, event_time)
        else:
            # 預設執行 NLP 自然語言記帳
            user_name = get_user_profile_name(user_id)
            reply_text = handle_nlp_record(trx_sheet, budget_sheet, text, user_id, user_name, event_time)

    except Exception as e:
        logger.error(f"處理指令 '{text}' 失敗：{e}", exc_info=True)
        reply_text = f"糟糕！小浣熊處理您的指令時出錯了：{str(e)}"

    # 最終回覆
    if not isinstance(reply_text, str):
        reply_text = str(reply_text)

    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except LineBotApiError as e:
        logger.error(f"回覆訊息失敗：{e}", exc_info=True)

# === 核心功能函式 (Helper Functions) ===

# === 關鍵修正：新增輔助函式 ===
def get_datetime_from_record(r):
    """
    相容性輔助函式：
    優先嘗試讀取 '時間' (新)，如果沒有，再讀取 '日期' (舊)
    """
    return r.get('時間', r.get('日期', ''))
# === 修正結束 ===


def get_cute_reply(category):
    """
    根據類別返回客製化的可愛回應 (隨機)
    """
    replies = {
        "餐飲": [
            "好好吃飯，才有力氣！ 🍜 (⁎⁍̴̛ᴗ⁍̴̛⁎)",
            "吃飽飽，心情好！ 😋",
            "這餐看起來真不錯！ 🍔",
            "美味 +1！ 🍕",
            "享受美食的時光～ 🍰"
        ],
        "飲料": [
            "是全糖嗎？ 🧋 快樂水 get daze！",
            "乾杯！ 🥂",
            "喝點飲料，放鬆一下～ 🥤",
            "是咖啡還是茶？ ☕",
            "續命水來啦！ 💧"
        ],
        "交通": [
            "嗶嗶！出門平安 🚗 目的地就在前方！",
            "出發！ 🚀",
            "路上小心喔！ 🚌",
            "通勤辛苦了！ 🚲",
            "讀萬卷書，行萬里路！ 🌍"
        ],
        "娛樂": [
            "哇！聽起來好好玩！ 🎮 (≧▽≦)",
            "Happy time! 🥳",
            "這錢花得值得！ 🎬",
            "充實生活，讚！ 🎭",
            "放鬆是為了走更長遠的路！ 💖"
        ],
        "購物": [
            "又要拆包裹啦！📦 快樂就是這麼樸實無華！",
            "買！都買！ 🛍️",
            "錢沒有不見，只是變成你喜歡的樣子！ 💸",
            "犒賞一下自己是應該的！ 🎁",
            "新夥伴 get！ 🤖"
        ],
        "雜項": [
            "嗯... 這筆花費有點神秘喔 🧐",
            "生活總有些意想不到的開銷～ 🤷",
            "筆記筆記... 📝",
            "OK，記下了！ ✍️",
            "這又是啥？ 😅"
        ],
        "收入": [
            "太棒了！💰 距離財富自由又近了一步！",
            "發財啦！ 🤑",
            "努力有回報！ 💪",
            "錢錢進來！ 🧧",
            "被動收入嗎？真好！ 📈"
        ]
    }
    default_replies = ["✅ 記錄完成！", "OK！記好囉！ ✍️", "小浣熊收到！ 🦝"]
    
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
                # === 關鍵修正：使用輔助函式 ===
                record_time_str = get_datetime_from_record(r)
                
                if (r.get('使用者ID') == user_id and
                    record_time_str.startswith(current_month_str) and
                    r.get('類別') == category and
                    amount < 0):
                    spent += abs(amount)
            except (ValueError, TypeError):
                continue
        
        # 判斷是否警告
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

def handle_nlp_record(sheet, budget_sheet, text, user_id, user_name, event_time):
    """
    使用 Gemini NLP 處理自然語言記帳
    """
    logger.debug(f"處理自然語言記帳指令：{text}")
    
    current_time_str = event_time.strftime('%Y-%m-%d %H:%M:%S')
    today_str = event_time.strftime('%Y-%m-%d')
    
    date_context_lines = [
        f"今天是 {today_str} (星期{event_time.weekday()})。",
        f"目前時間是: {event_time.strftime('%H:%M:%S')}",
        "日期參考：",
        f"- 昨天: {(event_time.date() - timedelta(days=1)).strftime('%Y-%m-%d')}"
    ]
    date_context = "\n".join(date_context_lines)
    
    prompt = f"""
    你是一個記帳機器人的 AI 助手，你的名字是「記帳小浣熊🦝」。
    使用者的輸入是：「{text}」
    
    目前的日期時間上下文如下：
    {date_context}

    請嚴格按照以下 JSON 格式回傳，不要有任何其他文字或 "```json" 標記：
    {{
      "status": "success" | "failure" | "chat",
      "data": [
        {{
          "datetime": "YYYY-MM-DD HH:MM:SS",
          "category": "餐飲" | "飲料" | "交通" | "娛樂" | "購物" | "雜項" | "收入",
          "amount": <number>,
          "notes": "<string>"
        }}
      ] | null,
      "message": "<string>"
    }}

    解析規則：
    1. 如果成功解析為記帳 (包含一筆或多筆)：
        - status: "success"
        - data: 必須是一個 "列表" (List)，包含一或多個記帳物件。
        - datetime: 必須是 "YYYY-MM-DD HH:MM:SS" 格式。
        - **時間規則**:
            - 如果沒提日期或時間 (例如 "雞排 80")，預設為當下時間 ({current_time_str})。
            - 如果只提日期 (例如 "昨天 50")，預設時間為 "12:00:00" (中午)。
            - 如果提到 "中午"、"晚餐" 等，請盡量推斷時間 (例如 12:00:00, 18:00:00)。
        - category: 必須是 [餐飲, 飲料, 交通, 娛樂, 購物, 雜項, 收入] 之一。
        - amount: 必須是數字。如果是「收入」，必須為正數 (+)。如果是「支出」，必須為負數 (-)。
        - notes: 盡可能擷取出花費的項目，例如「雞排」。
    2. 如果使用者只是在閒聊 (例如 "你好", "你是誰", "謝謝")：
        - status: "chat"
        - data: null
        - message: (請用「記帳小浣熊🦝」的語氣，"活潑"、"口語化"地友善回覆，可以適當聊天，但還是得拉回記帳，如果問你為甚麼叫小浣熊，回答因為開發我的人大家都叫他浣熊，回復可以適當加一些表情符號)
    3. 如果看起來像記帳，但缺少關鍵資訊 (例如 "雞排" (沒說金額))：
        - status: "failure"
        - data: null
        - message: "🦝？我不太確定... 麻煩請提供日期和金額喔！"
    
    範例：
    輸入: "今天中午吃了雞排80" -> {{"status": "success", "data": [{{"datetime": "{today_str} 12:00:00", "category": "餐飲", "amount": -80, "notes": "雞排"}}], "message": "記錄成功"}}
    輸入: "昨天 收入 1000" -> {{"status": "success", "data": [{{"datetime": "{(event_time.date() - timedelta(days=1)).strftime('%Y-%m-%d')} 12:00:00", "category": "收入", "amount": 1000, "notes": "收入"}}], "message": "記錄成功"}}
    輸入: "午餐1144、晚餐341" -> {{"status": "success", "data": [{{"datetime": "{today_str} 12:00:00", "category": "餐飲", "amount": -1144, "notes": "午餐"}}, {{"datetime": "{today_str} 18:00:00", "category": "餐飲", "amount": -341, "notes": "晚餐"}}], "message": "記錄 2 筆成功"}}
    輸入: "你好" -> {{"status": "chat", "data": null, "message": "哈囉！我是記帳小浣熊🦝 需要幫忙記帳嗎？還是想聊聊天呀？"}}
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
            records = data.get('data', [])
            if not records:
                return "🦝？ AI 分析成功，但沒有返回任何記錄。"
            
            reply_summary_lines = []
            last_category = "雜項" 
            
            # 迭代處理每一筆記錄
            for record in records:
                datetime_str = record.get('datetime', current_time_str)
                category = record.get('category', '雜項')
                amount_str = record.get('amount', 0)
                notes = record.get('notes', text)
                
                try:
                    amount = float(amount_str)
                    if amount == 0:
                        reply_summary_lines.append(f"• {notes} ({category}) 金額為 0，已跳過。")
                        continue
                except (ValueError, TypeError):
                    reply_summary_lines.append(f"• {notes} ({category}) 金額 '{amount_str}' 格式錯誤，已跳過。")
                    continue

                # 寫入 GSheet (第一欄)
                # 即使 GSheet 標頭是 '日期'，append_row 仍會寫入第一欄
                sheet.append_row([datetime_str, category, amount, user_id, user_name, notes])
                logger.debug(f"成功寫入 Google Sheet 記錄: {datetime_str}, {category}, {amount}, {notes}")
                
                try:
                    display_time = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
                except ValueError:
                    display_time = datetime_str 
                
                reply_summary_lines.append(f"• {display_time} {notes} ({category}) {abs(amount):.0f} 元")
                last_category = category
            
            logger.debug("所有記錄寫入完畢")

            cute_reply = get_cute_reply(last_category)
            warning_message = check_budget_warning(sheet, budget_sheet, user_id, last_category, event_time)
            
            all_records = sheet.get_all_records()
            user_balance = 0.0
            for r in all_records:
                if r.get('使用者ID') == user_id:
                    try:
                        user_balance += float(r.get('金額', 0))
                    except (ValueError, TypeError):
                        continue
            
            summary_text = "\n".join(reply_summary_lines)
            return (
                f"{cute_reply}\n\n"
                f"📝 **摘要 (共 {len(reply_summary_lines)} 筆)**：\n"
                f"{summary_text}\n\n"
                f"📈 {user_name} 目前總餘額：{user_balance:.0f} 元"
                f"{warning_message}"
            )

        elif status == 'chat':
            return message or "你好！我是記帳小浣熊 🦝"
        
        else:
            return message or "🦝？ 抱歉，我聽不懂..."

    except json.JSONDecodeError as e:
        logger.error(f"Gemini NLP JSON 解析失敗: {clean_response}")
        return f"糟糕！AI 分析器暫時罷工了 (JSON解析失敗)：{clean_response}"
    except Exception as e:
        logger.error(f"Gemini API 呼叫或 GSheet 寫入失敗：{e}", exc_info=True)
        return f"目前我無法處理這個請求：{str(e)}"

def handle_check_balance(sheet, user_id):
    """
    處理 '查帳' 指令
    """
    logger.debug(f"處理 '查帳' 指令，user_id: {user_id}")
    try:
        records = sheet.get_all_records()
        user_records = [r for r in records if r.get('使用者ID') == user_id]
        
        if not user_records:
            return "您目前沒有任何記帳記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        
        for r in user_records:
            try:
                amount = float(r.get('金額', 0))
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += amount
            except (ValueError, TypeError):
                continue

        total_balance = total_income + total_expense
        
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
    """
    處理 '月結' 指令
    """
    logger.debug(f"處理 '月結' 指令，user_id: {user_id}")
    try:
        records = sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        
        user_month_records = []
        for r in records:
            # === 關鍵修正：使用輔助函式 ===
            record_time_str = get_datetime_from_record(r)
            if (r.get('使用者ID') == user_id and 
                record_time_str.startswith(current_month_str)):
                user_month_records.append(r)
        
        if not user_month_records:
            return f"📅 {current_month_str} 月報表：\n您這個月還沒有任何記錄喔！"

        total_income = 0.0
        total_expense = 0.0
        category_spending = {}

        for r in user_month_records:
            try:
                amount = float(r.get('金額', 0))
                if amount > 0:
                    total_income += amount
                else:
                    total_expense += amount
                    category = r.get('類別', '雜項')
                    category_spending[category] = category_spending.get(category, 0) + abs(amount)
            except (ValueError, TypeError):
                continue
        
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

def handle_delete_record(sheet, user_id):
    """
    處理 '刪除' 指令，刪除使用者的最後一筆記錄
    (此函式使用 index-based 的 get_all_values, 不受標頭名稱影響)
    """
    logger.debug(f"處理 '刪除' 指令，user_id: {user_id}")
    try:
        all_values = sheet.get_all_values()
        user_id_col_index = 3 # A=0, B=1, C=2, D=3
        
        for row_index in range(len(all_values) - 1, 0, -1):
            row = all_values[row_index]
            if len(row) > user_id_col_index and row[user_id_col_index] == user_id:
                row_to_delete = row_index + 1
                
                try:
                    # row[0] 是 '時間'/'日期', row[1] 是 '類別', row[2] 是 '金額'
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

def handle_set_budget(sheet, text, user_id):
    """
    處理 '設置預算' 指令
    """
    logger.debug(f"處理 '設置預算' 指令，user_id: {user_id}, text: {text}")
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
        return f"設置預算失敗：{str(e)}"

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

        transactions_records = trx_sheet.get_all_records()
        current_month_str = event_time.strftime('%Y-%m')
        
        user_month_expenses = []
        for r in transactions_records:
            try:
                amount = float(r.get('金額', 0))
                # === 關鍵修正：使用輔助函式 ===
                record_time_str = get_datetime_from_record(r)
                
                if (r.get('使用者ID') == user_id and
                    record_time_str.startswith(current_month_str) and
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
            reply += f"\n{category} (限額 {limit:.0f} 元)\n"
            reply += f"   {status_icon} 已花費：{spent:.0f} 元\n"
            reply += f"   [{bar_fill}{bar_empty}] {percentage:.0f}%\n"
            reply += f"   剩餘：{remaining:.0f} 元\n"

        reply += "\n--------------------\n"
        if total_limit > 0:
            total_remaining = total_limit - total_spent
            total_percentage = (total_spent / total_limit) * 100
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

def handle_search_records(sheet, user_id, query_text, event_time):
    """
    處理關鍵字和日期區間查詢 (使用 NLP)
    """
    logger.debug(f"處理 '查詢' 指令，user_id: {user_id}, query: {query_text}")

    # 1. 建立日期上下文
    today = event_time.date()
    today_str = today.strftime('%Y-%m-%d')
    
    date_context_lines = [
        f"今天是 {today_str} (星期{today.weekday()})。",
        f"本週一: {(today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')}",
        f"本月一日: {today.replace(day=1).strftime('%Y-%m-%d')}",
        f"昨天: {(today - timedelta(days=1)).strftime('%Y-%m-%d')}",
    ]
    date_context = "\n".join(date_context_lines)

    # 2. 建立查詢專用 Prompt
    prompt = f"""
    你是一個查詢助手。使用者的查詢是：「{query_text}」
    
    目前的日期上下文如下：
    {date_context}

    請嚴格按照以下 JSON 格式回傳：
    {{
      "status": "success" | "failure",
      "keyword": "<string>" | null,
      "start_date": "YYYY-MM-DD" | null,
      "end_date": "YYYY-MM-DD" | null,
      "message": "<string>"
    }}
    
    解析規則：
    1. status: "success"
    2. keyword: 提取查詢的關鍵字 (例如 "雞排", "餐飲")。如果沒有關鍵字，則為 null。
    3. start_date: 提取查詢的 "起始日期"。
    4. end_date: 提取查詢的 "結束日期"。
    5. 如果只提到 "今天"、"昨天" 或 "10/20"，則 start_date 和 end_date 應為同一天。
    6. 如果提到 "這禮拜"，start_date 應為 {date_context_lines[1][-10:]}，end_date 應為 {today_str}。
    7. 如果提到 "這個月"，start_date 應為 {date_context_lines[2][-10:]}，end_date 應為 {today_str}。

    範例：
    輸入: "雞排" -> {{"status": "success", "keyword": "雞排", "start_date": null, "end_date": null, "message": "查詢關鍵字：雞排"}}
    輸入: "這禮拜的餐飲" -> {{"status": "success", "keyword": "餐飲", "start_date": "{(today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')}", "end_date": "{today_str}", "message": "查詢本週的餐飲"}}
    輸入: "10/1 到 10/10" -> {{"status": "success", "keyword": null, "start_date": "{today.year}-10-01", "end_date": "{today.year}-10-10", "message": "查詢 10/1 到 10/10"}}
    輸入: "昨天" -> {{"status": "success", "keyword": null, "start_date": "{(today - timedelta(days=1)).strftime('%Y-%m-%d')}", "end_date": "{(today - timedelta(days=1)).strftime('%Y-%m-%d')}", "message": "查詢昨天的記錄"}}
    """

    try:
        # 3. 呼叫 Gemini 解析查詢
        logger.debug("發送 search prompt 至 Gemini API")
        response = gemini_model.generate_content(prompt)
        clean_response = response.text.strip().replace("```json", "").replace("```", "")
        logger.debug(f"Gemini Search response: {clean_response}")
        
        try:
            parsed_query = json.loads(clean_response)
        except json.JSONDecodeError:
            logger.error(f"Gemini Search JSON 解析失敗: {clean_response}")
            return f"糟糕！AI 查詢分析器暫時罷工了 (JSON解析失敗)。"

        if parsed_query.get('status') == 'failure':
            return parsed_query.get('message', "🦝 查詢失敗，我不太懂您的意思。")

        keyword = parsed_query.get('keyword')
        start_date = parsed_query.get('start_date')
        end_date = parsed_query.get('end_date')
        nlp_message = parsed_query.get('message', f"關鍵字「{keyword or ''}」")

        # 4. 讀取並篩選 Google Sheet 資料
        records = sheet.get_all_records()
        matches = []
        
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None
            end_dt = datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None
        except ValueError as e:
            return f"AI 回傳的日期格式錯誤 ({start_date}, {end_date})。"

        for r in records:
            if r.get('使用者ID') != user_id:
                continue
            
            keyword_match = True
            date_match = True
            
            if keyword:
                keyword_match = (keyword in r.get('類別', '')) or (keyword in r.get('備註', ''))
            
            # === 關鍵修正：使用輔助函式並處理兩種日期格式 ===
            record_datetime_str = get_datetime_from_record(r)
            
            if (start_dt or end_dt) and record_datetime_str:
                try:
                    # 嘗試解析 YYYY-MM-DD HH:MM:SS (新)
                    if len(record_datetime_str) > 10:
                        record_dt = datetime.strptime(record_datetime_str, '%Y-%m-%d %H:%M:%S').date()
                    # 嘗試解析 YYYY-MM-DD (舊)
                    else:
                        record_dt = datetime.strptime(record_datetime_str, '%Y-%m-%d').date()
                        
                    if start_dt and record_dt < start_dt:
                        date_match = False
                    if end_dt and record_dt > end_dt:
                        date_match = False
                except ValueError:
                    date_match = False 
            
            if keyword_match and date_match:
                matches.append(r)
        
        # 5. 格式化回覆
        if not matches:
            return f"🦝 找不到關於「{nlp_message}」的任何記錄喔！"
        
        reply = f"🔎 {nlp_message} (共 {len(matches)} 筆)：\n\n"
        limit = 20 
        
        # === 關鍵修正：使用輔助函式排序 ===
        sorted_matches = sorted(matches, key=lambda x: get_datetime_from_record(x), reverse=True)
        
        total_amount_all_matches = 0.0
        
        for r in sorted_matches:
             try:
                amount = float(r.get('金額', 0))
                total_amount_all_matches += amount
                
                if len(reply.split('\n')) <= limit + 5: 
                    category = r.get('類別', 'N/A')
                    notes = r.get('備註', 'N/A')
                    
                    # ===  使用輔助函式並處理兩種日期格式  ===
                    date_str = get_datetime_from_record(r)
                    
                    if not date_str:
                         display_date = "N/A"
                    else:
                        try:
                            # 嘗試格式化 YYYY-MM-DD HH:MM:SS (新)
                            if len(date_str) > 10:
                                display_date = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M')
                            # 嘗試格式化 YYYY-MM-DD (舊)
                            else:
                                display_date = datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y-%m-%d')
                        except ValueError:
                            display_date = date_str # 備案：直接顯示原始字串
                    
                    reply += f"• {display_date} {notes} ({category}) {amount:.0f} 元\n"
                    
             except (ValueError, TypeError):
                continue
        
        reply += f"\n--------------------\n"
        reply += f"📈 查詢總計：{total_amount_all_matches:.0f} 元\n"
        
        if len(matches) > limit:
            reply += f"(僅顯示最近 {limit} 筆記錄)"
            
        return reply
        
    except Exception as e:
        logger.error(f"查詢記錄失敗：{e}", exc_info=True)
        return f"查詢失敗：{str(e)}"

# === 主程式入口 ===
if __name__ == "__main__":
    logger.info("Starting Flask server locally...")
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)