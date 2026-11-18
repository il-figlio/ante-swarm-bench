"""Transform stage: normalise raw records before load."""


def _to_amount(value):
    """Best-effort float coercion; returns None when it doesn't parse."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _text(value):
    return "" if value is None else str(value).strip()


def clean(records):
    """Normalise each raw record into a load-ready row.

    Trims whitespace on text fields and coerces ``amount`` to a float
    (None when unparseable). Rows are never dropped here.
    """
    rows = []
    for rec in records:
        rows.append({
            "order_id": _text(rec.get("order_id")),
            "date": _text(rec.get("date")),
            "customer": _text(rec.get("customer")),
            "category": _text(rec.get("category")),
            "amount": _to_amount(rec.get("amount")),
            "note": _text(rec.get("note")),
        })
    return rows


def dedupe(records):
    """Dedupe records on their natural key.

    The upstream export is supposed to be unique on order_id, so this
    currently passes everything through.
    """
    return records
