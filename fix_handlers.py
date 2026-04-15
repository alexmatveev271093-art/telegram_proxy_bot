#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Fix missing handlers in bot.py

with open('bot.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Check if clear_banlist handler already exists
if 'async def clear_banlist' in content:
    print("clear_banlist handler already exists")
else:
    # Add the clear_banlist handler before the users_handler
    handler_code = '''
@dp.message(F.text == "🗑 Очистить бан-лист")
async def clear_banlist(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    save_json(BANS_FILE, [])

    await safe_send(
        message.chat.id,
        "✅ Бан-лист очищен",
        reply_markup=ban_kb()
    )


'''
    
    # Find insertion point (before users_handler definition)
    search_str = 'async def users_handler(message: types.Message):'
    if search_str in content:
        pos = content.find(search_str)
        # Find the @dp.message decorator before this function
        newline_before = content.rfind('\n', 0, pos)
        decorator_start = content.rfind('\n', 0, newline_before)
        
        content = content[:decorator_start+1] + handler_code + content[decorator_start+1:]
        
        with open('bot.py', 'w', encoding='utf-8') as f:
            f.write(content)
        print("✅ Added clear_banlist handler")
    else:
        print("❌ Could not find insertion point")

# Verify the file compiles
import py_compile
try:
    py_compile.compile('bot.py', doraise=True)
    print("✅ File compiles successfully")
except py_compile.PyCompileError as e:
    print(f"❌ Compilation error: {e}")
