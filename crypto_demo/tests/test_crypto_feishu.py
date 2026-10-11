"""Offline opt-in notification and business-response checks; never send messages."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from crypto_demo.tests import bootstrap
from crypto_demo import crypto_trader as base
from crypto_demo import run as entry


class FeishuTests(unittest.TestCase):
    def setUp(self):
        self.notify = patch.object(base, 'FEISHU_NOTIFY_ENABLED', True)
        self.webhook = patch.object(base, 'FEISHU_WEBHOOK_URL', 'https://bot.example.test/webhook')
        self.secret = patch.object(base, 'FEISHU_WEBHOOK_SECRET', 'offline-test-signature')
        for item in (self.notify, self.webhook, self.secret):
            item.start()
            self.addCleanup(item.stop)
        self.transport = patch.object(base.request, 'urlopen')
        self.open = self.transport.start()
        self.addCleanup(self.transport.stop)

    def response(self, body, status=200):
        response = self.open.return_value.__enter__.return_value
        response.status = status
        response.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')

    def test_signed_asset_notification_success(self):
        self.response({'code': 0, 'msg': 'success'})
        with patch.object(base.time, 'time', return_value=1700000000):
            self.assertTrue(base.send_feishu_notification('ETHUSD 开仓成功', ['方向: BUY', None]))
        request = self.open.call_args.args[0]
        payload = json.loads(request.data)
        expected = base64.b64encode(hmac.new(b'1700000000\noffline-test-signature', b'', hashlib.sha256).digest()).decode()
        self.assertEqual(payload['sign'], expected)
        self.assertEqual(payload['timestamp'], '1700000000')
        self.assertEqual(payload['content']['text'], 'ETHUSD 开仓成功\n\n方向: BUY')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(self.open.call_args.kwargs['timeout'], 10)

    def test_legacy_success_and_unsigned_notification(self):
        self.response({'StatusCode': 0, 'StatusMessage': 'success'})
        with patch.object(base, 'FEISHU_WEBHOOK_SECRET', ''):
            self.assertTrue(base.send_feishu_notification('BTCUSD 启动', []))
        self.assertNotIn('sign', json.loads(self.open.call_args.args[0].data))

    def test_disabled_and_missing_webhook_do_not_send(self):
        with patch.object(base, 'FEISHU_NOTIFY_ENABLED', False):
            self.assertFalse(base.send_feishu_notification('disabled', []))
        with patch.object(base, 'FEISHU_WEBHOOK_URL', ''):
            self.assertFalse(base.send_feishu_notification('missing', []))
        self.open.assert_not_called()

    def test_http_200_business_rejection_and_unknown_payloads(self):
        for body in ({'code': 19021, 'StatusCode': 0}, {'StatusCode': 19024}, {}, [],
                     {'code': False}, {'code': '0'}, b'not JSON'):
            with self.subTest(body=body):
                self.response(body)
                self.assertFalse(base.send_feishu_notification('ETHUSD 模拟', []))

    def test_transport_failure_never_logs_webhook_or_secret(self):
        self.open.side_effect = ValueError('https://bot.example.test/webhook offline-test-signature')
        with patch.object(base.logger, 'warning') as warning:
            self.assertFalse(base.send_feishu_notification('BTCUSD 模拟', []))
        logged = str(warning.call_args_list)
        self.assertNotIn('https://bot.example.test', logged)
        self.assertNotIn('offline-test-signature', logged)
        self.assertIn('ValueError', logged)

    def test_http_error_is_not_reported_as_delivery(self):
        self.response({'code': 0}, status=503)
        self.assertFalse(base.send_feishu_notification('BTCUSD 模拟', []))

    def test_cli_respects_private_opt_in_in_each_mode(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(entry, 'BASE', Path(folder)):
            (Path(folder)/'.env').write_text('FEISHU_NOTIFY_ENABLED=true\nFEISHU_WEBHOOK_URL=https://bot.example.test/webhook\nFEISHU_WEBHOOK_SECRET=offline-test-signature\nCRYPTO_ETH_SYMBOL=ETHUSD\n', encoding='utf-8')
            for mode in ('observe', 'dry-run', 'demo'):
                with self.subTest(mode=mode), patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(entry.configure('ETH', mode), 'ETHUSD')
                    self.assertEqual(os.environ['FEISHU_NOTIFY_ENABLED'], 'true')
                    self.assertEqual(os.environ['FEISHU_WEBHOOK_SECRET'], 'offline-test-signature')

    def test_fresh_runtime_import_enables_mock_delivery(self):
        script = '''
from crypto_demo.tests import bootstrap
import os
os.environ.update(FEISHU_NOTIFY_ENABLED='true', FEISHU_WEBHOOK_URL='https://bot.example.test/webhook')
from crypto_demo import crypto_trader as runtime
from unittest.mock import patch
assert runtime.FEISHU_NOTIFY_ENABLED is True
with patch.object(runtime.request, 'urlopen') as transport, patch.object(runtime, 'logger'):
    response = transport.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = b'{"code":0}'
    assert runtime.send_feishu_notification('ETHUSD mock startup', []) is True
    transport.assert_called_once()
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(entry.__file__).resolve().parent.parent,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_and_unconfigured_never_inherit_notification_config(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(entry, 'BASE', Path(folder)):
            (Path(folder)/'.env').write_text('FEISHU_NOTIFY_ENABLED=true\nFEISHU_WEBHOOK_URL=https://bot.example.test/webhook\n', encoding='utf-8')
            with patch.dict(os.environ, {'FEISHU_WEBHOOK_SECRET': 'ambient-test-secret'}, clear=True):
                entry.configure('ETH', 'demo', offline=True)
                self.assertEqual(os.environ['FEISHU_NOTIFY_ENABLED'], 'false')
                self.assertNotIn('FEISHU_WEBHOOK_URL', os.environ)
                self.assertNotIn('FEISHU_WEBHOOK_SECRET', os.environ)
            (Path(folder)/'.env').write_text('CRYPTO_ETH_SYMBOL=ETHUSD\n', encoding='utf-8')
            with patch.dict(os.environ, {'FEISHU_NOTIFY_ENABLED': 'true'}, clear=True):
                entry.configure('ETH', 'demo')
                self.assertEqual(os.environ['FEISHU_NOTIFY_ENABLED'], 'false')


if __name__ == '__main__':
    unittest.main()
