"""Synthetic regression evidence only; none of these tests calibrates a rig."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from calibration_run import ArenaFillsScenes, CalibrationRun, RunAborted, load_plan
from crosstalk_analysis import analyze_bundle, main, _condition, _combine
from spectral_capture import SpectralDevice
from tests.fakes import FakeCitrus, FakeSpectrometer, InProcessCitrusControl, ScriptedOperator
from tests.test_calibration_run import BASE_PLAN, arena_fills_plan, write_plan

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/calibration_plan_omnifin0_source_crosstalk.json'


def spectral_model(arenas):
    """Known asymmetric receiver x source matrix with wavelength-dependent spill."""
    wl = np.linspace(346.9, 927.3, 1044)
    model = {}
    for r, receiver in enumerate(arenas):
        model[receiver] = {}
        for s, source in enumerate(arenas):
            gain = [1, .8, .74, .93][r] if r == s else (r * 4 + s + 1) * .003
            response = gain * (.25 + np.exp(-.5 * ((wl - (500 + 30*s)) / 70)**2))
            response[[0, 1, 2, 3, 1040, 1041, 1042, 1043]] = 0
            model[receiver][source] = response
    return model


class SourceSelectionTest(unittest.TestCase):
    def test_sources_are_independent_of_receiver_and_preserve_patterns(self):
        plan = arena_fills_plan()
        citrus = FakeCitrus(serve_socket=False, present_after_polls=0)
        driver = ArenaFillsScenes(InProcessCitrusControl(citrus), plan, 'test')
        for sources in (['arena_3'], ['arena_2', 'arena_4'], []):
            pos = dict(plan['positions'][0], pattern='selected_sources', source_arena_ids=sources)
            scene = driver.show([128, 128, 128, 255], pos)
            self.assertEqual(scene['source_arena_ids'], sources)
            for arena, rgba in citrus.rgba_by_arena.items():
                self.assertEqual(rgba, [128, 128, 128, 255] if arena in sources else [0, 0, 0, 255])
            driver.show([0, 0, 0, 255], pos)
            self.assertTrue(all(v == [0, 0, 0, 255] for v in citrus.rgba_by_arena.values()))
        for pattern, expected in [('all', plan['arena_ids']), ('target_only', ['arena_1']),
                                  ('all_but_target', ['arena_2', 'arena_3', 'arena_4'])]:
            scene = driver.show([255]*4, dict(plan['positions'][0], pattern=pattern))
            self.assertEqual(scene['source_arena_ids'], expected)
        driver.finish()
        self.assertEqual(citrus.restores, 1)

    def test_selector_validation_rejects_ambiguous_or_malformed_requests(self):
        mutations = [
            {'pattern':'selected_sources'},
            {'pattern':'selected_sources', 'source_arena_ids':'arena_2'},
            {'pattern':'selected_sources', 'source_arena_ids':[None]},
            {'pattern':'selected_sources', 'source_arena_ids':['arena_9']},
            {'pattern':'selected_sources', 'source_arena_ids':['arena_2','arena_2']},
            {'pattern':'all', 'source_arena_ids':['arena_2']},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for change in mutations:
                with self.subTest(change=change):
                    plan = arena_fills_plan()
                    plan['positions'][0] = dict(arena_id='arena_1', position_id='test', **change)
                    with self.assertRaisesRegex(ValueError, 'source_arena_ids'):
                        load_plan(write_plan(tmp, plan))
            plan = copy.deepcopy(BASE_PLAN)
            plan['positions'][0].update(pattern='selected_sources', source_arena_ids=['arena_2'])
            with self.assertRaisesRegex(ValueError, 'arena_fills'):
                load_plan(write_plan(tmp, plan))
            plan = arena_fills_plan()
            plan['positions'][0].update(pattern='selected_sources', source_arena_ids=[])
            load_plan(write_plan(tmp, plan))

    def test_wrong_reported_domain_aborts_capture(self):
        plan = arena_fills_plan()
        fake = FakeCitrus(serve_socket=False, present_after_polls=0)
        control = InProcessCitrusControl(fake)
        original = control.wait_for_state
        def wrong(*args, **kwargs):
            state = original(*args, **kwargs)
            state['arena_fills']['domain'] = 'experimental_area_masked'
            return state
        control.wait_for_state = wrong
        with self.assertRaisesRegex(RunAborted, 'domain'):
            ArenaFillsScenes(control, plan, 'test').show([255]*4, plan['positions'][0])


class CrosstalkAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.plan, _ = load_plan(EXAMPLE)
        cls.plan['settle_s'] = 0
        cls.plan['scans_per_patch'] = 4
        cls.plan['stability']['max_relative_change'] = 1
        cls.model = spectral_model(cls.plan['arena_ids'])
        cls.additive = cls.capture('additive')
        cls.nonadditive = cls.capture('nonadditive', nonadditive_fraction=.2)
        cls.report = analyze_bundle(cls.additive)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def capture(cls, name, plan=None, **fake_options):
        plan = copy.deepcopy(plan or cls.plan)
        fake = FakeCitrus(serve_socket=False, present_after_polls=0)
        device = SpectralDevice(FakeSpectrometer(fake, source_spectra=cls.model,
                                follow_probe_target=True, noise_counts=.2, **fake_options), 'fake')
        run = CalibrationRun(plan, 'synthetic-plan', InProcessCitrusControl(fake), device,
                             ScriptedOperator(), cls.root, run_id=name)
        with patch('calibration_run.time.sleep'):
            result = run.run()
        assert fake.restores == 1
        device.close()
        return result

    def edited_bundle(self, name, transform):
        import shutil
        out = self.root / name
        shutil.copytree(self.additive, out)
        transform(out)
        context_path = out / 'capture_context.json'
        context = json.loads(context_path.read_text())
        # Intentional synthetic fixture mutation, not repairing real evidence.
        for entry in context['files']:
            path = out / entry['path']
            if path.exists():
                entry.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        context_path.write_text(json.dumps(context))
        return out

    @staticmethod
    def edit_measurements(out, predicate, edit):
        for path in (out / 'measurements').glob('*/measurement.json'):
            m = json.loads(path.read_text())
            if predicate(m):
                edit(m)
                path.write_text(json.dumps(m))

    def test_optional_plan_has_all_16_pairs_and_four_controls(self):
        p, _ = load_plan(EXAMPLE)
        pairs = {(x['arena_id'], x['source_arena_ids'][0]) for x in p['positions'] if x['pattern']=='selected_sources'}
        self.assertEqual(pairs, {(r,s) for r in p['arena_ids'] for s in p['arena_ids']})
        self.assertEqual(sum(x['pattern']=='all' for x in p['positions']), 4)
        self.assertEqual(p['arena_fills']['background'], 'black')
        context = json.loads((self.additive/'capture_context.json').read_text())
        self.assertEqual(sum(m['role']=='projector_black' for m in context['measurements']),20)
        self.assertEqual(sum(m['role']=='projector_white' for m in context['measurements']),100)

    def test_additive_matrix_recovers_asymmetric_spectral_truth_and_uncertainty(self):
        report = self.report
        self.assertEqual(report['matrix_axes'], ['receiver', 'source'])
        self.assertEqual(report['evidence_kind'], 'synthetic_software_validation')
        self.assertEqual(len(report['groups']),1)
        g = report['groups'][0]
        self.assertEqual(g['status'], 'consistent_with_additivity')
        self.assertTrue(g['linear_model_supported_for_tested_conditions'])
        self.assertFalse(report['excluded_measurements'])
        for r, receiver in enumerate(report['arena_order']):
            for s, source in enumerate(report['arena_order']):
                est = g['single_source_response_matrix'][r][s]
                expected = self.model[receiver][source] * 400000
                np.testing.assert_allclose(est['counts_per_s'], expected, atol=6, rtol=0)
                self.assertGreater(np.max(est['standard_error']), 0)
                self.assertEqual(len(est['measurement_ids']),3)
                self.assertEqual(len(est['dark_measurement_ids']),1)
                px = 400
                ratio = g['receiver_normalized_matrix'][r][s]['ratio_to_receiver_own_source'][px]
                self.assertAlmostEqual(ratio, expected[px] / (self.model[receiver][receiver][px]*400000), delta=.0001)

    def test_nonadditive_control_fails_without_changing_single_source_estimates(self):
        g = analyze_bundle(self.nonadditive)['groups'][0]
        self.assertEqual(g['status'],'nonadditive')
        self.assertFalse(g['linear_model_supported_for_tested_conditions'])
        self.assertTrue(all(v['status']=='nonadditive' for v in g['additivity_by_receiver'].values()))
        self.assertEqual(g['single_source_response_matrix'][0][1],self.report['groups'][0]['single_source_response_matrix'][0][1])

    def test_shared_dark_uncertainty_does_not_shrink_with_lit_repeats(self):
        stats = {m:{'mean':np.array([v]), 'variance':np.array([var])}
                 for m,v,var in [('a',11,1),('b',13,1),('dark',10,9)]}
        key=('p','arena_1',100,'domain','plane',(0,0),None)
        records=[dict(measurement_id=m,position_id='p',arena_id='arena_1',integration_us=100,
                      _domain='domain',target_plane='plane',offset_mm=[0,0]) for m in ('a','b')]
        est=_condition(records,{key:['dark']},stats)
        np.testing.assert_allclose(est['mean'],[2])
        np.testing.assert_allclose(est['variance'],[9.5])
        a,b=[_condition([m],{key:['dark']},stats) for m in records]
        delta=_combine([a,b],[1,-1],stats)
        np.testing.assert_allclose(delta['variance'],[2])  # shared dark cancels

    def test_missing_source_dark_and_all_on_are_insufficient(self):
        predicates = {
            'missing-source': lambda m:m.get('position_id')=='arena_2_center_from_arena_3',
            'missing-dark': lambda m:m['role']=='projector_black' and m['arena_id']=='arena_2',
            'missing-all': lambda m:m.get('position_id')=='arena_2_center_all',
        }
        for name,pred in predicates.items():
            with self.subTest(name=name):
                def remove(out):
                    for path in (out/'measurements').glob('*/measurement.json'):
                        if pred(json.loads(path.read_text())):
                            path.unlink()
                report=analyze_bundle(self.edited_bundle(name,remove))
                self.assertEqual(report['groups'][0]['status'],'insufficient_evidence')
                self.assertTrue(report['excluded_measurements'])

    def test_incompatible_data_is_reported_not_pooled(self):
        changes = {
            'background': lambda m:m['scene']['status']['arena_fills'].update(background_rgba_u8=[255]*4),
            'saturated': lambda m:m.update(any_saturated=True),
            'unstable': lambda m:m['stability'].update(stable=False),
            'correction': lambda m:m.update(dark_correction_applied=True),
            'domain': lambda m:m['scene']['status']['arena_fills'].update(domain='experimental_area_masked'),
        }
        for name,edit in changes.items():
            with self.subTest(name=name):
                out=self.edited_bundle(name,lambda o:self.edit_measurements(o,lambda m:m['arena_id']=='arena_1',edit))
                report=analyze_bundle(out)
                self.assertEqual(report['groups'][0]['status'],'insufficient_evidence')
                self.assertIsNone(report['groups'][0]['single_source_response_matrix'][0][0])
                self.assertTrue(report['excluded_measurements'])

    def test_different_exposures_form_incomplete_separate_groups(self):
        def edit(m):
            m['integration_us']=200000
            for s in m['scans']: s['integration_us']=200000
            if m['role']=='projector_black': m['paired_dark_for_integration_us']=200000
        out=self.edited_bundle('exposures',lambda o:self.edit_measurements(o,lambda m:m['position_id'].endswith('_all'),edit))
        report=analyze_bundle(out)
        self.assertEqual(len(report['groups']),2)
        self.assertTrue(all(g['status']=='insufficient_evidence' for g in report['groups']))

    def test_wrong_wavelength_grid_is_excluded(self):
        def edit(out):
            path=next((out/'measurements').glob('arena_1_center_from_arena_2__white*/spectrum.csv'))
            a=np.loadtxt(path,delimiter=',',skiprows=1)
            a[:,1]+=1
            header=path.read_text().splitlines()[0]
            np.savetxt(path,a,delimiter=',',header=header,comments='')
        report=analyze_bundle(self.edited_bundle('grid',edit))
        self.assertTrue(any('wavelength' in e['reason'] for e in report['excluded_measurements']))

    def test_changed_projected_regions_are_not_pooled(self):
        def edit(m):
            m['scene']['status']['arena_fills']['arenas'][0]['region']['width_px']=400
        out=self.edited_bundle('regions',lambda o:self.edit_measurements(o,lambda m:m['position_id'].endswith('_all'),edit))
        report=analyze_bundle(out)
        self.assertEqual(len(report['groups']),2)
        self.assertTrue(all(g['status']=='insufficient_evidence' for g in report['groups']))

    def test_incomplete_run_cannot_support_linear_model(self):
        def edit(out):
            path=out/'capture_context.json'
            context=json.loads(path.read_text())
            context.update(outcome='interrupted',error='synthetic interrupt')
            path.write_text(json.dumps(context))
        report=analyze_bundle(self.edited_bundle('partial',edit))
        self.assertEqual(report['groups'][0]['status'],'insufficient_evidence')
        self.assertFalse(report['groups'][0]['linear_model_supported_for_tested_conditions'])

    def test_negative_responses_and_weak_denominators_are_not_forced_positive(self):
        def edit(out):
            for path in (out/'measurements').glob('arena_1_center_from_arena_1__white*/spectrum.csv'):
                a=np.loadtxt(path,delimiter=',',skiprows=1)
                a[:,2:]=1000-(a[:,2:]-1000)
                header=path.read_text().splitlines()[0]
                np.savetxt(path,a,delimiter=',',header=header,comments='')
        report=analyze_bundle(self.edited_bundle('negative',edit))
        group=report['groups'][0]
        self.assertEqual(group['status'],'insufficient_evidence')
        self.assertLess(group['single_source_response_matrix'][0][0]['counts_per_s'][400],0)
        self.assertIsNone(group['receiver_normalized_matrix'][0][1]['ratio_to_receiver_own_source'][400])

    def test_one_scan_keeps_estimates_but_no_uncertainty_claim(self):
        plan=copy.deepcopy(self.plan)
        plan['scans_per_patch']=1
        report=analyze_bundle(self.capture('one-scan',plan))
        self.assertEqual(report['groups'][0]['status'],'insufficient_evidence')
        self.assertIsNone(report['groups'][0]['single_source_response_matrix'][0][0]['standard_error'])

    def test_noisy_measurements_are_inconclusive_not_automatically_additive(self):
        def edit(out):
            for path in (out/'measurements').glob('*/spectrum.csv'):
                a=np.loadtxt(path,delimiter=',',skiprows=1)
                a[:,2:]+=np.array([-4000,4000,-4000,4000])
                header=path.read_text().splitlines()[0]
                np.savetxt(path,a,delimiter=',',header=header,comments='')
        report=analyze_bundle(self.edited_bundle('noisy',edit))
        self.assertEqual(report['groups'][0]['status'],'insufficient_evidence')

    def test_detected_drift_blocks_a_linear_model_claim(self):
        def edit(out):
            for path in (out/'measurements').glob('*__drift_reference_end__*/spectrum.csv'):
                a=np.loadtxt(path,delimiter=',',skiprows=1)
                a[:,2:]=1000+(a[:,2:]-1000)*1.3
                header=path.read_text().splitlines()[0]
                np.savetxt(path,a,delimiter=',',header=header,comments='')
        report=analyze_bundle(self.edited_bundle('drift',edit))
        self.assertEqual(report['groups'][0]['status'],'insufficient_evidence')
        self.assertTrue(any(v['status']=='drift_detected' for v in report['drift_checks_by_position'].values()))

    def test_legacy_aggregate_plan_cannot_identify_four_arena_matrix(self):
        plan=arena_fills_plan()
        plan['integration'].update(min_ms=100,max_ms=100,initial_ms=100)
        report=analyze_bundle(self.capture('legacy',plan))
        self.assertEqual(report['groups'][0]['status'],'insufficient_evidence')
        self.assertIsNone(report['groups'][0]['single_source_response_matrix'][0][1])

    def test_report_cli_preserves_bundle_and_refuses_overwrite(self):
        before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.additive.rglob('*') if p.is_file()}
        out=self.root/'analysis.json'
        self.assertEqual(main([str(self.additive),'--out',str(out)]),0)
        self.assertEqual(main([str(self.additive),'--out',str(out)]),2)
        with self.assertRaises(SystemExit):
            main([str(self.additive),'--out',str(self.additive/'analysis.json')])
        after={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.additive.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        self.assertTrue(json.loads(out.read_text())['groups'])


if __name__ == '__main__':
    unittest.main()
