"""Keep unittest off the real cache and the app timing.log.

Each test module also calls isolate_test_dirs(), because loading this package
is not guaranteed when a single test file is run by itself.
"""

from __future__ import annotations

from tests.harness import isolate_test_dirs

isolate_test_dirs()
