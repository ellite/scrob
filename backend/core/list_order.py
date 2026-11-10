"""Order of a user's own lists once they have been arranged by hand (#165).

Until the user reorders, lists keep the existing newest-activity-first order.
After that, arranged lists come first in their saved order and lists created
later follow at the bottom, oldest first, so a new list lands at the end.
"""


def arranged(lists):
    """`lists` are rows with sort_order, created_at and id, already in the
    default order; returns them in the user's arranged order when there is one."""
    if all(row.sort_order is None for row in lists):
        return list(lists)
    return sorted(lists, key=lambda row: (row.sort_order is None, row.sort_order or 0, row.created_at, row.id))
