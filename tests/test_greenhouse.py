import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from jobFilter.greenhouse import (MAX_BYTES, board_slug, fetch_greenhouse,
                                  is_work_model_only, parse_board)


class BoardSlugTests(unittest.TestCase):
    def test_accepts_a_bare_slug_and_every_documented_url_shape(self):
        for entry in ('anthropic',
                      {'slug': 'anthropic'},
                      {'api': 'https://boards-api.greenhouse.io/v1/boards/anthropic/jobs'},
                      {'api': 'https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true'},
                      {'careers_url': 'https://job-boards.greenhouse.io/anthropic'},
                      {'careers_url': 'https://job-boards.eu.greenhouse.io/anthropic'},
                      {'careers_url': 'https://boards.greenhouse.io/anthropic/jobs/5456619008'},
                      {'slug': 'job-boards.greenhouse.io/anthropic'}):
            self.assertEqual(board_slug(entry), 'anthropic', entry)

    def test_refuses_a_url_outside_greenhouse(self):
        """The board list is data copied from elsewhere; an unchecked api field
        would make the scanner fetch an arbitrary URL."""
        for bad in ('https://evil.example.com/v1/boards/anthropic/jobs',
                    'https://greenhouse.io.evil.example/anthropic',
                    'http://169.254.169.254/latest/meta-data/',
                    'file:///etc/passwd',
                    'https://boards-api.greenhouse.io.evil/v1/boards/x/jobs'):
            with self.assertRaises(ValueError, msg=bad):
                board_slug({'api': bad})

    def test_refuses_a_malformed_or_missing_token(self):
        for bad in ({}, {'slug': ''}, {'slug': '   '}, {'slug': 'has space'},
                    {'slug': '../../etc/passwd'}, {'slug': 'a' * 120}, 42, None):
            with self.assertRaises(ValueError, msg=bad):
                board_slug(bad)


class WorkModelTests(unittest.TestCase):
    def test_a_work_model_with_no_geography(self):
        for name in ('Remote', 'hybrid', 'On-Site', 'Distributed; Hybrid', 'Remote | Flexible'):
            self.assertTrue(is_work_model_only(name), name)

    def test_anything_naming_a_place_is_left_alone(self):
        for name in ('Remote (Canada)', 'Hybrid - London', 'San Francisco, CA', 'Remote US'):
            self.assertFalse(is_work_model_only(name), name)

    def test_non_strings_and_blanks(self):
        for name in (None, 42, '', '   ', ';;'):
            self.assertFalse(is_work_model_only(name))


class ParseBoardTests(unittest.TestCase):
    NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)

    def row(self, **changes):
        # Field names and shapes taken from a live boards-api response
        # (anthropic, 2026-11-23).
        return {'id': 5456619008, 'title': 'Software Engineer, New Grad',
                'company_name': 'Anthropic',
                'absolute_url': 'https://job-boards.greenhouse.io/anthropic/jobs/5456619008',
                'location': {'name': 'San Francisco, CA | Seattle, WA'},
                'first_published': '2026-11-21T11:17:24-04:00',
                'updated_at': '2026-11-22T09:00:00-04:00',
                'requisition_id': 'REQ-26-1145-01', 'internal_job_id': 7,
                'application_deadline': None, **changes}

    def parse(self, rows, **kw):
        kw.setdefault('now', self.NOW)
        return parse_board({'jobs': rows}, 'anthropic', company='Anthropic', **kw)

    def test_maps_the_fields_the_plain_list_carries(self):
        job, = self.parse([self.row()])
        self.assertEqual(job.id, 'greenhouse___anthropic___5456619008')
        self.assertEqual(job.via, 'greenhouse')
        self.assertEqual(job.source, 'greenhouse')
        self.assertEqual(job.company, 'Anthropic')
        self.assertEqual(job.title, 'Software Engineer, New Grad')
        self.assertEqual(job.apply_url, 'https://job-boards.greenhouse.io/anthropic/jobs/5456619008')
        self.assertEqual(job.hc_url, 'https://job-boards.greenhouse.io/anthropic')
        self.assertEqual(job.location, 'San Francisco, CA | Seattle, WA')
        self.assertEqual(job.countries, ['US'])
        self.assertEqual(job.states, ['CA', 'WA'])
        self.assertEqual(job.published_at, '2026-11-21T15:17:24+00:00')
        self.assertEqual(job.raw['requisition_id'], 'REQ-26-1145-01')

    def test_states_nothing_it_does_not_know(self):
        """Greenhouse carries no seniority, experience minimum or category; a
        company board is every department at once. Inventing any of them would
        make the rules act on a guess."""
        job, = self.parse([self.row()])
        self.assertIsNone(job.seniority)
        self.assertIsNone(job.min_yoe)
        self.assertIsNone(job.category)
        self.assertIsNone(job.visa_sponsorship)
        self.assertIsNone(job.security_clearance)

    def test_freshness_window_and_undated_rows(self):
        rows = [self.row(id=1, first_published='2026-11-21T00:00:00+00:00'),
                self.row(id=2, first_published='2026-11-01T00:00:00+00:00'),
                self.row(id=3, first_published=None, updated_at=None),
                self.row(id=4, first_published='not a date', updated_at=None)]
        kept = self.parse(rows, max_age_days=7)
        self.assertEqual([j.id.rsplit('___', 1)[1] for j in kept], ['1'])
        # With no window configured, an undated posting is still kept.
        self.assertEqual(len(self.parse(rows)), 4)

    def test_falls_back_to_updated_at_when_never_published_field(self):
        job, = self.parse([self.row(first_published=None)])
        self.assertEqual(job.published_at, '2026-11-22T13:00:00+00:00')

    def test_work_model_only_location_is_placed_as_remote(self):
        job, = self.parse([self.row(location={'name': 'Remote'})])
        self.assertEqual(job.workplace_type, 'Remote')
        self.assertEqual(job.countries, ['US'])
        self.assertEqual(job.states, [])

    def test_hybrid_only_location_names_no_country(self):
        job, = self.parse([self.row(location={'name': 'Hybrid'})])
        self.assertEqual(job.countries, [])

    def test_non_us_board_locations(self):
        job, = self.parse([self.row(location={'name': 'London, United Kingdom'})])
        self.assertEqual(job.countries, ['GB'])
        self.assertEqual(job.states, [])

    def test_skips_rows_it_cannot_identify(self):
        rows = [None, 'nope', {}, self.row(id=None), self.row(title=''), self.row(title=None),
                self.row(absolute_url=None), self.row(absolute_url='javascript:alert(1)'),
                self.row(absolute_url='https://evil.example.com/anthropic/jobs/1'),
                self.row(location='not an object'), self.row()]
        kept = self.parse(rows)
        self.assertEqual(len(kept), 2)                      # the string location row, and the good one
        self.assertIsNone(kept[0].location)

    def test_a_malformed_payload_is_empty_not_an_error(self):
        for payload in (None, {}, {'jobs': None}, {'jobs': 'x'}, [], 'x'):
            self.assertEqual(parse_board(payload, 'anthropic'), [])

    def test_content_true_is_never_requested(self):
        """The plain list is 0.45 MB for 632 jobs where content=true is 9.57 MB;
        descriptions.py fetches one body on demand instead."""
        from jobFilter.greenhouse import API_URL
        self.assertNotIn('content', API_URL)


class FetchGreenhouseTests(unittest.TestCase):
    def test_one_broken_board_does_not_stop_the_others(self):
        calls, logs = [], []

        def fake_fetch(slug, timeout=60):
            calls.append(slug)
            if slug == 'broken':
                raise RuntimeError('502')
            return {'jobs': [{'id': 1, 'title': 'Software Engineer',
                              'absolute_url': f'https://job-boards.greenhouse.io/{slug}/jobs/1',
                              'location': {'name': 'Remote'},
                              'first_published': '2026-11-21T00:00:00+00:00'}]}

        cfg = {'boards': [{'name': 'A', 'slug': 'aaa'}, {'name': 'B', 'slug': 'broken'},
                          {'name': 'C', 'slug': 'ccc'}, {'name': 'D', 'slug': 'ddd', 'enabled': False},
                          {'name': 'E', 'slug': 'has space'}]}
        with patch('jobFilter.greenhouse.fetch_board', fake_fetch), \
             patch('jobFilter.greenhouse.time.sleep'):
            jobs = fetch_greenhouse(cfg, log=logs.append)

        self.assertEqual(calls, ['aaa', 'broken', 'ccc'])    # disabled and malformed never fetched
        self.assertEqual([j.company for j in jobs], ['A', 'C'])
        self.assertTrue(any('FAILED RuntimeError' in m for m in logs))
        self.assertTrue(any('has space' in m for m in logs))

    def test_no_boards_is_a_no_op(self):
        with patch('jobFilter.greenhouse.fetch_board') as fetch:
            self.assertEqual(fetch_greenhouse({'boards': []}), [])
            self.assertEqual(fetch_greenhouse({}), [])
        fetch.assert_not_called()

    def test_oversized_response_is_refused(self):
        class Response:
            headers = {'content-length': str(MAX_BYTES + 1)}
            content = b''
            def raise_for_status(self): pass
            def json(self): raise AssertionError('must not parse an oversized body')

        from jobFilter import greenhouse
        with patch.object(greenhouse.requests, 'get', return_value=Response()):
            with self.assertRaises(ValueError):
                greenhouse.fetch_board('anthropic')

    def test_registered_as_a_source_and_off_by_default(self):
        from jobFilter.sources import DEFAULT_SOURCES, fetch_all
        self.assertFalse(DEFAULT_SOURCES['greenhouse']['enabled'])
        with patch('jobFilter.sources.fetch_greenhouse', return_value=[]) as run:
            fetch_all({'sources': {'hiringcafe': False, 'simplify': False, 'startupjobs': False,
                                   'applyguy': False, 'speedyapply': False,
                                   'greenhouse': {'enabled': True, 'boards': [{'slug': 'anthropic'}]}}})
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0]['boards'], [{'slug': 'anthropic'}])

    def test_live_config_boards_all_parse(self):
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).resolve().parent.parent / 'setup' / 'search.json').read_text())
        boards = ((cfg.get('sources') or {}).get('greenhouse') or {}).get('boards') or []
        self.assertTrue(boards, 'setup/search.json should carry a starting board list')
        for entry in boards:
            board_slug(entry)       # raises on a bad host or token


if __name__ == '__main__':
    unittest.main()
