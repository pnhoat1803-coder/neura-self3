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
import asyncio
import time
import random
import re
import core.state as state
from discord.ext import commands

class Gambling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active = True
        self.task = None
        self.martingale_states = {}
        self.last_outcomes = {}
        self.gambling_stats = {}
        
        # Cấu hình chuỗi cược phân mức Low / High cho Mines (tham khảo chuẩn GhoSty OwO Blackjack)
        self.mines_seq_index = 0
        self.MINES_BET_SEQUENCES = {
            "Low": [100, 250, 500, 1000, 2500, 5000, 10000],
            "High": [10000, 25000, 50000, 75000, 100000]
        }

    def _get_prefix(self):
        core_cfg = self.bot.config.get('core', {})
        return core_cfg.get('prefix', 'owo ').strip()

    def _get_gambling_cfg(self):
        return self.bot.config.get('gambling', {})

    def _get_cmd_cfg(self, cmd):
        return self.bot.config.get('commands', {}).get(cmd, {})

    def _get_current_cash(self):
        uid = str(self.bot.user.id) if (hasattr(self.bot, '_connection') and self.bot.user) else str(getattr(self.bot, 'user_id', ''))
        st = state.account_stats.get(uid, {})
        return st.get('current_cash', None)

    def _check_safeguards(self):
        gamble_cfg = self._get_gambling_cfg()
        min_balance = gamble_cfg.get('min_balance', 0)
        max_balance = gamble_cfg.get('max_balance', 999999999)
        current_cash = self._get_current_cash()
        if current_cash is None:
            self.bot.log("GAMBLING", "Cash unknown. Skipping bet until balance sync.")
            return False
        if current_cash < min_balance:
            self.bot.log("GAMBLING", f"Stop-loss: {current_cash} < min {min_balance}. Suspending.")
            return False
        if current_cash > max_balance:
            self.bot.log("GAMBLING", f"Take-profit: {current_cash} > max {max_balance}. Suspending.")
            return False
        return True

    def _get_mines_bet_amount(self):
        gamble_cfg = self._get_gambling_cfg()
        seq_name = gamble_cfg.get('bet_sequence', 'Low').capitalize()
        if seq_name not in self.MINES_BET_SEQUENCES:
            seq_name = 'Low'
            
        sequence = self.MINES_BET_SEQUENCES[seq_name]
        if self.mines_seq_index >= len(sequence):
            self.mines_seq_index = len(sequence) - 1
        return sequence[self.mines_seq_index]

    def _update_mines_sequence(self, won):
        gamble_cfg = self._get_gambling_cfg()
        seq_name = gamble_cfg.get('bet_sequence', 'Low').capitalize()
        sequence = self.MINES_BET_SEQUENCES.get(seq_name, self.MINES_BET_SEQUENCES['Low'])
        
        if won:
            self.mines_seq_index = 0
            self.bot.log("GAMBLING", f"Mines Sequence ({seq_name}): Won! Reset bet index to 0 (Bet: {sequence[0]})")
        else:
            self.mines_seq_index += 1
            if self.mines_seq_index >= len(sequence):
                self.mines_seq_index = len(sequence) - 1
            self.bot.log("GAMBLING", f"Mines Sequence ({seq_name}): Lost! Increased bet index to {self.mines_seq_index} (Bet: {sequence[self.mines_seq_index]})")

    def _update_martingale(self, cmd, won):
        if cmd not in self.martingale_states:
            return
        m_state = self.martingale_states[cmd]
        base = m_state['base_bet']
        max_bet = self._get_gambling_cfg().get('max_bet', 100000)
        if won:
            m_state['current_bet'] = base
            m_state['consecutive_losses'] = 0
            m_state['consecutive_wins'] += 1
            self.bot.log("GAMBLING", f"Martingale: Won! Reset to {base}")
        else:
            m_state['consecutive_losses'] += 1
            m_state['consecutive_wins'] = 0
            m_state['current_bet'] = min(m_state['current_bet'] * 2, max_bet)
            self.bot.log("GAMBLING", f"Martingale: Lost! Next: {m_state['current_bet']}")

    def _record_outcome(self, cmd, won, amount):
        uid = str(self.bot.user.id) if (hasattr(self.bot, '_connection') and self.bot.user) else str(getattr(self.bot, 'user_id', ''))
        if uid not in self.gambling_stats:
            self.gambling_stats[uid] = {
                'total_wins': 0, 'total_losses': 0, 'total_wagered': 0,
                'net_profit': 0, 'current_streak': 0, 'best_streak': 0,
                'worst_streak': 0, 'biggest_win': 0, 'last_outcome': None
            }
        gs = self.gambling_stats[uid]
        gs['total_wagered'] += amount
        gs['last_outcome'] = 'win' if won else 'loss'
        if won:
            gs['total_wins'] += 1
            gs['net_profit'] += amount
            gs['current_streak'] = max(1, gs['current_streak'] + 1) if gs['current_streak'] >= 0 else 1
            gs['best_streak'] = max(gs['best_streak'], gs['current_streak'])
            gs['biggest_win'] = max(gs['biggest_win'], amount)
        else:
            gs['total_losses'] += 1
            gs['net_profit'] -= amount
            gs['current_streak'] = min(-1, gs['current_streak'] - 1) if gs['current_streak'] <= 0 else -1
            gs['worst_streak'] = min(gs['worst_streak'], gs['current_streak'])
        st = state.account_stats.get(uid, {})
        if st:
            st['gambling_stats'] = gs
            state.save_account_stats()

    def _get_current_bet_for_cmd(self, cmd):
        if cmd == 'mines':
            return self._get_mines_bet_amount()
        cmd_cfg = self._get_cmd_cfg(cmd)
        base_amount = cmd_cfg.get('amount', 1)
        gamble_cfg = self._get_gambling_cfg()
        strategy = gamble_cfg.get('bet_strategy', 'flat')
        max_bet = gamble_cfg.get('max_bet', 100000)

        if strategy == 'martingale':
            if cmd not in self.martingale_states:
                self.martingale_states[cmd] = {
                    'current_bet': base_amount,
                    'base_bet': base_amount,
                    'consecutive_losses': 0,
                    'consecutive_wins': 0
                }
            bet = self.martingale_states[cmd]['current_bet']
            return min(bet, max_bet)
        else:
            return min(base_amount, max_bet)

    def trigger_mines(self):
        if not self._check_safeguards():
            return
        prefix = self._get_prefix()
        amount = self._get_current_bet_for_cmd('mines')
        cmd_str = f"{prefix}mines {amount}"
        
        if 'mines' in self.bot.cmd_states:
            self.bot.cmd_states['mines']['content'] = cmd_str
            self.bot.cmd_states['mines']['delay'] = random.uniform(30, 60)
        
        self.bot.log("GAMBLING", f"Mines: Betting {amount}")

    async def register_actions(self):
        prefix = self._get_prefix()
        cfg_mines = self._get_cmd_cfg('mines')
        if cfg_mines.get('enabled', False):
            self.bot.log("SYS", "Gambling (Mines) Module configured.")
            initial_amount = self._get_mines_bet_amount()
            mines_cmd_str = f"{prefix}mines {initial_amount}"
            await self.bot.neura_register_command("mines", mines_cmd_str, priority=self.bot.get_cmd_priority("mines", 3), delay=random.uniform(30, 60), initial_offset=10)
            self.trigger_mines()

    @commands.Cog.listener()
    async def on_message(self, message):
        await self._process_response(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        await self._process_response(after)

    async def _safe_click_button(self, message, custom_id, success_msg):
        """Hàm chuyên dụng bấm nút cực kỳ quan trọng, sử dụng click_button_raw để đảm bảo không bao giờ lỗi."""
        try:
            guild_id = message.guild.id if message.guild else None
            flags = message.flags.value if hasattr(message.flags, 'value') else getattr(message, 'flags', 0)
            author_id = str(message.author.id) if message.author else str(self.bot.owo_bot_id)
            
            await self.bot.interactions.click_button_raw(
                custom_id=custom_id,
                message_id=message.id,
                channel_id=message.channel.id,
                author_id=author_id,
                guild_id=guild_id,
                flags=flags
            )
            self.bot.log("SUCCESS", success_msg)
            return True
        except Exception as e:
            self.bot.log("WARN", f"Failed to click button interaction: {e}")
            return False

    async def _handle_mines_board(self, message):
        unknown_buttons = []
        cashout_button = None

        if not message.components:
            return

        for row in message.components:
            for child in row.children:
                if isinstance(child, discord.Button) and not child.disabled:
                    label = str(child.label).lower() if child.label else ""
                    emoji_name = child.emoji.name if child.emoji else ""
                    custom_id = getattr(child, 'custom_id', None)
                    
                    if not custom_id:
                        continue
                    
                    if "?" in label or "?" in emoji_name or "❓" in emoji_name or label == "":
                        unknown_buttons.append(child)
                    elif "cash out" in label or "cashout" in label:
                        cashout_button = child

        if not unknown_buttons and not cashout_button:
            return

        # Nếu là ván mới (có các ô chưa bấm), chọn ô đầu tiên để mở an toàn
        if unknown_buttons and len(unknown_buttons) >= 9:
            target = unknown_buttons[0]
            self.bot.log("GAMBLING", "Mines: New game started, clicking top-left corner tile via raw interaction...")
            await asyncio.sleep(random.uniform(0.8, 1.5))
            await self._safe_click_button(message, target.custom_id, "Successfully clicked Mines tile!")
                
        # Sau khi mở 1 ô an toàn, tự động bấm Cash Out ngay lập tức để nhận lãi
        elif cashout_button and not cashout_button.disabled:
            self.bot.log("GAMBLING", "Mines: Safe tile opened, clicking 'Cash Out' button immediately via raw interaction...")
            await asyncio.sleep(random.uniform(0.4, 0.9))
            await self._safe_click_button(message, cashout_button.custom_id, "Successfully clicked Cash Out!")

    async def _process_response(self, message):
        core_config = self.bot.config.get('core', {})
        monitor_id = str(core_config.get('monitor_bot_id', '408785106942164992'))
        if str(message.author.id) != monitor_id:
            return
        if self.bot.owo_user is None:
            self.bot.owo_user = message.author
        all_channels = [str(c) for c in self.bot.channels]
        if str(message.channel.id) not in all_channels:
            return
        
        full_content = self.bot.get_full_content(message)
        is_for_me = self.bot.is_message_for_me(message)
        if not is_for_me:
            return
        content_lower = full_content.lower()

        # Kiểm tra bảng Mines và thực hiện click nút
        if "mines" in content_lower and message.components:
            asyncio.create_task(self._handle_mines_board(message))

        # Xử lý kết quả thắng/thua của Mines
        if "cashed out" in content_lower or "hit a mine" in content_lower:
            won = "cashed out" in content_lower
            self._handle_gamble_outcome('mines', won, content_lower)

    def _handle_gamble_outcome(self, cmd, won, content):
        prefix = self._get_prefix()
        
        if cmd == 'mines':
            bet_amount = self._get_mines_bet_amount()
            self._update_mines_sequence(won)
        else:
            bet_amount = self._get_current_bet_for_cmd(cmd)

        self._record_outcome(cmd, won, bet_amount)

        next_bet = self._get_current_bet_for_cmd(cmd)
        if cmd == 'mines':
            self.bot.cmd_states['mines']['content'] = f"{prefix}mines {next_bet}"

        self._sync_cash_from_response(content)
        outcome_str = "WON" if won else "LOST"
        uid = str(self.bot.user.id) if (hasattr(self.bot, '_connection') and self.bot.user) else str(getattr(self.bot, 'user_id', ''))
        gs = self.gambling_stats.get(uid, {})
        self.bot.log("GAMBLING", f"{cmd.upper()}: {outcome_str} (Bet: {bet_amount}, Net: {gs.get('net_profit', 0):,})")

    def _sync_cash_from_response(self, content):
        cash_match = re.search(r'(?:now have|balance[^\d]*)([,\d]+)', content, re.IGNORECASE)
        if not cash_match:
            cash_match = re.search(r'(?:won|lost)\s+([,\d]+)\s+cowoncy', content, re.IGNORECASE)
        if cash_match:
            try:
                cash_str = cash_match.group(1).replace(',', '')
                cash_val = int(cash_str)
                uid = str(self.bot.user.id) if (hasattr(self.bot, '_connection') and self.bot.user) else str(getattr(self.bot, 'user_id', ''))
                st = state.account_stats.get(uid, {})
                if st:
                    st['current_cash'] = cash_val
                    st['last_cash_update'] = time.time()
                    state.save_account_stats()
            except (ValueError, IndexError):
                pass

async def setup(bot):
    cog = Gambling(bot)
    await bot.add_cog(cog)