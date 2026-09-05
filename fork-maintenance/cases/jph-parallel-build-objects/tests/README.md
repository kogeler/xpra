The case patch adds `unit.codecs.jph_build_test` beside the upstream codec tests.
It runs the real setup/Cython extension planner and compiler object-name mapping
in a disposable source copy, checks independent encoder/decoder selection and
shared dependencies, and exercises actual clean/source-distribution retention.
Its clean-source failure is the multiply owned `jph.o` path. This test does not
require OpenJPH to be installed in every upstream-test image and does not claim
native compilation or linkage.

The manifest also selects the existing picture-codec lossless roundtrip module.
Its optional JPH discovery is surrounding-code coverage, not mandatory JPH
acceptance. Final Ubuntu 26.04 and Debian 13 DEB validation must load the actual
packaged JPH encoder and decoder and prove deterministic quality-100 RGB pixels,
as specified in the main case README. No separate ad hoc probe or live profile
replaces that package boundary.
