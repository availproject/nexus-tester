import unittest

from fastbridge_scenarios import SCENARIOS, resolve_run_config


class ResolveRunConfigTests(unittest.TestCase):
    def test_each_named_scenario_uses_its_canonical_config(self):
        for scenario_id, expected in SCENARIOS.items():
            with self.subTest(scenario_id=scenario_id):
                config = resolve_run_config({"FASTBRIDGE_SCENARIO": scenario_id})
                self.assertEqual(config.execution_kind, "scenario")
                self.assertEqual(config.scenario_id, scenario_id)
                for field, value in expected.items():
                    self.assertEqual(getattr(config, field), value)

    def test_u04_is_always_ui_only(self):
        self.assertTrue(resolve_run_config({"FASTBRIDGE_SCENARIO": "EXP-U04"}).stop_before_execution)

    def test_custom_exact_out_run_is_not_a_named_scenario(self):
        config = resolve_run_config(
            {
                "FASTBRIDGE_EXACT_MODE": "out",
                "FASTBRIDGE_DEST_SLUG": "base",
                "FASTBRIDGE_ASSET": "USDC",
                "FASTBRIDGE_RECEIVE_ASSET": "USDC",
                "FASTBRIDGE_RECEIVE_CHAIN": "Base",
                "FASTBRIDGE_BRIDGE_AMOUNT": "1",
            }
        )
        self.assertEqual(config.execution_kind, "custom")
        self.assertIsNone(config.scenario_id)
        self.assertEqual(config.amount, "1")

    def test_conflicting_route_override_for_named_scenario_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "FASTBRIDGE_ASSET.*EXP-U05"):
            resolve_run_config({"FASTBRIDGE_SCENARIO": "EXP-U05", "FASTBRIDGE_ASSET": "USDC"})

    def test_matching_route_override_for_named_scenario_is_allowed(self):
        config = resolve_run_config(
            {"FASTBRIDGE_SCENARIO": "EXP-U07", "FASTBRIDGE_EXACT_MODE": "exact-out"}
        )
        self.assertEqual(config.exact_mode, "out")

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown FASTBRIDGE_SCENARIO"):
            resolve_run_config({"FASTBRIDGE_SCENARIO": "EXP-U99"})


if __name__ == "__main__":
    unittest.main()
