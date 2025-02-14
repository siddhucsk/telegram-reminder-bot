import sqlite3
import sys
from datetime import datetime

def migrate():
    print("Running database migrations...")
    
    try:
        conn = sqlite3.connect('bot.db')
        c = conn.cursor()
        
        # Create migration history table
        c.execute('''
            CREATE TABLE IF NOT EXISTS migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Get applied migrations
        c.execute('SELECT name FROM migrations')
        applied = set(row[0] for row in c.fetchall())
        
        # Define migrations
        migrations = [
            ('001_initial', '''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    reminder_time TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT CHECK (status IN ('pending', 'completed', 'cancelled')) DEFAULT 'pending',
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                );
            '''),
            ('002_indexes', '''
                CREATE INDEX IF NOT EXISTS idx_reminders_status_time 
                ON reminders(status, reminder_time);
                
                CREATE INDEX IF NOT EXISTS idx_reminders_user_status 
                ON reminders(user_id, status);
                
                CREATE INDEX IF NOT EXISTS idx_users_timezone 
                ON users(timezone);
            '''),
            ('003_recurring_and_priority', '''
                ALTER TABLE reminders ADD COLUMN priority TEXT 
                CHECK (priority IN ('high', 'medium', 'low')) DEFAULT 'medium';
                
                ALTER TABLE reminders ADD COLUMN recurrence_type TEXT 
                CHECK (recurrence_type IN ('day', 'week', 'month') OR recurrence_type IS NULL);
                
                ALTER TABLE reminders ADD COLUMN recurrence_interval INTEGER 
                CHECK (recurrence_interval > 0 OR recurrence_interval IS NULL);
                
                ALTER TABLE reminders ADD COLUMN recurrence_end_date TIMESTAMP;
                ALTER TABLE reminders ADD COLUMN parent_reminder_id TEXT REFERENCES reminders(id);
                
                CREATE INDEX IF NOT EXISTS idx_reminders_priority 
                ON reminders(priority) WHERE priority != 'medium';
                
                CREATE INDEX IF NOT EXISTS idx_reminders_recurrence 
                ON reminders(recurrence_type, recurrence_interval) 
                WHERE recurrence_type IS NOT NULL;
                
                CREATE INDEX IF NOT EXISTS idx_reminders_parent 
                ON reminders(parent_reminder_id) 
                WHERE parent_reminder_id IS NOT NULL;
            '''),
            ('004_cleanup_and_limits', '''
                -- Add columns for reminder limits and cleanup
                ALTER TABLE users ADD COLUMN max_reminders INTEGER 
                CHECK (max_reminders > 0) DEFAULT 50;
                
                ALTER TABLE users ADD COLUMN reminder_count INTEGER 
                CHECK (reminder_count >= 0) DEFAULT 0;
                
                -- Add trigger to update reminder count
                CREATE TRIGGER IF NOT EXISTS update_reminder_count_insert
                AFTER INSERT ON reminders
                WHEN NEW.status = 'pending'
                BEGIN
                    UPDATE users 
                    SET reminder_count = reminder_count + 1
                    WHERE user_id = NEW.user_id;
                END;
                
                CREATE TRIGGER IF NOT EXISTS update_reminder_count_delete
                AFTER UPDATE ON reminders
                WHEN NEW.status != 'pending' AND OLD.status = 'pending'
                BEGIN
                    UPDATE users 
                    SET reminder_count = reminder_count - 1
                    WHERE user_id = NEW.user_id;
                END;
            ''')
        ]
        
        # Apply new migrations
        for name, sql in migrations:
            if name not in applied:
                print(f"Applying migration: {name}")
                c.executescript(sql)
                c.execute('INSERT INTO migrations (name) VALUES (?)', (name,))
                conn.commit()
                print(f"Applied migration: {name}")
        
        print("Migrations completed successfully.")
        
    except Exception as e:
        print(f"Error during migration: {e}")
        sys.exit(1)
        
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    migrate() 