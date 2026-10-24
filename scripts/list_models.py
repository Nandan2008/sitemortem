"""List the models your Nebius Token Factory key can call (GET /v1/models).

    python scripts/list_models.py          # NVIDIA / Nemotron models only
    python scripts/list_models.py --all    # everything on the key
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.llm import Nemotron, ModelError, base_url  # noqa: E402

show_all = "--all" in sys.argv
if not os.getenv("NEBIUS_API_KEY"):
    sys.exit("NEBIUS_API_KEY is empty. Open .env, paste the key right after NEBIUS_API_KEY=, save, and retry.")
try:
    ids = Nemotron().list_ids()
except ModelError as e:
    sys.exit(f"Could not list models: {e}")

chosen = ids if show_all else [i for i in ids if "nvidia" in i.lower() or "nemotron" in i.lower()]
print(f"Endpoint: {base_url()}")
print("Models:\n  " + "\n  ".join(chosen or ["(none matched; try --all)"]))
print(f"\n{len(ids)} models total on this key.")
print("Pin the ones you want in .env as NEMOTRON_NANO_MODEL / NEMOTRON_SUPER_MODEL / NEMOTRON_ULTRA_MODEL.")
print("Note: being listed does not guarantee a model supports every feature; run scripts/live_check.py to test.")
