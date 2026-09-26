"""
CompetitorMonitor.exe - the double-click launcher.

This file is compiled into CompetitorMonitor.exe by build_exe.bat. It is a
thin launcher, deliberately: it finds your installed Python and runs
monitor.py with it. The monitor's own code is NOT baked into the exe, so you
can edit competitors.txt, selectors.json or any .py file and just
double-click again - no rebuild needed.

Three ways it gets started:

  Double-click            shows a small menu, waits for Enter at the end
                          so the window does not vanish before you read it
  CompetitorMonitor.exe --dry-run --limit 3
                          passes the options straight to monitor.py
  CompetitorMonitor.exe --scheduled
                          what Task Scheduler uses: no menu and, crucially,
                          no "press Enter" - a scheduled run that waits for a
                          keypress would sit there forever
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TITLE = "Competitor Monitor"


def project_folder() -> Path:
    """The folder the exe (or this script) lives in."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def candidate_pythons():
    """
    Places Python might be, most likely first.

    Same order as run.ps1 and start_testshop.ps1, so all three always pick
    the same interpreter: the project's virtual environment if it exists,
    then the regular Python installs.
    """
    venv = Path(os.environ.get("USERPROFILE", "")) / ".venvs" / "cardcopy" / "Scripts" / "python.exe"
    yield [str(venv)]
    local = os.environ.get("LOCALAPPDATA", "")
    for version in ("313", "312", "311", "310"):
        yield [str(Path(local) / "Programs" / "Python" / f"Python{version}" / "python.exe")]
    if shutil.which("py"):
        yield ["py", "-3"]
    found = shutil.which("python")
    if found:
        yield [found]


def find_python() -> list[str] | None:
    """
    The first Python that can actually run the monitor.

    Checking for the installed packages matters on this machine: the plain
    `python` command points at a Poetry environment that has none of them,
    and picking it would fail on the very first import.
    """
    for command in candidate_pythons():
        executable = command[0]
        if executable not in ("py",) and not Path(executable).exists():
            continue
        try:
            probe = subprocess.run(
                command + ["-c", "import scrapling, gspread, dotenv"],
                capture_output=True, timeout=90,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return command
    return None


def menu() -> list[str] | None:
    """Ask what to do. Returns the script + options to run, or None to quit."""
    print()
    print("  " + "=" * 52)
    print(f"   {TITLE}")
    print("  " + "=" * 52)
    print()
    print("   1  Run now        check prices, update the Google Sheet,")
    print("                     email you if anything changed")
    print("   2  Test run       check prices only - changes nothing")
    print("   3  Quick test     like 2, but only the first 3 URLs")
    print("   4  Self-test      check the code itself (offline, ~1 second)")
    print("   5  Test shop      run against the local pretend shop and write")
    print("                     to the TEST tabs only (start it first with")
    print("                     start_testshop.ps1)")
    print()
    print("   Q  Quit")
    print()
    while True:
        try:
            choice = input("   Choose 1-5 or Q: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if choice == "1":
            return ["monitor.py"]
        if choice == "2":
            return ["monitor.py", "--dry-run"]
        if choice == "3":
            return ["monitor.py", "--dry-run", "--limit", "3"]
        if choice == "4":
            return ["self_test.py"]
        if choice == "5":
            return ["monitor.py", "--test"]
        if choice in ("q", "quit", "exit"):
            return None
        print("   Please type 1, 2, 3, 4, 5 or Q.")


def pause(scheduled: bool) -> None:
    if scheduled:
        return
    try:
        input("\n  Press Enter to close this window...")
    except (EOFError, KeyboardInterrupt):
        pass


def main() -> int:
    args = sys.argv[1:]
    scheduled = "--scheduled" in args
    args = [a for a in args if a != "--scheduled"]

    folder = project_folder()
    if not (folder / "monitor.py").exists():
        print(f"\n  monitor.py was not found next to this program:\n    {folder}")
        print("  Keep CompetitorMonitor.exe in the same folder as the project files.")
        pause(scheduled)
        return 3

    python = find_python()
    if python is None:
        print("\n  Could not find a Python installation with Scrapling installed.")
        print("  Install Python 3.10+ from python.org, then in a terminal run:")
        print('    pip install "scrapling[fetchers]" gspread google-auth python-dotenv')
        print("    scrapling install")
        pause(scheduled)
        return 3

    if args:
        # Options given on the command line go straight through.
        script_and_args = ["monitor.py"] + args
    elif scheduled:
        script_and_args = ["monitor.py"]
    else:
        script_and_args = menu()
        if script_and_args is None:
            return 0

    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    print()
    try:
        completed = subprocess.run(python + script_and_args, cwd=folder, env=environment)
        code = completed.returncode
    except KeyboardInterrupt:
        print("\n  Stopped.")
        code = 130

    if not scheduled:
        print()
        print("  Finished successfully." if code == 0 else
              f"  Finished with problems (code {code}) - see the messages above,"
              f"\n  or the log in the 'logs' folder.")
    pause(scheduled)
    return code


if __name__ == "__main__":
    sys.exit(main())
