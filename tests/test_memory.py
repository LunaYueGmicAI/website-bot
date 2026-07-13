"""Memory-management sanity checks — no API keys required. Run: python tests/test_memory.py"""
import os
os.environ["MAX_TURNS_IN_MEMORY"] = "4"
os.environ["MAX_SESSIONS"] = "3"
os.environ["SESSION_TTL_SECONDS"] = "1800"
os.environ["LLM_HISTORY_TURNS"] = "3"

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from bot import sessions  # noqa: E402


def main():
    S = sessions.SessionStore()
    ok = True

    S.get_or_create("u1", {"page_url": "/"})
    for i in range(6):
        S.append_turn("u1", "user", f"m{i}")
    turns = [t["text"] for t in S.snapshot("u1")["turns"]]
    ok &= _check("turn cap keeps newest 4", turns == ["m2", "m3", "m4", "m5"], turns)

    win = [t["text"] for t in S.window("u1")]
    ok &= _check("sliding window = last 3", win == ["m3", "m4", "m5"], win)

    S.set_intent("u1", "odm")
    S.update_lead("u1", {"need": "recorder", "email": "a@x.com"})
    lead = S.snapshot("u1")["lead"]
    ok &= _check("lead backfill + missing", lead["missing"] == ["name"] and lead["email"] == "a@x.com", lead)

    S.set_intent("u1", "products")
    ok &= _check("first intent not clobbered", S.snapshot("u1")["intent"] == "odm", S.snapshot("u1")["intent"])

    S.get_or_create("u2"); S.get_or_create("u3"); S.get_or_create("u4")
    ok &= _check("LRU evicts u1", S.snapshot("u1") is None and S.snapshot("u4") is not None, S.stats())

    # backdate u2 past the TTL (>1800s idle); fresh u3/u4 should survive the sweep
    S._data["u2"]["last_seen"] -= 4000
    n = S.sweep_expired()
    ok &= _check("TTL sweep drops idle only", S.snapshot("u2") is None and S.snapshot("u4") is not None and n == 1, f"evicted={n}")

    print("\nALL PASS" if ok else "\nSOME FAILED")
    sys.exit(0 if ok else 1)


def _check(name, cond, detail):
    print(f"[{'OK' if cond else 'FAIL'}] {name}: {detail}")
    return cond


if __name__ == "__main__":
    main()
