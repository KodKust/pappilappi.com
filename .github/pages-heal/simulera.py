#!/usr/bin/env python3
"""Simulator för pages-heal.yml: kör workflowens EGET run-block (extraherat ur
YAML-filen) mot ett fejkat GitHub med virtuell klocka.

Shimmar på PATH: gh (svarar ur ett tillstånd, kör jq-filtren på riktigt),
sleep (flyttar den virtuella klockan), date (läser den) och timeout (kör
kommandot rakt av — macOS saknar GNU timeout). Inget nät.

VAD SIMULATORN INTE VISAR (oberoende granskning 2026-11-13, L4):
- Väckningen via workflow_run och köersättningen i concurrency-gruppen.
  De är mätta live i stället: varje avslutad publicering gav en läkkörning
  (även läkarens egen begärda, 16:37:22Z), och en avbruten hoppades över.
- GitHubs felaktiga tidsstämplar (1/11) i full skala. Shimmen lämnar
  körningarna i tidsordning, som API:t; läkaren ordnar om dem efter id.
  Scenario R visar ett läge där de två ordningarna skiljer sig.
- Att en lyckad körning kan bära förälderns sha: scenario P visar vad
  läkaren gör då (en onödig men ofarlig begäran).

Kör (från sajtrepots rot):
    python3 .github/pages-heal/simulera.py .github/workflows/pages-heal.yml
Kräver python3, bash och jq. PyYAML används om det finns.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

T0 = 1_800_000_000  # virtuell starttid (epoch)

GH_SHIM = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
D = os.environ["SIM_DIR"]
def lad(): return json.load(open(os.path.join(D, "state.json")))
def spara(s): json.dump(s, open(os.path.join(D, "state.json"), "w"))
def nu(): return int(open(os.path.join(D, "clock")).read())
def iso(t): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

def materialisera(s, t):
    """Planerade pushar vars tid passerats blir körningar + flyttar HEAD."""
    kvar = []
    for p in s["pushar"]:
        if p["vid"] <= t:
            ny_korning(s, p["vid"], p["sha"], p["dur"], p["utfall"])
            s["head"] = p["sha"]
        else:
            kvar.append(p)
    s["pushar"] = kvar

def ny_korning(s, t, sha, dur, utfall, aktor="KodKust"):
    # Pages avbryter en äldre pågående publicering när en ny startar.
    for r in s["runs"]:
        if r["created"] + r["dur"] > t and r["utfall"] != "cancelled":
            r["dur"] = max(1, t - r["created"]); r["utfall"] = "cancelled"
    s["nasta_id"] = s.get("nasta_id", 1000) + 1
    s["runs"].append({"id": s["nasta_id"], "sha": sha, "created": t, "dur": dur,
                      "utfall": utfall, "aktor": aktor})

def som_api(r, t):
    klar = t >= r["created"] + r["dur"]
    return {"id": r["id"], "path": "dynamic/pages/pages-build-deployment", "head_sha": r["sha"],
            "triggering_actor": {"login": r.get("aktor", "KodKust")},
            "status": "completed" if klar else "in_progress",
            "conclusion": r["utfall"] if klar else None,
            "created_at": iso(r["created"]), "run_started_at": iso(r["created"]),
            "updated_at": iso(min(t, r["created"] + r["dur"]))}

a = sys.argv[1:]
assert a and a[0] == "api", a
a = a[1:]
metod, falt, jqf, path = "GET", {}, None, None
i = 0
while i < len(a):
    if a[i] == "-X": metod = a[i + 1]; i += 2
    elif a[i] == "-f": k, v = a[i + 1].split("=", 1); falt[k] = v; i += 2
    elif a[i] == "--jq": jqf = a[i + 1]; i += 2
    else: path = a[i]; i += 1
s = lad(); t = nu(); materialisera(s, t)
s["anrop"].append(f"{metod} {path} {falt or ''}".strip())

def fel(nyckel):
    if s["fel"].get(nyckel, 0) > 0:
        s["fel"][nyckel] -= 1; spara(s)
        sys.stderr.write(f"gh: HTTP 502 (simulerat fel: {nyckel})\n"); sys.exit(1)

if metod == "POST" and path.endswith("/pages/builds"):
    fel("post")
    utfall, dur = (s["post_ko"].pop(0) if s["post_ko"] else ["success", 75])
    if s.get("post_skapar", True):
        ny_korning(s, t + s.get("post_latens", 5), s["head"], dur, utfall,
                   aktor="github-pages[bot]")
    s["poster"].append(t)
    ut = {"status": "queued"}
elif "/commits" in path:
    fel("commits")
    ut = [{"sha": s["head"]}]
elif "/actions/runs" in path:
    fel("runs")
    runs = sorted((r for r in s["runs"] if r["created"] <= t),
                  key=lambda r: r["created"], reverse=True)
    if "created" in falt:
        grans = falt["created"].lstrip(">=")
        runs = [r for r in runs if iso(r["created"]) >= grans]
    ut = {"total_count": len(runs), "workflow_runs": [som_api(r, t) for r in runs[:30]]}
else:
    sys.stderr.write(f"shim: okänt anrop {metod} {path}\n"); sys.exit(2)
spara(s)
if jqf:
    p = subprocess.run(["jq", "-r", jqf], input=json.dumps(ut), capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write("jq-fel: " + p.stderr); sys.exit(3)
    sys.stdout.write(p.stdout)
else:
    print(json.dumps(ut))
'''

TIMEOUT_SHIM = '#!/bin/bash\nshift\nexec "$@"\n'

SLEEP_SHIM = r'''#!/usr/bin/env python3
import os, sys
f = os.path.join(os.environ["SIM_DIR"], "clock")
t = int(open(f).read()) + int(float(sys.argv[1]))
open(f, "w").write(str(t))
'''

DATE_SHIM = r'''#!/usr/bin/env python3
import os, sys, time
t = int(open(os.path.join(os.environ["SIM_DIR"], "clock")).read())
a = sys.argv[1:]
if "-d" in a:
    spec = a[a.index("-d") + 1]          # t.ex. '-30 seconds'
    t += int(spec.split()[0])
fmt = next((x[1:] for x in a if x.startswith("+")), "%s")
print(str(t) if fmt == "%s" else time.strftime(fmt, time.gmtime(t)))
'''


def run_block(yml: Path) -> str:
    """Workflowens run-block, extraherat ur filen (en enda källa)."""
    try:
        import yaml  # type: ignore
        d = yaml.safe_load(yml.read_text(encoding="utf-8"))
        return d["jobs"]["lak"]["steps"][0]["run"]
    except ImportError:
        rader = yml.read_text(encoding="utf-8").split("\n")
        i = next(n for n, r in enumerate(rader) if r.strip() == "run: |")
        indrag = len(rader[i]) - len(rader[i].lstrip()) + 2
        ut = []
        for r in rader[i + 1:]:
            if r.strip() and len(r) - len(r.lstrip()) < indrag:
                break
            ut.append(r[indrag:])
        return "\n".join(ut) + "\n"


def korning(sha, min_sedan, utfall="success", dur=75, id=None, aktor="KodKust"):
    """id=None → simulera() numrerar i verklig skapelseordning (som GitHub).
    Ett uttryckligt id används bara där ordningen ska AVVIKA från tiderna."""
    return {"id": id, "sha": sha, "created": T0 - int(min_sedan * 60), "dur": dur,
            "utfall": utfall, "aktor": aktor}


def simulera(skript: str, *, runs, head, post_ko=(), pushar=(), fel=None,
             tvinga=False, post_latens=5, post_skapar=True):
    d = Path(tempfile.mkdtemp(prefix="pagesheal_"))
    for namn, kod in (("gh", GH_SHIM), ("sleep", SLEEP_SHIM), ("date", DATE_SHIM),
                      ("timeout", TIMEOUT_SHIM)):
        p = d / namn
        p.write_text(kod)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    (d / "clock").write_text(str(T0))
    # GitHubs körnings-id stiger i verklig skapelseordning: numrera därefter.
    runs = [dict(r) for r in runs]
    for n, r in enumerate(sorted((r for r in runs if r["id"] is None),
                                 key=lambda r: r["created"]), start=1):
        r["id"] = n
    (d / "state.json").write_text(json.dumps({
        "runs": list(runs), "head": head, "post_ko": [list(x) for x in post_ko],
        "pushar": [{"vid": T0 + int(m * 60), "sha": s, "utfall": u, "dur": du}
                   for m, s, u, du in pushar],
        "fel": dict(fel or {}), "poster": [], "anrop": [], "post_latens": post_latens,
        "post_skapar": post_skapar}))
    (d / "skript.sh").write_text(skript)
    env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "SIM_DIR": str(d),
           "REPO": "KodKust/pappilappi.com", "TVINGA": "true" if tvinga else "false",
           "GH_TOKEN": "x", "GITHUB_STEP_SUMMARY": str(d / "summary.md")}
    p = subprocess.run(["bash", str(d / "skript.sh")], env=env,
                       capture_output=True, text=True, timeout=120)
    s = json.loads((d / "state.json").read_text())
    return {"exit": p.returncode, "poster": [(t - T0) for t in s["poster"]],
            "minuter": (int((d / "clock").read_text()) - T0) / 60,
            "ut": p.stdout.strip(), "err": p.stderr.strip(), "anrop": len(s["anrop"]),
            "summary": (d / "summary.md").read_text() if (d / "summary.md").exists() else ""}


def main() -> int:
    skript = run_block(Path(sys.argv[1]))
    ok = True

    def test(namn, r, villkor, visa=False):
        nonlocal ok
        bra = bool(villkor)
        ok &= bra
        print(f"  {'✓' if bra else '✗'} {namn}  → exit {r['exit']}, "
              f"{len(r['poster'])} begärda (vid {[round(x / 60, 1) for x in r['poster']]} min), "
              f"{r['minuter']:.0f} min, {r['anrop']} API-anrop")
        if not bra or visa:
            print(textwrap.indent(r["ut"][-900:], "      | "))
            if r["err"]:
                print(textwrap.indent(r["err"][-500:], "      ! "))

    bas = [korning("new", 12, "failure", 669), korning("old", 90)]

    r = simulera(skript, runs=bas, head="new")
    test("A. en publicering föll, den begärda lyckas", r,
         r["exit"] == 0 and len(r["poster"]) == 1 and "Läkt" in r["ut"] and r["minuter"] < 5, visa=True)

    r = simulera(skript, runs=[korning("new", 3), korning("new", 15, "failure", 669), korning("old", 90)], head="new")
    test("B. en vanlig push har redan läkt → rör ingenting", r,
         r["exit"] == 0 and not r["poster"] and "Inget att göra" in r["ut"])

    r = simulera(skript, runs=[korning("ny2", 0.5, "success", 90)] + bas, head="ny2")
    test("C. en vanlig publicering pågår → väntar in den, rör ingenting", r,
         r["exit"] == 0 and not r["poster"] and "Inget att göra" in r["ut"])

    r = simulera(skript, runs=bas, head="new", post_ko=[("failure", 669), ("success", 75)])
    test("D. första begärda faller, andra lyckas efter 10 min paus", r,
         r["exit"] == 0 and len(r["poster"]) == 2 and "efter 2" in r["ut"]
         and r["poster"][1] - r["poster"][0] >= 600 + 669, visa=True)

    r = simulera(skript, runs=bas, head="new", post_ko=[("failure", 669)] * 3)
    test("E. alla tre faller → ger upp rött efter tre försök, aldrig ett fjärde", r,
         r["exit"] == 1 and len(r["poster"]) == 3 and "Gav upp efter 3" in r["ut"]
         and r["minuter"] < 150, visa=True)

    ihall = [korning(f"f{i}", 10 + i * 20, "failure", 669) for i in range(4)] + [korning("old", 200)]
    r = simulera(skript, runs=ihall, head="f0")
    test("F. fyra fall i rad vid start → begär inga fler, gult inte rött", r,
         r["exit"] == 0 and not r["poster"] and "begär inga fler" in r["ut"]
         and "::warning::" in r["ut"])

    r = simulera(skript, runs=bas, head="new", post_ko=[("failure", 669)],
                 pushar=[(15, "ny3", "success", 75)])
    test("G. begärd faller, en vanlig push läker under pausen → inget andra försök", r,
         r["exit"] == 0 and len(r["poster"]) == 1)

    rent = [korning("new", 3), korning("old", 90)]
    r = simulera(skript, runs=rent, head="new", tvinga=True)
    test("H1. provkörning i rent läge → begär en, den lyckas", r,
         r["exit"] == 0 and len(r["poster"]) == 1 and "den begärda publiceringen (körning" in r["ut"]
         and "kom till och lyckades" in r["ut"], visa=True)
    r = simulera(skript, runs=rent, head="new", tvinga=True, post_ko=[("failure", 669)])
    test("H2. provkörning där den begärda faller → rött", r,
         r["exit"] == 1 and len(r["poster"]) == 1 and "slutade i »failure«" in r["ut"])
    r = simulera(skript, runs=rent, head="new", tvinga=True, post_skapar=False)
    test("H3. provkörning där begäran inte ger någon ny publicering → rött (granskningens L2)", r,
         r["exit"] == 1 and len(r["poster"]) == 1 and "gav ingen ny publicering" in r["ut"]
         and "syns inte i körningslistan" in r["ut"], visa=True)

    r = simulera(skript, runs=[korning("new", 1, "success", 99999), korning("old", 90)], head="new",
                 post_ko=[("success", 75)])
    test("I. publicering som hängt i 30 min → begär en ny, som lyckas", r,
         r["exit"] == 0 and len(r["poster"]) == 1 and "bedömer läget ändå" in r["ut"])

    r = simulera(skript, runs=bas, head="new", fel={"commits": 2})
    test("J1. två nätfel i rad mot GitHub → tredje försöket går igenom", r,
         r["exit"] == 0 and len(r["poster"]) == 1)
    r = simulera(skript, runs=bas, head="new", fel={"post": 9})
    test("J2. begäran nekas tre gånger → avbryter rött, hänger sig inte", r,
         r["exit"] != 0 and not r["poster"] and "misslyckades tre gånger" in r["err"])

    r = simulera(skript, runs=bas, head="new", post_ko=[("success", 600)],
                 pushar=[(2, "ny4", "success", 75)])
    test("K. vanlig push kommer under den begärda (som avbryts) → den nya läker", r,
         r["exit"] == 0 and len(r["poster"]) == 1)

    r = simulera(skript, runs=[korning("mid", 5, "cancelled", 30), korning("old", 90)], head="mid")
    test("L. senaste avbröts och ingen ny startade → begär en (ofarligt)", r,
         r["exit"] == 0 and len(r["poster"]) == 1 and "Läkt" in r["ut"])

    r = simulera(skript, runs=bas, head="new", post_latens=40)
    test("M. den begärda körningen syns först efter 40 s → ingen dubbelbegäran", r,
         r["exit"] == 0 and len(r["poster"]) == 1)

    r = simulera(skript, runs=bas, head="new", fel={"commits": 99})
    test("N1. GitHub går inte att läsa alls → rött stopp, inget begärs", r,
         r["exit"] == 1 and not r["poster"] and "Kunde inte läsa läget" in r["ut"]
         and r["minuter"] < 2)
    r = simulera(skript, runs=bas, head="new", fel={"runs": 99})
    test("N2. körningslistan går inte att läsa → rött stopp, inget begärs", r,
         r["exit"] == 1 and not r["poster"] and "Kunde inte läsa läget" in r["ut"])
    r2 = simulera(skript, runs=rent, head="new", tvinga=True, fel={"runs": 99})
    test("N3. provkörning när läget inte går att läsa → rött, inget begärs", r2,
         r2["exit"] == 1 and not r2["poster"] and "Kunde inte läsa läget" in r2["ut"])

    r = simulera(skript, runs=[], head="new")
    test("O. tom körningshistorik → begär en publicering och blir klar", r,
         r["exit"] == 0 and len(r["poster"]) == 1 and "Läkt" in r["ut"])

    r = simulera(skript, runs=[korning("par", 3), korning("barn", 20, "failure", 669),
                               korning("old", 90)], head="barn")
    test("P. lyckad körning bär förälderns sha → en onödig men ofarlig begäran, sedan klart", r,
         r["exit"] == 0 and len(r["poster"]) == 1 and "Läkt" in r["ut"])

    r = simulera(skript, runs=bas, head="new", post_skapar=False)
    test("Q. begäran ger aldrig någon körning → tre försök, sedan rött (inte för evigt)", r,
         r["exit"] == 1 and len(r["poster"]) == 3 and "Gav upp efter 3" in r["ut"])

    # 1/11 2026: två körningar fick +1 h på sina API-tider. I tidsordning ser fallet (9380)
    # ut att komma EFTER publiceringen som läkte det (ac2d); id-ordningen är den sanna.
    forskjutna = [korning("9380", 5, "failure", 57, id=2), korning("c878", 10, "success", 70, id=1),
                  korning("ac2d", 30, "success", 68, id=3), korning("old", 200, id=0)]
    r = simulera(skript, runs=forskjutna, head="ac2d")
    test("R. förskjutna API-tider (1/11): id-ordningen visar att sajten är läkt → begär inget", r,
         r["exit"] == 0 and not r["poster"] and "Inget att göra" in r["ut"])

    r = simulera(skript, runs=rent, head="new", tvinga=True, post_skapar=False,
                 pushar=[(1, "ny5", "success", 60)])
    test("S. provkörning: begäran ger inget, men en vanlig push publicerar under tiden → rött ändå", r,
         r["exit"] == 1 and "gav ingen ny publicering" in r["ut"], visa=True)

    print("\nALLA SCENARIER OK" if ok else "\nSCENARIER FALLERADE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
