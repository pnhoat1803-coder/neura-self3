# This file is part of NeuraSelf-UwU.
# Copyright (c) 2025-Present Routo

from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from functools import wraps
import threading
import time
import json
import logging
import os
import secrets
import asyncio
from datetime import datetime, timedelta
import socket
import aiohttp
import requests

import core.state as state
import utils.utils as utils

# --- PATCH SOCKET FOR OWO BOT ---
_original_getaddrinfo = socket.getaddrinfo

def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == 'owobot.com':
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('104.21.35.189', port))]
    return _original_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = patched_getaddrinfo

# --- FLASK APP CONFIGURATION ---
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
try:
    app.json.sort_keys = False
except AttributeError:
    pass

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

AUTH_FILE = os.path.join(state.CONFIG_DIR, 'auth.json')
DASHBOARD_OVERRIDES_FILE = os.path.join(state.CONFIG_DIR, 'dashboard_overrides.json')

# Thư mục lưu Chrome Profiles / Cookies cho từng Account
BROWSER_PROFILES_DIR = os.path.join(state.CONFIG_DIR, 'browser_profiles')
os.makedirs(BROWSER_PROFILES_DIR, exist_ok=True)

# --- THREAD LOCKS & GLOBAL VARIABLES ---
LOGIN_ATTEMPTS = {}
_pending_captchas = {}

attempts_lock = threading.Lock()
overrides_lock = threading.Lock()
captcha_lock = threading.Lock()

BLOCK_DURATION = 300  
MAX_ATTEMPTS = 5

def load_auth_config():
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            if cfg.get('secret_key', '').startswith("generate_a_random_long_secret_key_here_please"):
                new_secret = secrets.token_hex(32)
                cfg['secret_key'] = new_secret
                with open(AUTH_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, indent=4)
            return cfg
        except Exception:
            pass
    return None

auth_cfg = load_auth_config()
if auth_cfg:
    app.secret_key = auth_cfg.get('secret_key', 'neuraself_fallback_secret')
else:
    app.secret_key = 'temporary_secret_key'

# --- HELPER FUNCTIONS FOR CHROME COOKIE / PROFILE PATHS ---

def get_account_profile_dir(account_id):
    """Tạo và trả về đường dẫn thư mục lưu Profile/Cookie Chrome riêng cho tài khoản"""
    safe_id = str(account_id or 'default_profile').replace('/', '_').replace('\\', '_')
    profile_path = os.path.join(BROWSER_PROFILES_DIR, safe_id)
    os.makedirs(profile_path, exist_ok=True)
    return profile_path

def load_dashboard_overrides():
    """Tải danh sách các cài đặt đã bị can thiệp bởi Dashboard"""
    with overrides_lock:
        if os.path.exists(DASHBOARD_OVERRIDES_FILE):
            try:
                with open(DASHBOARD_OVERRIDES_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

def save_dashboard_overrides(data):
    """Lưu cố định các cài đặt Dashboard đã thay đổi"""
    with overrides_lock:
        try:
            with open(DASHBOARD_OVERRIDES_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving dashboard overrides: {e}")

def deep_merge(base, override):
    """Hợp nhất hai dictionary: Giữ nguyên các key trong base trừ khi bị đè bởi override"""
    if not isinstance(base, dict):
        base = {}
    if not isinstance(override, dict):
        return override
    
    merged = base.copy()
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def check_rate_limit(ip):
    now = time.time()
    with attempts_lock:
        if ip in LOGIN_ATTEMPTS:
            attempts, block_time = LOGIN_ATTEMPTS[ip]
            if block_time > now:
                return False, int(block_time - now)
            if now - block_time > BLOCK_DURATION: 
                LOGIN_ATTEMPTS[ip] = [0, 0]
    return True, 0

def fail_login(ip):
    now = time.time()
    with attempts_lock:
        if ip not in LOGIN_ATTEMPTS:
            LOGIN_ATTEMPTS[ip] = [1, 0]
        else:
            attempts, block_time = LOGIN_ATTEMPTS[ip]
            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                block_time = now + BLOCK_DURATION
            LOGIN_ATTEMPTS[ip] = [attempts, block_time]

def protect_large_ints(obj):
    if isinstance(obj, dict):
        return {k: protect_large_ints(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [protect_large_ints(v) for v in obj]
    elif isinstance(obj, int) and (obj > 9007199254740991 or obj < -9007199254740991):
        return str(obj)
    return obj

def restore_large_ints(obj):
    if isinstance(obj, dict):
        return {k: restore_large_ints(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [restore_large_ints(v) for v in obj]
    elif isinstance(obj, str) and obj.isdigit() and len(obj) >= 15:
        try:
            return int(obj)
        except ValueError:
            return obj
    return obj

def get_bot(account_id):
    if not account_id:
        return state.bot_instances[0] if state.bot_instances else None
    for bot in state.bot_instances:
        if bot.user and str(bot.user.id) == str(account_id):
            return bot
    return state.bot_instances[0] if state.bot_instances else None


@app.route('/')
@login_required
def home():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr
        allowed, wait_time = check_rate_limit(ip)
        if not allowed:
            return jsonify({'success': False, 'error': f'Too many failed attempts. Try again in {wait_time}s'})
        
        data = request.json or {}
        cfg = load_auth_config()
        if not cfg:
            return jsonify({'success': False, 'error': 'Auth config missing'})
            
        if data.get('username') == cfg.get('username') and data.get('password') == cfg.get('password'):
            session['logged_in'] = True
            session.permanent = True
            with attempts_lock:
                LOGIN_ATTEMPTS.pop(ip, None)
            return jsonify({'success': True})
        else:
            fail_login(ip)
            return jsonify({'success': False, 'error': 'Invalid Credentials'})
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/api/accounts/list')
@login_required
def account_list():
    accounts = []
    for bot in state.bot_instances:
        if not bot.user or not bot.is_ready: 
            continue
        uid = str(bot.user.id)
        st = state.account_stats.get(uid, {})
        session_total = st.get('session_hunt_count', 0) + st.get('session_battle_count', 0) + st.get('session_owo_count', 0) + st.get('session_other_count', 0)
        accounts.append({
            'id': uid,
            'username': bot.username,
            'avatar': str(bot.user.display_avatar.url) if bot.user.display_avatar else None,
            'paused': bot.paused,
            'cash': st.get('current_cash', 0),
            'session_total': session_total,
            'gems_used': st.get('gems_used', 0)
        })
    return jsonify(accounts)

@app.route('/api/stats')
@login_required
def stats():
    account_id = request.args.get('id')
    bot = get_bot(account_id)
    uid = str(account_id) if account_id else (str(bot.user.id) if bot and bot.user else None)
    if not uid: 
        return jsonify({})
        
    st = state.account_stats.get(uid)
    if not st:
        if bot and bot.user:
            st = state.get_empty_stats()
            st['username'] = bot.username
            state.account_stats[uid] = st
        else: 
            return jsonify({})
    
    uptime_start = st.get('uptime_start', time.time())
    elapsed = time.time() - uptime_start
    session_cmds = st.get('session_hunt_count', 0) + st.get('session_battle_count', 0) + st.get('session_owo_count', 0) + st.get('session_other_count', 0)
    mins = elapsed / 60
    cpm = round(session_cmds / mins, 1) if mins > 0.1 else 0
    cph = 0
    history = st.get('cowoncy_history', [])
    if len(history) > 1:
        first = history[0]
        last = history[-1]
        time_diff_hrs = (last[0] - first[0]) / 3600
        cash_diff = last[1] - first[1]
        if time_diff_hrs > 0.01: 
            cph = round(cash_diff / time_diff_hrs)

    is_active = bot and str(bot.user.id) == uid if bot and bot.user else False
    current_status = ("PAUSED" if bot.paused else "ONLINE") if is_active else "OFFLINE"
    
    response_data = {
        'uptime': utils.format_seconds(elapsed),
        'cash': st.get('current_cash', 0),
        'logs': [l for l in state.command_logs if str(l.get('bot_id')) == uid][:200],
        'status': current_status,
        'security': {
             'captchas': st.get('captchas_solved', 0),
             'bans': st.get('bans_detected', 0),
             'warnings': st.get('warnings_detected', 0),
             'last_message': st.get('last_captcha_msg', '')
        },
        'analytics': {'cph': cph, 'gems_used': st.get('gems_used', 0)},
        'bot': {
            'user_id': uid,
            'username': st.get('username', 'Unknown'),
            'channel_id': bot.channel_id if is_active else None,
            'paused': bot.paused if is_active else True,
            'throttled': (time.time() < bot.throttle_until) if is_active else False,
            'cooldown_remaining': 999999 if (is_active and bot.throttle_until == float('inf')) else (max(0, int(bot.throttle_until - time.time())) if is_active else 0),
            'cooldown_command': bot.last_sent_command if is_active else None
        },
        'chart_data': {
            'hunt': st.get('hunt_count', 0), 'battle': st.get('battle_count', 0),
            'session_hunt': st.get('session_hunt_count', 0), 'session_battle': st.get('session_battle_count', 0),
            'session_owo': st.get('session_owo_count', 0), 'other': st.get('other_count', 0),
            'owo': st.get('owo_count', 0), 'total': st.get('total_cmd_count', 0), 'perf_bpm': cpm
        },
        'system': {'last_cash_update': st.get('last_cash_update', 0), 'pending_commands': len(st.get('pending_commands', []))},
        'quest_data': st.get('quest_data', []),
        'next_quest_timer': st.get('next_quest_timer'),
        'cmd_states': {k: {**v, 'content': '[Dynamic function]' if callable(v.get('content')) else v.get('content')} for k, v in bot.cmd_states.items()} if bot else {},
        'gambling_stats': st.get('gambling_stats', {})
    }
    return jsonify(response_data)

@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def settings():
    account_id = request.args.get('id')
    acc_key = str(account_id) if account_id else 'global'
    
    if account_id:
        config_path = os.path.join(state.CONFIG_DIR, f'settings_{account_id}.json')
    else:
        config_path = os.path.join(state.CONFIG_DIR, 'settings.json')
        
    all_overrides = load_dashboard_overrides()
    account_overrides = all_overrides.get(acc_key, {})

    if request.method == 'POST':
        new_config = request.json or {}
        new_config = restore_large_ints(new_config)
        
        try:
            save_to_all = request.args.get('all_accounts') == 'true' or request.args.get('all') == 'true'
            
            if save_to_all:
                for k in list(all_overrides.keys()):
                    all_overrides[k] = deep_merge(all_overrides.get(k, {}), new_config)
                all_overrides['global'] = deep_merge(all_overrides.get('global', {}), new_config)
            else:
                all_overrides[acc_key] = deep_merge(account_overrides, new_config)
            
            save_dashboard_overrides(all_overrides)

            if save_to_all:
                global_path = os.path.join(state.CONFIG_DIR, 'settings.json')
                existing_global = {}
                if os.path.exists(global_path):
                    try:
                        with open(global_path, 'r', encoding='utf-8') as f:
                            existing_global = json.load(f)
                    except Exception: 
                        pass
                
                merged_global = deep_merge(existing_global, new_config)
                with open(global_path, 'w', encoding='utf-8') as f:
                    json.dump(merged_global, f, indent=4, ensure_ascii=False)

                for filename in os.listdir(state.CONFIG_DIR):
                    if filename.startswith("settings_") and filename.endswith(".json"):
                        file_path = os.path.join(state.CONFIG_DIR, filename)
                        ex_cfg = {}
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                ex_cfg = json.load(f)
                        except Exception: 
                            pass
                        m_cfg = deep_merge(ex_cfg, new_config)
                        with open(file_path, 'w', encoding='utf-8') as f:
                            json.dump(m_cfg, f, indent=4, ensure_ascii=False)

                for bot in state.bot_instances:
                    if hasattr(bot, 'loop') and bot.loop and bot.loop.is_running():
                        asyncio.run_coroutine_threadsafe(bot.sync_settings(merged_global), bot.loop)
                state.log_command("SYS", "Settings updated for ALL accounts", "success")
            else:
                existing_cfg = {}
                if os.path.exists(config_path):
                    try:
                        with open(config_path, 'r', encoding='utf-8') as f:
                            existing_cfg = json.load(f)
                    except Exception: 
                        pass
                
                merged_cfg = deep_merge(existing_cfg, new_config)
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(merged_cfg, f, indent=4, ensure_ascii=False)

                for bot in state.bot_instances:
                    if (not account_id) or (bot.user and str(bot.user.id) == str(account_id)):
                        if hasattr(bot, 'loop') and bot.loop and bot.loop.is_running():
                            asyncio.run_coroutine_threadsafe(bot.sync_settings(merged_cfg), bot.loop)
                state.log_command("SYS", f"Settings updated for {'Account ' + account_id if account_id else 'Global'}", "success")
            return jsonify({"status": "success"})
        except Exception as e:
            state.log_command("ERROR", f"Failed to save settings: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        try:
            base_config = {}
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    base_config = json.load(f)
            elif account_id:
                global_path = os.path.join(state.CONFIG_DIR, 'settings.json')
                if os.path.exists(global_path):
                    with open(global_path, 'r', encoding='utf-8') as f:
                        base_config = json.load(f)
            
            final_config = deep_merge(base_config, account_overrides)
            return jsonify(protect_large_ints(final_config))
        except Exception:
            return jsonify({})

@app.route('/api/debug')
@login_required
def debug():
    return jsonify({
        'account_stats': state.account_stats,
        'bot_instances': len(state.bot_instances),
        'command_logs_count': len(state.command_logs),
        'full_history_count': len(state.full_session_history)
    })

@app.route('/api/debug_status')
def debug_status():
    res = []
    for bot in state.bot_instances:
        res.append({
            'username': bot.username,
            'id': str(bot.user.id) if bot.user else None,
            'ready': bot.is_ready,
            'cmd_count': len(bot.cmd_states),
            'cmds': list(bot.cmd_states.keys())
        })
    return jsonify(res)

@app.route('/api/history')
@login_required
def get_history():
    return jsonify(list(reversed(state.full_session_history)))

@app.route('/api/history/analytics')
@login_required
def get_analytics():
    try:
        from utils import history_tracker
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        dat = history_tracker.get_analytics_data(start_date=start_date, end_date=end_date)
        dat['recent_logs'] = list(state.full_session_history)[-500:]
        return jsonify(dat)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/accounts/config', methods=['GET', 'POST'])
@login_required
def accounts_config_api():
    accounts_path = os.path.join(state.CONFIG_DIR, 'accounts.json')
    if request.method == 'POST':
        payload = request.json or {}
        accounts = payload.get('accounts', payload if isinstance(payload, list) else [])
        try:
            with open(accounts_path, 'w', encoding='utf-8') as f:
                json.dump({'accounts': accounts}, f, indent=4)
            from utils import proxy_manager
            proxy_manager.sync_proxy_assignments()
            for bot in state.bot_instances:
                bot.accounts = accounts
            state.log_command("SYS", "Accounts config updated. Restart recommended.", "success")
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    try:
        from utils import proxy_manager
        with open(accounts_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        accounts = data.get('accounts', [])
        for acc in accounts:
            if acc.get('token'):
                acc['token_masked'] = proxy_manager.mask_token(acc['token'])
        return jsonify({'accounts': accounts})
    except Exception:
        return jsonify({'accounts': []})

@app.route('/api/accounts', methods=['GET', 'POST'])
@login_required
def accounts_api():
    accounts_path = os.path.join(state.CONFIG_DIR, 'accounts.json')
    if request.method == 'POST':
        new_accounts = request.json
        try:
            with open(accounts_path, 'w', encoding='utf-8') as f:
                json.dump(new_accounts, f, indent=4)
            for bot in state.bot_instances:
                bot.accounts = new_accounts.get('accounts', new_accounts) if isinstance(new_accounts, dict) else new_accounts
            state.log_command("SYS", "Accounts updated successfully. Restart recommended.", "success")
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        try:
            with open(accounts_path, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        except Exception:
            return jsonify([])

@app.route('/api/proxies', methods=['GET', 'POST'])
@login_required
def proxies_api():
    from utils import proxy_manager
    if request.method == 'POST':
        payload = request.json or {}
        proxies = payload.get('proxies', [])
        proxy_manager.save_proxies(proxies)
        proxy_manager.sync_proxy_assignments()
        state.log_command("SYS", "Proxy pool saved", "success")
        return jsonify({"status": "success", "proxies": proxy_manager.load_proxies()})
    return jsonify({"proxies": proxy_manager.load_proxies()})

@app.route('/api/proxies/bulk', methods=['POST'])
@login_required
def proxies_bulk():
    from utils import proxy_manager
    text = (request.json or {}).get('text', '')
    result = proxy_manager.bulk_import(text)
    state.log_command("SYS", f"Bulk imported {len(result['added'])} proxies", "success")
    return jsonify({
        "status": "success",
        "added": len(result['added']),
        "errors": result['errors'],
        "proxies": proxy_manager.load_proxies(),
    })

@app.route('/api/proxies/test', methods=['POST'])
@login_required
def proxies_test():
    from utils import proxy_manager
    payload = request.json or {}
    proxy_id = payload.get('id')
    
    async def _run():
        if proxy_id:
            proxy = proxy_manager.get_proxy_by_id(proxy_id)
            if not proxy: 
                return {"ok": False, "error": "not found"}
            ok = await proxy_manager.test_proxy(proxy)
            proxies = proxy_manager.load_proxies()
            for p in proxies:
                if p.get('id') == proxy_id:
                    p['status'] = proxy['status']
                    p['last_check'] = proxy['last_check']
            proxy_manager.save_proxies(proxies)
            return {"ok": ok, "id": proxy_id, "status": proxy['status']}
        results = await proxy_manager.test_all_proxies()
        return {"results": results, "proxies": proxy_manager.load_proxies()}
    
    try:
        bot = state.bot_instances[0] if state.bot_instances else None
        if bot and hasattr(bot, 'loop') and bot.loop and bot.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(_run(), bot.loop)
            result = future.result(timeout=30)
        else:
            result = asyncio.run(_run())
        return jsonify({"status": "success", **result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/proxies/assign', methods=['POST'])
@login_required
def proxies_assign():
    from utils import proxy_manager
    assigned = proxy_manager.auto_assign()
    state.log_command("SYS", f"Auto-assigned {len(assigned)} proxies to accounts", "success")
    return jsonify({"status": "success", "assigned": assigned, "proxies": proxy_manager.load_proxies()})

@app.route('/api/proxies/<proxy_id>', methods=['DELETE'])
@login_required
def proxies_delete(proxy_id):
    from utils import proxy_manager
    proxy_manager.remove_proxy(proxy_id)
    state.log_command("SYS", f"Removed proxy {proxy_id}", "info")
    return jsonify({"status": "success", "proxies": proxy_manager.load_proxies()})

@app.route('/api/proxies/all', methods=['DELETE'])
@login_required
def proxies_delete_all():
    from utils import proxy_manager
    proxy_manager.remove_all_proxies()
    state.log_command("SYS", "Deleted ALL proxies", "info")
    return jsonify({"status": "success", "proxies": []})

@app.route('/api/proxies/failed', methods=['DELETE'])
@login_required
def proxies_delete_failed():
    from utils import proxy_manager
    count = proxy_manager.remove_failed_proxies()
    state.log_command("SYS", f"Deleted {count} failed proxies", "info")
    return jsonify({"status": "success", "count": count, "proxies": proxy_manager.load_proxies()})

@app.route('/api/security/test', methods=['POST'])
@login_required
def test_security():
    account_id = request.args.get('id')
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'status': 'error', 'message': 'Bot not found'}), 404
    sec = bot.get_cog('Security')
    if sec:
        asyncio.run_coroutine_threadsafe(sec.play_beep(), bot.loop)
        sec._show_desktop_notification("Test: Neura Security Alert working!")
        sec._send_webhook("SYSTEM TEST", "This is a test of your security notification system. All systems are operational.")
        return jsonify({'status': 'success', 'message': 'Test signals sent'})
    return jsonify({'status': 'error', 'message': 'Security module not loaded'}), 500

@app.route('/api/control', methods=['POST'])
@login_required
def control():
    data = request.json or {}
    action = data.get('action')
    account_id = data.get('id')
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'success': False, 'error': 'Bot not found'})
        
    if action == 'stop':
        bot.paused = True
        bot.log("SYS", "Bot STOPPED via Dashboard")
    elif action == 'start':
        bot.paused = False
        bot.throttle_until = 0
        bot.log("SYS", "Bot RESUMED via Dashboard")
    elif action == 'cash':
        asyncio.run_coroutine_threadsafe(bot.send_message(f"{bot.prefix}cash", skip_typing=True, priority=True), bot.loop)
        state.log_command("CMD", "Manual Cash Check Sent", "info", bot_name=bot.username)
    return jsonify({'success': True})

@app.route('/api/security', methods=['POST'])
@login_required
def security():
    data = request.json or {}
    action = data.get('action')
    account_id = data.get('id')
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'success': False, 'error': 'Bot not found'})
    if action == 'resume':
        bot.paused = False
        bot.throttle_until = 0
        state.log_command("SEC", f"User Resumed {bot.username} from Security Alert", "success")
    return jsonify({'success': True})

@app.route('/api/captcha/current')
@login_required
def captcha_current():
    account_id = request.args.get('id')
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'success': False})
    st = bot.stats
    captcha_data = st.get('current_captcha')
    if captcha_data and captcha_data.get('image_url'):
        timestamp = captcha_data.get('timestamp', 0)
        if time.time() - timestamp < 600:
            return jsonify({
                'success': True, 
                'url': captcha_data['image_url'], 
                'cash': captcha_data.get('cash', 16000), 
                'command': captcha_data.get('command_template', 'owo autohunt {cash} {password}'), 
                'age_seconds': int(time.time() - timestamp)
            })
        else:
            st.pop('current_captcha', None)
    return jsonify({'success': False, 'message': 'No active captcha'})

@app.route('/api/captcha/submit', methods=['POST'])
@login_required
def captcha_submit():
    data = request.json or {}
    code = data.get('code', '').strip()
    account_id = data.get('id')
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'success': False, 'error': 'Bot not found'})
    if not code: 
        return jsonify({'success': False, 'error': 'No password provided'})
        
    st = bot.stats
    captcha_data = st.get('current_captcha')
    if not captcha_data: 
        return jsonify({'success': False, 'error': 'No active captcha'})
        
    cash = captcha_data.get('cash', 16000)
    command_template = captcha_data.get('command_template', f"owo autohunt {cash} {{password}}")
    full_command = command_template.replace('{password}', code)
    
    asyncio.run_coroutine_threadsafe(bot.send_message(full_command, skip_typing=True, priority=True), bot.loop)
    st.pop('current_captcha', None)
    st['captchas_solved_today'] = st.get('captchas_solved_today', 0) + 1
    st['captcha_success_count'] = st.get('captcha_success_count', 0) + 1
    state.log_command("CMD", f"Captcha solution sent: {full_command}", bot_name=bot.username)
    return jsonify({'success': True, 'message': f'Captcha solution sent: {full_command}'})

@app.route('/api/captcha/balance', methods=['GET', 'POST'])
@login_required
def captcha_balance():
    account_id = request.args.get('id')
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'balance': None, 'service': 'unknown', 'error': 'Bot not found'})
        
    cfg = bot.config.get('security', {}).get('captcha_solver', {})
    service = cfg.get('service', 'yescaptcha')
    api_key = ''
    
    if request.method == 'POST':
        data = request.json or {}
        if 'service' in data: 
            service = data['service']
        if 'api_key' in data: 
            api_key = data['api_key']
            
    if not api_key:
        if service == 'nopecha': 
            api_key = cfg.get('nopecha_api_key', cfg.get('api_key', ''))
        elif service == 'anticaptcha': 
            api_key = cfg.get('anticaptcha_api_key', cfg.get('api_key', ''))
        elif service == 'captchaly': 
            api_key = cfg.get('captchaly_api_key', cfg.get('api_key', ''))
        else: 
            api_key = cfg.get('yescaptcha_api_key', cfg.get('api_key', ''))
            
    temp_solver = None
    if service == 'nopecha':
        from modules.services.nopecha import NopeCaptchaService
        temp_solver = NopeCaptchaService(bot, api_key, "")
    elif service == 'anticaptcha':
        from modules.services.anticaptcha import AntiCaptchaService
        temp_solver = AntiCaptchaService(bot, api_key, "")
    elif service == 'captchaly':
        from modules.services.captchaly import CaptchalyService
        temp_solver = CaptchalyService(bot, api_key, "")
    else:
        from modules.services.yescaptcha import YesCaptchaService
        temp_solver = YesCaptchaService(bot, api_key, "")
        
    try:
        future = asyncio.run_coroutine_threadsafe(temp_solver.get_balance(), bot.loop)
        balance = future.result(timeout=10)
        return jsonify({'balance': balance, 'service': service, 'enabled': cfg.get('enabled', False)})
    except Exception as e:
        return jsonify({'balance': None, 'service': service, 'error': str(e)})

@app.route('/api/captcha/stats')
@login_required
def captcha_stats():
    account_id = request.args.get('id')
    bot = get_bot(account_id)
    st = bot.stats if bot else {}
    solved = st.get('captchas_solved_today', 0)
    success = st.get('captcha_success_count', 0)
    success_rate = 100 if solved == 0 else round((success / max(solved, 1)) * 100)
    return jsonify({'solved': solved, 'success_rate': success_rate})

@app.route('/api/bot/command', methods=['POST'])
@login_required
def bot_command():
    data = request.json or {}
    command = data.get('command', '').strip()
    account_id = data.get('id')
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'success': False, 'error': 'Bot not found'})
    if not command: 
        return jsonify({'success': False, 'error': 'No command provided'})
    asyncio.run_coroutine_threadsafe(bot.send_message(command, skip_typing=True, priority=True), bot.loop)
    state.log_command("CMD", f"Manual command sent: {command}", bot_name=bot.username)
    return jsonify({'success': True, 'message': f'Command sent: {command}'})

@app.route('/api/captcha_challenge', methods=['GET'])
@login_required
def get_captcha_challenge():
    account_id = request.args.get('account_id', type=str)
    with captcha_lock:
        if account_id and account_id in _pending_captchas:
            return jsonify({'success': True, 'challenge': _pending_captchas[account_id]})
        if _pending_captchas:
            for acc_id, challenge in _pending_captchas.items():
                return jsonify({'success': True, 'challenge': challenge, 'account_id': acc_id})
    return jsonify({'success': False, 'message': 'No captcha pending'})

@app.route('/api/captcha_solve', methods=['POST'])
@login_required
def submit_captcha_solution():
    data = request.get_json() or {}
    account_id = data.get('account_id', '')
    token = data.get('token', '')
    if not account_id or not token: 
        return jsonify({'success': False, 'error': 'Missing account_id or token'})
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'success': False, 'error': 'Bot not found'})
        
    headers = {
        "Authorization": bot.token,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    payload = {"token": token}
    try:
        response = requests.post("https://owobot.com/api/captcha/verify", json=payload, headers=headers, verify=False, timeout=10)
        if response.status_code == 200:
            from modules.web_solver import WebSolver
            WebSolver.mark_verification_done(account_id)
            clear_captcha_challenge(account_id)
            state.log_command("SEC", f"Captcha verified for account {account_id}", "success")
            return jsonify({'success': True, 'message': 'Captcha verified successfully'})
        else:
            state.log_command("SEC", f"Captcha verification failed: {response.text}", "error")
            return jsonify({'success': False, 'error': 'Invalid captcha token'})
    except Exception as e:
        state.log_command("SEC", f"Verification error: {e}", "error")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/captcha/oauth_url', methods=['POST'])
@login_required
def captcha_oauth_url():
    data = request.get_json() or {}
    account_id = data.get('account_id')
    mode = data.get('mode', 'verify') # Đã hỗ trợ phân biệt mode
    
    if not account_id: 
        return jsonify({'success': False, 'error': 'Missing account_id'})
    bot = get_bot(account_id)
    if not bot: 
        return jsonify({'success': False, 'error': 'Bot not found'})

    # Lấy đường dẫn thư mục lưu Profile/Cookie Chrome của riêng tài khoản này
    profile_dir = get_account_profile_dir(account_id)
    state.log_command("SYS", f"Using Chrome Profile at: {profile_dir} (Login mode: {mode})", "info")
    
    # Ở LOGIN MODE: Trực tiếp trả về URL Authorization OAuth2 của OwO, BỎ MỞ @ME DISCORD
    auth_url = "https://discord.com/api/v9/oauth2/authorize?client_id=408785106942164992&response_type=code&redirect_uri=https://owobot.com/api/auth/discord/redirect&scope=identify guilds"
    
    async def get_redirect_url():
        headers = {
            "Authorization": bot.token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            auth_payload = {
                "authorize": True,
                "permissions": "0",
                "integration_type": 0,
                "location_context": {"guild_id": "10000", "channel_id": "10000", "channel_type": 10000}
            }
            async with session.post(auth_url, json=auth_payload) as resp:
                if resp.status != 200: 
                    return None
                resp_json = await resp.json()
                return resp_json.get("location")
                
    try:
        if hasattr(bot, 'loop') and bot.loop and bot.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(get_redirect_url(), bot.loop)
            redirect_url = future.result(timeout=10)
        else:
            redirect_url = asyncio.run(get_redirect_url())
            
        if not redirect_url: 
            return jsonify({'success': False, 'error': 'Failed to get OAuth URL'})
            
        return jsonify({
            'success': True, 
            'url': redirect_url,
            'profile_dir': profile_dir
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/captcha/pending', methods=['GET'])
@login_required
def pending_captchas():
    pending = []
    with captcha_lock:
        for acc_id, challenge in _pending_captchas.items():
            pending.append({
                'account_id': acc_id, 
                'account_name': challenge.get('account_name', acc_id), 
                'created_at': challenge.get('created_at', time.time())
            })
    return jsonify({'pending': pending})

def register_captcha_challenge(account_id, challenge_data):
    with captcha_lock:
        _pending_captchas[account_id] = {'account_id': account_id, 'created_at': time.time(), **challenge_data}
    state.log_command("SEC", f"Captcha challenge registered for account {account_id}", "info")

def clear_captcha_challenge(account_id):
    with captcha_lock:
        if account_id in _pending_captchas:
            _pending_captchas.pop(account_id, None)
            state.log_command("SEC", f"Captcha challenge cleared for account {account_id}", "info")