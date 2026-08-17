from __future__ import annotations

import re

# Google document IDs are opaque URL path components. The adapter validates
# shape only and never persists a real identifier in its repository.
DOCUMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
