def event_totals(events):
    totals = {}
    for event in events:
        totals[event["key"]] = event["delta"]
    return [{"key": key, "total": value} for key, value in totals.items() if value]
