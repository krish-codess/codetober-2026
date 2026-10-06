"""The Compression Bake-Off."""

import os
from importlib import resources

# Arrow's ORC writer needs an IANA tz database; Windows has none, so point it at the tzdata wheel.
if os.name == "nt":
    os.environ.setdefault("TZDIR", str(resources.files("tzdata") / "zoneinfo"))
