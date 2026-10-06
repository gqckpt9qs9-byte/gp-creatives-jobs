#!/usr/bin/env python3
"""Regenerate history/states.csv from every archived snapshot.

The hourly job appends one row per run. Run this to backfill, to recover a row
the job skipped, or after changing the rules in unit_states(), so the whole
history is reclassified consistently. Recomputes state from each snapshot's
data rather than trusting any stored block.

  ./rebuild-states.py            # rebuild and upload
  ./rebuild-states.py --dry-run  # print a summary only
"""
import gzip, importlib.util, json, os, subprocess, sys, tempfile

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("job", os.path.join(here, "update-binance-feed.py"))
job = importlib.util.module_from_spec(spec); spec.loader.exec_module(job)

ls = subprocess.run([job.AWS, "s3", "ls", job.HISTORY_PREFIX + "/"], capture_output=True, text=True, check=True)
names = sorted(l.split()[-1] for l in ls.stdout.splitlines() if l.split()[-1].startswith("prices-"))
tmp = tempfile.mkdtemp()
subprocess.run([job.AWS, "s3", "cp", job.HISTORY_PREFIX + "/", tmp, "--recursive", "--exclude", "*",
                "--include", "prices-*.json", "--only-show-errors"], check=True)
rows, bad = [], []
for n in names:
    b = open(os.path.join(tmp, n), "rb").read()
    try:
        doc = json.loads(gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b)
        doc.pop("state", None)
        rows.append(job.state_row(doc, n[len("prices-"):-len(".json")]))
    except Exception as e:
        bad.append(f"{n}: {e}")
body = job.states_csv(rows)
print(f"{len(rows)} rows from {len(names)} snapshots" + (f", {len(bad)} unreadable: {bad[:3]}" if bad else ""))
if "--dry-run" not in sys.argv:
    job._put_text(body, job.STATES_KEY, "text/csv; charset=utf-8", "max-age=300")
    print("uploaded", job.STATES_KEY)
