def merge_windows(windows):
    windows.sort()
    merged = []
    for start, end in windows:
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
