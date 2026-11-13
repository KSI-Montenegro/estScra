"""Missing production history is an error; fresh installs require explicit opt-out."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import phone_matches


class PhoneBaselineTests(unittest.TestCase):
    def test_missing_history_still_fails_without_explicit_opt_out(self):
        with tempfile.TemporaryDirectory() as directory:
            missing=Path(directory)/'missing.json'
            with patch.dict(os.environ,{'GRADEN_SKIP_HISTORICAL_PHONE_BASELINE':'0'}),patch.object(phone_matches,'LEGACY',missing):
                with self.assertRaisesRegex(ValueError,'Historical phone baseline missing'):
                    phone_matches.legacy_records()

    def test_fresh_install_opt_out_uses_no_historical_records(self):
        with patch.dict(os.environ,{'GRADEN_SKIP_HISTORICAL_PHONE_BASELINE':'1'}),patch.object(phone_matches,'load_records') as load:
            self.assertEqual(phone_matches.legacy_records(),[])
            load.assert_not_called()

    def test_full_clone_still_loads_both_historical_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);legacy=root/'listings.json';csv=root/'qualified.csv'
            legacy.write_text('[{"property_key":"old_1"}]');csv.write_text('property_key\nold_2\n')
            with patch.dict(os.environ,{'GRADEN_SKIP_HISTORICAL_PHONE_BASELINE':'0'}),patch.object(phone_matches,'LEGACY',legacy),patch.object(phone_matches,'QUALIFIED',csv):
                self.assertEqual([r['property_key'] for r in phone_matches.legacy_records()],['old_1','old_2'])


if __name__=='__main__':unittest.main()
