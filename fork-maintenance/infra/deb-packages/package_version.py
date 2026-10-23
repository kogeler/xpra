# Copyright (C) 2026 kogeler
"""One explicit downstream revision; preserve the real upstream source revision."""

import re


# Advance for a new release of changed packages on the same upstream base.
# Unpublished development rebuilds can share this next revision.
DEBIAN_REVISION = 2


def package_version(base_version: str, upstream_revision: int) -> str:
    if (not isinstance(base_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", base_version)
            or type(upstream_revision) is not int or upstream_revision <= 0
            or type(DEBIAN_REVISION) is not int or DEBIAN_REVISION <= 0):
        raise ValueError("invalid fork Debian package version inputs")
    return f"{base_version}-r{upstream_revision}-{DEBIAN_REVISION}"
