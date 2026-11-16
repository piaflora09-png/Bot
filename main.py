import asyncio
import json
import os
import time
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from bs4 import BeautifulSoup
from google import genai
from google.genai.errors import APIError
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# --- CONFIGURATION (Kukunin sa Render Environment Variables) ---
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
SHOP_URL = "https://vortexshop.onrender.com/"

# Siguraduhing may token na nailagay
if not DISCORD_BOT_TOKEN:
    raise ValueError("CRITICAL ERROR: DISCORD_TOKEN or DISCORD_BOT_TOKEN environment variable is missing!")

FREE_MODEL = "gemini-3.5-flash-lite"
CHECK_INTERVAL_MINUTES = 5

ai_client = genai.Client(api_key=GEMINI_API_KEY)

_previous_item_names = set()
_is_first_check = True

class VortexFreeAIBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        auto_check_shop.start()
        print("Slash commands synced and auto-checker started!")

bot = VortexFreeAIBot()

def scrape_vortexshop_sync() -> str:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") # Kailangan para sa Render container

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(SHOP_URL)

        def catalog_loaded(d):
            grid = d.find_elements(By.CSS_SELECTOR, '[aria-live="polite"]')
            has_items = any(el.text.strip() != "" for el in grid)
            empty_shown = any(
                "No products yet" in el.text and el.is_displayed()
                for el in d.find_elements(By.TAG_NAME, "div")
            )
            return has_items or empty_shown

        try:
            WebDriverWait(driver, 25).until(catalog_loaded)
        except Exception:
            pass

        time.sleep(2.0)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        for hidden_el in soup.select("[hidden], [aria-hidden='true']"):
            hidden_el.decompose()

        main_content = soup.find("main") or soup
        return main_content.get_text(separator="\n", strip=True)
    finally:
        driver.quit()

async def get_structured_items(page_text: str) -> list[dict]:
    prompt = f"""
Analyze the website text below and extract all items available in the shop.
Return ONLY a valid JSON list (array) of objects. Each object must have:
- "category": string (e.g. ADOPT ME, or game title if visible, otherwise "VORTEXSHOP")
- "name": string (Item name, e.g. Strawberry Shortcake Bat Dragon)
- "price": string (Price with currency, e.g. $18.79)
- "stock": string (Stock badge text, e.g. 3 IN STOCK or In Stock)
- "image": string (Image URL if found in the HTML src, otherwise empty string)

If there are no products or it says "No products yet", return an empty JSON list: []

Website text:
{page_text[:6000]}
"""
    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model=FREE_MODEL,
            contents=prompt,
        )
        raw_text = response.text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
        
        items = json.loads(raw_text.strip())
        return items if isinstance(items, list) else []
    except Exception:
        return []

async def send_single_item(webhook_or_interaction, item: dict, is_webhook: bool = True):
    category = item.get('category', 'VORTEXSHOP').upper()
    name = item.get('name', 'Unknown Item')
    price = item.get('price', 'N/A')
    stock_status = item.get('stock', 'In Stock')

    embed = discord.Embed(
        title=name,
        url=f"{SHOP_URL}#shop",
        description=f"**{category}**\n\n📦 **Stock:** `{stock_status}`\n💵 **PRICE:** **{price}**",
        color=discord.Color.from_rgb(17, 24, 39)
    )
    
    img_url = item.get('image', '')
    if img_url and img_url.startswith("http"):
        embed.set_image(url=img_url)

    if is_webhook:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(DISCORD_WEBHOOK_URL, session=session)
            await webhook.send(username="VortexShop New Item", embed=embed)
    else:
        await webhook_or_interaction.followup.send(embed=embed)

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def auto_check_shop():
    global _previous_item_names, _is_first_check
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD_WEBHOOK_URL" in DISCORD_WEBHOOK_URL:
        return
    try:
        page_text = await asyncio.to_thread(scrape_vortexshop_sync)
        current_items = await get_structured_items(page_text)
        current_names = {item.get('name') for item in current_items if item.get('name')}

        if _is_first_check:
            _previous_item_names = current_names
            _is_first_check = False
            print("Initial shop scan completed on Render. Tracking new items...")
            return

        new_item_names = current_names - _previous_item_names

        if new_item_names:
            for item in current_items:
                if item.get('name') in new_item_names:
                    await send_single_item(None, item, is_webhook=True)
                    print(f"New item detected and sent: {item.get('name')}")
                    await asyncio.sleep(1.0)
            
            _previous_item_names = current_names
        else:
            print("No new items added.")
    except Exception as e:
        print(f"Error in auto_check_shop: {e}")

@auto_check_shop.before_loop
async def before_auto_check():
    await bot.wait_until_ready()

@bot.tree.command(name="stock", description="Check current items in the shop")
@app_commands.describe(refresh="True to force refresh from the website")
async def stock(interaction: discord.Interaction, refresh: bool = False):
    await interaction.response.defer(thinking=True)

    try:
        page_text = await asyncio.to_thread(scrape_vortexshop_sync)
        items = await get_structured_items(page_text)
        
        if not items:
            await interaction.followup.send("No products are currently listed in the shop.")
            return

        for item in items:
            await send_single_item(interaction, item, is_webhook=False)
            await asyncio.sleep(1.0)
    except Exception as e:
        await interaction.followup.send(f"An error occurred while fetching stock: `{e}`")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} on Render!")

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
