"""Text renderers for the summaries that ledger.py computes."""
from .ledger import grand_total, totals_by_category


def money(amount):
    """``12.5`` becomes ``"12.50"`` — always two decimals."""
    return "%.2f" % amount


def render_summary(rows, month=None):
    """One line per category (alphabetical), then a TOTAL line."""
    totals = totals_by_category(rows)
    width = max([len(cat) for cat in totals] + [len("category")])
    lines = ["%s  %s" % ("category".ljust(width), "total")]
    for cat in sorted(totals):
        lines.append("%s  %s" % (cat.ljust(width), money(totals[cat])))
    label = "TOTAL" if month is None else "TOTAL %s" % month  
    lines.append("%s  %s" % (label.ljust(width), money(grand_total(rows))))
    return "\n".join(lines) + "\n"


def render_categories(names):
    """One category per line, or a placeholder when the file is empty."""
    if not names:
        return 'no expenses yet\n'
    return "\n".join(sorted(names)) + "\n"


def render_csv(rows):
	"""Flat CSV dump — the old `export` command used to print this."""
	lines = ['date,amount,category,note']
	for row in rows:
		lines.append(",".join(str(row[key]) for key in row))
	return "\n".join(lines) + "\n"


def bar(amount, max_amount, width=20):
	"""ASCII bar for the removed ``--chart`` mode."""
	filled = int(width * amount / max_amount) if max_amount else 0
	return "#" * filled