"""Conservative quality gates shared by collection and analysis."""
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))


def intraday_quality(rows, trade_date):
    accepted = {}
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row['time'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                continue
            stamp = stamp.astimezone(CN)
        except (KeyError, ValueError, TypeError):
            continue
        minute = stamp.hour * 60 + stamp.minute
        # GitHub Actions may start the 15:00 task a few minutes late. The
        # market pools are already frozen after 15:00, so 15:00-15:20 is
        # normalized to the close slot instead of being discarded.
        if stamp.date().isoformat() != trade_date or not (570 <= minute <= 690 or 780 <= minute <= 920):
            continue
        if any(row.get(k) is None for k in ('limit_up_count', 'broken_rate_pct', 'promotion_rate_pct', 'limit_down_count')):
            continue
        accepted[min(minute, 900)] = row
    minutes = sorted(accepted)
    # Six samples alone cannot establish a full-day path. Treat 14:45+ as
    # valid tail coverage because scheduled close collection can arrive one
    # minute before 14:50 while already carrying the 15:00 slot.
    ready = (len(minutes) >= 6 and minutes[0] <= 600 and minutes[-1] >= 885
             and any(660 <= m <= 690 for m in minutes)
             and any(810 <= m <= 840 for m in minutes))
    return [accepted[m] for m in minutes], {
        'snapshot_count': len(rows), 'valid_snapshot_count': len(minutes),
        'excluded_snapshot_count': len(rows) - len(minutes),
        'timeline_ready': ready,
        'note': '仅接受同日北京时间交易时段的有效分钟快照；需覆盖09:35、午前、13:45附近及14:45后的尾盘。',
    }
