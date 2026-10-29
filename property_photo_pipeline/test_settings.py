import os
import unittest
from unittest.mock import patch

import pipeline


class SettingsTests(unittest.TestCase):
    def test_project_delivery_settings_need_no_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = pipeline.r2_settings()
        self.assertEqual(settings['bucket_name'], 'graden-property-photos')
        self.assertEqual(settings['public_base_url'], 'https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev')
        self.assertTrue(settings['endpoint_url'].endswith('.r2.cloudflarestorage.com'))
        self.assertEqual(set(settings), {'endpoint_url', 'bucket_name', 'public_base_url'})

    def test_runtime_settings_override_project_defaults(self):
        with patch.dict(os.environ, {'R2_BUCKET_NAME': 'test-bucket', 'R2_PUBLIC_BASE_URL': 'https://images.example.com'}):
            settings = pipeline.r2_settings()
        self.assertEqual(settings['bucket_name'], 'test-bucket')
        self.assertEqual(settings['public_base_url'], 'https://images.example.com')


if __name__ == '__main__':
    unittest.main()
