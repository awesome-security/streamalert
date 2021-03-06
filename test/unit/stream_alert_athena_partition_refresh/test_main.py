'''
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

# command: nosetests -v -s test/unit/
# specific test: nosetests -v -s test/unit/file.py:TestStreamPayload.test_name
import json
from collections import namedtuple
from datetime import datetime

import boto3
from mock import patch, MagicMock
from moto import mock_sqs
from nose.tools import (
    assert_equal,
    raises,
    nottest,
    assert_true,
    assert_false
)

from stream_alert.athena_partition_refresh.main import (
    _backoff_handler,
    _load_config,
    _success_handler,
    handler,
    StreamAlertAthenaClient,
    StreamAlertSQSClient,
    ConfigError,
    AthenaPartitionRefreshError
)
from unit.helpers.base import mock_open
from unit.helpers.aws_mocks import MockAthenaClient

GLOBAL_FILE = 'conf/global.json'
LAMBDA_FILE = 'conf/lambda.json'
TEST_REGION = 'us-east-2'

CONFIG_DATA = {
    'global': {
        'account': {
            'aws_account_id': '111111111111',
            'kms_key_alias': 'stream_alert_secrets',
            'prefix': 'unit-testing',
            'region': TEST_REGION
        },
        'terraform': {
            'tfstate_bucket': 'unit-testing.streamalert.terraform.state',
            'tfstate_s3_key': 'stream_alert_state/terraform.tfstate',
            'tfvars': 'terraform.tfvars'
        },
        'infrastructure': {
            'monitoring': {
                'create_sns_topic': True
            }
        }
    },
    'lambda': {
        'alert_processor_config': {
            'handler': 'stream_alert.alert_processor.main.handler',
            'source_bucket': 'unit-testing.streamalert.source',
            'source_current_hash': '<auto_generated>',
            'source_object_key': '<auto_generated>',
            'third_party_libraries': []
        },
        'rule_processor_config': {
            'handler': 'stream_alert.rule_processor.main.handler',
            'source_bucket': 'unit-testing.streamalert.source',
            'source_current_hash': '<auto_generated>',
            'source_object_key': '<auto_generated>',
            'third_party_libraries': [
                'jsonpath_rw',
                'netaddr'
            ]
        },
        'athena_partition_refresh_config': {
            "enabled": True,
            "refresh_type": {
              "repair_hive_table": {
                "unit-testing.streamalerts": "alerts"
              },
              "add_hive_partition": {}
            },
            "handler": "main.handler",
            "timeout": "60",
            "memory": "128",
            "source_bucket": "unit-testing.streamalert.source",
            "source_current_hash": "<auto_generated>",
            "source_object_key": "<auto_generated",
            "third_party_libraries": [
              "backoff"
            ]
        }
    }
}


class TestStreamAlertAthenaGlobals(object):
    """Test class for global functions in Athena Partition Refresh"""
    @raises(ConfigError)
    def test_invalid_json_config(self):
        """Athena - Load Invalid Config"""
        invalid_config_data = 'This is not JSON!!!'
        with mock_open(LAMBDA_FILE, invalid_config_data):
            with mock_open(GLOBAL_FILE, invalid_config_data):
                _load_config()


    @raises(ConfigError)
    def test_invalid_missing_config(self):
        """Athena - Load Missing Config File"""
        invalid_config_data = 'test'
        with mock_open(LAMBDA_FILE, invalid_config_data):
            with mock_open(GLOBAL_FILE, invalid_config_data):
                with patch('os.path.exists') as mock_exists:
                    mock_exists.return_value = False
                    _load_config()


    def test_load_valid_config(self):
        """Athena - Load Config"""
        global_contents = json.dumps(CONFIG_DATA['global'], indent=4)
        lambda_contents = json.dumps(CONFIG_DATA['lambda'], indent=4)

        with mock_open(GLOBAL_FILE, global_contents):
            with mock_open(LAMBDA_FILE, lambda_contents):
                config = _load_config()

                assert_equal(type(config), dict)
                assert_equal(set(config), {'global', 'lambda'})


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_backoff_and_success_handlers(self, mock_logging):
        """Athena - Backoff Handlers"""
        def backoff():
            pass
        _backoff_handler({'wait': 1.0, 'tries': 3, 'target': backoff})
        assert_true(mock_logging.debug.called)

        _success_handler({'tries': 3, 'target': backoff})
        assert_true(mock_logging.debug.called)

    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    @patch('stream_alert.athena_partition_refresh.main._load_config',
           return_value=CONFIG_DATA)
    @patch('stream_alert.athena_partition_refresh.main.StreamAlertSQSClient')
    @mock_sqs
    def test_handler_no_received_messages(self, mock_sqs_client, mock_config, mock_logging):
        """Athena - Handler - No Receieved Messages"""
        test_sqs_client = TestStreamAlertSQSClient()
        test_sqs_client.setup()
        mock_sqs_client.return_value.received_messages = []

        handler(None, None)

        mock_config.assert_called()
        mock_logging.info.assert_called_with('No messages recieved, exiting')


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    @patch('stream_alert.athena_partition_refresh.main._load_config',
           return_value=CONFIG_DATA)
    @patch('stream_alert.athena_partition_refresh.main.StreamAlertSQSClient.unique_buckets_from_messages',
           return_value=set())
    @mock_sqs
    def test_handler_no_unique_buckets(self, _, mock_config, mock_logging):
        """Athena - Handler - No Unique Buckets"""
        test_sqs_client = TestStreamAlertSQSClient()
        test_sqs_client.setup()

        handler(None, None)

        mock_config.assert_called()
        mock_logging.error.assert_called_with('No s3 buckets to refresh, exiting')


@mock_sqs
class TestStreamAlertSQSClient(object):
    """Test class for StreamAlertSQSClient"""

    def setup(self):
        sqs = boto3.resource('sqs', region_name=TEST_REGION)
        self.queue = sqs.create_queue(QueueName=StreamAlertSQSClient.QUEUENAME)
        self.client = StreamAlertSQSClient(CONFIG_DATA)

        # Create a fake s3 notification message to send
        bucket = 'unit-testing.streamalerts'
        test_s3_notificaiton = {
          'Records': [
            {
              'eventVersion': '2.0',
              'eventSource': 'aws:s3',
              'awsRegion': 'us-east-1',
              'eventTime': '2017-08-07T18:26:30.956Z',
              'eventName': 'ObjectCreated:Put',
              'userIdentity': {
                'principalId': 'AWS:AAAAAAAAAAAAAAA'
              },
              'requestParameters': {
                'sourceIPAddress': '127.0.0.1'
              },
              'responseElements': {
                'x-amz-request-id': 'FOO',
                'x-amz-id-2': 'BAR'
              },
              's3': {
                's3SchemaVersion': '1.0',
                'configurationId': 'queue',
                'bucket': {
                  'name': bucket,
                  'ownerIdentity': {
                    'principalId': 'AAAAAAAAAAAAAAA'
                  },
                  'arn': 'arn:aws:s3:::{}'.format(bucket)
                },
                'object': {
                  'key': 'alerts/foo.json',
                  'size': 1494,
                  'eTag': '12214134141431431',
                  'versionId': 'asdfasdfasdf.dfadCJkj1',
                  'sequencer': '1212312321312321321'
                }
              }
            }
          ]
        }
        self.queue.send_message(MessageBody=json.dumps(test_s3_notificaiton),
                                QueueUrl=self.client.athena_sqs_url)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_delete_messages(self, mock_logging):
        """Athena SQS - Delete Messages - No Receieved Messages"""
        self.client.delete_messages()

        assert_true(mock_logging.error.called)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_delete_messages(self, mock_logging):
        """Athena SQS - Delete Messages"""
        self.client.get_messages()
        self.client.unique_buckets_from_messages()
        self.client.delete_messages()

        assert_true(mock_logging.info.called)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_get_messages(self, mock_logging):
        """Athena SQS - Get Messages - Valid"""
        self.client.get_messages()

        assert_equal(len(self.client.received_messages), 1)
        assert_true(mock_logging.info.called)


    def test_unique_buckets_from_messages(self):
        """Athena SQS - Get Unique Bucket Ids"""
        self.client.get_messages()
        unique_buckets = self.client.unique_buckets_from_messages()

        assert_equal(unique_buckets, {'unit-testing.streamalerts'})
        assert_equal(len(self.client.processed_messages), 1)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_unique_buckets_invalid_sqs(self, mock_logging):
        """Athena SQS - Unique Buckets - Invalid SQS Message"""
        self.client.received_messages = ['wrong-format-test']
        unique_buckets = self.client.unique_buckets_from_messages()

        assert_false(unique_buckets)
        assert_true(mock_logging.error.called)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_unique_buckets_invalid_record(self, mock_logging):
        """Athena SQS - Unique Buckets - Missing Records Key in SQS Message"""
        self.client.received_messages = [{'Body': '{"missing-key": 1}'}]
        unique_buckets = self.client.unique_buckets_from_messages()

        assert_false(unique_buckets)
        assert_true(mock_logging.error.called)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_unique_buckets_non_s3_notification(self, mock_logging):
        """Athena SQS - Unique Buckets - Non S3 Notification"""
        self.client.received_messages = [{'Body': '{"Records": [{"kinesis": 1}]}'}]
        unique_buckets = self.client.unique_buckets_from_messages()

        assert_false(unique_buckets)
        assert_true(mock_logging.info.called)
        assert_true(mock_logging.debug.called)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_unique_buckets_no_mesages(self, mock_logging):
        """Athena SQS - Unique Buckets - No Receieved Messages"""
        self.client.received_messages = []
        unique_buckets = self.client.unique_buckets_from_messages()

        assert_equal(unique_buckets, None)
        assert_true(mock_logging.error.called)


class TestStreamAlertAthenaClient(object):
    """Test class for StreamAlertAthenaClient"""
    def setup(self):
        self.client = StreamAlertAthenaClient(CONFIG_DATA,
                                              results_key_prefix='unit-testing')


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    @raises(NotImplementedError)
    def test_add_hive_partition(self, mock_logging):
        """Athena - Add Hive Partition"""
        self.client.add_hive_partition(None)

        assert_true(mock_logging.error.called)


    def test_check_table_exists(self):
        """Athena - Check Table Exists"""
        query_result = [{'alerts': True}]
        self.client.athena_client = MockAthenaClient(results=query_result)

        result = self.client.check_table_exists('unit-test')
        assert_true(result)

        generated_results_key = 'unit-testing/{}'.format(datetime.now().strftime('%Y/%m/%d'))
        assert_equal(self.client.athena_results_key, generated_results_key)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_check_table_exists_invalid(self, mock_logging):
        """Athena - Check Table Exists - Does Not Exist"""
        query_result = None
        self.client.athena_client = MockAthenaClient(results=query_result)

        result = self.client.check_table_exists('unit-test')
        assert_false(result)
        assert_true(mock_logging.info.called)


    def test_check_database_exists_invalid(self):
        """Athena - Check Database Exists - Does Not Exist"""
        query_result = None
        self.client.athena_client = MockAthenaClient(results=query_result)

        assert_false(self.client.check_database_exists())


    def test_check_database_exists(self):
        """Athena - Check Database Exists"""
        query_result = [{'streamalert': True}]
        self.client.athena_client = MockAthenaClient(results=query_result)

        assert_true(self.client.check_database_exists())


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_run_athena_query_empty(self, mock_logging):
        """Athena - Run Athena Query - Empty Result"""
        query_result = None
        self.client.athena_client = MockAthenaClient(results=query_result)

        query_success, query_results = self.client.run_athena_query(
            query='SHOW DATABASES;'
        )

        assert_true(query_success)
        assert_equal(query_results['ResultSet']['Rows'], [])
        assert_true(mock_logging.debug.called)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_run_athena_query_error(self, mock_logging):
        """Athena - Run Athena Query - Error Result"""
        self.client.athena_client = MockAthenaClient(results=None, result_state='FAILED')

        query_success, query_results = self.client.run_athena_query(
            query='SHOW DATABASES;'
        )

        assert_true(mock_logging.error.called)
        assert_false(query_success)
        assert_equal(query_results, {})


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_repair_hive_table_unknown_bucket(self, mock_logging):
        """Athena - Repair Hive Table - Unknown Bucket"""
        self.client.athena_client = MockAthenaClient(result_state='SUCCEEDED')

        # This bucket is not in our `repair_hive_table` config map
        self.client.repair_hive_table({'my-test.result.bucket'})
        assert_true(mock_logging.warning.called)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_repair_hive_table_failed_refresh(self, mock_logging):
        """Athena - Repair Hive Table - Failed Refresh"""
        self.client.athena_client = MockAthenaClient(result_state='FAILED')

        # This bucket is not in our `repair_hive_table` config map
        self.client.repair_hive_table({'unit-testing.streamalerts'})
        assert_true(mock_logging.error.called)


    @patch('stream_alert.athena_partition_refresh.main.LOGGER')
    def test_repair_hive_table(self, mock_logging):
        """Athena - Repair Hive Table"""
        query_result = [{'Status': 'SUCCEEDED'}]
        self.client.athena_client = MockAthenaClient(results=query_result)

        self.client.repair_hive_table({'unit-testing.streamalerts'})
        assert_true(mock_logging.info.called)


    def test_run_athena_query(self):
        """Athena - Run Athena Query - Normal Result"""
        self.client.athena_client = MockAthenaClient()

        query_success, query_results = self.client.run_athena_query(
            query='SHOW DATABASES;'
        )

        assert_true(query_success)
        assert_equal(query_results['ResultSet']['Rows'], [{'Data': [{'test':'test'}]}])
