"""The pipeline package must not know Telegram exists: any front end can drive it."""

import pkgutil
import subprocess
import sys

import music_downloader.pipeline as pipeline_pkg

PIPELINE_MODULES = ["music_downloader.pipeline"] + [
    f"music_downloader.pipeline.{m.name}" for m in pkgutil.iter_modules(pipeline_pkg.__path__)
]


def test_every_pipeline_module_is_listed():
    # Positive control for the subprocess check: a new module is picked up automatically.
    assert {"music_downloader.pipeline.fetch", "music_downloader.pipeline.search"} <= set(PIPELINE_MODULES)


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
