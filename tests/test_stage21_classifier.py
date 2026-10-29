# -*- coding: utf-8 -*-
"""G25.2-D Stage 21 — offline tests for the experiment-only classifier
(tmp_g252d_stage21_classifier.py) and the Stage-19C re-score. No network,
no AI, no production/existing-test edits. Covers the required boundary
cases, per-product single-labeling, sum==count reconciliation, and
determinism across repeated runs.
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cls = _load("stage21_classifier", "tmp_g252d_stage21_classifier.py")


class Q2BoundaryCases(unittest.TestCase):
    def test_genuine_complete_bagless_vacuum_is_target(self) -> None:
        fine, scheme, _ = cls.classify21(
            "Q2", "Мощный вертикальный пылесос Airo 1000 Вт для сухой и "
                  "влажной уборки, без мешка для пыли")
        self.assertEqual(scheme, "TARGET")
        self.assertEqual(fine, "TARGET")

    def test_replacement_hepa_filter_for_bagless_model_not_target(self) -> None:
        fine, scheme, _ = cls.classify21(
            "Q2", "Моющийся HEPA-фильтр H13 для пылесоса Bosch VSZ6 BGC41X36 "
                  "без мешка для пыли, запчасти")
        self.assertNotEqual(scheme, "TARGET")
        self.assertEqual(scheme, "RELATED")

    def test_filter_kit_with_bagless_phrase_not_target(self) -> None:
        fine, scheme, _ = cls.classify21(
            "Q2", "Комплект из 3 фильтров и 6 ремней для пылесоса Eureka "
                  "Powerspeed без мешка, запасные части")
        self.assertNotEqual(scheme, "TARGET")
        self.assertIn(fine, cls.FINE)
        self.assertEqual(scheme, "RELATED")

    def test_replacement_bag_is_wrong_domain(self) -> None:
        fine, scheme, _ = cls.classify21(
            "Q2", "10 шт. мешков-пылесборников для пылесоса, мешки для "
                  "пылесоса A2004")
        self.assertEqual(scheme, "WRONG_DOMAIN")

    def test_spare_part_motor_not_target(self) -> None:
        fine, scheme, _ = cls.classify21(
            "Q2", "Двигатель всасывания для пылесоса Samsung, запасная часть")
        self.assertNotEqual(scheme, "TARGET")
        self.assertEqual(scheme, "RELATED")

    def test_appliance_mentioning_included_filters_is_target(self) -> None:
        # A complete appliance whose title also lists included filters/brush
        # must still be TARGET (not penalized for naming its own components).
        fine, scheme, _ = cls.classify21(
            "Q2", "Беспроводной ручной пылесос без мешка с боковой щеткой, "
                  "фильтром, шлангом, HEPA-фильтром, 25 кПа")
        self.assertEqual(scheme, "TARGET")

    def test_ambiguous_appliance_filter_mix_is_uncertain(self) -> None:
        fine, scheme, _ = cls.classify21(
            "Q2", "SN50T6 Турбо-циклон, фильтр для пылесоса, циклонный "
                  "пылесборник без мешка")
        self.assertEqual(scheme, "UNCERTAIN")


def fcheme_guard(x):
    return x


class LabelingInvariants(unittest.TestCase):
    def test_scheme_mapping_is_conservative(self) -> None:
        self.assertEqual(cls.to_scheme("CONSUMABLE"), "RELATED")
        self.assertEqual(cls.to_scheme("ACCESSORY"), "RELATED")
        self.assertEqual(cls.to_scheme("SPARE_PART"), "RELATED")
        self.assertEqual(cls.to_scheme("TARGET"), "TARGET")
        self.assertEqual(cls.to_scheme("UNCERTAIN"), "UNCERTAIN")

    def test_every_fine_label_maps_to_a_scheme_label(self) -> None:
        for fine in cls.FINE:
            self.assertIn(cls.to_scheme(fine),
                          {"TARGET", "RELATED", "WRONG_DOMAIN",
                           "UNRELATED", "UNCERTAIN"})


class ReScoreInvariants(unittest.TestCase):
    """Read the produced re-score JSON and assert the structural guarantees.
    (The JSON is produced by tmp_g252d_stage21_rescore.py before tests run.)"""

    @classmethod
    def setUpClass(cls) -> None:
        p = ROOT / "tmp_g252d_stage21_rescored_results.json"
        if not p.exists():
            raise unittest.SkipTest("run tmp_g252d_stage21_rescore.py first")
        cls.data = json.loads(p.read_text(encoding="utf-8"))

    def test_all_twelve_requests_present(self) -> None:
        self.assertEqual(len(self.data["requests"]), 12)

    def test_consistency_ok_flag(self) -> None:
        self.assertTrue(self.data["consistency_ok"])

    def test_each_request_sums_and_extracts(self) -> None:
        for rid, r in self.data["requests"].items():
            self.assertEqual(sum(r["scheme_counts"].values()), r["items"],
                            rid)
            self.assertEqual(r["items"], r["extracted"], rid)
            if r["status"] == "ok":
                self.assertEqual(r["items"], r["raw"], rid)

    def test_every_product_one_valid_label(self) -> None:
        for rid, r in self.data["requests"].items():
            for it in r["products"]:
                self.assertIn(it["fine"], cls.FINE, rid)
                self.assertIn(it["scheme"],
                              {"TARGET", "RELATED", "WRONG_DOMAIN",
                               "UNRELATED", "UNCERTAIN"}, rid)
                self.assertIn(it["orig_label"],
                              {"TARGET", "RELATED", "WRONG_DOMAIN",
                               "UNRELATED", "UNCERTAIN", None}, rid)

    def test_q2_corrected_improves_without_appliance_inflation(self) -> None:
        # The corrected Q2 must still show a candidate gain, but the candidate
        # TARGET must be strictly fewer than the buggy 43 (accessories gone).
        q2 = self.data["pairs"]["Q2"]
        self.assertGreater(q2["dT_pp"], 0)
        cand_t = q2["candidate"]["TARGET"]
        self.assertLess(cand_t, 43)
        self.assertGreater(cand_t, 0)

    def test_rescore_is_deterministic(self) -> None:
        # Re-run classify21 on every saved item; labels must not drift.
        for rid, r in self.data["requests"].items():
            for it in r["products"]:
                fine, scheme, _ = cls.classify21(r["pair"], it["title"])
                self.assertEqual(scheme, it["scheme"], rid)
                self.assertEqual(fine, it["fine"], rid)


if __name__ == "__main__":
    unittest.main()

