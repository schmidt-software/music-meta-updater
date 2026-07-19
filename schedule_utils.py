#!/usr/bin/env python3
"""Cron schedule validation and parsing utilities."""

import re


def validate_cron_expression(schedule):
    """Validate a cron expression format.
    
    Standard format: minute hour day month weekday
    - minute: 0-59
    - hour: 0-23
    - day: 1-31
    - month: 1-12
    - weekday: 0-6 (0 = Sunday)
    
    Supports: * / , - ranges
    
    Returns (True, None) if valid, (False, error_msg) if invalid.
    """
    if not schedule or not isinstance(schedule, str):
        return False, "Schedule must be a non-empty string"
    
    parts = schedule.strip().split()
    
    if len(parts) != 5:
        return False, f"Expected 5 fields (minute hour day month weekday), got {len(parts)}"
    
    minute, hour, day, month, weekday = parts
    
    # Basic validation: each field should contain digits, *, /, -, or ,
    valid_chars = re.compile(r'^[0-9,\-/*]+$')
    
    for field, name in [(minute, 'minute'), (hour, 'hour'), (day, 'day'),
                        (month, 'month'), (weekday, 'weekday')]:
        if not valid_chars.match(field):
            return False, f"Invalid characters in {name} field: {field}"
    
    # Check ranges (simplified)
    ranges = {
        'minute': (0, 59),
        'hour': (0, 23),
        'day': (1, 31),
        'month': (1, 12),
        'weekday': (0, 6),
    }
    
    for field, (field_name, (min_val, max_val)) in \
        zip(parts, ranges.items()):
        
        if field == '*':
            continue
        
        # Extract all numbers
        numbers = re.findall(r'\d+', field)
        for num_str in numbers:
            num = int(num_str)
            if num < min_val or num > max_val:
                return False, f"{field_name} value {num} out of range [{min_val}, {max_val}]"
    
    return True, None


def describe_cron_expression(schedule):
    """Generate human-readable description of a cron expression.
    
    Returns a description string or None if schedule is invalid.
    """
    valid, error = validate_cron_expression(schedule)
    if not valid:
        return None
    
    parts = schedule.strip().split()
    minute, hour, day, month, weekday = parts
    
    descriptions = []
    
    # Hour
    if hour == '*':
        descriptions.append("every hour")
    elif '/' in hour:
        interval = hour.split('/')[1]
        descriptions.append(f"every {interval} hours")
    else:
        descriptions.append(f"at {hour}:00")
    
    # Day of month
    if day != '*':
        descriptions.append(f"on day {day}")
    
    # Month
    if month != '*':
        descriptions.append(f"in month {month}")
    
    # Weekday
    weekday_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    if weekday != '*':
        if weekday in '0123456':
            descriptions.append(f"on {weekday_names[int(weekday)]}")
    
    # Minute
    if minute != '0' and minute != '*':
        descriptions.append(f"at minute {minute}")
    
    return ", ".join(descriptions) if descriptions else "runs: " + schedule


if __name__ == "__main__":
    # Test
    test_schedules = [
        "0 2 * * *",      # Daily at 2 AM
        "0 */6 * * *",    # Every 6 hours
        "0 0 * * 0",      # Weekly on Sunday
        "invalid",        # Invalid
        "0 25 * * *",     # Invalid hour
    ]
    
    for schedule in test_schedules:
        valid, error = validate_cron_expression(schedule)
        desc = describe_cron_expression(schedule) if valid else f"Invalid: {error}"
        print(f"{schedule:20} -> {desc}")


def validate_threshold(threshold_str):
    """Validate a matching confidence threshold.
    
    Args:
        threshold_str: String representation of threshold (e.g., "0.85")
    
    Returns:
        (True, float_value) if valid, (False, error_msg) if invalid.
    """
    if not threshold_str:
        return False, "Threshold cannot be empty"
    
    try:
        threshold = float(threshold_str)
    except ValueError:
        return False, f"Threshold must be a number, got: {threshold_str}"
    
    if threshold < 0.0 or threshold > 1.0:
        return False, f"Threshold must be between 0.0 and 1.0, got: {threshold}"
    
    return True, threshold


def describe_threshold(threshold):
    """Generate a human-readable description of a threshold value.
    
    Args:
        threshold: Float value between 0.0 and 1.0
    
    Returns:
        Description string
    """
    if threshold >= 0.95:
        return "Ultra-conservative (almost no matches)"
    elif threshold >= 0.90:
        return "Conservative (strict matching, recommended for classical/jazz)"
    elif threshold >= 0.80:
        return "Balanced (default, recommended for pop/rock)"
    elif threshold >= 0.60:
        return "Aggressive (higher tagging rate, recommended for compilations)"
    else:
        return "Very aggressive (many false positives)"
