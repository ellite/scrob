DEFAULT_SCROBBLE_PERCENT = 75


def scrobble_threshold_for(connection) -> float:
    percent = connection.scrobble_percent if connection is not None else DEFAULT_SCROBBLE_PERCENT
    return percent / 100


def reached_threshold(position: int, duration: int, threshold: float) -> bool:
    return duration > 0 and position / duration >= threshold
