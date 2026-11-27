"""Demo 'build' for the self-healing CI job: fails once with a network blip, then passes.

Usage: python scripts/flaky_build.py <marker-file>
"""
import pathlib
import sys

marker = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".flaky_build_ran")
if not marker.exists():
    marker.write_text("first attempt done", encoding="utf-8")
    print("[INFO] Downloading org/example/lib/1.4.2/lib-1.4.2.jar")
    print("[ERROR] Could not transfer artifact: Connection reset by peer")
    sys.exit(1)
print("[INFO] BUILD SUCCESS")
