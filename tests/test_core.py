"""Tests voor de rekenkern. Draaien: python -m unittest discover -s tests -v"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "energie_adviseur"))

from core import DETAIL_S, SLOT_S  # noqa: E402
from core.cycle_detector import CycleDetector, quiet_gap, split_cycle  # noqa: E402
from core.heating_model import DayRecord, cop_at, fit, hourly_advice, implied_cop  # noqa: E402
from core.price import parse_intervals, price_scale  # noqa: E402
from core.program_matcher import Program, match, rebuild  # noqa: E402
from core.scheduler import best_start, build_grid, plan_chain  # noqa: E402

T0 = 1_800_000_000.0 - (1_800_000_000 % 3600)  # heel uur


def run(detector, segments, t=T0, step=30):
    """segments: [(minuten, watt)]. Geeft (cycli, eindtijd)."""
    cycles = []
    for minutes, watt in segments:
        for _ in range(int(minutes * 60 / step)):
            c = detector.update(t, watt)
            if c:
                cycles.append(c)
            t += step
    return cycles, t


WASH_40 = [(15, 2000), (40, 150), (4, 2), (30, 300), (10, 500)]  # met pauze van 4 min
WASH_60 = [(30, 2100), (60, 150), (4, 2), (35, 300), (10, 500)]
IDLE = [(20, 1)]


class DetectorTest(unittest.TestCase):
    def test_cycle_with_pause_is_one_cycle(self):
        cycles, _ = run(CycleDetector(), IDLE + WASH_40 + IDLE)
        self.assertEqual(len(cycles), 1)
        c = cycles[0]
        self.assertAlmostEqual(c.duration_s, 99 * 60, delta=60)
        expected = (15 * 2000 + 40 * 150 + 4 * 2 + 30 * 300 + 10 * 500) / 60 / 1000
        self.assertAlmostEqual(c.energy_kwh, expected, delta=0.02)
        self.assertEqual(len(c.profile), 20)
        self.assertAlmostEqual(sum(c.profile) * SLOT_S / 3.6e6, c.energy_kwh, delta=0.02)

    def test_short_blip_is_ignored(self):
        cycles, _ = run(CycleDetector(), IDLE + [(3, 800)] + IDLE)
        self.assertEqual(cycles, [])

    def test_two_cycles_are_separated(self):
        cycles, _ = run(CycleDetector(), WASH_40 + IDLE + WASH_60 + IDLE)
        self.assertEqual(len(cycles), 2)

    def test_stop_detected_by_tick_without_new_measurement(self):
        d = CycleDetector()
        _, t = run(d, WASH_40)
        d.update(t, 0.0)
        self.assertTrue(d.running)
        self.assertIsNone(d.update(t + 120))
        self.assertIsNotNone(d.update(t + 400))
        self.assertFalse(d.running)

    def test_state_survives_restart(self):
        d = CycleDetector()
        _, t = run(d, WASH_40[:2])
        d2 = CycleDetector()
        d2.load(d.to_dict())
        self.assertTrue(d2.running)
        cycles, _ = run(d2, WASH_40[2:] + IDLE, t=t + 600)  # 10 minuten uit de lucht
        self.assertEqual(len(cycles), 1)
        self.assertAlmostEqual(cycles[0].duration_s, 109 * 60, delta=90)


def learned(name, segments, n=3):
    cycles = []
    for i in range(n):
        cs, _ = run(CycleDetector(), segments + IDLE, t=T0 + i * 86400)
        cs[0].program_id = name
        cycles += cs
    program = Program(name, name)
    rebuild(program, cycles)
    return program


class MatcherTest(unittest.TestCase):
    def setUp(self):
        self.programs = [learned("katoen40", WASH_40), learned("katoen60", WASH_60)]

    def test_rebuild_averages(self):
        p = self.programs[0]
        self.assertEqual(p.count, 3)
        self.assertAlmostEqual(sum(p.profile) * SLOT_S / 3.6e6, p.energy_kwh, delta=0.005)

    def test_recognises_known_program_with_variation(self):
        varied = [(16, 2050), (42, 150), (4, 2), (30, 300), (10, 480)]
        cycles, _ = run(CycleDetector(), varied + IDLE)
        m = match(cycles[0], self.programs)
        self.assertIsNotNone(m.program)
        self.assertEqual(m.program.name, "katoen40")

    def test_distinguishes_programs(self):
        cycles, _ = run(CycleDetector(), WASH_60 + IDLE)
        self.assertEqual(match(cycles[0], self.programs).program.name, "katoen60")

    def test_unknown_program(self):
        cycles, _ = run(CycleDetector(), [(5, 1800), (20, 100)] + IDLE)
        m = match(cycles[0], self.programs)
        self.assertIsNone(m.program)
        self.assertIsNotNone(m.distance)

    def test_no_programs(self):
        cycles, _ = run(CycleDetector(), WASH_40 + IDLE)
        self.assertIsNone(match(cycles[0], []).program)


def hourly(values, start=T0):
    return [(start + i * 3600, start + (i + 1) * 3600, v) for i, v in enumerate(values)]


class PriceTest(unittest.TestCase):
    def test_nordpool_style(self):
        attrs = {
            "raw_today": [
                {"start": "2026-11-13T00:00:00+01:00", "end": "2026-11-13T01:00:00+01:00", "value": 0.25},
                {"start": "2026-11-13T01:00:00+01:00", "end": "2026-11-13T02:00:00+01:00", "value": 0.20},
            ],
            "unit": "kWh",
            "current_price": 0.25,
        }
        out = parse_intervals(attrs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][1] - out[0][0], 3600)
        self.assertEqual(out[1][2], 0.20)

    def test_without_end_and_with_scale(self):
        attrs = {"prices": [{"time": "2026-11-13 00:00:00+01:00", "price": 250.0}, {"time": "2026-11-13 00:15:00+01:00", "price": 200.0}]}
        out = parse_intervals(attrs, scale=price_scale("EUR/MWh"))
        self.assertEqual(out[0][1] - out[0][0], 900)
        self.assertEqual(out[1][1] - out[1][0], 900)
        self.assertAlmostEqual(out[0][2], 0.25)

    def test_duplicates_across_attributes_are_merged(self):
        item = {"from": "2026-11-13T00:00:00+01:00", "till": "2026-11-13T01:00:00+01:00", "price": 0.3}
        self.assertEqual(len(parse_intervals({"a": [item], "b": [item]})), 1)

    def test_units(self):
        self.assertEqual(price_scale("ct/kWh"), 0.01)
        self.assertEqual(price_scale("€/kWh"), 1.0)


class SchedulerTest(unittest.TestCase):
    def test_picks_cheapest_window(self):
        prices = hourly([0.30] * 5 + [0.10] * 2 + [0.30] * 5)
        grid = build_grid(T0, prices)
        self.assertEqual(len(grid), 12 * 12)
        advice = best_start(grid, [1000.0] * 24)  # 2 uur, 1 kW
        self.assertEqual(advice.start, T0 + 5 * 3600)
        self.assertAlmostEqual(advice.cost, 0.20, places=3)
        self.assertAlmostEqual(advice.cost_now, 0.60, places=3)
        self.assertAlmostEqual(advice.saving, 0.40, places=3)

    def test_heavy_part_lands_in_cheap_hour(self):
        prices = hourly([0.30, 0.30, 0.05, 0.30, 0.30, 0.30])
        profile = [2000.0] * 6 + [100.0] * 18  # zwaar eerste half uur
        advice = best_start(build_grid(T0, prices), profile)
        self.assertGreaterEqual(advice.start, T0 + 2 * 3600)
        self.assertLessEqual(advice.start, T0 + 2.5 * 3600)

    def test_solar_surplus_only_counts_with_export_deduction(self):
        prices = hourly([0.25] * 8)
        solar = hourly([0, 0, 0, 0, 3000, 3000, 0, 0])
        flat = best_start(build_grid(T0, prices, solar, 300), [1500.0] * 12)
        self.assertEqual(flat.start, T0)  # alles even duur: zo vroeg mogelijk
        sunny = best_start(build_grid(T0, prices, solar, 300, export_deduction=0.15), [1500.0] * 12)
        self.assertEqual(sunny.start, T0 + 4 * 3600)
        self.assertAlmostEqual(sunny.cost, 1.5 * 0.10, places=3)

    def test_allowed_window_and_deadline(self):
        prices = hourly([0.05] + [0.30] * 7)
        grid = build_grid(T0, prices)
        allowed = [i >= 24 for i in range(len(grid))]  # pas na 2 uur
        advice = best_start(grid, [1000.0] * 12, allowed=allowed)
        self.assertEqual(advice.start, T0 + 2 * 3600)
        self.assertIsNone(best_start(grid, [1000.0] * 12, deadline=T0 + 1800))

    def test_grid_starts_at_next_slot_and_stops_at_gap(self):
        prices = hourly([0.2, 0.2]) + [(T0 + 3 * 3600, T0 + 4 * 3600, 0.2)]
        grid = build_grid(T0 + 61, prices)
        self.assertEqual(grid.t0, T0 + 300)
        self.assertEqual(len(grid), 23)

    def test_too_long_for_horizon(self):
        self.assertIsNone(best_start(build_grid(T0, hourly([0.2])), [1000.0] * 13))


class ChainTest(unittest.TestCase):
    WASH = [2000.0] * 3 + [200.0] * 15  # 1,5 uur
    DRY = [900.0] * 24  # 2 uur
    GAP = 2  # blokken (10 minuten)

    def check(self, plan, loads, pending=0):
        washes = sorted((j for j in plan.jobs if j.kind == "wash"), key=lambda j: j.load)
        dries = [j for j in plan.jobs if j.kind == "dry"]
        self.assertEqual(len(washes), loads)
        self.assertEqual(len(dries), loads + pending)
        for a, b in zip(washes, washes[1:]):
            self.assertGreaterEqual(b.start, a.end + self.GAP)
        ordered = sorted(dries, key=lambda j: j.start)
        for a, b in zip(ordered, ordered[1:]):
            self.assertGreaterEqual(b.start, a.end)
        for w in washes:
            d = next(j for j in dries if j.load == w.load)
            self.assertGreaterEqual(d.start, w.end + self.GAP)
            self.assertLessEqual(d.start, w.end + 36)
        for j in plan.jobs:
            self.assertGreaterEqual(j.start, 0)

    def test_one_to_three_loads(self):
        prices = hourly([0.30, 0.28, 0.25, 0.10, 0.08, 0.09, 0.20, 0.30, 0.32, 0.15, 0.12, 0.25] * 2)
        grid = build_grid(T0, prices)
        previous = 0.0
        for loads in (1, 2, 3):
            plan = plan_chain(grid, self.WASH, self.DRY, loads)
            self.assertIsNotNone(plan)
            self.check(plan, loads)
            self.assertGreater(plan.cost, previous)
            previous = plan.cost

    def test_chain_beats_starting_now(self):
        prices = hourly([0.40] * 4 + [0.05] * 6 + [0.40] * 4)
        grid = build_grid(T0, prices)
        plan = plan_chain(grid, self.WASH, self.DRY, 1)
        wash = next(j for j in plan.jobs if j.kind == "wash")
        self.assertGreaterEqual(wash.start, 3 * 12)
        kwh = (sum(self.WASH) + sum(self.DRY)) * SLOT_S / 3.6e6
        self.assertLess(plan.cost, kwh * 0.10)

    def test_pending_dry_and_busy_machines(self):
        grid = build_grid(T0, hourly([0.2] * 16))
        plan = plan_chain(
            grid, self.WASH, self.DRY, 1, pending_dry=1,
            washer_free_at=T0 + 3600, dryer_free_at=T0 + 1800, pending_dry_ready_at=T0 + 4200,
        )
        self.check(plan, 1, pending=1)
        wash = next(j for j in plan.jobs if j.kind == "wash")
        loose = next(j for j in plan.jobs if j.kind == "dry" and j.load == 0)
        self.assertGreaterEqual(wash.start, 12)
        self.assertGreaterEqual(loose.start, 14)

    def test_allowed_window(self):
        grid = build_grid(T0, hourly([0.05] * 2 + [0.3] * 14))
        allowed = [i >= 36 for i in range(len(grid))]
        plan = plan_chain(grid, self.WASH, self.DRY, 2, allowed=allowed)
        self.check(plan, 2)
        self.assertTrue(all(j.start >= 36 for j in plan.jobs))

    def test_does_not_fit(self):
        self.assertIsNone(plan_chain(build_grid(T0, hourly([0.2] * 3)), self.WASH, self.DRY, 2))

    def test_as_list_is_sorted(self):
        plan = plan_chain(build_grid(T0, hourly([0.2] * 12)), self.WASH, self.DRY, 2)
        starts = [j["start"] for j in plan.as_list()]
        self.assertEqual(starts, sorted(starts))


class HeatingTest(unittest.TestCase):
    def records(self):
        out = []
        for i in range(30):
            dd = 4 + (i % 10)
            noise = 0.15 * ((i * 7) % 5 - 2)
            out.append(DayRecord(f"2026-01-{i + 1:02d}", 0.8 + 0.55 * dd + noise, dd))
        return out

    def test_fit_recovers_base_and_slope(self):
        model = fit(self.records())
        self.assertAlmostEqual(model.m3_per_degree_day, 0.55, delta=0.05)
        self.assertAlmostEqual(model.base_m3, 0.8, delta=0.4)
        self.assertGreater(model.r2, 0.9)

    def test_too_little_data(self):
        self.assertIsNone(fit(self.records()[:5]))

    def test_implied_cop(self):
        records = self.records()
        model = fit(records)
        # airco-dagen: 3 m3 gas minder, vervangen door 3*9.77*0.95/3.5 kWh stroom
        heat = 3 * 9.77 * 0.95
        airco_days = [DayRecord(f"2026-02-{i + 1:02d}", 0.8 + 0.55 * 8 - 3, 8, airco_kwh=heat / 3.5) for i in range(5)]
        cop = implied_cop(records + airco_days, model, 0.95)
        self.assertAlmostEqual(cop, 3.5, delta=0.4)
        self.assertEqual(fit(records + airco_days).days, 30)

    def test_cop_curve(self):
        self.assertAlmostEqual(cop_at(7, 4.0, 2.5), 4.0)
        self.assertAlmostEqual(cop_at(-7, 4.0, 2.5), 2.5)
        self.assertAlmostEqual(cop_at(0, 4.0, 2.5), 3.25)
        self.assertEqual(cop_at(-40, 4.0, 2.5), 1.0)

    def test_hourly_advice(self):
        model = fit(self.records())
        hours = [(T0, 0.20, 5.0), (T0 + 3600, 0.60, 5.0)]
        cheap, pricey = hourly_advice(hours, 1.30, 0.95, 4.0, 2.5, 20.0, model)
        self.assertAlmostEqual(cheap.gas_cost, 1.30 / (9.77 * 0.95), places=3)
        self.assertTrue(cheap.airco_cheaper)
        self.assertFalse(pricey.airco_cheaper)
        self.assertGreater(cheap.saving, 0)
        self.assertEqual(pricey.saving, 0)


class SplitTest(unittest.TestCase):
    """Twee beurten die als één cyclus zijn geteld, weer uit elkaar halen."""

    def merged(self):
        # 20 minuten stand-by van 8 W (boven de stopdrempel) tussen twee wasbeurten
        segments = [(15, 2000), (40, 150), (30, 300), (10, 500), (20, 8), (15, 2000), (40, 150), (30, 300), (10, 500)]
        cycles, _ = run(CycleDetector(), segments + IDLE)
        self.assertEqual(len(cycles), 1)
        return cycles[0]

    def test_quiet_gap_is_the_standby_period(self):
        first, blocks = quiet_gap(self.merged())
        self.assertEqual(first * SLOT_S, 95 * 60)
        self.assertEqual(blocks * SLOT_S, 20 * 60)

    def test_split_at_quiet_gap(self):
        cycle = self.merged()
        a, b = split_cycle(cycle)
        self.assertAlmostEqual(a.duration_s, 95 * 60, delta=1)
        self.assertAlmostEqual(b.duration_s, 95 * 60, delta=SLOT_S)
        self.assertAlmostEqual(a.energy_kwh, b.energy_kwh, delta=0.02)
        self.assertAlmostEqual(a.energy_kwh + b.energy_kwh, cycle.energy_kwh, delta=0.01)
        self.assertEqual(a.start, cycle.start)
        self.assertAlmostEqual(b.end, cycle.end, delta=1)
        self.assertNotEqual(a.cycle_id, b.cycle_id)

    def test_split_at_given_moment(self):
        cycle = self.merged()
        a, b = split_cycle(cycle, at_s=100 * 60)
        self.assertEqual(a.duration_s, 100 * 60)
        self.assertEqual(b.start, cycle.start + 100 * 60)
        self.assertAlmostEqual(a.energy_kwh + b.energy_kwh, cycle.energy_kwh, delta=0.001)

    def test_split_outside_cycle_fails(self):
        cycle = self.merged()
        for at in (0, cycle.duration_s + SLOT_S):
            with self.assertRaises(ValueError):
                split_cycle(cycle, at_s=at)

    def test_detail_is_kept_per_30_seconds(self):
        cycle = self.merged()
        self.assertEqual(cycle.step_s, DETAIL_S)
        self.assertEqual(len(cycle.detail), round(cycle.duration_s / DETAIL_S))
        self.assertEqual(len(cycle.profile), round(cycle.duration_s / SLOT_S))
        wh = sum(w * DETAIL_S / 3600 for w in cycle.detail)
        self.assertAlmostEqual(wh / 1000, cycle.energy_kwh, delta=0.001)

    def test_split_on_half_minute(self):
        cycle = self.merged()
        a, b = split_cycle(cycle, at_s=97.5 * 60)
        self.assertEqual(a.duration_s, 97.5 * 60)
        self.assertEqual(b.start, cycle.start + 97.5 * 60)
        self.assertEqual(len(a.detail), 195)
        self.assertEqual(len(a.profile), 20)  # 19 hele blokken en een half blok
        self.assertAlmostEqual(a.energy_kwh + b.energy_kwh, cycle.energy_kwh, delta=0.001)
        for part in (a, b):
            wh = sum(w * min(SLOT_S, part.duration_s - i * SLOT_S) / 3600 for i, w in enumerate(part.profile))
            self.assertAlmostEqual(wh / 1000, part.energy_kwh, delta=0.001)

    def test_old_cycle_without_detail_splits_per_five_minutes(self):
        cycle = self.merged()
        cycle.detail = []
        self.assertEqual(cycle.step_s, SLOT_S)
        a, b = split_cycle(cycle, at_s=97.5 * 60)  # afgerond op 100 minuten
        self.assertEqual(a.duration_s, 100 * 60)
        self.assertEqual(a.detail, [])
        self.assertAlmostEqual(a.energy_kwh + b.energy_kwh, cycle.energy_kwh, delta=0.001)

    def test_split_parts_match_learned_program(self):
        programs = [learned("Katoen 40", [(15, 2000), (40, 150), (30, 300), (10, 500)])]
        a, b = split_cycle(self.merged())
        self.assertIsNone(match(self.merged(), programs).program)
        self.assertEqual(match(a, programs).program.name, "Katoen 40")
        self.assertEqual(match(b, programs).program.name, "Katoen 40")


if __name__ == "__main__":
    unittest.main()
