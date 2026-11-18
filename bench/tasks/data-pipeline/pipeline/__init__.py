"""orderload — CSV -> SQLite order loader.

Stages: extract (read the export), transform (clean + dedupe),
load (write the orders table). Driven by run.py.
"""
