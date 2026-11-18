.PHONY: check digest

check:
	python3 -m unittest discover -s tests -t . -p 'test_*.py' -q

digest:
	python3 scripts/weekly_digest.py
