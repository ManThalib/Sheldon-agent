def _created_epoch(created_at: str) -> float:
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0.0