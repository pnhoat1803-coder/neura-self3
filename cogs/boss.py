# This file is part of NeuraSelf-UwU.
# Copyright (c) 2025-Present Routo
#
# NeuraSelf-UwU is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# You should have received a copy of the GNU General Public License
# along with NeuraSelf-UwU. If not, see <https://www.gnu.org/licenses/>.


"""
Author: Routo
NeuraSelf-UwU - https://github.com/routo-loop/neura-self
"""



import discord
from discord.ext import commands
import asyncio
import time
import random
from component_v2_neura import parse_v2_message, get_boss_battle_id
import json
import os
import re

class Boss(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.state_file = "data/boss_state.json"
        self._load_state()
        self._update_settings()
        self.bot.loop.create_task(self.initial_boss_scan())

    def _update_settings(self):
        boss_cfg = self.bot.config.get("commands", {}).get("boss", {})
        
        # Cấu hình đánh Boss cơ bản
        self.enabled = boss_cfg.get("enabled", True)
        self.join_chance = boss_cfg.get("join_chance", 100)
        self.join_all_guilds = boss_cfg.get("join_all_guilds", False)
        self.ignore_guilds = [str(g).strip() for g in boss_cfg.get("ignore_guilds", []) if str(g).strip()]
        self.allowed_guilds = [str(g).strip() for g in boss_cfg.get("allowed_guilds", []) if str(g).strip()]

        # Cấu hình tính năng All-in Ticket
        self.all_in_enabled = boss_cfg.get("all_in_enabled", False)
        self.all_in_guilds = [str(g).strip() for g in boss_cfg.get("all_in_guilds", []) if str(g).strip()]

    def _is_guild_allowed(self, guild_id):
        if not guild_id:
            return True
        if guild_id in self.ignore_guilds:
            return False
        if self.join_all_guilds:
            return True
        if self.allowed_guilds:
            return guild_id in self.allowed_guilds
        return True

    def _clean_owo_text(self, content, v2_text):
        text = v2_text if len(v2_text) >= len(content) else content
        if content and v2_text and content != v2_text:
            if content not in v2_text:
                text = f"{content} {v2_text}"
            
        text = re.sub(r'<a?:\w+:\d+>', '', text)
        text = re.sub(r'<t:\d+:[a-zA-Z]>', '', text)
        text = re.sub(r'#{1,6}\s*', '', text)
        text = re.sub(r'-\#\s*', '', text)
        text = re.sub(r'\*\*|__|~~|\*|_', '', text)
        
        return " ".join(text.split()).strip()

    async def initial_boss_scan(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(2)
        
        # Chuyển sang chế độ Passive (an toàn). Không tự động spam lệnh "owo boss" nữa để chống ban.
        if self.enabled:
            self.bot.log("SYS", "Boss Module active in PASSIVE SCAN mode (Safe Mode).")

    async def register_actions(self):
        self._update_settings()
        self.bot.log("SYS", "Boss Module config refreshed.")

    def _load_state(self):
        self.last_reset = 0
        self.joined_ids = set()
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    self.last_reset = data.get("last_reset", 0)
                    self.joined_ids = set(data.get("joined_ids", []))
            except: pass

    def _save_state(self):
        try:
            if not os.path.exists("data"): os.makedirs("data")
            with open(self.state_file, "w") as f:
                json.dump({"last_reset": self.last_reset, "joined_ids": list(self.joined_ids)}, f)
        except Exception as e: 
            self.bot.log("ERROR", f"State save error: {e}")

    def _check_reset(self):
        now = time.time()
        if now - self.last_reset > 86400: 
            self.last_reset = now
            self.joined_ids.clear()
            self._save_state()

    @commands.Cog.listener()
    async def on_socket_raw_receive(self, msg):
        if not self.enabled or self.bot.paused:
            return
        if isinstance(msg, bytes):
            return
        try:
            raw_data = json.loads(msg)
        except:
            return
            
        evt_type = raw_data.get("t")
        if evt_type not in ["MESSAGE_CREATE", "MESSAGE_UPDATE"]:
            return

        data = raw_data.get("d", {})
        if str(data.get("author", {}).get("id")) != self.bot.owo_bot_id:
            return

        content = data.get("content") or ""
        components = parse_v2_message(data) or []
        v2_text = " ".join([c.content for c in components if c.name == "text_display"]) if components else ""
        raw_combined = f"{content} {v2_text}".lower()

        # Log khi hết vé
        if any(keyword in raw_combined for keyword in ["don't have", "ran out"]):
            clean_txt = self._clean_owo_text(content, v2_text)
            if clean_txt:
                self.bot.log("BOSS", clean_txt)

        if evt_type == "MESSAGE_UPDATE":
            return

        channel_id = int(data.get("channel_id"))
        guild_id = str(data.get("guild_id") or "")
        if not guild_id:
            channel = self.bot.get_channel(channel_id)
            if channel and hasattr(channel, "guild") and channel.guild:
                guild_id = str(channel.guild.id)

        # ==========================================
        # 1. TÍNH NĂNG ALL-IN TICKET (Xác nhận lần 2)
        # ==========================================
        if "already fought this boss" in raw_combined and "are you sure" in raw_combined:
            if not self.all_in_enabled:
                return # Tính năng tắt, bỏ qua
            
            if guild_id not in self.all_in_guilds:
                return # Server này không có trong danh sách cho phép All-in

            # Tìm nút "Fight!" (Thường là nút đầu tiên có ID hoặc ID chứa chữ fight/confirm)
            confirm_btn = next((c for c in components if c.custom_id and ("fight" in c.custom_id.lower() or "confirm" in c.custom_id.lower())), None)
            if not confirm_btn:
                confirm_btn = next((c for c in components if c.custom_id), None) # Fallback lấy nút đầu tiên (màu xanh)

            if confirm_btn:
                self.bot.log("BOSS", f"All-in enabled! Confirming 2nd ticket in guild {guild_id}...")
                await asyncio.sleep(random.uniform(1.0, 2.5)) # Đợi 1 chút cho giống người thật
                
                if self.bot.paused: return
                success = await self.bot.interactions.click_button_raw(
                    custom_id=confirm_btn.custom_id,
                    message_id=data.get("id"),
                    channel_id=channel_id,
                    author_id=data.get("author", {}).get("id"),
                    guild_id=guild_id,
                    flags=data.get("flags", 0)
                )
                if success:
                    self.bot.log("SUCCESS", f"All-in Ticket used successfully in {guild_id}!")
                else:
                    self.bot.log("ERROR", "Failed to click All-in confirm button.")
            return # Đã xử lý xong All-in, dừng code tại đây

        # ==========================================
        # 2. TÍNH NĂNG AUTO JOIN BOSS (Lần 1)
        # ==========================================
        is_spawn = "runs away" in raw_combined or "guild boss" in raw_combined or "a guild boss" in raw_combined
        fight_btn = next((c for c in components if c.custom_id == "guildboss_fight"), None)
        
        if not is_spawn and not fight_btn:
            return

        if not self._is_guild_allowed(guild_id):
            return
            
        if not fight_btn:
            return

        battle_id = get_boss_battle_id(components)
        if battle_id and battle_id in self.joined_ids:
            return

        tracking_id = battle_id or f"msg_{data.get('id')}"
        self._check_reset()

        if random.randint(1, 100) > self.join_chance:
            self.joined_ids.add(tracking_id)
            return

        self.bot.log("BOSS", f"Valid Boss detected in {channel_id}! Attempting to engage...")
        
        await asyncio.sleep(random.uniform(0.5, 1.5))
        
        if self.bot.paused:
            return
        
        success = await self.bot.interactions.click_button_raw(
            custom_id=fight_btn.custom_id,
            message_id=data.get("id"),
            channel_id=channel_id,
            author_id=data.get("author", {}).get("id"),
            guild_id=guild_id,
            flags=data.get("flags", 0)
        )

        if success:
            if tracking_id: 
                self.joined_ids.add(tracking_id)
            self._save_state()
            self.bot.log("SUCCESS", f"Engaged Boss Battle! (ID: {tracking_id})")
        else:
            self.bot.log("ERROR", "Interaction failed for Boss Battle.")

async def setup(bot):
    await bot.add_cog(Boss(bot))