"""The API image ships without polars/numpy/duckdb/dagster (smaller, less attack surface).
This guards against an import that would make the container crash at start."""

import subprocess
import sys


def test_api_imports_none_of_the_pipeline_dependencies():
    code = (
        "import sys, goldstandard.api.app; "
        "bad = [m for m in ('polars', 'numpy', 'duckdb', 'dagster', 'pyarrow') if m in sys.modules]; "
        "assert not bad, bad"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
