"""Extract stage: read the order export CSV into record dicts.

The export's header row is always:

    order_id,date,customer,category,amount,note
"""

COLUMNS = ["order_id", "date", "customer", "category", "amount", "note"]


def read_records(path):
    """Read the CSV at ``path`` into a list of ``{column: value}`` dicts.

    Every non-blank line after the header becomes one record, keyed by
    the header columns.
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        lines = f.read().splitlines()
    if not lines:
        return []
    header = [h.strip() for h in lines[0].split(",")]
    records = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split(",")
        records.append(dict(zip(header, fields)))
    return records
