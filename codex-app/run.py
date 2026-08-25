"""Start the standalone Codex Config Studio application from this directory."""

from pathlib import Path
import runpy


runpy.run_path(str(Path(__file__).with_name("codex_config_gui.py")), run_name="__main__")
