"""
Launcher script — starts both FastAPI backend and Gradio frontend.

Usage:
    python run.py

This starts:
    - FastAPI on http://127.0.0.1:8000 (API + docs at /docs)
    - Gradio on http://127.0.0.1:7860 (UI)

Why two processes instead of mounting Gradio inside FastAPI?
- Clean separation of concerns (API is usable without the UI)
- Each can be restarted independently during development
- The API is a standalone product — other tools can call it
- Gradio's event loop doesn't interfere with FastAPI's
"""

import subprocess
import sys
import time
import os


def main():
    project_root = os.path.dirname(os.path.abspath(__file__))
    python = os.path.join(project_root, "venv", "Scripts", "python.exe")
    
    # Fallback to system python if venv doesn't exist
    if not os.path.exists(python):
        python = sys.executable

    print("=" * 50)
    print("  Invoize")
    print("=" * 50)
    print()

    # Start FastAPI backend
    print("[1/2] Starting FastAPI backend on http://127.0.0.1:8000")
    print("      API docs: http://127.0.0.1:8000/docs")
    api_process = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=project_root,
    )

    # Give the API a moment to start
    time.sleep(2)

    # Start Gradio frontend
    print("[2/2] Starting Gradio UI on http://127.0.0.1:7860")
    print()
    print("Open http://127.0.0.1:7860 in your browser to use the app.")
    print("Press Ctrl+C to stop both servers.")
    print()

    ui_process = subprocess.Popen(
        [python, os.path.join("frontend", "app.py")],
        cwd=project_root,
    )

    try:
        # Wait for either process to exit
        api_process.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        api_process.terminate()
        ui_process.terminate()
        api_process.wait()
        ui_process.wait()
        print("Both servers stopped.")


if __name__ == "__main__":
    main()
