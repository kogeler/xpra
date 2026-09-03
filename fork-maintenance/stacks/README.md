# Patch stacks

`develop.toml` is the complete downstream queue in application order. It
contains every active production case plus the single test-quarantine duty case
and declares their combined focused, quarantine, native, full, and physical-GPU
gates. `develop` is the stable queue slug, not a Git-branch requirement for
branch-agnostic consumers.

Add another stack only when a durable integration scenario requires a strict
subset or a different dependency-safe composition. A stack never replaces an
atomic case and never becomes a committed applied-source diff on `develop`.
