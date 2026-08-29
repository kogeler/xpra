# Focused cleanup regression

`fix.patch` extends the upstream cleanup tests with production-path worker and
FIFO scenarios in `unit.server.window.video_compress_test`. `case.toml`
declares the focused and full coverage; hardware lifetime acceptance uses
`stacks/develop.toml`.
