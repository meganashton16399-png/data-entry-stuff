import os
import logging
import asyncio
import json
import traceback
import html
from dotenv import load_dotenv

# Telegram & Files
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from pdf2image import convert_from_path
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

# AI Agents
from playwright.async_api import async_playwright
import google.generativeai as genai

# Load Environment Variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))

# --- CREDENTIALS ---
OPENAI_EMAIL = os.getenv("OPENAI_EMAIL")
OPENAI_PASS = os.getenv("OPENAI_PASS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

# --- LOGGING SETUP ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. THE NOTIFIER (Sends logs & Errors to You) ---
async def send_log(context: ContextTypes.DEFAULT_TYPE, message: str, is_error=False):
    """Sends a system log to your Telegram Chat"""
    try:
        header = "❌ <b>ERROR</b>" if is_error else "📝 <b>LOG</b>"
        # Truncate message if it's too long for Telegram (4096 chars limit)
        clean_message = html.escape(str(message))[:3500] 
        
        await context.bot.send_message(
            chat_id=ALLOWED_USER_ID, 
            text=f"{header}: <code>{clean_message}</code>", 
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to send log to Telegram: {e}")

# --- 2. GLOBAL ERROR HANDLER (Catches Crashes) ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log the error and send a telegram message to notify the developer."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Get the traceback (the error details)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    # Send error to Telegram
    error_message = f"An exception was raised while handling an update\n\n{tb_string}"
    await send_log(context, error_message, is_error=True)

# --- 3. BROWSER AGENT (ChatGPT Pro) ---
async def browser_agent_extract(image_path):
    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"]) 
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            page = await context.new_page()

            # Login
            await page.goto("https://chatgpt.com/auth/login", timeout=30000)
            await page.fill('input[type="email"]', OPENAI_EMAIL)
            await page.click('button.continue-btn') 
            await page.fill('input[type="password"]', OPENAI_PASS, timeout=5000)
            await page.click('button[type="submit"]')
            await page.wait_for_selector("#prompt-textarea", timeout=15000)

            # Upload
            async with page.expect_file_chooser() as fc_info:
                await page.click('.upload-button') 
            file_chooser = await fc_info.value
            await file_chooser.set_files(image_path)

            prompt = "Extract this handwritten Hindi Parivar Register into valid JSON. Keys: name, father_husband_name, gender, caste, dob, occupation, literacy."
            await page.fill('#prompt-textarea', prompt)
            await page.keyboard.press('Enter')

            # Scrape
            await page.wait_for_selector(".markdown", timeout=45000)
            response_text = await page.inner_text(".markdown")
            await browser.close()
            return json.loads(response_text)

        except Exception as e:
            if browser: await browser.close()
            raise e # Pass error up to trigger fallback

# --- 4. GEMINI API (Fallback) ---
async def gemini_api_extract(image_path):
    model = genai.GenerativeModel('gemini-1.5-pro-latest')
    myfile = genai.upload_file(image_path)
    prompt = "Extract this handwritten Hindi Parivar Register into JSON list. Keys: name, father_husband_name, gender, caste, dob, occupation, literacy. Output ONLY JSON."
    
    result = await model.generate_content_async([myfile, prompt])
    clean_text = result.text.replace("```json", "").replace("```", "")
    return json.loads(clean_text)

# --- 5. PDF GENERATOR ---
def create_hindi_pdf(data, output_filename):
    doc = SimpleDocTemplate(output_filename, pagesize=A4)
    elements = []
    
    # Attempt to load Hindi Font
    try:
        pdfmetrics.registerFont(TTFont('HindiFont', 'Nirmala.ttf'))
        font_name = 'HindiFont'
    except:
        print("Font Error")
        font_name = 'Helvetica'

    styles = getSampleStyleSheet()
    elements.append(Paragraph("Parivar Register Extraction", styles['Title']))
    
    headers = ["Name", "Father", "Gender", "Caste", "DOB", "Occ.", "Lit."]
    table_data = [headers]
    
    for item in data:
        row = [
            item.get('name', '-'), item.get('father_husband_name', '-'), 
            item.get('gender', '-'), item.get('caste', '-'), 
            item.get('dob', '-'), item.get('occupation', '-'), item.get('literacy', '-')
        ]
        table_data.append(row)

    t = Table(table_data)
    t.setStyle(TableStyle([('FONT', (0,0), (-1,-1), font_name), ('GRID', (0,0), (-1,-1), 1, colors.black)]))
    elements.append(t)
    doc.build(elements)

# --- 6. MAIN DOCUMENT HANDLER ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID: return

    # Notify Start
    await send_log(context, "PDF received. Starting process...")
    status_msg = await update.message.reply_text("⏳ Processing...")
    
    file = await update.message.document.get_file()
    pdf_path = f"temp_{user_id}.pdf"
    await file.download_to_drive(pdf_path)
    
    try:
        images = convert_from_path(pdf_path)
        final_data = []

        for i, img in enumerate(images):
            page_num = i + 1
            img_path = f"page_{i}.jpg"
            img.save(img_path, "JPEG")
            
            # 1. Try Browser
            try:
                await send_log(context, f"Page {page_num}: Trying ChatGPT Pro (Browser)...")
                page_data = await browser_agent_extract(img_path)
                await send_log(context, f"✅ Page {page_num}: ChatGPT Success!")
            
            # 2. Fallback to API
            except Exception as e:
                await send_log(context, f"⚠️ Page {page_num} Browser Failed: {e}", is_error=True)
                await send_log(context, f"↪️ Switching to Gemini API...")
                page_data = await gemini_api_extract(img_path)
                await send_log(context, f"✅ Page {page_num}: Gemini API Success!")

            final_data.extend(page_data)
            os.remove(img_path)

        # Generate & Send
        create_hindi_pdf(final_data, "output.pdf")
        await update.message.reply_document(document=open("output.pdf", "rb"), caption="✅ Done!")
        await send_log(context, "Task Complete.")
        os.remove("output.pdf")

    except Exception as e:
        # This catches logic errors in the main loop
        await send_log(context, f"CRITICAL LOOP ERROR: {traceback.format_exc()}", is_error=True)
    
    finally:
        if os.path.exists(pdf_path): os.remove(pdf_path)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Add Handlers
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    
    # Add the Global Error Handler (The most important part for you)
    app.add_error_handler(error_handler)
    
    print("Bot Started...")
    app.run_polling()

