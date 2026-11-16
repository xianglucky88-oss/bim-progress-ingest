def load_records(payload: dict) -> list[dict]:
    rows = payload.get("components", [])
    if not isinstance(rows, list):
        raise ValueError("components must be a list")
    return [dict(row) for row in rows if isinstance(row, dict)]
