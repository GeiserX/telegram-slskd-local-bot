"""The pipeline and the MCP front end must not know Telegram exists: any front end can drive the pipeline."""

import pkgutil
import subprocess
import sys

import music_downloader.mcp as mcp_pkg
import music_downloader.pipeline as pipeline_pkg

PIPELINE_MODULES = [
    f"{pkg.__name__}{suffix}"
    for pkg in (pipeline_pkg, mcp_pkg)
    for suffix in ["", *(f".{m.name}" for m in pkgutil.iter_modules(pkg.__path__))]
]


def test_every_pipeline_module_is_listed():
    # Positive control for the subprocess check: a new module is picked up automatically.
    assert {
        "music_downloader.pipeline.fetch",
        "music_downloader.pipeline.search",
        "music_downloader.mcp.tools",
        "music_downloader.mcp.server",
    } <= set(PIPELINE_MODULES)


def test_importing_the_pipeline_never_imports_telegram():
    """Run in a fresh interpreter: this test process has telegram loaded through the bot tests."""
    script = (
        "import importlib, sys\n"
        f"for name in {PIPELINE_MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "loaded = sorted(m for m in sys.modules if m == 'telegram' or m.startswith('telegram.'))\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"telegram modules loaded by the pipeline: {proc.stdout}{proc.stderr}"
