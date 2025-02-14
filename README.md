# Telegram Reminder Bot

A feature-rich Telegram bot for managing reminders with support for recurring reminders, priorities, and natural language processing.

## Features

- 🕒 Natural language time parsing
- 🔄 Recurring reminders (daily, weekly, monthly)
- ⭐ Priority levels (high, medium, low)
- 🌍 Timezone support
- ✏️ Edit existing reminders
- 📝 Multiple reminder formats
- ⚡ Quick time buttons
- 🔍 List and manage reminders

## Setup

1. Clone the repository:
```bash
git clone <your-repo-url>
cd telegram-reminder-bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file with your bot token:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
```

4. Run database migrations:
```bash
python migrations.py
```

5. Start the bot:
```bash
python bot.py
```

## Usage

### Basic Commands

- `/start` - Start the bot
- `/help` - Show help message
- `/timezone` - Set your timezone
- `/format` - Show reminder formats
- `/list` - List your reminders
- `/cancel` - Cancel a reminder

### Setting Reminders

1. **Natural Language**:
```
remind me tomorrow at 3pm to call mom
urgent reminder at 9am for meeting
daily reminder at 8pm to sleep
```

2. **Structured Format**:
```
reminder
priority: high
time: 9:00am
date: 25/02/2024
repeat: daily
Take medicine
```

3. **Multiple Reminders**:
```
reminder
time: 9:00am
Take medicine

time: 2:30pm
Doctor appointment
```

### Priority Levels

- 🔴 High: "urgent reminder..." or "priority: high"
- 🟡 Medium: (default)
- 🟢 Low: "low priority..." or "priority: low"

### Recurring Options

- Daily: "daily reminder..." or "every day..."
- Weekly: "weekly reminder..." or "every week..."
- Monthly: "monthly reminder..." or "every month..."
- Custom: "every 2 days...", "every 3 weeks..."

## Development

### Project Structure

- `bot.py` - Main bot logic and command handlers
- `migrations.py` - Database migrations
- `nlp_parser.py` - Natural language processing for reminders
- `requirements.txt` - Project dependencies

### Database Schema

The bot uses SQLite with the following schema:

1. **users**
   - user_id (PRIMARY KEY)
   - timezone
   - created_at
   - last_active_at
   - max_reminders
   - reminder_count

2. **reminders**
   - id (PRIMARY KEY)
   - user_id (FOREIGN KEY)
   - message
   - reminder_time
   - status
   - priority
   - recurrence_type
   - recurrence_interval
   - parent_reminder_id

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details. 