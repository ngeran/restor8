"""restor8-core — shared library installed by every restor8 service.

Only the connector service actually opens device sessions; this package
exists so that connector, backup, restore, scenario and gateway share one
definition of progress events, device models and typed errors instead of
growing seven private copies that drift apart.
"""
