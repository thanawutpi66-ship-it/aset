import pytest
from unittest.mock import MagicMock, patch
import json
import urllib.request
import urllib.error
from aset_batt.storage.cloud_push import CloudPusher, _NumpySafeEncoder

def test_numpy_safe_encoder():
    import numpy as np
    encoder = _NumpySafeEncoder()
    assert encoder.default(np.float32(1.0)) == 1.0
    assert encoder.default(np.int64(1)) == 1
    assert encoder.default(np.array([1.0, 2.0])) == [1.0, 2.0]
    assert encoder.default(np.bool_(True)) is True

@patch('aset_batt.storage.cloud_push.urllib.request.urlopen')
def test_cloud_pusher_send(mock_urlopen):
    pusher = CloudPusher('http://test.com')
    
    # Mock successful response
    mock_response = MagicMock()
    mock_response.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    with patch.object(pusher, '_poll_and_analyze') as mock_poll:
        mock_poll.return_value = {"test": "data"}
        pusher.push_once()
        mock_urlopen.assert_called_once()

@patch('aset_batt.storage.cloud_push.urllib.request.urlopen')
def test_cloud_pusher_failure(mock_urlopen):
    pusher = CloudPusher('http://test.com')
    
    # Mock failure response
    mock_urlopen.side_effect = urllib.error.URLError("Connection Refused")
    
    with patch.object(pusher, '_poll_and_analyze') as mock_poll:
        mock_poll.return_value = {"test": "data"}
        pusher.push_once()
        # Should catch error and log, but not crash
        mock_urlopen.assert_called_once()
    
def test_cloud_pusher_start_stop():
    pusher = CloudPusher('http://test.com')
    with patch('threading.Thread') as mock_thread:
        pusher.start()
        assert pusher._running is True
        mock_thread.return_value.start.assert_called_once()
        
        pusher.stop()
        assert pusher._running is False
