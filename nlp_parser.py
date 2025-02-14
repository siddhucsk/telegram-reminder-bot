from datetime import datetime, timedelta
import re
from typing import Dict, Optional, Tuple
import calendar

class ReminderParser:
    DAYS_OF_WEEK = {
        'monday': 0, 'mon': 0,
        'tuesday': 1, 'tue': 1,
        'wednesday': 2, 'wed': 2,
        'thursday': 3, 'thu': 3,
        'friday': 4, 'fri': 4,
        'saturday': 5, 'sat': 5,
        'sunday': 6, 'sun': 6
    }
    
    RELATIVE_DAYS = {
        'today': 0,
        'tomorrow': 1,
        'tmr': 1,
        'next week': 7,
    }
    
    RELATIVE_TIMES = {
        'morning': '9:00am',
        'noon': '12:00pm',
        'afternoon': '2:00pm',
        'evening': '6:00pm',
        'night': '8:00pm',
        'midnight': '12:00am',
    }
    
    RECURRENCE_PATTERNS = {
        'daily': ('day', 1),
        'weekly': ('week', 1),
        'monthly': ('month', 1),
        'every day': ('day', 1),
        'every week': ('week', 1),
        'every month': ('month', 1),
    }
    
    PRIORITY_KEYWORDS = {
        'urgent': 'high',
        'important': 'high',
        'high': 'high',
        'medium': 'medium',
        'normal': 'medium',
        'low': 'low',
    }

    @classmethod
    def parse_natural_time(cls, text: str, current_time: datetime) -> Dict:
        text = text.lower().strip()
        result = {
            'time': None,
            'date': None,
            'recurrence_type': None,
            'recurrence_interval': None,
            'priority': 'medium',
            'message': text
        }
        
        # Extract priority if present
        for keyword, priority in cls.PRIORITY_KEYWORDS.items():
            if keyword in text:
                result['priority'] = priority
                text = text.replace(keyword, '').strip()
        
        # Extract recurrence if present
        for pattern, (rec_type, interval) in cls.RECURRENCE_PATTERNS.items():
            if pattern in text:
                result['recurrence_type'] = rec_type
                result['recurrence_interval'] = interval
                text = text.replace(pattern, '').strip()
        
        # Custom recurrence patterns
        every_x_match = re.search(r'every (\d+) (day|week|month)s?', text)
        if every_x_match:
            interval, rec_type = every_x_match.groups()
            result['recurrence_type'] = rec_type
            result['recurrence_interval'] = int(interval)
            text = text.replace(every_x_match.group(), '').strip()
        
        # Try to find a time
        time_match = re.search(r'(?:at )?((?:1[0-2]|0?[1-9])(?::[0-5][0-9])?\s*(?:am|pm)|(?:[01]?[0-9]|2[0-3]):[0-5][0-9])', text)
        if time_match:
            time_str = time_match.group(1)
            try:
                if ':' not in time_str:
                    time_str = time_str.replace(' ', '')
                    if len(time_str) > 4:  # Has AM/PM
                        result['time'] = datetime.strptime(time_str, '%I%p').time()
                    else:
                        result['time'] = datetime.strptime(time_str, '%H').time()
                else:
                    if 'am' in time_str.lower() or 'pm' in time_str.lower():
                        result['time'] = datetime.strptime(time_str, '%I:%M%p').time()
                    else:
                        result['time'] = datetime.strptime(time_str, '%H:%M').time()
            except ValueError:
                pass
        
        # Check for relative times
        for rel_time, time_str in cls.RELATIVE_TIMES.items():
            if rel_time in text:
                result['time'] = datetime.strptime(time_str, '%I:%M%p').time()
                text = text.replace(rel_time, '').strip()
        
        # Check for relative days
        for rel_day, days_ahead in cls.RELATIVE_DAYS.items():
            if rel_day in text:
                result['date'] = current_time.date() + timedelta(days=days_ahead)
                text = text.replace(rel_day, '').strip()
                break
        
        # Check for day of week
        for day_name, day_num in cls.DAYS_OF_WEEK.items():
            if day_name in text:
                current_day = current_time.weekday()
                days_ahead = (day_num - current_day) % 7
                if days_ahead == 0:
                    days_ahead = 7  # Next week if same day
                if 'next' in text:
                    days_ahead += 7
                result['date'] = current_time.date() + timedelta(days=days_ahead)
                text = text.replace(day_name, '').strip()
                break
        
        # Clean up the message
        result['message'] = re.sub(r'\s+', ' ', text).strip()
        if not result['message']:
            result['message'] = "Reminder"
        
        # Set defaults if needed
        if not result['date']:
            result['date'] = current_time.date()
        if not result['time']:
            result['time'] = current_time.time()
        
        return result

    @classmethod
    def format_reminder_text(cls, parsed: Dict) -> str:
        """Convert parsed reminder back to bot format"""
        reminder_text = "reminder\n"
        
        # Add priority if not medium
        if parsed['priority'] != 'medium':
            reminder_text += f"priority: {parsed['priority']}\n"
        
        # Add date if not today
        if parsed['date'] != datetime.now().date():
            reminder_text += f"date: {parsed['date'].strftime('%d/%m/%Y')}\n"
        
        # Add time
        reminder_text += f"time: {parsed['time'].strftime('%I:%M%p').lower()}\n"
        
        # Add recurrence if present
        if parsed['recurrence_type']:
            reminder_text += f"repeat: every {parsed['recurrence_interval']} {parsed['recurrence_type']}(s)\n"
        
        # Add message
        reminder_text += parsed['message']
        
        return reminder_text 