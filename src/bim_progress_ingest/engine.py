def calculate(rows, payload):
    updates={r["guid"]: float(r["actual"]) for r in payload.get("updates", [])}; total=sum(float(r["planned"]) for r in rows); actual=sum(min(float(r["planned"]), updates.get(r["guid"],0)) for r in rows)
    return {"component_count":len(rows), "planned":total, "actual":actual, "progress_pct":round(100*actual/total,2) if total else 0, "missing_updates":sorted({r["guid"] for r in rows}-set(updates))}
