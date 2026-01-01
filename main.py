import os
import logging
import json
import asyncio
import traceback
import html
from dotenv import load_dotenv

# Telegram
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from pdf2image import convert_from_path

# AI Libraries
from playwright.async_api import async_playwright
import google.generativeai as genai

# Load Config
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER = int(os.getenv("ALLOWED_USER_ID"))

# Credentials
GOOGLE_EMAIL = os.getenv("GOOGLE_EMAIL")
GOOGLE_PASS = os.getenv("GOOGLE_PASS")
OPENAI_EMAIL = os.getenv("OPENAI_EMAIL")
OPENAI_PASS = os.getenv("OPENAI_PASS")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# Setup Fallback API
genai.configure(api_key=GEMINI_KEY)

# Logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. LIVE LOGGING SYSTEM ---
async def send_log(context: ContextTypes.DEFAULT_TYPE, message: str, is_error=False):
    """Sends real-time logs to Telegram"""
    try:
        emoji = "❌" if is_error else "📝"
        # Escape HTML chars to prevent bot crashing on special symbols
        clean_msg = html.escape(str(message))[:3000]
        await context.bot.send_message(
            chat_id=ALLOWED_USER,
            text=f"{emoji} <code>{clean_msg}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Log Error: {e}")

# --- 2. AGENT A: GEMINI BROWSER (Primary Extraction) ---
async def gemini_browser_extract(image_path, context):
    """Login to Google -> Gemini -> Extract"""
    await send_log(context, "🔹 Agent A: Launching Gemini Web Browser...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context_browser = await browser.new_context()
        page = await context_browser.new_page()

        try:
            # Login
            await page.goto("https://accounts.google.com/signin/v2/identifier?service=mail", timeout=30000)
            await page.fill('input[type="email"]', GOOGLE_EMAIL)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2000)
            await page.fill('input[type="password"]', GOOGLE_PASS)
            await page.keyboard.press("Enter")
            
            # Wait for redirect (This is where it usually breaks on servers)
            await page.wait_for_url("**/myaccount.google.com**", timeout=20000)
            await send_log(context, "🔹 Agent A: Login Success!")

            # Go to Gemini
            await page.goto("https://gemini.google.com/app")
            
            # Upload
            async with page.expect_file_chooser() as fc_info:
                await page.click('div[role="button"][aria-label*="Upload"]', timeout=5000)
            file_chooser = await fc_info.value
            await file_chooser.set_files(image_path)

            # Prompt
            prompt = "Extract handwritten Hindi Parivar Register. Return ONLY valid JSON list. Keys: makan_no, name, father_husband, gender, caste, dob, occupation."
            await page.fill('div[role="textbox"]', prompt)
            await page.keyboard.press('Enter')

            # Scrape
            await page.wait_for_selector("model-response", timeout=60000)
            text = await page.inner_text("model-response")
            
            await browser.close()
            return json.loads(text.replace("```json", "").replace("```", ""))

        except Exception as e:
            await browser.close()
            raise Exception(f"Gemini Browser Failed: {str(e)[:50]}")

# --- 3. AGENT B: GEMINI API (Fallback Extraction) ---
async def gemini_api_extract(image_path, context):
    """Uses Free API Key if Browser Fails"""
    await send_log(context, "🔸 Agent B: Switching to Gemini API...")
    
    model = genai.GenerativeModel('gemini-1.5-pro-latest')
    myfile = genai.upload_file(image_path)
    
    prompt = """
    Extract this Hindi Parivar Register data into a JSON List.
    Keys required: 
    - makan_no (House Number)
    - name (Member Name in Hindi)
    - father_husband (Name in Hindi)
    - gender (Male/Female)
    - caste (Hindi)
    - dob (DD-MM-YYYY)
    - occupation (Hindi)
    
    Output strictly valid JSON.
    """
    
    result = await model.generate_content_async([myfile, prompt])
    clean_text = result.text.replace("```json", "").replace("```", "")
    return json.loads(clean_text)

# --- 4. AGENT C: CHATGPT BROWSER (Verification) ---
async def chatgpt_verify(image_path, initial_data, context):
    """Login to ChatGPT -> Verify Data -> Return Final JSON"""
    await send_log(context, "🧠 Agent C: Sending to ChatGPT Pro for Verification...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()

        try:
            # Login
            await page.goto("https://chatgpt.com/auth/login")
            await page.fill('input[type="email"]', OPENAI_EMAIL)
            await page.click('button.continue-btn')
            await page.fill('input[type="password"]', OPENAI_PASS)
            await page.click('button[type="submit"]')
            await page.wait_for_selector("#prompt-textarea", timeout=20000)
            
            # Upload & Prompt
            async with page.expect_file_chooser() as fc_info:
                await page.click('.upload-button')
            await fc_info.value.set_files(image_path)

            data_str = json.dumps(initial_data, ensure_ascii=False)
            prompt = f"""
            I have extracted this data: {data_str}
            
            VERIFY this data against the uploaded image.
            1. Correct any spelling mistakes in Hindi names.
            2. Verify Dates of Birth.
            3. Ensure 'Makan No' is correct.
            
            Return the corrected data as a valid JSON List.
            """
            
            await page.fill('#prompt-textarea', prompt)
            await page.keyboard.press('Enter')

            # Scrape
            await page.wait_for_selector(".markdown", timeout=60000)
            text = await page.inner_text(".markdown")
            
            await browser.close()
            return json.loads(text.replace("```json", "").replace("```", ""))

        except Exception as e:
            await browser.close()
            await send_log(context, f"⚠️ ChatGPT Verification Failed: {e}. Using original data.", is_error=True)
            return initial_data # Return original data if verification fails

# --- 5. FORMATTER (Sends Individual Messages) ---
async def send_formatted_output(context, data_list):
    """Sends one message per family member"""
    await send_log(context, f"📤 Sending {len(data_list)} extracted entries...")
    
    for item in data_list:
        msg = (
            f"🏠 **Makan No:** {item.get('makan_no', 'N/A')}\n"
            f"👤 **Name:** {item.get('name', 'N/A')}\n"
            f"👨‍👦 **Father/Husband:** {item.get('father_husband', 'N/A')}\n"
            f"🚻 **Gender:** {item.get('gender', 'N/A')}\n"
            f"📅 **DOB:** {item.get('dob', 'N/A')}\n"
            f"🛠 **Occupation:** {item.get('occupation', 'N/A')}\n"
            f"🏷 **Caste:** {item.get('caste', 'N/A')}"
        )
        # Send formatted message
        await context.bot.send_message(chat_id=ALLOWED_USER, text=msg)
        await asyncio.sleep(0.5) # Prevent flooding Telegram

# --- 6. MAIN LOGIC ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER: return

    await send_log(context, "📥 PDF Received. Starting Sequence...")
    
    file = await update.message.document.get_file()
    pdf_path = f"temp.pdf"
    await file.download_to_drive(pdf_path)
    
    try:
        images = convert_from_path(pdf_path)
        
        for i, img in enumerate(images):
            img_path = f"page_{i}.jpg"
            img.save(img_path, "JPEG")
            
            await send_log(context, f"--- Processing Page {i+1} ---")
            
            # STEP 1: EXTRACTION (Try Browser -> Fail to API)
            raw_data = []
            try:
                raw_data = await gemini_browser_extract(img_path, context)
                await send_log(context, "✅ Extraction Source: Gemini Web")
            except Exception as e:
                await send_log(context, f"⚠️ Agent A Failed: {e}", is_error=True)
                # Fallback
                try:
                    raw_data = await gemini_api_extract(img_path, context)
                    await send_log(context, "✅ Extraction Source: Gemini API")
                except Exception as api_e:
                     await send_log(context, f"❌ FATAL: API also failed. Skipping page. {api_e}", is_error=True)
                     continue

            # STEP 2: VERIFICATION (ChatGPT)
            final_data = await chatgpt_verify(img_path, raw_data, context)
            
            # STEP 3: INDIVIDUAL MESSAGE OUTPUT
            await send_formatted_output(context, final_data)
            
            os.remove(img_path)

        await send_log(context, "🏁 All pages processed successfully.")

    except Exception as e:
        await send_log(context, f"CRITICAL SYSTEM ERROR: {traceback.format_exc()}", is_error=True)
    finally:
        if os.path.exists(pdf_path): os.remove(pdf_path)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    print("Bot is Alive...")
    app.run_polling()
