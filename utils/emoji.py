"""Named emoji constants used in user-facing text.

They live in one module and are written as escape sequences so that the
exact characters are guaranteed regardless of editor or console encoding,
and so they can be referenced (and tested) by name.
"""

from __future__ import annotations

WAVE = "\U0001F44B"  # 👋
SEARCH = "\U0001F50D"  # 🔍
PHOTO = "\U0001F4F7"  # 📷
#: The «✨ Находки» feed (G24) - the sparkle in the menu label.
FINDINGS = "\u2728"  # ✨
CATEGORIES = "\U0001F4C2"  # 📂
FAVORITES = "\u2764\uFE0F"  # ❤️
TRACKING = "\U0001F514"  # 🔔
CURRENCY = "\U0001F30D"  # 🌍
SETTINGS = "\u2699\uFE0F"  # ⚙️
HELP = "\u2753"  # ❓
STAR = "\u2B50"  # 
BUY = "\U0001F6D2"  # 🛒
SHARE = "\U0001F517"  # 
WARNING = "\u26A0\uFE0F"  # ⚠️
INFO = "\u2139\uFE0F"  # ℹ️
STORE = "\U0001F3EC"  # 
RECEIPT = "\U0001F9FE"  # 🧾
TICKET = "\U0001F39F"  # 🎟
CHECK = "\u2705"  # ✅
BOX = "\U0001F4E6"  # 
BACK = "\u2B05\uFE0F"  # ⬅️
NEXT = "\u27A1\uFE0F"  # ➡️
STOP = "\u23F9"  # ⏹
TRASH = "\U0001F5D1"  # 🗑
LOOKING_GLASS = "\U0001F50E"  # 🔎