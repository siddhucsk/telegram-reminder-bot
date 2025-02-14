import logging
from telegram import Update, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
import pytz
from datetime import datetime, timedelta, calendar
import threading
import re
from collections import defaultdict
import uuid
import sqlite3
from typing import Dict, List, Optional, Tuple
from functools import wraps
from nlp_parser import ReminderParser
import time
import os
from dotenv import load_dotenv
from keep_alive import keep_alive  # Add this import at the top

# Load environment variables
load_dotenv()

# Get configuration from environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not found in environment variables. Please set it in .env file.")

MAX_REMINDERS_PER_USER = int(os.getenv('MAX_REMINDERS_PER_USER', '50'))
DEFAULT_TIMEZONE = os.getenv('DEFAULT_TIMEZONE', 'UTC')

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class ReminderError(Exception):
    """Base class for reminder exceptions"""
    pass

class DatabaseError(ReminderError):
    """Database operation errors"""
    pass

class ReminderLimitError(ReminderError):
    """User has reached their reminder limit"""
    pass

def safe_db_operation(func):
    """Decorator for safe database operations with retries"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            try:
                with sqlite3.connect('bot.db', timeout=20) as conn:
                    conn.row_factory = sqlite3.Row
                    return func(conn, *args, **kwargs)
            except sqlite3.Error as e:
                last_error = e
                if attempt < max_retries - 1:
                    logger.warning(f"Database operation failed (attempt {attempt + 1}): {e}")
                    time.sleep(0.1 * (attempt + 1))
                    continue
                logger.error(f"Database operation failed after {max_retries} attempts: {e}")
                raise DatabaseError(f"Database operation failed: {str(e)}")
    return wrapper

@safe_db_operation
def get_user_timezone(conn, user_id: int) -> Optional[str]:
    c = conn.cursor()
    c.execute('SELECT timezone FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    return result['timezone'] if result else None

@safe_db_operation
def set_user_timezone(conn, user_id: int, timezone: str):
    c = conn.cursor()
    c.execute('''
        INSERT INTO users (user_id, timezone) 
        VALUES (?, ?) 
        ON CONFLICT(user_id) DO UPDATE SET 
            timezone = excluded.timezone,
            last_active_at = CURRENT_TIMESTAMP
    ''', (user_id, timezone))

@safe_db_operation
def save_reminder(conn, user_id: int, reminder_id: str, message: str, reminder_time: datetime, 
                 priority: str = 'medium', recurrence_type: str = None, 
                 recurrence_interval: int = None, parent_id: str = None) -> bool:
    c = conn.cursor()
    
    # Check user's reminder limit
    c.execute('SELECT reminder_count, max_reminders FROM users WHERE user_id = ?', (user_id,))
    user = c.fetchone()
    if not user:
        raise DatabaseError("User not found")
    
    if user['reminder_count'] >= user['max_reminders']:
        raise ReminderLimitError(f"Maximum number of reminders ({user['max_reminders']}) reached")
    
    c.execute(
        '''INSERT INTO reminders 
           (id, user_id, message, reminder_time, priority, recurrence_type, 
            recurrence_interval, parent_reminder_id) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (reminder_id, user_id, message, reminder_time.isoformat(), priority,
         recurrence_type, recurrence_interval, parent_id)
    )
    return True

@safe_db_operation
def get_user_reminders(conn, user_id: int) -> List[Dict]:
    c = conn.cursor()
    c.execute('''
        SELECT id, message, reminder_time, status, priority, 
               recurrence_type, recurrence_interval 
        FROM reminders 
        WHERE user_id = ? AND status = 'pending'
        ORDER BY reminder_time ASC
    ''', (user_id,))
    
    return [{
        'id': row['id'],
        'message': row['message'],
        'time': datetime.fromisoformat(row['reminder_time']),
        'status': row['status'],
        'priority': row['priority'],
        'recurrence_type': row['recurrence_type'],
        'recurrence_interval': row['recurrence_interval']
    } for row in c.fetchall()]

@safe_db_operation
def delete_reminder(conn, reminder_id: str, user_id: int = None) -> bool:
    c = conn.cursor()
    if user_id:
        c.execute('''
            UPDATE reminders 
            SET status = 'cancelled' 
            WHERE id = ? AND user_id = ? AND status = 'pending'
        ''', (reminder_id, user_id))
    else:
        c.execute('''
            UPDATE reminders 
            SET status = 'cancelled' 
            WHERE id = ? AND status = 'pending'
        ''', (reminder_id,))
    return c.rowcount > 0

@safe_db_operation
def mark_reminder_complete(conn, reminder_id: str) -> bool:
    c = conn.cursor()
    c.execute('''
        UPDATE reminders 
        SET status = 'completed' 
        WHERE id = ? AND status = 'pending'
    ''', (reminder_id,))
    return c.rowcount > 0

@safe_db_operation
def get_all_pending_reminders(conn) -> List[Dict]:
    c = conn.cursor()
    c.execute('''
        SELECT r.id, r.user_id, r.message, r.reminder_time, u.timezone,
               r.priority, r.recurrence_type, r.recurrence_interval
        FROM reminders r 
        JOIN users u ON r.user_id = u.user_id 
        WHERE r.status = 'pending'
        ORDER BY r.reminder_time ASC
    ''')
    
    return [{
        'id': row['id'],
        'user_id': row['user_id'],
        'message': row['message'],
        'time': datetime.fromisoformat(row['reminder_time']),
        'timezone': row['timezone'],
        'priority': row['priority'],
        'recurrence_type': row['recurrence_type'],
        'recurrence_interval': row['recurrence_interval']
    } for row in c.fetchall()]

@safe_db_operation
def cleanup_old_reminders(conn, days: int = 30) -> int:
    """Clean up old completed/cancelled reminders"""
    c = conn.cursor()
    cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
    
    c.execute('''
        DELETE FROM reminders 
        WHERE status IN ('completed', 'cancelled') 
        AND reminder_time < ?
    ''', (cutoff_date,))
    
    return c.rowcount

@safe_db_operation
def get_reminder_by_id(conn, reminder_id: str, user_id: int = None) -> Optional[Dict]:
    c = conn.cursor()
    if user_id:
        c.execute('''
            SELECT id, message, reminder_time, priority, recurrence_type, 
                   recurrence_interval, status
            FROM reminders 
            WHERE id = ? AND user_id = ? AND status = 'pending'
        ''', (reminder_id, user_id))
    else:
        c.execute('''
            SELECT id, message, reminder_time, priority, recurrence_type, 
                   recurrence_interval, status
            FROM reminders 
            WHERE id = ? AND status = 'pending'
        ''', (reminder_id,))
    
    row = c.fetchone()
    if not row:
        return None
    
    return {
        'id': row['id'],
        'message': row['message'],
        'time': datetime.fromisoformat(row['reminder_time']),
        'priority': row['priority'] or 'medium',
        'recurrence_type': row['recurrence_type'],
        'recurrence_interval': row['recurrence_interval'],
        'status': row['status']
    }

@safe_db_operation
def update_reminder(conn, reminder_id: str, user_id: int = None, **updates) -> bool:
    if not updates:
        return False
    
    set_clauses = []
    values = []
    
    # Build the SET clause and values
    for key, value in updates.items():
        if value is not None:
            set_clauses.append(f"{key} = ?")
            # Convert datetime to ISO format for storage
            if isinstance(value, datetime):
                value = value.isoformat()
            values.append(value)
    
    if not set_clauses:
        return False
    
    c = conn.cursor()
    if user_id:
        c.execute(f'''
            UPDATE reminders 
            SET {', '.join(set_clauses)}
            WHERE id = ? AND user_id = ? AND status = 'pending'
        ''', [*values, reminder_id, user_id])
    else:
        c.execute(f'''
            UPDATE reminders 
            SET {', '.join(set_clauses)}
            WHERE id = ? AND status = 'pending'
        ''', [*values, reminder_id])
    
    return c.rowcount > 0

# Dictionary to store active reminder timers
active_reminders: Dict[str, threading.Timer] = {}

# Priority emojis
PRIORITY_EMOJIS = {
    'high': '🔴',
    'medium': '🟡',
    'low': '🟢'
}

# Quick time buttons
QUICK_TIMES = {
    'in_1_hour': '⏰ In 1 hour',
    'in_2_hours': '⏰ In 2 hours',
    'tonight': '🌙 Tonight (8 PM)',
    'tomorrow_morning': '🌅 Tomorrow Morning (9 AM)',
    'tomorrow_afternoon': '☀️ Tomorrow Afternoon (2 PM)',
    'this_weekend': '🎉 This Weekend',
}

# Dictionary to store timezone suggestions
TIMEZONE_SUGGESTIONS = {
    'Asia/Kolkata': '🇮🇳 India (IST)',
    'America/New_York': '🇺🇸 New York (EST/EDT)',
    'Europe/London': '🇬🇧 London (GMT/BST)',
    'Asia/Dubai': '🇦🇪 Dubai (GST)',
    'Asia/Singapore': '🇸🇬 Singapore (SGT)',
    'Australia/Sydney': '🇦🇺 Sydney (AEST)',
    'Europe/Paris': '🇫🇷 Paris (CET/CEST)',
    'Asia/Tokyo': '🇯🇵 Tokyo (JST)'
}

def init_db():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()
    
    # Create users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create reminders table
    c.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            message TEXT,
            reminder_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending',
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    
    conn.commit()
    conn.close()

def start(update: Update, context: CallbackContext) -> None:
    commands = [
        ('/start', 'Start the bot'),
        ('/help', 'Show help message'),
        ('/timezone', 'Set your timezone'),
        ('/format', 'Show reminder format'),
        ('/list', 'List your reminders')
    ]
    
    command_text = "\n".join([f"<a href='tg://command?command={cmd}&bot=dancingReminder_bot'>{cmd}</a> - {desc}" 
                             for cmd, desc in commands])
    
    welcome_text = (
        f"Welcome! Here are the available commands:\n\n"
        f"{command_text}\n\n"
        f"Please set your timezone first using /timezone"
    )
    
    update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)

def format_command(update: Update, context: CallbackContext) -> None:
    format_text = """
*Reminder Formats*

1️⃣ *Quick Format:*
Just type your reminder naturally:
• "remind me tomorrow at 3pm to call mom"
• "daily reminder at 9am to take medicine"
• "urgent reminder every monday at 10am for team meeting"

2️⃣ *Structured Format:*
```
reminder
priority: high
time: 9:00am
date: 25/02/2024
repeat: daily
Take medicine
```

3️⃣ *Multiple Reminders:*
```
reminder
time: 9:00am
Take medicine

time: 2:30pm
Doctor appointment
```

*Priority Levels:*
🔴 High (urgent, important)
🟡 Medium (default)
🟢 Low

*Recurrence Options:*
• daily, weekly, monthly
• every 2 days
• every week
• every month

*Time Formats:*
• 12-hour: 9:00am, 2:30pm
• 24-hour: 09:00, 14:30
• Words: morning, noon, evening, night

*Quick Words:*
• today, tomorrow, tonight
• next monday, next week
• morning, afternoon, evening

Use the ⌨️ Quick Time buttons below for common times!
    """
    update.message.reply_text(
        format_text, 
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_quick_time_keyboard()
    )

def timezone_command(update: Update, context: CallbackContext) -> None:
    keyboard = []
    for tz, label in list(TIMEZONE_SUGGESTIONS.items())[:4]:  # Show top 4 suggestions
        keyboard.append([InlineKeyboardButton(label, callback_data=f"tz_{tz}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "Please select your timezone from these common options, or type a timezone name (e.g., 'Asia/Kolkata'):",
        reply_markup=reply_markup
    )

def button_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    
    if query.data.startswith("tz_"):
        timezone = query.data[3:]
        user_id = query.from_user.id
        set_user_timezone(user_id, timezone)
        query.edit_message_text(
            f"✅ Time zone set to {timezone} ({TIMEZONE_SUGGESTIONS.get(timezone, '')}).\n\n"
            "You can now set reminders! Use /format to see reminder formats."
        )
    elif query.data.startswith("edit_"):
        reminder_id = query.data[5:]
        reminder = get_reminder_by_id(reminder_id)
        
        if not reminder:
            query.edit_message_text("❌ Reminder not found or already completed.")
            return
        
        # Store the reminder ID in user data for the edit flow
        context.user_data[query.from_user.id] = {
            'editing_reminder': reminder_id
        }
        
        # Create edit options keyboard
        keyboard = [
            [InlineKeyboardButton("⏰ Change Time", callback_data=f"edit_time_{reminder_id}")],
            [InlineKeyboardButton("📝 Change Message", callback_data=f"edit_msg_{reminder_id}")],
            [InlineKeyboardButton("🔄 Change Recurrence", callback_data=f"edit_recur_{reminder_id}")],
            [InlineKeyboardButton("⭐ Change Priority", callback_data=f"edit_prio_{reminder_id}")],
            [InlineKeyboardButton("❌ Cancel Editing", callback_data=f"edit_cancel_{reminder_id}")]
        ]
        
        time_str = reminder['time'].strftime('%I:%M %p')
        date_str = reminder['time'].strftime('%d %b %Y')
        priority_emoji = PRIORITY_EMOJIS.get(reminder['priority'], '')
        
        edit_text = (
            f"*Editing Reminder*\n\n"
            f"Current settings:\n"
            f"🕐 Time: {time_str}\n"
            f"📅 Date: {date_str}\n"
            f"{priority_emoji} Priority: {reminder['priority']}\n"
            f"📝 Message: {reminder['message']}\n"
        )
        
        if reminder['recurrence_type']:
            edit_text += f"🔄 Repeats: Every {reminder['recurrence_interval']} {reminder['recurrence_type']}(s)\n"
        
        edit_text += "\nWhat would you like to change?"
        
        query.edit_message_text(
            edit_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data.startswith("edit_time_"):
        reminder_id = query.data[10:]
        context.user_data[query.from_user.id]['edit_action'] = 'time'
        query.edit_message_text(
            "Please send the new time for this reminder in one of these formats:\n"
            "• 3:00pm\n"
            "• 15:00\n"
            "• tomorrow 3pm\n"
            "• 25/02/2024 3:00pm\n\n"
            "Or use the quick time buttons below:",
            reply_markup=get_quick_time_keyboard()
        )
    
    elif query.data.startswith("edit_msg_"):
        reminder_id = query.data[9:]
        context.user_data[query.from_user.id]['edit_action'] = 'message'
        query.edit_message_text(
            "Please send the new message for this reminder."
        )
    
    elif query.data.startswith("edit_recur_"):
        reminder_id = query.data[11:]
        keyboard = [
            [InlineKeyboardButton("Daily", callback_data=f"set_recur_{reminder_id}_day_1")],
            [InlineKeyboardButton("Weekly", callback_data=f"set_recur_{reminder_id}_week_1")],
            [InlineKeyboardButton("Monthly", callback_data=f"set_recur_{reminder_id}_month_1")],
            [InlineKeyboardButton("No Recurrence", callback_data=f"set_recur_{reminder_id}_none_0")]
        ]
        query.edit_message_text(
            "Choose the recurrence pattern:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif query.data.startswith("edit_prio_"):
        reminder_id = query.data[10:]
        keyboard = [
            [InlineKeyboardButton("🔴 High", callback_data=f"set_prio_{reminder_id}_high")],
            [InlineKeyboardButton("🟡 Medium", callback_data=f"set_prio_{reminder_id}_medium")],
            [InlineKeyboardButton("🟢 Low", callback_data=f"set_prio_{reminder_id}_low")]
        ]
        query.edit_message_text(
            "Choose the priority level:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif query.data.startswith("set_recur_"):
        _, reminder_id, rec_type, interval = query.data.split('_')
        if rec_type == 'none':
            success = update_reminder(reminder_id, recurrence_type=None, recurrence_interval=None)
        else:
            success = update_reminder(reminder_id, recurrence_type=rec_type, recurrence_interval=int(interval))
        
        if success:
            reminder = get_reminder_by_id(reminder_id)
            if reminder:
                # Reschedule the reminder with new recurrence
                schedule_reminder(
                    context,
                    query.message.chat_id,
                    query.from_user.id,
                    reminder_id,
                    reminder['time'],
                    reminder['message'],
                    reminder['priority'],
                    rec_type if rec_type != 'none' else None,
                    int(interval) if rec_type != 'none' else None
                )
            query.edit_message_text("✅ Recurrence pattern updated! Use /list to see your reminders.")
        else:
            query.edit_message_text("❌ Failed to update reminder. It might have been cancelled or completed.")
    
    elif query.data.startswith("set_prio_"):
        _, reminder_id, priority = query.data.split('_')
        success = update_reminder(reminder_id, priority=priority)
        if success:
            query.edit_message_text(f"✅ Priority updated to {PRIORITY_EMOJIS[priority]} {priority}! Use /list to see your reminders.")
        else:
            query.edit_message_text("❌ Failed to update reminder. It might have been cancelled or completed.")
    
    elif query.data.startswith("edit_cancel_"):
        reminder_id = query.data[12:]
        if query.from_user.id in context.user_data:
            context.user_data.pop(query.from_user.id, None)
        query.edit_message_text("✅ Edit cancelled. Use /list to see your reminders.")
    
    elif query.data.startswith("cancel_"):
        reminder_id = query.data[7:]
        if delete_reminder(reminder_id):
            # Cancel the timer if it exists
            if reminder_id in active_reminders:
                active_reminders[reminder_id].cancel()
                del active_reminders[reminder_id]
            query.edit_message_text("✅ Reminder cancelled.")
        else:
            query.edit_message_text("❌ Reminder not found or already cancelled.")

def help_command(update: Update, context: CallbackContext) -> None:
    help_text = """
🤖 *Dancing Reminder Bot Help*

*Available Commands:*
📌 /start - Start the bot
❓ /help - Show this help message
🌍 /timezone - Set your timezone
📝 /format - Show reminder formats
📋 /list - List your active reminders
❌ /cancel - Cancel a reminder

*Quick Guide:*

1️⃣ *Natural Language:*
Just type naturally:
• "remind me tomorrow at 3pm to call mom"
• "urgent reminder at 9am for meeting"
• "daily reminder at 8pm to sleep"

2️⃣ *Priority Levels:*
• 🔴 High: "urgent reminder..." or "priority: high"
• 🟡 Medium: (default)
• 🟢 Low: "low priority..." or "priority: low"

3️⃣ *Recurring Reminders:*
• "daily reminder..."
• "every monday..."
• "every 2 days..."
• "monthly reminder..."

4️⃣ *Quick Times:*
Use the ⌨️ button panel for common times:
• In 1 hour
• Tonight
• Tomorrow morning
• This weekend

5️⃣ *Managing Reminders:*
• /list - See all reminders
• Click cancel buttons
• Use /cancel with reminder ID

*Pro Tips:*
• Use natural language for quick reminders
• Set priority for important tasks
• Use recurring reminders for habits
• Check /format for all options

Need examples? Type /format to see all formats!
    """
    update.message.reply_text(
        help_text, 
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_quick_time_keyboard()
    )

def parse_reminder(text):
    reminders = []
    current_reminder = defaultdict(str)
    
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    for line in lines:
        lower_line = line.lower()
        if lower_line.startswith('date:'):
            # If we already have time and message, save the current reminder
            if current_reminder.get('time') and current_reminder.get('message'):
                reminders.append(dict(current_reminder))
                current_reminder.clear()
            current_reminder['date'] = re.search(r'date:\s*(.+)', line, re.IGNORECASE).group(1).strip()
        elif lower_line.startswith('time:'):
            # If we already have time and message, save the current reminder
            if current_reminder.get('time') and current_reminder.get('message'):
                reminders.append(dict(current_reminder))
                current_reminder.clear()
            current_reminder['time'] = re.search(r'time:\s*(.+)', line, re.IGNORECASE).group(1).strip()
        elif current_reminder.get('time'):  # This is a message line
            current_reminder['message'] = current_reminder.get('message', '') + ' ' + line
    
    # Don't forget to add the last reminder
    if current_reminder.get('time') and current_reminder.get('message'):
        reminders.append(dict(current_reminder))
    
    return reminders

def list_reminders(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    reminders = get_user_reminders(user_id)
    
    if not reminders:
        update.message.reply_text("You don't have any active reminders.")
        return
    
    keyboard = []
    reminder_text = "*Your Active Reminders:*\n\n"
    
    for reminder in reminders:
        reminder_time = reminder['time']
        date_str = reminder_time.strftime('%d %b %Y')
        time_str = reminder_time.strftime('%I:%M %p')
        
        reminder_text += f"🕐 {date_str} at {time_str}\n"
        reminder_text += f"📝 {reminder['message']}\n"
        reminder_text += f"ID: `{reminder['id']}`\n\n"
        
        # Add buttons for each reminder
        keyboard.append([
            InlineKeyboardButton(f"✏️ Edit", callback_data=f"edit_{reminder['id']}"),
            InlineKeyboardButton(f"❌ Cancel", callback_data=f"cancel_{reminder['id']}")
        ])
    
    reminder_text += "\nTo edit or cancel a reminder, use the buttons below each reminder."
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(reminder_text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

def cancel_reminder(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    
    # Check if command has reminder_id
    if not context.args:
        update.message.reply_text("Please provide a reminder ID. Use /list to see your reminders and their IDs.")
        return
        
    reminder_id = context.args[0]
    if delete_reminder(reminder_id):
        update.message.reply_text(f"✅ Reminder {reminder_id} cancelled.")
    else:
        update.message.reply_text("❌ Reminder not found. Use /list to see your active reminders.")

def calculate_next_occurrence(last_time: datetime, recurrence_type: str, interval: int) -> datetime:
    """Calculate next occurrence properly handling months and DST"""
    if recurrence_type == 'day':
        return last_time + timedelta(days=interval)
    elif recurrence_type == 'week':
        return last_time + timedelta(weeks=interval)
    elif recurrence_type == 'month':
        # Properly handle month transitions
        year = last_time.year
        month = last_time.month + interval
        
        # Adjust year if needed
        if month > 12:
            year += month // 12
            month = month % 12
            if month == 0:
                month = 12
                year -= 1
        
        # Handle day overflow (e.g., Jan 31 -> Feb 28)
        day = min(last_time.day, calendar.monthrange(year, month)[1])
        
        # Create new datetime with same time but updated date
        next_time = last_time.replace(year=year, month=month, day=day)
        
        # Handle DST transitions
        if next_time.tzinfo:
            # Get the UTC offset difference
            old_offset = last_time.utcoffset()
            new_offset = next_time.utcoffset()
            if old_offset != new_offset:
                # Adjust time to maintain the same local time
                next_time += old_offset - new_offset
        
        return next_time

def schedule_reminder(context: CallbackContext, chat_id: int, user_id: int, reminder_id: str, 
                     reminder_time: datetime, message: str, priority: str = 'medium',
                     recurrence_type: str = None, recurrence_interval: int = None):
    """Schedule a reminder with proper timezone and DST handling"""
    # Ensure the reminder_time has timezone info
    if reminder_time.tzinfo is None:
        user_timezone = pytz.timezone(get_user_timezone(user_id))
        reminder_time = user_timezone.localize(reminder_time)
    
    # Convert to UTC for delay calculation
    now_utc = datetime.now(pytz.UTC)
    reminder_time_utc = reminder_time.astimezone(pytz.UTC)
    
    delay = (reminder_time_utc - now_utc).total_seconds()
    if delay > 0:
        timer = threading.Timer(
            delay,
            send_reminder,
            args=(context, chat_id, user_id, reminder_id, message, priority, 
                  recurrence_type, recurrence_interval, reminder_time)
        )
        
        # Store timer and metadata
        active_reminders[reminder_id] = {
            'timer': timer,
            'scheduled_time': reminder_time,
            'user_id': user_id,
            'chat_id': chat_id,
            'message': message,
            'priority': priority,
            'recurrence_type': recurrence_type,
            'recurrence_interval': recurrence_interval
        }
        
        timer.start()
        logger.info(f"Scheduled reminder {reminder_id} for {reminder_time}")

def reschedule_reminder(context: CallbackContext, reminder_id: str, 
                       new_time: Optional[datetime] = None) -> bool:
    """Reschedule an existing reminder, optionally with a new time"""
    if reminder_id not in active_reminders:
        return False
    
    reminder = active_reminders[reminder_id]
    reminder['timer'].cancel()
    
    if new_time:
        scheduled_time = new_time
    else:
        scheduled_time = reminder['scheduled_time']
    
    schedule_reminder(
        context,
        reminder['chat_id'],
        reminder['user_id'],
        reminder_id,
        scheduled_time,
        reminder['message'],
        reminder['priority'],
        reminder['recurrence_type'],
        reminder['recurrence_interval']
    )
    
    return True

def send_reminder(context: CallbackContext, chat_id: int, user_id: int, reminder_id: str, 
                 message: str, priority: str, recurrence_type: str, recurrence_interval: int,
                 last_time: datetime):
    """Send a reminder and handle recurrence with proper timezone handling"""
    try:
        # Send the reminder with priority emoji
        priority_emoji = PRIORITY_EMOJIS.get(priority, '')
        context.bot.send_message(
            chat_id=chat_id,
            text=f"{priority_emoji} Reminder: {message}"
        )
        
        # Clean up the active reminder
        if reminder_id in active_reminders:
            del active_reminders[reminder_id]
        
        # Handle recurrence
        if recurrence_type:
            # Calculate next occurrence
            next_time = calculate_next_occurrence(last_time, recurrence_type, recurrence_interval)
            
            # Check if we should create the next occurrence
            if not should_create_next_occurrence(reminder_id, next_time):
                mark_reminder_complete(reminder_id)
                return
            
            # Generate new reminder ID for the next occurrence
            next_reminder_id = str(uuid.uuid4())[:8]
            
            try:
                # Save and schedule next occurrence
                save_reminder(
                    user_id, next_reminder_id, message, next_time,
                    priority, recurrence_type, recurrence_interval, reminder_id
                )
                
                schedule_reminder(
                    context, chat_id, user_id, next_reminder_id,
                    next_time, message, priority, recurrence_type, recurrence_interval
                )
                
                logger.info(f"Created next occurrence of recurring reminder: {next_reminder_id}")
                
            except Exception as e:
                logger.error(f"Failed to create next occurrence of reminder {reminder_id}: {str(e)}")
                context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Failed to schedule next occurrence of recurring reminder. Please check /list"
                )
        
        mark_reminder_complete(reminder_id)
        
    except Exception as e:
        logger.error(f"Error sending reminder {reminder_id}: {str(e)}")
        if reminder_id in active_reminders:
            del active_reminders[reminder_id]

@safe_db_operation
def should_create_next_occurrence(conn, reminder_id: str, next_time: datetime) -> bool:
    """Check if we should create the next occurrence of a recurring reminder"""
    c = conn.cursor()
    c.execute('''
        SELECT recurrence_end_date 
        FROM reminders 
        WHERE id = ?
    ''', (reminder_id,))
    
    row = c.fetchone()
    if not row or not row['recurrence_end_date']:
        return True
    
    end_date = datetime.fromisoformat(row['recurrence_end_date'])
    return next_time <= end_date

def handle_missed_reminders(context: CallbackContext):
    """Handle missed reminders with proper timezone handling"""
    now = datetime.now(pytz.UTC)
    try:
        reminders = get_all_pending_reminders()
        
        for reminder in reminders:
            reminder_time = reminder['time']
            if reminder_time.tzinfo is None:
                user_timezone = pytz.timezone(reminder['timezone'])
                reminder_time = user_timezone.localize(reminder_time)
            
            reminder_time_utc = reminder_time.astimezone(pytz.UTC)
            
            if reminder_time_utc < now:
                # Send missed reminder
                context.bot.send_message(
                    chat_id=reminder['user_id'],
                    text=f"⚠️ Missed Reminder from {reminder_time.strftime('%d %b %Y %I:%M %p')}:\n{reminder['message']}"
                )
                
                # Handle recurring reminders
                if reminder['recurrence_type']:
                    next_time = calculate_next_occurrence(
                        reminder_time,
                        reminder['recurrence_type'],
                        reminder['recurrence_interval']
                    )
                    
                    if next_time > now and should_create_next_occurrence(reminder['id'], next_time):
                        # Create next occurrence
                        next_reminder_id = str(uuid.uuid4())[:8]
                        save_reminder(
                            reminder['user_id'],
                            next_reminder_id,
                            reminder['message'],
                            next_time,
                            reminder['priority'],
                            reminder['recurrence_type'],
                            reminder['recurrence_interval'],
                            reminder['id']
                        )
                        
                        schedule_reminder(
                            context,
                            reminder['user_id'],
                            reminder['user_id'],
                            next_reminder_id,
                            next_time,
                            reminder['message'],
                            reminder['priority'],
                            reminder['recurrence_type'],
                            reminder['recurrence_interval']
                        )
                
                mark_reminder_complete(reminder['id'])
            else:
                # Schedule future reminder
                schedule_reminder(
                    context,
                    reminder['user_id'],
                    reminder['user_id'],
                    reminder['id'],
                    reminder_time,
                    reminder['message'],
                    reminder['priority'],
                    reminder['recurrence_type'],
                    reminder['recurrence_interval']
                )
    except Exception as e:
        logger.error(f"Error handling missed reminders: {str(e)}")

def get_quick_time_keyboard():
    keyboard = [
        [KeyboardButton(QUICK_TIMES['in_1_hour']), KeyboardButton(QUICK_TIMES['in_2_hours'])],
        [KeyboardButton(QUICK_TIMES['tonight']), KeyboardButton(QUICK_TIMES['tomorrow_morning'])],
        [KeyboardButton(QUICK_TIMES['tomorrow_afternoon']), KeyboardButton(QUICK_TIMES['this_weekend'])]
    ]
    return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)

def process_quick_time(text: str, user_timezone: datetime) -> Optional[Dict]:
    now = datetime.now(user_timezone)
    
    if text == QUICK_TIMES['in_1_hour']:
        return {'time': (now + timedelta(hours=1)).time(), 'date': now.date()}
    elif text == QUICK_TIMES['in_2_hours']:
        return {'time': (now + timedelta(hours=2)).time(), 'date': now.date()}
    elif text == QUICK_TIMES['tonight']:
        return {'time': datetime.strptime('8:00PM', '%I:%M%p').time(), 'date': now.date()}
    elif text == QUICK_TIMES['tomorrow_morning']:
        return {'time': datetime.strptime('9:00AM', '%I:%M%p').time(), 
                'date': (now + timedelta(days=1)).date()}
    elif text == QUICK_TIMES['tomorrow_afternoon']:
        return {'time': datetime.strptime('2:00PM', '%I:%M%p').time(), 
                'date': (now + timedelta(days=1)).date()}
    elif text == QUICK_TIMES['this_weekend']:
        days_until_saturday = (5 - now.weekday()) % 7
        return {'time': datetime.strptime('10:00AM', '%I:%M%p').time(), 
                'date': (now + timedelta(days=days_until_saturday)).date()}
    return None

def handle_message(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    message_text = update.message.text.strip()
    
    # Check if user is in edit mode
    if user_id in context.user_data and 'editing_reminder' in context.user_data[user_id]:
        reminder_id = context.user_data[user_id]['editing_reminder']
        edit_action = context.user_data[user_id].get('edit_action')
        reminder = get_reminder_by_id(reminder_id)
        
        if not reminder:
            update.message.reply_text("❌ Reminder not found or already completed.")
            context.user_data.pop(user_id, None)
            return
        
        if edit_action == 'message':
            if update_reminder(reminder_id, message=message_text):
                update.message.reply_text("✅ Message updated! Use /list to see your reminders.")
            else:
                update.message.reply_text("❌ Failed to update reminder.")
            context.user_data.pop(user_id, None)
            return
        
        elif edit_action == 'time':
            try:
                # Try to parse the new time
                user_timezone = pytz.timezone(get_user_timezone(user_id))
                now = datetime.now(user_timezone)
                
                # Parse natural language time
                parsed = ReminderParser.parse_natural_time(message_text, now)
                new_time = datetime.combine(parsed['date'], parsed['time'])
                new_time = user_timezone.localize(new_time)
                
                if new_time < now:
                    if 'date' not in parsed:  # Only add a day if no specific date was set
                        new_time += timedelta(days=1)
                
                if new_time < now:
                    update.message.reply_text("❌ Cannot set reminder for past time.")
                    return
                
                if update_reminder(reminder_id, reminder_time=new_time):
                    # Reschedule the reminder
                    if reminder_id in active_reminders:
                        active_reminders[reminder_id].cancel()
                        del active_reminders[reminder_id]
                    
                    schedule_reminder(
                        context,
                        update.message.chat_id,
                        user_id,
                        reminder_id,
                        new_time,
                        reminder['message'],
                        reminder['priority'],
                        reminder['recurrence_type'],
                        reminder['recurrence_interval']
                    )
                    
                    update.message.reply_text(
                        f"✅ Time updated to {new_time.strftime('%I:%M %p on %d %b %Y')}!\n"
                        "Use /list to see your reminders."
                    )
                else:
                    update.message.reply_text("❌ Failed to update reminder.")
            except Exception as e:
                logger.error(f"Error updating reminder time: {str(e)}")
                update.message.reply_text(
                    "❌ Invalid time format. Please use formats like:\n"
                    "• 3:00pm\n"
                    "• 15:00\n"
                    "• tomorrow 3pm\n"
                    "• 25/02/2024 3:00pm"
                )
            context.user_data.pop(user_id, None)
            return
    
    # Check for quick time buttons
    quick_time = None
    if message_text in QUICK_TIMES.values():
        user_timezone = pytz.timezone(get_user_timezone(user_id))
        quick_time = process_quick_time(message_text, user_timezone)
        if quick_time:
            context.user_data[user_id] = {'quick_time': quick_time}
            update.message.reply_text(
                "Great! Now send me the reminder message.",
                reply_markup=None  # Remove keyboard
            )
            return
    
    # Handle reminder setting
    if message_text.lower().startswith('reminder') or message_text.lower().startswith('remind'):
        if not get_user_timezone(user_id):
            update.message.reply_text("Please set your time zone first using /timezone")
        return

    try:
            user_timezone = pytz.timezone(get_user_timezone(user_id))
            now = datetime.now(user_timezone)
            
            # Parse natural language if it doesn't follow the structured format
            if 'time:' not in message_text and 'date:' not in message_text:
                parsed = ReminderParser.parse_natural_time(message_text, now)
                message_text = ReminderParser.format_reminder_text(parsed)
            
            reminders = parse_reminder(message_text)
            if not reminders:
                raise ValueError("No valid reminders found")
            
            for reminder in reminders:
        # Parse the time
                try:
                    reminder_time = datetime.strptime(reminder['time'], '%I:%M%p')
                except ValueError:
                    try:
                        reminder_time = datetime.strptime(reminder['time'], '%I:%M %p')
                    except ValueError:
                        reminder_time = datetime.strptime(reminder['time'], '%H:%M')
                
                # Parse the date if provided
                if 'date' in reminder:
                    try:
                        reminder_date = datetime.strptime(reminder['date'], '%d/%m/%Y')
                        reminder_time = reminder_time.replace(
                            year=reminder_date.year,
                            month=reminder_date.month,
                            day=reminder_date.day
                        )
                    except ValueError:
                        update.message.reply_text(f"❌ Invalid date format: {reminder['date']}. Use DD/MM/YYYY")
                        continue
                else:
                    reminder_time = reminder_time.replace(
                        year=now.year,
                        month=now.month,
                        day=now.day
                    )
                
                reminder_time = user_timezone.localize(reminder_time)

        # Schedule the reminder
        if reminder_time < now:
                    if 'date' not in reminder:  # Only add a day if no specific date was set
                        reminder_time += timedelta(days=1)
                
                if reminder_time < now:
                    update.message.reply_text(f"❌ Cannot set reminder for past time: {reminder['message']}")
                    continue
                
                # Generate unique ID for the reminder
                reminder_id = str(uuid.uuid4())[:8]
                
                # Get priority and recurrence
                priority = reminder.get('priority', 'medium')
                recurrence_type = reminder.get('recurrence_type')
                recurrence_interval = reminder.get('recurrence_interval', 1)
                
                # Store reminder in persistent storage
                save_reminder(
                    user_id, reminder_id, reminder['message'], reminder_time,
                    priority, recurrence_type, recurrence_interval
                )
                
                # Schedule the reminder
                schedule_reminder(
                    context,
                    update.message.chat_id,
                    user_id,
                    reminder_id,
                    reminder_time,
                    reminder['message'],
                    priority,
                    recurrence_type,
                    recurrence_interval
                )
                
                # Format response
                priority_emoji = PRIORITY_EMOJIS.get(priority, '')
                date_str = reminder_time.strftime('%d %b %Y')
                time_str = reminder_time.strftime('%I:%M %p')
                
                response = f"{priority_emoji} Reminder set for {date_str} at {time_str}:\n{reminder['message']}"
                
                if recurrence_type:
                    response += f"\n🔄 Repeats every {recurrence_interval} {recurrence_type}(s)"
                
                response += f"\nID: `{reminder_id}`"
                
                update.message.reply_text(
                    response,
                    parse_mode=ParseMode.MARKDOWN
                )

    except Exception as e:
            logger.error(f"Error setting reminder: {str(e)}")
            update.message.reply_text(
                "❌ Error setting reminder. Use /format to see the correct format."
            )
    
    # Handle quick time message
    elif 'quick_time' in context.user_data.get(user_id, {}):
        quick_time = context.user_data[user_id]['quick_time']
        reminder_text = f"reminder\ntime: {quick_time['time'].strftime('%I:%M%p')}\n"
        if quick_time['date'] != datetime.now().date():
            reminder_text += f"date: {quick_time['date'].strftime('%d/%m/%Y')}\n"
        reminder_text += message_text
        
        # Clear quick time data
        del context.user_data[user_id]['quick_time']
        
        # Process as normal reminder
        update.message.text = reminder_text
        handle_message(update, context)
        return
    
    # Handle timezone setting
    elif message_text in pytz.all_timezones:
        set_user_timezone(user_id, message_text)
        update.message.reply_text(
            f"✅ Time zone set to {message_text}.\n\n"
            "You can now set reminders! Use /format to see reminder formats.",
            reply_markup=get_quick_time_keyboard()
        )
    else:
        # Check if it looks like a timezone attempt
        if '/' in message_text:
            similar_timezones = [tz for tz in list(TIMEZONE_SUGGESTIONS.keys())[:4] 
                               if tz.lower().startswith(message_text.split('/')[0].lower())]
            if similar_timezones:
                keyboard = []
                for tz in similar_timezones:
                    keyboard.append([InlineKeyboardButton(f"{TIMEZONE_SUGGESTIONS[tz]}", callback_data=f"tz_{tz}")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                update.message.reply_text(
                    "Did you mean one of these timezones?",
                    reply_markup=reply_markup
                )
                return
        
        update.message.reply_text(
            "❌ Invalid message. Use /help to see available commands.",
            reply_markup=get_quick_time_keyboard()
        )

def main() -> None:
    # Initialize database
    init_db()
    
    # Start the keep_alive server
    keep_alive()
    
    # Create the Updater with token from environment variable
    updater = Updater(TELEGRAM_BOT_TOKEN)

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    # Register handlers
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("format", format_command))
    dispatcher.add_handler(CommandHandler("timezone", timezone_command))
    dispatcher.add_handler(CommandHandler("list", list_reminders))
    dispatcher.add_handler(CommandHandler("cancel", cancel_reminder))
    dispatcher.add_handler(CallbackQueryHandler(button_callback))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    
    # Handle missed reminders and schedule future ones
    handle_missed_reminders(updater.dispatcher)

    # Start the Bot
    updater.start_polling()

    # Run the bot until you send a signal to stop
    updater.idle()

if __name__ == '__main__':
    main()
