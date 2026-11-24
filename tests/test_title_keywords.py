import unittest

from jobFilter import title_keywords as tk
from jobFilter.filters import apply_rules
from jobFilter.sources import _job


def matches(keyword, title, positive=False):
    return tk.any_match(tk.compile_list([keyword], positive=positive), title)


class TitleKeywordTests(unittest.TestCase):
    def test_plain_entry_stays_a_substring(self):
        """Existing configs must not change meaning: a plain multi-char keyword
        is still matched loosely on both sides."""
        for title in ('Agentforce Developer', 'AI Agents Engineer', 'Reagents Chemist'):
            self.assertTrue(matches('agent', title), title)

    def test_stem_requires_the_keyword_to_start_a_word(self):
        self.assertTrue(matches('stem:agent', 'Agentforce Developer'))
        self.assertTrue(matches('stem:agent', 'AI Agents Engineer'))
        self.assertFalse(matches('stem:agent', 'Reagents Chemist'))

    def test_word_requires_the_whole_word(self):
        self.assertTrue(matches('word:agent', 'Support Agent'))
        for title in ('Agentforce Developer', 'AI Agents Engineer', 'Reagents Chemist'):
            self.assertFalse(matches('word:agent', title), title)

    def test_word_intern_spares_internal_and_international(self):
        """The case the old substring rule could not express: rejecting
        internships without rejecting every Internal/International title."""
        self.assertTrue(matches('word:Intern', 'Operations Intern'))
        self.assertFalse(matches('word:Intern', 'Internal Platform Engineer'))
        self.assertFalse(matches('word:Intern', 'International Software Engineer'))
        # A bare 'intern' is why the anchor exists.
        self.assertTrue(matches('intern', 'Internal Platform Engineer'))

    def test_short_acronyms_are_anchored_automatically(self):
        self.assertTrue(matches('sde', 'SDE I, New Grad'))
        self.assertFalse(matches('sde', 'SDET Engineer'))
        self.assertTrue(matches('ai', 'AI Engineer'))
        self.assertFalse(matches('ai', 'Maintenance Technician'))
        self.assertFalse(matches('ai', 'Thai Cuisine Manager'))
        # Four letters or more, or anything with a non-letter, stays substring.
        self.assertTrue(matches('java ', 'Core Java Developer'))
        self.assertTrue(matches('.net', 'ASP.NET Engineer'))

    def test_cjk_is_a_word_edge_for_a_latin_acronym(self):
        for title in ('AI工程师', 'AIエンジニア', 'AI엔지니어'):
            self.assertTrue(matches('ai', title), title)

    def test_accents_fold_on_both_sides(self):
        self.assertTrue(matches('técnico', 'TECNICO DE SOFTWARE'))
        self.assertTrue(matches('tecnico', 'Técnico de Software'))

    def test_whitespace_runs_collapse(self):
        self.assertTrue(matches('software engineer', 'Software Engineer'))
        self.assertTrue(matches('software engineer', 'Software  Engineer'))

    def test_and_groups_require_every_term_in_any_order(self):
        kw = 'director + engineering'
        for title in ('Director of Engineering', 'Director - Software Engineering',
                      'Senior Director, Platform Engineering', 'Director Engineering (Mobile)'):
            self.assertTrue(matches(kw, title, positive=True), title)
        self.assertFalse(matches(kw, 'Director of Finance', positive=True))

    def test_and_group_terms_keep_their_own_anchoring(self):
        self.assertTrue(matches('ml + engineer', 'ML Engineer', positive=True))
        self.assertFalse(matches('ml + engineer', 'HTML Engineer', positive=True))

    def test_plus_without_spaces_is_a_literal_keyword(self):
        """A bare split on '+' would turn 'C++' into 'c' and match almost
        everything."""
        self.assertTrue(matches('c++', 'C++ Systems Engineer', positive=True))
        self.assertFalse(matches('c++', 'Data Analyst', positive=True))

    def test_bare_prefix_matches_nothing_rather_than_everything(self):
        self.assertFalse(matches('word:', 'Anything At All'))
        self.assertFalse(matches('stem:  ', 'Anything At All'))

    def test_malformed_entries_are_dropped_not_crashed(self):
        compiled = tk.compile_list(['engineer', None, 7, '', '   ', {'a': 1}])
        self.assertEqual([raw for raw, _ in compiled], ['engineer'])
        self.assertTrue(tk.any_match(compiled, 'Software Engineer'))

    def test_non_string_title_does_not_raise(self):
        compiled = tk.compile_list(['engineer'])
        self.assertFalse(tk.any_match(compiled, None))
        self.assertFalse(tk.any_match(compiled, 123))

    def test_first_match_names_the_entry_as_written(self):
        compiled = tk.compile_list(['senior', 'word:Intern'])
        self.assertEqual(tk.first_match(compiled, 'Operations Intern'), 'word:Intern')
        self.assertEqual(tk.first_match(compiled, 'Senior Engineer'), 'senior')
        self.assertIsNone(tk.first_match(compiled, 'Software Engineer'))


class TitleRuleIntegrationTests(unittest.TestCase):
    def job(self, title):
        return _job('greenhouse', id=title, title=title, company='Example', countries=['US'],
                    category='Software Development', dedup_key=title)

    def test_rules_report_the_matching_keyword(self):
        rules = {'title_include': ['engineer'], 'title_exclude': ['word:Intern', 'senior']}
        kept, rejected = apply_rules(
            [self.job('Software Engineer'), self.job('Software Engineer Intern'),
             self.job('Internal Platform Engineer'), self.job('Senior Engineer'),
             self.job('Chef de Partie')], rules)
        self.assertEqual([j.title for j in kept], ['Software Engineer', 'Internal Platform Engineer'])
        self.assertEqual([(j.title, r) for j, r in rejected],
                         [('Software Engineer Intern', "title contains 'word:Intern'"),
                          ('Senior Engineer', "title contains 'senior'"),
                          ('Chef de Partie', 'title lacks include keyword')])

    def test_empty_include_list_is_no_constraint(self):
        kept, rejected = apply_rules([self.job('Chef de Partie')], {'title_exclude': ['senior']})
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejected, [])

    def test_max_age_reads_published_at_when_millis_is_missing(self):
        """hiring.cafe sends a publish date but no millis field, so a rule
        reading only published_millis was blind to the source producing most of
        the volume and let through postings first published years earlier."""
        import time
        from datetime import datetime, timezone
        fresh = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc).isoformat()
        rules = {'max_age_hours': 720}

        def job(name, **kw):
            return _job('hiringcafe', id=name, title='Software Engineer', company='Example',
                        countries=['US'], category='Software Development', dedup_key=name, **kw)

        jobs = [
            job('zombie', published_at='2013-11-07T00:00:00+00:00', published_millis=None),
            job('recent', published_at=fresh, published_millis=None),
            job('undated', published_at=None, published_millis=None),
            job('unparseable', published_at='not a date', published_millis=None),
            # An explicit millis still wins, and a naive timestamp is read as UTC.
            job('by_millis', published_at=None, published_millis=int((time.time() - 3600) * 1000)),
            job('naive_zombie', published_at='2013-11-07T00:00:00', published_millis=None),
        ]
        kept, rejected = apply_rules(jobs, rules)
        self.assertEqual([j.id for j in kept], ['recent', 'undated', 'unparseable', 'by_millis'])
        self.assertEqual([j.id for j, _ in rejected], ['zombie', 'naive_zombie'])
        self.assertTrue(all('posted' in r and 'ago >' in r for _, r in rejected))
        # Off by default: no window configured means no age rule at all.
        self.assertEqual(len(apply_rules([jobs[0]], {})[0]), 1)

    def test_unknown_category_is_not_a_mismatch(self):
        """A direct employer board carries no category taxonomy; the title rules
        are the role filter there. A KNOWN wrong category is still rejected."""
        rules = {'require_categories': ['Software Development']}
        unknown = _job('greenhouse', id='u', title='Software Engineer', company='Example',
                       countries=['US'], category=None, dedup_key='u')
        wrong = _job('hiringcafe', id='w', title='Welder', company='Example',
                     countries=['US'], category='Skilled Trades', dedup_key='w')
        kept, rejected = apply_rules([unknown, wrong], rules)
        self.assertEqual([j.id for j in kept], ['u'])
        self.assertEqual([(j.id, r) for j, r in rejected], [('w', "category 'Skilled Trades'")])


if __name__ == '__main__':
    unittest.main()
