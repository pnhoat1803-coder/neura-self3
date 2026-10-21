import discord
from discord.ext import commands, tasks
import json
import asyncio
import time
import datetime
from colorama import init, Fore, Style
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import glob
import os
import re
import sys

init(autoreset=True)

def get_time():
    return datetime.datetime.now().strftime("%I:%M:%S %p")

def log_msg(level, msg, user="Bot"):
    colors = {
        "SYS": Fore.CYAN,
        "SUCCESS": Fore.GREEN,
        "ERROR": Fore.RED,
        "INFO": Fore.CYAN,
        "VOTE": Fore.WHITE,
        "WARN": Fore.YELLOW
    }
    color = colors.get(level, Fore.WHITE)
    print(f"{Fore.MAGENTA}[{user}] {Fore.GREEN}{get_time()} {color}[{level}] {Style.RESET_ALL}{msg}")

def check_login_required(driver):
    """
    Scans the current page DOM to detect if the Top.gg 'Login' button is present.
    Returns True if login is required, otherwise False.
    """
    login_xpath = "//a[contains(@href, '/auth/login') and contains(translate(., 'LOGIN', 'login'), 'login')]"
    try:
        login_elements = driver.find_elements(By.XPATH, login_xpath)
        for elem in login_elements:
            if elem.is_displayed():
                return True, elem
    except Exception:
        pass
    return False, None

def handle_login_mode(driver, username, login_btn=None):
    """
    Executes login mode procedures when authentication is required.
    Navigates to the Discord OAuth authorization flow.
    """
    log_msg("WARN", "Top.gg authentication required! Switching to LOGIN MODE...", user=username)
    try:
        if login_btn:
            login_url = login_btn.get_attribute("href")
            log_msg("SYS", f"Redirecting to login URL: {login_url}", user=username)
            driver.get(login_url)
        else:
            driver.get("https://top.gg/api/client/login")
        
        time.sleep(5)
        # Check if redirected to Discord authorization page
        if "discord.com" in driver.current_url:
            log_msg("INFO", "Navigated to Discord OAuth page. Attempting session sync...", user=username)
            time.sleep(3)
    except Exception as e:
        log_msg("ERROR", f"Failed during login mode execution: {e}", user=username)

def run_selenium_logic(config, token, username, links_to_vote):
    is_headless = config.get("headless", False)
    mode_str = "Headless Mode" if is_headless else "GUI Mode (Visible)"
    log_msg("SYS", f"Initializing Chrome browser ({mode_str})...", user=username)
    
    options = Options()
    if is_headless:
        options.add_argument("--headless=new")
        
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--mute-audio")
    
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--remote-allow-origins=*")
    
    profile_dir = os.path.abspath("chrome_profile")
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--log-level=3")
    
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    results = {}
    driver = None
    
    try:
        driver = webdriver.Chrome(options=options)
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            '''
        })
        log_msg("SUCCESS", "Chrome opened successfully!", user=username)
    except Exception as e:
        log_msg("ERROR", f"Failed to open Chrome: {e}", user=username)
        return False
        
    try:
        log_msg("SYS", "Checking Discord login session...", user=username)
        driver.get("https://discord.com/channels/@me")
        time.sleep(3)
        
        is_logged_in = False
        try:
            if "channels" in driver.current_url or driver.find_elements(By.XPATH, "//div[contains(@class, 'channels-')]"):
                log_msg("SUCCESS", "Discord session restored!", user=username)
                is_logged_in = True
        except Exception:
            pass

        if not is_logged_in and token:
            log_msg("SYS", "Injecting token into Chrome...", user=username)
            driver.get("https://discord.com/login")
            time.sleep(2)
            script = f"""
            let iframe = document.createElement('iframe');
            document.body.appendChild(iframe);
            iframe.contentWindow.localStorage.token = '"{token}"';
            setTimeout(() => {{ location.reload(); }}, 1500);
            """
            driver.execute_script(script)
            time.sleep(5)

        MAX_TABS = 5
        batches = [links_to_vote[i:i + MAX_TABS] for i in range(0, len(links_to_vote), MAX_TABS)]

        for batch_idx, batch in enumerate(batches):
            log_msg("SYS", f"Batch {batch_idx + 1}/{len(batches)}: Processing {len(batch)} targets...", user=username)
            
            handles = []
            for idx, link in enumerate(batch):
                if idx == 0:
                    driver.get(link)
                    handles.append(driver.current_window_handle)
                else:
                    driver.execute_script("window.open(arguments[0], '_blank');", link)
                    handles.append(driver.window_handles[-1])
                time.sleep(1)

            for idx, (handle, link) in enumerate(zip(handles, batch)):
                try:
                    driver.switch_to.window(handle)
                except Exception:
                    continue
                time.sleep(4)

                bot_name = "Unknown Bot"
                try:
                    title_text = driver.title
                    if "Vote for" in title_text:
                        bot_name = title_text.split("|")[0].replace("Vote for", "").strip()
                    else:
                        h1_el = driver.find_elements(By.XPATH, "//h1")
                        if h1_el:
                            bot_name = h1_el[0].text.replace("Voting for", "").replace("Vote for", "").strip()
                except Exception:
                    pass
                    
                if not bot_name or bot_name.lower() == "unknown bot":
                    bot_name = link.split("/")[-2] if "bot" in link else "Bot"
                
                log_msg("VOTE", f"Target: {bot_name.upper()} (Tab {idx + 1})", user=username)

                # --- NEW FEATURE: LOGIN DETECTION & LOGIN MODE TRIGGER ---
                needs_login, login_btn = check_login_required(driver)
                if needs_login:
                    handle_login_mode(driver, username, login_btn)
                    # Set short retry interval since voting was skipped due to login process
                    results[link] = 600
                    continue
                # ---------------------------------------------------------

                already_voted = driver.find_elements(By.XPATH, "//p[contains(text(), 'You can vote again in')]")
                if already_voted:
                    time_text = already_voted[0].text
                    log_msg("INFO", f"[{bot_name}] Cooldown active: {time_text}", user=username)
                    
                    hours_match = re.search(r'(\d+)\s*hour', time_text)
                    mins_match = re.search(r'(\d+)\s*minute', time_text)
                    
                    if hours_match:
                        cooldown = (int(hours_match.group(1)) * 3600) + 300
                    elif mins_match:
                        cooldown = (int(mins_match.group(1)) * 60) + 60
                    else:
                        cooldown = 12 * 3600
                    
                    results[link] = cooldown
                    continue

                try:
                    xpath_query = (
                        "//button[contains(translate(., 'VOTE', 'vote'), 'vote')]"
                        " | //*[@role='button' and contains(translate(., 'VOTE', 'vote'), 'vote')]"
                    )
                    
                    vote_btn = WebDriverWait(driver, 12).until(
                        EC.element_to_be_clickable((By.XPATH, xpath_query))
                    )
                    
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", vote_btn)
                    time.sleep(1)
                    vote_btn.click()
                    log_msg("SUCCESS", f"[{bot_name}] Clicked VOTE button!", user=username)
                    time.sleep(4)
                except TimeoutException:
                    # Re-check login requirement if button timeout occurred
                    needs_login, login_btn = check_login_required(driver)
                    if needs_login:
                        handle_login_mode(driver, username, login_btn)
                        results[link] = 600
                    else:
                        log_msg("ERROR", f"[{bot_name}] Vote button not clickable/found.", user=username)
                        results[link] = 3600
                    continue
                    
                try:
                    ad_vote_btn = driver.find_elements(By.XPATH, "//button[contains(translate(., 'VOTE', 'vote'), 'vote')]")
                    if ad_vote_btn:
                        time.sleep(3)
                        target_btn = ad_vote_btn[-1]
                        target_btn.click()
                        log_msg("SUCCESS", f"[{bot_name}] VOTE COMPLETED!", user=username)
                except Exception:
                    pass
                    
                interval_h = config.get("interval_h") or config.get("intervalH") or 12
                results[link] = int(interval_h) * 3600 if interval_h else 12 * 3600

            for handle in handles[1:]:
                try:
                    driver.switch_to.window(handle)
                    driver.close()
                except Exception:
                    pass
            if handles:
                try:
                    driver.switch_to.window(handles[0])
                except Exception:
                    pass

    finally:
        log_msg("SYS", "Closing Chrome...", user=username)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            
    return results

class AutoVote(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.vote_schedule = {}
        self.vote_task.start()

    def get_config(self):
        if hasattr(self, 'bot') and getattr(self.bot, 'config', None):
            bot_cfg = self.bot.config
            cfg = bot_cfg.get("autoVote") or bot_cfg.get("vote") or bot_cfg.get("auto_vote")
            if cfg and isinstance(cfg, dict):
                return cfg, "Dashboard (Live Memory)"

        try:
            import core.state as state
            if hasattr(state, 'config') and isinstance(state.config, dict):
                cfg = state.config.get("autoVote") or state.config.get("vote") or state.config.get("auto_vote")
                if cfg and isinstance(cfg, dict):
                    return cfg, "Dashboard (core.state)"
        except Exception:
            pass

        possible_files = []
        try:
            import core.state as state
            if hasattr(state, 'CONFIG_DIR'):
                if hasattr(self.bot, 'user') and self.bot.user:
                    possible_files.append(os.path.join(state.CONFIG_DIR, f'settings_{self.bot.user.id}.json'))
                possible_files.append(os.path.join(state.CONFIG_DIR, 'settings.json'))
        except Exception:
            pass

        possible_files += [
            "settings.json",
            "config/settings.json",
            "config.json",
            "config/config.json"
        ] + glob.glob("config/settings*.json") + glob.glob("settings*.json")

        seen = set()
        files_to_check = []
        for f in possible_files:
            if f not in seen and os.path.exists(f):
                seen.add(f)
                files_to_check.append(f)

        for file_path in files_to_check:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    cfg = data.get("autoVote") or data.get("vote") or data.get("auto_vote")
                    if cfg and isinstance(cfg, dict):
                        return cfg, file_path
            except Exception:
                continue
                
        return {}, "Settings not found"

    def extract_vote_links(self, config):
        links = []
        raw_items = (
            config.get("targetLinks") or 
            config.get("TargetLinks") or 
            config.get("target_links") or 
            config.get("bot_ids") or []
        )
        
        if isinstance(raw_items, str):
            raw_items = [b.strip() for b in re.split(r'[\n,\s]+', raw_items) if b.strip()]
        elif isinstance(raw_items, (int, float)):
            raw_items = [str(raw_items)]
        elif not isinstance(raw_items, list):
            raw_items = []

        for item in raw_items:
            item_str = str(item).strip()
            if not item_str:
                continue
            
            if item_str.isdigit():
                link = f"https://top.gg/bot/{item_str}/vote"
            elif item_str.startswith("http://") or item_str.startswith("https://"):
                link = item_str
            else:
                clean_id = re.sub(r'\D', '', item_str)
                link = f"https://top.gg/bot/{clean_id}/vote" if clean_id else item_str

            if link not in links:
                links.append(link)

        return links

    @tasks.loop(minutes=2)
    async def vote_task(self):
        username = self.bot.user.name if self.bot.user else "SYS"
        
        config, config_source = self.get_config()
        
        if not config.get("enabled", False) and not config.get("Enabled", False):
            return

        target_links = self.extract_vote_links(config)
        if not target_links:
            log_msg("ERROR", f"No vote targets configured (Source: {config_source}).", user=username)
            return

        current_time = time.time()
        links_to_vote = [link for link in target_links if current_time >= self.vote_schedule.get(link, 0)]
                
        if not links_to_vote:
            return

        log_msg("SYS", f"--- AUTO VOTE SCANNING {len(links_to_vote)} TARGETS (Source: {config_source}) ---", user=username)
        token = getattr(self.bot, 'token', None) or self.bot.http.token

        result = await asyncio.to_thread(run_selenium_logic, config, token, username, links_to_vote)
        
        if isinstance(result, dict):
            for link, cooldown_sec in result.items():
                next_vote_time = time.time() + cooldown_sec
                self.vote_schedule[link] = next_vote_time
                dt = datetime.datetime.fromtimestamp(next_vote_time).strftime("%I:%M %p")
                log_msg("INFO", f"Link {link[-25:]}... next vote at {dt}", user=username)

    @vote_task.before_loop
    async def before_vote_task(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(AutoVote(bot))