"""The greenhouse exercise: plan, research, simulate, test, iterate.

GreenLoop is piloting an automatic irrigation controller for greenhouse Zone 3. The org must:

- research the zone in a knowledge base (which crop and pump are installed, soil physics, rain,
  operating rules, budget and acceptance criteria; some documents are outdated or about other zones),
- build `sim.py`, a simulator of the zone's soil moisture, from the documented physics,
- build `controller.py`, which decides how long to run the pump each hour,
- product-test it: `run_tests` checks the simulator on sample inputs and runs the controller through
  two official 30-day acceptance scenarios, reporting every criterion,
- iterate until everything passes.

Grading is automatic: hidden simulator test cases (35%) plus six hidden 30-day weather scenarios
(heat wave, rainy spell, dry spell...) that the controller must pass (65%). Parameters are drawn
from the seed, so every seed is a different greenhouse.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..sandbox import sandbox_env, set_limits
from .base import Corpus, Doc, Grade, TaskInstance, run_cases

CROPS = {
    # name: (target low, target high, wilting point, crop coefficient) - shifted per seed
    "basil": (24, 32, 16, 1.10),
    "butterhead lettuce": (27, 35, 19, 0.95),
    "tomato seedlings": (22, 30, 14, 1.20),
    "strawberries": (25, 33, 17, 1.05),
}
PUMPS = ["AquaFlow P2", "AquaFlow P3", "HydroMax 40"]
SIM_WEIGHT = 0.35
HOURS = 30 * 24

# Simulation loop shared by the in-process calibration and the sandboxed grader (kept as source so
# exactly the same code runs in both places).
SIM_CORE = r'''
import math

def quiet(hour, P):
    qs, qe = P["quiet_start"], P["quiet_end"]
    return hour >= qs or hour < qe

def simulate(decide, step, P, W):
    m = P["m0"]
    inband = 0
    lowest = m
    water = 0.0
    quiet_seconds = 0.0
    for i, (t, hu, r) in enumerate(W):
        hour, day = i % 24, i // 24
        obs = {"day": day, "hour": hour, "moisture": round(m, 3), "temp": t, "humidity": hu,
               "rain_next_6h": round(sum(x[2] for x in W[i:i + 6]), 3)}
        s = decide(dict(obs))
        s = float(s) if s is not None else 0.0
        if not math.isfinite(s) or s < 0:
            s = 0.0
        if quiet(hour, P) and s > 0:
            quiet_seconds += s
        s = min(s, P["max_on"])
        water += s / 60.0 * P["flow"]
        m = step(m, s, t, hu, r)
        if P["lo"] <= m <= P["hi"]:
            inband += 1
        lowest = min(lowest, m)
    return {"in_band": inband / len(W), "min_moisture": round(lowest, 2), "water_l": round(water, 1),
            "quiet_violation_s": round(quiet_seconds, 1)}

def verdict(metrics, P):
    checks = {
        "in_band": metrics["in_band"] >= P["required_in_band"],
        "never_wilts": metrics["min_moisture"] >= P["wilt"],
        "water_budget": metrics["water_l"] <= P["budget_l"],
        "quiet_hours": metrics["quiet_violation_s"] == 0,
    }
    return {"passed": all(checks.values()), "checks": checks}
'''

RUNNER = r'''
import importlib, json, signal, sys
sys.path.insert(0, sys.argv[1])
data = json.load(open(sys.argv[2]))
P = data["params"]
''' + SIM_CORE + r'''
ns = {}
exec(data["ref_sim"], ns)
class Timeout(BaseException):
    pass
def _alarm(signum, frame):
    raise Timeout("controller took too long")
signal.signal(signal.SIGALRM, _alarm)
results = []
try:
    ctrl = importlib.import_module("controller")
    decide = ctrl.decide
except BaseException as e:
    decide = None
    load_error = type(e).__name__ + ": " + str(e)
for sc in data["scenarios"]:
    if decide is None:
        results.append({"name": sc["name"], "error": load_error[:300]})
        continue
    try:
        signal.alarm(20)
        metrics = simulate(decide, ns["step"], P, sc["weather"])
        signal.alarm(0)
        results.append({"name": sc["name"], "metrics": metrics, **verdict(metrics, P)})
    except BaseException as e:
        signal.alarm(0)
        results.append({"name": sc["name"], "error": (type(e).__name__ + ": " + str(e))[:300]})
sys.stdout.write("\n__SWARMY_SCENARIOS__" + json.dumps(results) + "\n")
'''

REF_SIM = '''"""Zone 3 soil-moisture simulator (reference)."""
A = {a}
T0 = {t0}
K = {k}
R_COEF = {r}
GAIN = {gain}
MAX_ON = {max_on}
FC = {fc}
D = {d}
SAT = {sat}


def step(moisture, pump_seconds, temp, humidity, rain_mm):
    s = min(max(pump_seconds, 0.0), MAX_ON)
    m = moisture + s * GAIN + rain_mm * R_COEF
    m -= K * A * max(0.0, temp - T0) * (1 - humidity / 100)
    if m > FC:
        m -= D * (m - FC)
    return min(max(m, 0.0), SAT)
'''

REF_CONTROLLER = '''"""Zone 3 irrigation controller (reference)."""
LO, HI = {lo}, {hi}
GAIN = {gain}
MAX_ON = {max_on}
R_COEF = {r}
QUIET_START, QUIET_END = {qs}, {qe}


def decide(obs):
    hour = obs["hour"]
    if hour >= QUIET_START or hour < QUIET_END:
        return 0.0
    target = LO + 0.55 * (HI - LO)
    if hour == QUIET_START - 1:
        target = HI - 0.6  # top up before the night, when pumping is not allowed
    if obs["rain_next_6h"] * R_COEF > 1.5:
        target = LO + 0.8  # rain is coming: let it do the work
    deficit = target - obs["moisture"]
    if deficit <= 0:
        return 0.0
    return min(MAX_ON, deficit / GAIN)
'''


def _weather(rng: random.Random, kind: str) -> list[list[float]]:
    base = {"mild": 21, "hot": 27, "heatwave": 23, "rainy": 19, "dry": 25, "mixed": 22}[kind]
    rain_p = {"mild": 0.12, "hot": 0.05, "heatwave": 0.06, "rainy": 0.35, "dry": 0.02, "mixed": 0.15}[kind]
    hum_base = {"dry": 32, "rainy": 70}.get(kind, 55)
    out = []
    for day in range(30):
        heat = 9 if kind == "heatwave" and 10 <= day < 17 else 0
        storm = None
        if rng.random() < rain_p:
            start = rng.randint(0, 23)
            storm = (start, start + rng.randint(1, 4), round(rng.uniform(0.5, 2.5), 2))
        for hour in range(24):
            t = base + heat + 6 * math.sin(2 * math.pi * (hour - 9) / 24) + rng.uniform(-1.5, 1.5)
            raining = storm is not None and storm[0] <= hour < storm[1]
            hu = hum_base - (t - base - heat) * 1.5 + rng.uniform(-8, 8) + (20 if raining else 0)
            out.append([round(t, 1), round(min(98, max(15, hu)), 1), storm[2] if raining else 0.0])
    return out


class GreenhouseTask(TaskInstance):
    family = "greenhouse"

    def __init__(self, params: dict, seed: int):
        super().__init__(params, seed)
        rng = random.Random(f"greenhouse-{seed}")
        crops = list(CROPS)
        crop = rng.choice(crops)
        lo, hi, wilt, k = CROPS[crop]
        shift = rng.randint(-2, 2)
        pumps = {name: (round(rng.uniform(6, 14), 1), rng.choice([600, 720, 900, 1200])) for name in PUMPS}
        pump = rng.choice(PUMPS)
        flow, max_on = pumps[pump]
        zone_l, eff = rng.randint(40, 90) * 10, round(rng.uniform(0.7, 0.9), 2)
        a, t0 = round(rng.uniform(0.022, 0.038), 4), rng.randint(5, 10)
        fc = hi + shift + rng.randint(1, 2)
        p = {
            "crop": crop, "pump": pump, "lo": lo + shift, "hi": hi + shift, "wilt": wilt + shift,
            "k": k, "flow": flow, "max_on": max_on, "zone_l": zone_l, "eff": eff, "a": a, "t0": t0,
            "fc": fc, "d": round(rng.uniform(0.25, 0.4), 2), "sat": 45, "r": round(rng.uniform(0.3, 0.6), 2),
            "quiet_start": rng.choice([21, 22]), "quiet_end": rng.choice([6, 7]), "m0": float(lo + shift + 4),
        }
        p["gain"] = flow / 60 * eff / zone_l * 100
        self.p = p
        self.pumps = pumps
        self.ref_sim = REF_SIM.format(a=a, t0=t0, k=k, r=p["r"], gain=p["gain"], max_on=max_on, fc=fc, d=p["d"],
                                      sat=p["sat"])
        self.ref_controller = REF_CONTROLLER.format(lo=p["lo"], hi=p["hi"], gain=p["gain"], max_on=max_on,
                                                    r=p["r"], qs=p["quiet_start"], qe=p["quiet_end"])
        self.public_scenarios = [{"name": f"official-{kind}", "weather": _weather(random.Random(f"pub-{seed}-{kind}"), kind)}
                                 for kind in ("mild", "rainy")]
        self.hidden_scenarios = [{"name": kind, "weather": _weather(random.Random(f"hid-{seed}-{kind}"), kind)}
                                 for kind in ("mild", "hot", "heatwave", "rainy", "dry", "mixed")]
        self._calibrate()
        sim_ns: dict = {}
        exec(self.ref_sim, sim_ns)  # trusted: generated by us
        self._step = sim_ns["step"]
        self.public_cases = self._sim_cases(random.Random(f"simpub-{seed}"), 4)
        self.hidden_cases = self._sim_cases(random.Random(f"simhid-{seed}"), 24)
        self.corpus = self._corpus(rng)
        self.key_terms = [crop, pump, "quiet hours"]
        self.title = "Greenhouse irrigation controller (plan, research, simulate, test, iterate)"
        spec = self._spec()
        self.workspace_files = {
            "SPEC.md": spec,
            "sim.py": "# Zone 3 soil-moisture simulator. See SPEC.md.\n\n\n"
                      "def step(moisture, pump_seconds, temp, humidity, rain_mm):\n    raise NotImplementedError\n",
            "controller.py": "# Zone 3 irrigation controller. See SPEC.md.\n\n\ndef decide(obs):\n    raise NotImplementedError\n",
        }
        self.brief = (
            "GreenLoop is piloting an automatic irrigation controller for greenhouse Zone 3. Deliver, in the shared "
            "workspace:\n"
            "1. sim.py: a faithful simulator of one hour of Zone 3's soil moisture, built from the engineering "
            "documents.\n"
            "2. controller.py: the controller that decides how long to run the pump each hour.\n"
            "3. TEST_REPORT.md: the scenarios you tried, how the controller did against each acceptance criterion, "
            "and what you changed between iterations.\n\n"
            "Everything about the zone is in the knowledge base: which crop and pump are installed, the soil physics, "
            "rain, operating rules, the water budget and the acceptance criteria. Some documents are outdated or "
            "describe equipment that is not in Zone 3.\n\n"
            "Work like an engineering team: plan, research the facts, build the simulator and use it to try "
            "controller strategies, then product-test with `run_tests` (it checks sim.py on sample inputs and runs "
            "controller.py through two official 30-day acceptance scenarios, reporting every criterion). Iterate "
            "until everything passes. Grading uses hidden scenarios with different weather (heat waves, rainy and "
            "dry spells) plus hidden simulator checks, so don't tune to the public scenarios alone.\n\n"
            f"Interfaces (also in SPEC.md):\n\n{spec}"
        )

    # ---- calibration: acceptance thresholds the reference controller meets with margin ----
    def _calibrate(self) -> None:
        ns: dict = {}
        exec(SIM_CORE, ns)
        sim_ns: dict = {}
        exec(self.ref_sim, sim_ns)
        ctl_ns: dict = {}
        exec(self.ref_controller, ctl_ns)
        runs = [ns["simulate"](ctl_ns["decide"], sim_ns["step"], self.p, sc["weather"])
                for sc in self.public_scenarios + self.hidden_scenarios]
        self.p["required_in_band"] = min(0.9, math.floor((min(r["in_band"] for r in runs) - 0.04) * 100) / 100)
        self.p["budget_l"] = int(math.ceil(max(r["water_l"] for r in runs) * 1.3 / 10) * 10)
        self.reference_runs = runs

    def _sim_cases(self, rng: random.Random, n: int) -> list[dict]:
        p, cases = self.p, []
        for i in range(n):
            m = round(rng.uniform(p["lo"] - 8, p["sat"]), 2) if i % 3 else round(rng.uniform(p["fc"], p["sat"]), 2)
            s = round(rng.uniform(0, p["max_on"] * 1.5), 1) if i % 4 else 0.0
            t = round(rng.uniform(p["t0"] - 3, 38), 1)
            hu = round(rng.uniform(20, 95), 1)
            r = round(rng.uniform(0, 4), 2) if i % 2 else 0.0
            args = [m, s, t, hu, r]
            cases.append({"module": "sim", "func": "step", "args": args, "expected": self._step(*args), "tol": 1e-3})
        return cases

    # ---- knowledge base ----
    def _corpus(self, rng: random.Random) -> Corpus:
        p = self.p
        other_crop = next(c for c in CROPS if c != p["crop"])
        other_pump = next(x for x in PUMPS if x != p["pump"])
        future_pump = next(x for x in PUMPS if x not in (p["pump"], other_pump))
        docs = [
            ("GreenLoop pilot: client requirements",
             f"The pilot runs in Zone 3 only. A season is 30 days of hourly control. To be accepted, in every 30-day "
             f"scenario the controller must: keep soil moisture inside the crop's target band for at least "
             f"{p['required_in_band'] * 100:.0f}% of hours (measured at the end of each hour); never let moisture fall "
             f"below the crop's wilting point; use at most {p['budget_l']} liters of water over the season; and never "
             f"run the pump during quiet hours (see the operating rules)."),
            ("Zone 3 layout",
             f"Zone 3 grows {p['crop']}. Irrigation is by one {p['pump']} pump. The zone holds {p['zone_l']} liters of "
             f"soil and irrigation efficiency is {p['eff'] * 100:.0f}%: adding L liters of water raises soil moisture "
             f"by L x efficiency / soil volume x 100 percentage points. Moisture at the start of the season is "
             f"{p['m0']:.1f}%."),
            ("Zone 1 layout",
             f"Zone 1 grows {other_crop} with a {other_pump} pump and {rng.randint(40, 90) * 10} liters of soil "
             f"(efficiency {rng.randint(70, 90)}%). Zone 1 is not part of the GreenLoop pilot."),
            ("Soil physics notes (2024 revision)",
             f"Soil moisture is volumetric water content in percent. Each hour, evapotranspiration removes "
             f"ET = k x a x max(0, T - T0) x (1 - H/100) percentage points, where T is air temperature in °C, H is "
             f"relative humidity in %, k is the crop coefficient, a = {p['a']} and T0 = {p['t0']} °C. Above field "
             f"capacity ({p['fc']}%) the soil drains: each hour moisture drops by {p['d']} x (moisture - field "
             f"capacity). Moisture cannot exceed saturation ({p['sat']}%) or fall below 0. Within one hour: add "
             f"irrigation and rain first, then subtract ET, then apply drainage if above field capacity, then limit "
             f"to [0, saturation]. This revision supersedes the 2019 notes."),
            ("Soil physics notes (2019)",
             f"[Superseded by the 2024 revision.] Evapotranspiration was estimated as ET = k x "
             f"{round(p['a'] * 1.4, 4)} x max(0, T - {p['t0'] + 3}) percentage points per hour, ignoring humidity. "
             f"Drainage above field capacity was {round(p['d'] / 2, 2)} x the excess per hour."),
            ("Rain infiltration",
             f"Each millimetre of rain falling on Zone 3 in an hour raises soil moisture by {p['r']} percentage points "
             f"in that hour (the greenhouse roof vents are open during rain)."),
            ("Operating rules",
             f"Quiet hours run from {p['quiet_start']}:00 to {p['quiet_end']:02d}:00 (local noise ordinance): the pump "
             f"must not run at all during these hours. Outside quiet hours the pump may run for any number of seconds "
             f"per hour, but commands above the pump's maximum on-time per hour are clipped by the hardware."),
            ("Weather feed",
             "Each hour the controller receives: day (0-29), hour (0-23), moisture (% at the start of the hour), temp "
             "(°C), humidity (%), and rain_next_6h: total rain in mm over the next six hours, including the current "
             "hour. The rain forecast is accurate."),
            ("Memo: Zone 3 pump upgrade",
             f"Facilities plans to replace the Zone 3 pump with a {future_pump} next year. Until then the current pump "
             f"stays in service; the pilot uses the current pump."),
        ]
        for name, (lo, hi, wilt, k) in CROPS.items():
            sh = 0 if name != p["crop"] else p["lo"] - lo
            docs.append((f"Crop profile: {name}",
                         f"Target soil moisture band for {name}: {lo + sh}% to {hi + sh}%. Wilting point: {wilt + sh}%. "
                         f"Crop coefficient k = {k}."))
        for name, (flow, max_on) in self.pumps.items():
            docs.append((f"Pump datasheet: {name}",
                         f"The {name} delivers {flow} liters per minute. Maximum on-time: {max_on} seconds in any hour "
                         f"(longer commands are clipped)."))
        rng.shuffle(docs)
        return Corpus([Doc(doc_id=f"doc-{i + 1:03d}", title=t, text=b) for i, (t, b) in enumerate(docs)])

    def _spec(self) -> str:
        return (
            "# Zone 3 irrigation: interfaces\n\n"
            "## sim.py\n\n"
            "`step(moisture, pump_seconds, temp, humidity, rain_mm) -> float`: soil moisture (%) at the end of one "
            "hour, given moisture at the start of the hour, the pump command in seconds, air temperature (°C), "
            "relative humidity (%) and rain (mm) during that hour. Implement Zone 3's physics exactly as documented "
            "(including the pump's maximum on-time).\n\n"
            "## controller.py\n\n"
            "`decide(obs) -> float`: pump seconds for the coming hour. `obs` is a dict with keys `day`, `hour`, "
            "`moisture`, `temp`, `humidity`, `rain_next_6h` (see the weather feed document).\n\n"
            "## TEST_REPORT.md\n\n"
            "Scenarios tried, results against each acceptance criterion, and what changed between iterations.\n\n"
            "Use only the Python standard library (numpy and matplotlib are available for your own analysis "
            "scripts). `run_tests` checks both files against the official public cases.\n"
        )

    # ---- testing & grading ----
    @property
    def has_public_tests(self) -> bool:
        return True

    def _scenarios(self, workspace: Path, scenarios: list[dict], timeout: float = 120) -> list[dict]:
        data = {"params": self.p, "ref_sim": self.ref_sim, "scenarios": scenarios}
        with tempfile.TemporaryDirectory(prefix="swarmy-gh-") as tmp:
            tmp = Path(tmp)
            ws = tmp / "ws"
            shutil.copytree(workspace, ws, ignore=shutil.ignore_patterns("__pycache__", ".*"))
            (tmp / "data.json").write_text(json.dumps(data))
            (tmp / "runner.py").write_text(RUNNER)
            try:
                proc = subprocess.run([sys.executable, "-I", str(tmp / "runner.py"), str(ws), str(tmp / "data.json")],
                                      cwd=ws, capture_output=True, text=True, timeout=timeout,
                                      env=sandbox_env(ws, tmp), preexec_fn=set_limits(timeout))
            except subprocess.TimeoutExpired:
                return [{"name": s["name"], "error": "timed out"} for s in scenarios]
            out = proc.stdout
            marker = out.rfind("__SWARMY_SCENARIOS__")
            if marker < 0:
                err = (proc.stderr or "no output")[-300:]
                return [{"name": s["name"], "error": f"runner failed: {err}"} for s in scenarios]
            return json.loads(out[marker + len("__SWARMY_SCENARIOS__"):].splitlines()[0])

    def public_tests(self, workspace: Path) -> str:
        sim_results = run_cases(workspace, self.public_cases)
        lines = [f"sim.py: {sum(bool(r.get('ok')) for r in sim_results)}/{len(sim_results)} sample cases match"]
        for c, r in zip(self.public_cases, sim_results):
            call = f"step({', '.join(repr(a) for a in c['args'])})"
            lines.append(f"  {'PASS' if r.get('ok') else 'FAIL'} {call} -> expected {c['expected']:.4f}, got "
                         f"{r.get('got') or r.get('error')}")
        p = self.p
        lines.append(f"\ncontroller.py on the official scenarios (criteria: in band >= {p['required_in_band']:.0%}, "
                     f"min moisture >= {p['wilt']}%, water <= {p['budget_l']} L, no pumping in quiet hours):")
        for res in self._scenarios(workspace, self.public_scenarios):
            if "error" in res:
                lines.append(f"  ERROR {res['name']}: {res['error']}")
                continue
            m, c = res["metrics"], res["checks"]
            mark = lambda ok: "ok" if ok else "FAIL"  # noqa: E731
            lines.append(f"  {'PASS' if res['passed'] else 'FAIL'} {res['name']}: in band {m['in_band']:.1%} "
                         f"[{mark(c['in_band'])}], min moisture {m['min_moisture']}% [{mark(c['never_wilts'])}], "
                         f"water {m['water_l']} L [{mark(c['water_budget'])}], quiet-hour pumping "
                         f"{m['quiet_violation_s']} s [{mark(c['quiet_hours'])}]")
        return "\n".join(lines)

    def _score(self, workspace: Path) -> tuple[float, dict]:
        sim_results = run_cases(workspace, self.hidden_cases)
        sim_frac = sum(bool(r.get("ok")) for r in sim_results) / len(sim_results)
        scen = self._scenarios(workspace, self.hidden_scenarios)
        passed = [s["name"] for s in scen if s.get("passed")]
        ctrl_frac = len(passed) / len(scen)
        details = {"sim_cases_passed": f"{sum(bool(r.get('ok')) for r in sim_results)}/{len(sim_results)}",
                   "scenarios_passed": passed,
                   "scenarios": {s["name"]: s.get("metrics") or s.get("error") for s in scen}}
        return SIM_WEIGHT * sim_frac + (1 - SIM_WEIGHT) * ctrl_frac, details

    def grade(self, answer: str | None, workspace: Path) -> Grade:
        score, details = self._score(workspace)
        return Grade(score=round(score, 4), passed=score >= 0.999, details=details)

    def progress(self, workspace: Path) -> float | None:
        return self._score(workspace)[0]

    def reference_files(self) -> dict[str, str]:
        return {"sim.py": self.ref_sim, "controller.py": self.ref_controller}

    def gold(self) -> dict:
        p = self.p
        return {"crop": p["crop"], "pump": p["pump"], "required_in_band": p["required_in_band"],
                "budget_l": p["budget_l"], "reference_runs": self.reference_runs}
