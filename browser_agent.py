import asyncio
import os
import re
from playwright.async_api import async_playwright

class AutonomousBrowserAgent:
    def __init__(self):
        pass

    async def open_and_search(self, target_url: str, search_query: str):
        """
        Opens a real browser window, goes to a website, types query, and screenshots.
        Automatically cleans markdown artifacts from URLs.
        """
        # --- AUTO-CLEAN URL (Removes markdown junk if pasted) ---
        target_url = re.sub(r'\]\(.*?\)', '', target_url)
        target_url = target_url.strip('[]() <>\'"')
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            
            try:
                await page.goto(target_url)
                await asyncio.sleep(2)
                
                search_box = await page.wait_for_selector('input[type="text"], input[name="field-keywords"], input[name="q"]', timeout=5000)
                if search_box:
                    await search_box.fill(search_query)
                    await search_box.press('Enter')
                    await asyncio.sleep(4)
                    
                    title = await page.title()
                    os.makedirs("workspace/output", exist_ok=True)
                    screenshot_path = "workspace/output/browser_view.png"
                    await page.screenshot(path=screenshot_path)
                    
                    await browser.close()
                    return f"Success! Navigated to {target_url}, searched for '{search_query}'. Page Title: {title}. Screenshot saved at {screenshot_path}"
                else:
                    await browser.close()
                    return "Could not find a search box on this page."
            except Exception as e:
                await browser.close()
                return f"Browser Agent Error: {str(e)}"
