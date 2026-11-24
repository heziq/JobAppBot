import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobFilter.descriptions import stated_refusal
from jobFilter.screen import DEFAULT_SCREENING, save_screening_settings, screen_job, screening_config


class StatedRefusalTests(unittest.TestCase):
    def test_catches_the_wordings_this_install_actually_saw(self):
        """Taken from real screenings on this machine that cost money to reach."""
        for text in [
            "Visa sponsorship will not be offered for this position.",
            "Veeva does not provide sponsorship for employment visas.",
            "Crowe is not sponsoring for work authorization at this time.",
            "Applicants must not require visa sponsorship now or in the future.",
            "We are unable to offer visa sponsorship for this role.",
            "This position is not eligible for visa sponsorship.",
            "You must be able to work without requiring sponsorship.",
            "Candidates must have work authorization that does not require sponsorship.",
            "We can't provide sponsorship for this opening.",
            "We can’t provide sponsorship for this opening.",   # curly apostrophe
            "NO VISA SPONSORSHIP IS AVAILABLE FOR THIS ROLE.",
        ]:
            self.assertIsNotNone(stated_refusal(text), text)

    def test_does_not_fire_on_an_offer_of_sponsorship(self):
        """A false 'unlikely' costs an opportunity to save about ten cents, so
        the rule has to stay quiet whenever sponsorship is on offer."""
        for text in [
            "We offer visa sponsorship for this role.",
            "Visa sponsorship is available for exceptional candidates.",
            "We sponsor H-1B and welcome applicants who need sponsorship.",
            "We sponsor visas and also welcome those who do not require sponsorship.",
            "Sponsorship: yes, we file H-1B petitions every year.",
            "This employer has sponsored 412 H-1B petitions.",
            "",
            "A posting that never mentions work authorization at all.",
        ]:
            self.assertIsNone(stated_refusal(text), text)

    def test_returns_the_sentence_so_the_decision_can_be_checked(self):
        text = ("About the team. We build payments infrastructure.\n"
                "Visa sponsorship is not offered for this role.\n"
                "Benefits include medical and dental.")
        self.assertEqual(stated_refusal(text), "Visa sponsorship is not offered for this role.")

    def test_a_refusal_elsewhere_in_a_long_posting_is_still_found(self):
        text = "Lorem ipsum. " * 400 + "We are unable to provide sponsorship. " + "Dolor sit. " * 50
        self.assertIn("unable to provide sponsorship", stated_refusal(text))

    def test_matching_is_scoped_to_the_sentence_mentioning_sponsorship(self):
        """Two unrelated sentences must not combine into a match."""
        self.assertIsNone(stated_refusal("We do not offer relocation. Sponsorship questions go to HR."))


class ScreenJobShortCircuitTests(unittest.TestCase):
    def row(self):
        return {"id": "j1", "job": {"company": "Example", "title": "Software Engineer",
                                    "location": "Austin, TX", "apply_url": "https://example.com/j/1",
                                    "via": "hiringcafe", "source": "greenhouse"}}

    class FakeStore:
        def __init__(self):
            self.saved = {}
        def company_sponsorship(self, *a, **k):
            return None
        def save_screening(self, job_id, data):
            self.saved[job_id] = data

    def run_screen(self, description, **cfg_changes):
        store = self.FakeStore()
        cfg = {**DEFAULT_SCREENING, **cfg_changes}
        with patch('jobFilter.screen.fetch_description', return_value=description), \
             patch('jobFilter.screen.run_claude') as claude:
            claude.return_value = ({"verdict": "likely", "statement": "not_mentioned",
                                    "requires_citizenship": False, "new_grad_fit": True,
                                    "intern_return_requirement": "not_an_internship", "fit_score": 7,
                                    "summary": "model ran", "evidence": [], "company_evidence": [],
                                    "sources": []}, {"total_cost_usd": 0.11})
            data = screen_job(store, self.row(), cfg)
        return data, claude, store

    def test_a_stated_refusal_skips_the_paid_call(self):
        data, claude, store = self.run_screen("Visa sponsorship is not offered for this role.")
        claude.assert_not_called()
        self.assertEqual(data["verdict"], "unlikely")
        self.assertEqual(data["statement"], "no_sponsorship")
        self.assertEqual(data["cost_usd"], 0.0)
        self.assertEqual(data["model"], "phrase-rule")
        self.assertEqual(data["fit_score"], 2)
        self.assertEqual(data["evidence"], ["Visa sponsorship is not offered for this role."])
        self.assertEqual(data["sources"], ["https://example.com/j/1"])
        self.assertEqual(store.saved["j1"], data)       # persisted like any screening

    def test_everything_else_still_reaches_the_model(self):
        for description in ("We offer visa sponsorship.", "Nothing about work authorization.", ""):
            data, claude, _ = self.run_screen(description)
            claude.assert_called_once()
            self.assertEqual(data["summary"], "model ran", description)

    def test_the_short_circuit_can_be_turned_off(self):
        data, claude, _ = self.run_screen("Visa sponsorship is not offered.", refusal_skips_llm=False)
        claude.assert_called_once()
        self.assertEqual(data["summary"], "model ran")


class MaxPerRunSettingTests(unittest.TestCase):
    def config_file(self, screening=None):
        d = Path(tempfile.mkdtemp())
        p = d / "search.json"
        p.write_text(json.dumps({"rules": {"title_include": ["engineer"]},
                                 "screening": screening or {"max_per_run": 40}}))
        return p

    def test_saving_a_limit_keeps_the_search_rules(self):
        p = self.config_file()
        result = save_screening_settings(p, {"max_per_run": 10})
        self.assertEqual(result["max_per_run"], 10)
        saved = json.loads(p.read_text())
        self.assertEqual(saved["screening"]["max_per_run"], 10)
        self.assertEqual(saved["rules"]["title_include"], ["engineer"])   # untouched

    def test_a_string_from_a_form_field_is_accepted(self):
        self.assertEqual(save_screening_settings(self.config_file(), {"max_per_run": "25"})["max_per_run"], 25)

    def test_out_of_range_and_nonsense_are_refused(self):
        for bad in (0, -1, 201, "", "ten", None, 1.5e9):
            with self.assertRaises(ValueError, msg=bad):
                save_screening_settings(self.config_file(), {"max_per_run": bad})

    def test_other_screening_keys_are_unaffected(self):
        p = self.config_file({"max_per_run": 40, "company_cache_days": 30, "concurrency": 3})
        save_screening_settings(p, {"max_per_run": 5, "enabled": False})
        saved = json.loads(p.read_text())["screening"]
        self.assertEqual((saved["max_per_run"], saved["enabled"]), (5, False))
        self.assertEqual((saved["company_cache_days"], saved["concurrency"]), (30, 3))

    def test_default_includes_the_new_keys(self):
        cfg = screening_config({})
        self.assertEqual(cfg["max_per_run"], 40)
        self.assertTrue(cfg["refusal_skips_llm"])


if __name__ == '__main__':
    unittest.main()
