"""Lines for the push job's alert email: every changed keyword with the bid it
had on Blinkit before the push and the bid it was sent, so the email alone
shows what moved and by how much."""


def _num(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def change_lines(new_cpm_by_kw, old_cpm_by_kw, paused_keywords=(), removed_products=()):
    """new_cpm_by_kw: keyword -> bid sent. old_cpm_by_kw: keyword -> bid on Blinkit
    before the push (Blinkit_CPM). paused_keywords: left out of the list this push."""
    lines = []
    for kw, new in sorted(new_cpm_by_kw.items()):
        old, new = _num(old_cpm_by_kw.get(kw)), _num(new)
        if old:
            pct = (new - old) / old * 100
            arrow = "raised" if new > old else ("cut" if new < old else "unchanged")
            lines.append(f"  {kw}: {old} -> {new} ({pct:+.0f}%, {arrow})")
        else:
            lines.append(f"  {kw}: ? -> {new} (old bid not found)")
    for kw in sorted(paused_keywords):
        old = _num(old_cpm_by_kw.get(kw))
        lines.append(f"  {kw}: PAUSED" + (f" (was {old})" if old else ""))
    for pid in removed_products:
        lines.append(f"  product {pid}: removed from campaign")
    return lines
