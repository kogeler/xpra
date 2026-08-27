# Patch stacks

`develop.toml` is the complete downstream queue in application order. It
contains only the two active cases and declares their combined focused, native,
full, and physical-GPU gates.

Add another stack only when a durable integration scenario requires a strict
subset or a different dependency-safe composition. A stack never replaces an
atomic case and never becomes a committed applied-source diff on `develop`.
