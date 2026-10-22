def humanize_minutes(total: int, *, zero: str = "0m") -> str:
    if total <= 0:
        return zero
    hours, minutes = divmod(total, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"
