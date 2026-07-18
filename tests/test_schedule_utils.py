import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import schedule_utils as su


def test_validate_cron_expression_valid():
    """Valid cron expressions are accepted."""
    valid_schedules = [
        "0 2 * * *",      # Daily at 2 AM
        "0 */6 * * *",    # Every 6 hours
        "0 0 * * 0",      # Weekly on Sunday
        "30 14 * * 1-5",  # 2:30 PM weekdays
        "*/15 * * * *",   # Every 15 minutes
    ]
    for schedule in valid_schedules:
        valid, error = su.validate_cron_expression(schedule)
        assert valid is True, f"Expected valid, got error: {error}"


def test_validate_cron_expression_invalid_format():
    """Invalid formats are rejected."""
    invalid_schedules = [
        "",                # Empty
        "* * *",          # Too few fields
        "* * * * * *",    # Too many fields
        "abc def ghi jkl mno",  # Non-numeric
    ]
    for schedule in invalid_schedules:
        valid, error = su.validate_cron_expression(schedule)
        assert valid is False, f"Expected invalid for: {schedule}"


def test_validate_cron_expression_out_of_range():
    """Out-of-range values are rejected."""
    invalid_schedules = [
        "60 * * * *",     # Minute out of range
        "0 25 * * *",     # Hour out of range
        "0 0 32 * *",     # Day out of range
        "0 0 * 13 *",     # Month out of range
        "0 0 * * 7",      # Weekday out of range
    ]
    for schedule in invalid_schedules:
        valid, error = su.validate_cron_expression(schedule)
        assert valid is False, f"Expected invalid for: {schedule}, got: {error}"


def test_describe_cron_expression():
    """Human-readable descriptions are generated."""
    schedule = "0 2 * * *"
    desc = su.describe_cron_expression(schedule)
    assert desc is not None
    assert "2" in desc or "2:00" in desc


def test_describe_invalid_schedule():
    """Invalid schedules return None."""
    schedule = "invalid cron"
    desc = su.describe_cron_expression(schedule)
    assert desc is None


def test_common_schedules():
    """Common production schedules are valid."""
    common = [
        "0 2 * * *",      # Daily 2 AM
        "0 0 * * 0",      # Weekly Sunday
        "0 */12 * * *",   # Every 12 hours
        "0 1 * * *",      # Daily 1 AM
    ]
    for schedule in common:
        valid, _ = su.validate_cron_expression(schedule)
        assert valid is True
