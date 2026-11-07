"""One-command local setup for the listings explorer. Safe to re-run.

    python scripts/dev_setup.py                    # venv, backend deps, frontend deps, demo users
    python scripts/dev_setup.py --dump-listings    # also save the BigQuery listings table to ./listings/

What it does, in order:
  1. creates ./.venv (uv with Python 3.12 if uv is installed, otherwise `python -m venv`)
  2. installs backend/requirements.txt into it
  3. runs `npm install` in frontend/
  4. creates ./trident.db and the local demo logins (scripts/seed_users.py)
  5. with --dump-listings: writes ./listings/listing_observation_raw.ndjson from BigQuery
     (Application Default Credentials; ~75 s). Then run the backend with
     LISTINGS_NDJSON_PATH=./listings so reloads are instant instead of re-querying BigQuery.

Everything it creates is git-ignored. A fresh git worktree starts without any of it, so run this
once per worktree (after `git submodule update --init` if this repo is a submodule checkout).
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def run(cmd, cwd=ROOT, env=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True, env=env)


def make_venv() -> None:
    if VENV_PY.exists():
        print(f"venv exists: {VENV}")
        return
    if shutil.which("uv"):
        run(["uv", "venv", "--python", "3.12", VENV])
    else:
        run([sys.executable, "-m", "venv", VENV])


def install_backend() -> None:
    req = ROOT / "backend" / "requirements.txt"
    if shutil.which("uv"):
        run(["uv", "pip", "install", "--python", VENV_PY, "-r", req])
    else:
        run([VENV_PY, "-m", "pip", "install", "-r", req])


def install_frontend() -> None:
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm not found; install Node.js 20+ and re-run.")
    run([npm, "install", "--no-audit", "--no-fund"], cwd=ROOT / "frontend")


def seed_users() -> None:
    run([VENV_PY, "scripts/seed_users.py"])


def dump_listings() -> None:
    out_dir = ROOT / "listings"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "listing_observation_raw.ndjson"
    code = (
        "import json, sys\n"
        "sys.path.insert(0, 'backend')\n"
        "from app import listings\n"
        "rows = listings._load_bigquery()\n"
        f"with open({str(out)!r}, 'w', encoding='utf-8') as f:\n"
        "    for r in rows:\n"
        "        f.write(json.dumps(r) + '\\n')\n"
        "print(len(rows), 'rows written')\n"
    )
    run([VENV_PY, "-c", code])
    print(f"Run the backend with LISTINGS_NDJSON_PATH={out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump-listings", action="store_true",
                    help="save the BigQuery listings table to ./listings/ for fast local reloads")
    args = ap.parse_args()

    make_venv()
    install_backend()
    install_frontend()
    seed_users()
    if args.dump_listings:
        dump_listings()
    print("\nSetup done. Start the app with the commands in README.md (Quickstart).")


if __name__ == "__main__":
    main()
