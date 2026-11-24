import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from jobFilter.ashby import MAX_BYTES, board_slug, fetch_ashby, parse_board


class BoardSlugTests(unittest.TestCase):
    def test_accepts_a_slug_and_the_url_shapes(self):
        for entry in ('ramp',
                      {'slug': 'ramp'},
                      {'api': 'https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true'},
                      {'careers_url': 'https://jobs.ashbyhq.com/ramp'},
                      {'careers_url': 'https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245'},
                      {'slug': 'jobs.ashbyhq.com/ramp'}):
            self.assertEqual(board_slug(entry), 'ramp', entry)

    def test_refuses_a_url_outside_ashby(self):
        for bad in ('https://evil.example.com/posting-api/job-board/ramp',
                    'https://api.ashbyhq.com.evil/posting-api/job-board/ramp',
                    'http://169.254.169.254/latest/meta-data/',
                    'file:///etc/passwd'):
            with self.assertRaises(ValueError, msg=bad):
                board_slug({'api': bad})

    def test_refuses_a_malformed_or_missing_token(self):
        for bad in ({}, {'slug': ''}, {'slug': 'has space'}, {'slug': '../etc'}, 42, None):
            with self.assertRaises(ValueError, msg=bad):
                board_slug(bad)


class ParseBoardTests(unittest.TestCase):
    NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)

    def row(self, **changes):
        # Shape taken from a live posting-api response (ramp, 2026-11-24).
        return {
            'id': '34413f8d-26bf-4bbc-8ade-eb309a0e2245',
            'title': 'Software Engineer, New Grad',
            'location': 'New York, NY (HQ)',
            'jobUrl': 'https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245',
            'applyUrl': 'https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245/application',
            'publishedAt': '2026-11-22T17:12:35.753+00:00',
            'employmentType': 'FullTime', 'isListed': True, 'isRemote': False,
            'workplaceType': 'Onsite', 'department': 'Engineering', 'team': 'Platform',
            'address': {'postalAddress': {'addressRegion': 'NY', 'addressCountry': 'USA',
                                          'addressLocality': 'New York City'}},
            'secondaryLocations': [],
            'compensation': {'compensationTierSummary': '$211.4K – $290.6K'},
            'descriptionPlain': 'We are hiring. 0-1 years of experience.',
            **changes}

    def parse(self, rows, **kw):
        kw.setdefault('now', self.NOW)
        return parse_board({'jobs': rows}, 'ramp', company='Ramp', **kw)

    def test_maps_the_fields_and_returns_the_body(self):
        (job, description), = self.parse([self.row()])
        self.assertEqual(job.id, 'ashby___ramp___34413f8d-26bf-4bbc-8ade-eb309a0e2245')
        self.assertEqual((job.via, job.source), ('ashby', 'ashby'))
        self.assertEqual(job.company, 'Ramp')
        self.assertEqual(job.apply_url, self.row()['applyUrl'])
        self.assertEqual(job.hc_url, self.row()['jobUrl'])
        self.assertEqual(job.countries, ['US'])
        self.assertEqual(job.states, ['NY'])
        self.assertEqual(job.commitment, ['Full Time'])
        self.assertEqual(job.published_at, '2026-11-22T17:12:35+00:00')
        self.assertEqual(description, 'We are hiring. 0-1 years of experience.')

    def test_country_comes_from_the_structured_address(self):
        """Ashby gives a postal address, so the country is read rather than
        guessed from a display string."""
        job, _ = self.parse([self.row(location='Anywhere', address={
            'postalAddress': {'addressCountry': 'Canada'}})])[0]
        self.assertEqual(job.countries, ['CA'])
        # A US state with the country left out still implies the US.
        job, _ = self.parse([self.row(location='Austin', address={
            'postalAddress': {'addressRegion': 'TX'}})])[0]
        self.assertEqual((job.countries, job.states), (['US'], ['TX']))

    def test_secondary_locations_add_countries(self):
        job, _ = self.parse([self.row(secondaryLocations=[
            {'location': 'Remote (Canada)', 'address': {'postalAddress': {'addressCountry': 'Canada'}}},
            {'location': 'London', 'address': {'postalAddress': {'addressCountry': 'United Kingdom'}}}])])[0]
        self.assertEqual(job.countries, ['CA', 'GB', 'US'])

    def test_remote_with_no_geography_is_placed_like_the_other_sources(self):
        job, _ = self.parse([self.row(location='Remote', isRemote=True, address={})])[0]
        self.assertEqual(job.countries, ['US'])
        self.assertEqual(job.workplace_type, 'Remote')

    def test_states_nothing_it_does_not_know(self):
        job, _ = self.parse([self.row()])[0]
        self.assertIsNone(job.seniority)
        self.assertIsNone(job.min_yoe)
        self.assertIsNone(job.category)
        self.assertIsNone(job.visa_sponsorship)

    def test_unlisted_postings_are_skipped(self):
        """isListed false means withdrawn from the public board."""
        self.assertEqual(self.parse([self.row(isListed=False)]), [])
        self.assertEqual(len(self.parse([self.row(isListed=True)])), 1)
        self.assertEqual(len(self.parse([self.row(isListed=None)])), 1)   # absent is not "withdrawn"

    def test_freshness_window(self):
        rows = [self.row(id='fresh', publishedAt='2026-11-22T00:00:00+00:00'),
                self.row(id='stale', publishedAt='2021-04-27T20:13:45.158+00:00'),
                self.row(id='undated', publishedAt=None)]
        self.assertEqual([j.id.rsplit('___', 1)[1] for j, _ in self.parse(rows, max_age_days=7)], ['fresh'])
        self.assertEqual(len(self.parse(rows)), 3)

    def test_skips_rows_it_cannot_identify(self):
        rows = [None, 'nope', {}, self.row(id=None), self.row(title=''), self.row(applyUrl=None, jobUrl=None),
                self.row(applyUrl='https://evil.example.com/x', jobUrl=None), self.row()]
        self.assertEqual(len(self.parse(rows)), 1)

    def test_a_malformed_payload_is_empty_not_an_error(self):
        for payload in (None, {}, {'jobs': None}, {'jobs': 'x'}, [], 'x'):
            self.assertEqual(parse_board(payload, 'ramp'), [])

    def test_internships_are_kept_and_labelled(self):
        """Internships are applied to as well; only the posting's own return-to-
        school requirement rules one out, and that is screening's job."""
        job, _ = self.parse([self.row(title='Software Engineer Intern', employmentType='Intern')])[0]
        self.assertEqual(job.commitment, ['Internship'])


class FetchAshbyTests(unittest.TestCase):
    def payload(self, slug):
        return {'jobs': [{'id': 'abc', 'title': 'Software Engineer',
                          'applyUrl': f'https://jobs.ashbyhq.com/{slug}/abc/application',
                          'jobUrl': f'https://jobs.ashbyhq.com/{slug}/abc',
                          'location': 'Remote', 'isRemote': True, 'isListed': True,
                          'publishedAt': '2026-11-22T00:00:00+00:00',
                          'descriptionPlain': 'Body for ' + slug}]}

    def test_one_broken_board_does_not_stop_the_others_and_bodies_are_collected(self):
        logs, descriptions = [], {}

        def fake_fetch(slug, timeout=90):
            if slug == 'broken':
                raise RuntimeError('502')
            return self.payload(slug)

        cfg = {'boards': [{'name': 'A', 'slug': 'aaa'}, {'name': 'B', 'slug': 'broken'},
                          {'name': 'C', 'slug': 'ccc'}, {'name': 'D', 'slug': 'ddd', 'enabled': False},
                          {'name': 'E', 'slug': 'has space'}]}
        with patch('jobFilter.ashby.fetch_board', fake_fetch), patch('jobFilter.ashby.time.sleep'):
            jobs = fetch_ashby(cfg, log=logs.append, descriptions=descriptions)

        self.assertEqual([j.company for j in jobs], ['A', 'C'])
        self.assertEqual(sorted(descriptions.values()), ['Body for aaa', 'Body for ccc'])
        self.assertTrue(any('FAILED RuntimeError' in m for m in logs))
        self.assertTrue(any('has space' in m for m in logs))

    def test_no_boards_is_a_no_op(self):
        with patch('jobFilter.ashby.fetch_board') as fetch:
            self.assertEqual(fetch_ashby({'boards': []}), [])
            self.assertEqual(fetch_ashby({}), [])
        fetch.assert_not_called()

    def test_oversized_response_is_refused(self):
        class Response:
            headers = {'content-length': str(MAX_BYTES + 1)}
            content = b''
            def raise_for_status(self): pass
            def json(self): raise AssertionError('must not parse an oversized body')

        from jobFilter import ashby
        with patch.object(ashby.requests, 'get', return_value=Response()):
            with self.assertRaises(ValueError):
                ashby.fetch_board('ramp')

    def test_registered_as_a_source_and_off_by_default(self):
        from jobFilter.sources import DEFAULT_SOURCES, fetch_all
        self.assertFalse(DEFAULT_SOURCES['ashby']['enabled'])
        collected = {}
        with patch('jobFilter.sources.fetch_ashby', return_value=[]) as run:
            fetch_all({'sources': {k: False for k in ('hiringcafe', 'simplify', 'startupjobs',
                                                      'applyguy', 'speedyapply', 'greenhouse')}
                       | {'ashby': {'enabled': True, 'boards': [{'slug': 'ramp'}]}}},
                      descriptions=collected)
        run.assert_called_once()
        self.assertIs(run.call_args.kwargs['descriptions'], collected)

    def test_live_config_boards_all_parse(self):
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).resolve().parent.parent / 'setup' / 'search.json').read_text())
        for entry in ((cfg.get('sources') or {}).get('ashby') or {}).get('boards') or []:
            board_slug(entry)


if __name__ == '__main__':
    unittest.main()
