import pytest
import asyncio
from bacnet_console.reads import ReadScheduler
from bacnet_console.config import load_config
from bacnet_console.bacnet import ReadResult
from tests.test_batching import BatchClient

class RpmFailureClient(BatchClient):
    def __init__(self, fail_with=None):
        super().__init__()
        self.fail_with = fail_with
        self.read_multiple_calls = 0
        self.read_calls = 0

    async def read_multiple(self, address, keys):
        self.read_multiple_calls += 1
        self.batches.append(list(keys))
        if self.fail_with:
            raise self.fail_with
        return {key: ReadResult(value=71) for key in keys}

    async def read(self, address, obj, prop):
        self.read_calls += 1
        return 72

@pytest.mark.asyncio
async def test_rpm_timeout_reduces_size_but_no_fallback(config_file, monkeypatch):
    import time
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0.)
    async def sleep(delay): clock.now += delay
    monkeypatch.setattr('bacnet_console.reads.time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr('bacnet_console.reads.asyncio', SimpleNamespace(Lock=asyncio.Lock, timeout=asyncio.timeout, sleep=sleep))

    from dataclasses import replace
    client = RpmFailureClient(fail_with=TimeoutError('timeout'))
    config = load_config(config_file)
    config = replace(config, rpm_batch_size=10)
    reader = ReadScheduler(client, config)

    keys = [(f'analog-input,{i}', 'present-value') for i in range(4)]
    results = {k: result async for k, result in reader.many('192.0.2.1', keys)}

    assert len(results) == 4
    for k, result in results.items():
        if k in keys[:2]:
            assert "timeout" in result.error
        else:
            assert result.error is None
            assert result.value == 72
    # Because size drops to 1, the remaining two items are processed individually.
    # The requirement is that the failed batch doesn't *immediately* trigger fallback
    # for the entire chunk, which we verify by seeing 2 (not 4) read calls.
    assert client.read_calls == 2
    # The remaining two items hit the `if len(chunk) == 1:` which bumps fallback counts.
    assert reader.individual_fallback_reads == 2

@pytest.mark.asyncio
async def test_rpm_unsupported_causes_fallback(config_file, monkeypatch):
    import time
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0.)
    async def sleep(delay): clock.now += delay
    monkeypatch.setattr('bacnet_console.reads.time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr('bacnet_console.reads.asyncio', SimpleNamespace(Lock=asyncio.Lock, timeout=asyncio.timeout, sleep=sleep))

    client = RpmFailureClient(fail_with=ValueError('unsupported service'))
    config = load_config(config_file)
    reader = ReadScheduler(client, config)

    keys = [(f'analog-input,{i}', 'present-value') for i in range(2)]
    results = [result async for _, result in reader.many('192.0.2.1', keys)]

    assert len(results) == 2
    for result in results:
        assert result.value == 72 # got fallback value
    assert client.read_calls == 2
    assert reader.individual_fallback_reads == 2

@pytest.mark.asyncio
async def test_rpm_reject_retries_smaller_batch(config_file, monkeypatch):
    import time
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0.)
    async def sleep(delay): clock.now += delay
    monkeypatch.setattr('bacnet_console.reads.time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr('bacnet_console.reads.asyncio', SimpleNamespace(Lock=asyncio.Lock, timeout=asyncio.timeout, sleep=sleep))

    class RejectClient(BatchClient):
        def __init__(self):
            super().__init__()
            self.read_calls = 0

        async def read_multiple(self, address, keys):
            self.batches.append(list(keys))
            if len(keys) > 1: # simulate oversized reject
                raise ValueError('reject: too large')
            return {key: ReadResult(value=71) for key in keys}

        async def read(self, address, obj, prop):
            self.read_calls += 1
            return 72

    from dataclasses import replace
    client = RejectClient()
    config = load_config(config_file)
    config = replace(config, rpm_batch_size=4)
    reader = ReadScheduler(client, config)

    keys = [(f'analog-input,{i}', 'present-value') for i in range(2)]
    results = [result async for _, result in reader.many('192.0.2.1', keys)]

    assert len(results) == 2
    # It should have failed the 2-sized batch, then retried as two 1-sized batches
    # BUT since it halves the batch size *and retains it* because of `is_retrying_chunk=True`,
    # the second iteration picks up the same 2 items, notices size=1, and correctly falls back via one().
    assert len(client.batches) == 1
    assert len(client.batches[0]) == 2
    assert client.read_calls == 2 # The fallback one() calls
    for result in results:
        assert result.value == 72 # It fell back correctly

@pytest.mark.asyncio
async def test_rpm_reject_bounded_halving(config_file, monkeypatch):
    import time
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0.)
    async def sleep(delay): clock.now += delay
    monkeypatch.setattr('bacnet_console.reads.time', SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr('bacnet_console.reads.asyncio', SimpleNamespace(Lock=asyncio.Lock, timeout=asyncio.timeout, sleep=sleep))

    from bacpypes3.apdu import ErrorRejectAbortNack, RejectPDU

    class BoundedRejectClient(BatchClient):
        def __init__(self):
            super().__init__()
            self.read_calls = 0

        async def read_multiple(self, address, keys):
            self.batches.append(list(keys))
            if len(keys) > 2: # simulate oversized reject for any batch > 2
                raise RejectPDU(reason=1) # 1 is buffer-overflow, which is valid and handled as a size_error
            return {key: ReadResult(value=71) for key in keys}

        async def read(self, address, obj, prop):
            self.read_calls += 1
            return 72

    from dataclasses import replace
    client = BoundedRejectClient()
    config = load_config(config_file)
    config = replace(config, rpm_batch_size=8)
    reader = ReadScheduler(client, config)

    # Force initial batch size to 8
    state = reader._state('192.0.2.1')
    state.size = 8

    keys = [(f'analog-input,{i}', 'present-value') for i in range(8)]
    results = [result async for _, result in reader.many('192.0.2.1', keys)]

    assert len(results) == 8

    # Expected batch attempts:
    # 1. Tries 8 -> fails (apduTooLong), halves size to 4, sets is_retrying_chunk=True
    # 2. Tries 4 -> fails (apduTooLong), halves size to 2, sets is_retrying_chunk=True
    # 3. Tries 2 -> succeeds. Loop advances position by 2.
    # 4. Loop continues for the remaining 6 items. Since state.size is 2, it will request 2 at a time.
    # 5. Tries 2 -> succeeds. (4 items done). successes=2, size grows to 4!
    # 6. Tries 4 -> fails, halves size to 2, retries chunk.
    # 7. Tries 2 -> succeeds. (6 items done)
    # 8. Tries 2 -> succeeds. (8 items done)
    # Therefore, batch lengths should be: 8, 4, 2, 2, 4, 2, 2

    attempted_lengths = [len(b) for b in client.batches]
    assert attempted_lengths == [8, 4, 2, 2, 4, 2, 2]

    # No individual reads should have occurred
    assert client.read_calls == 0
    assert reader.individual_fallback_reads == 0

    # All values should be correctly fetched
    for result in results:
        assert result.value == 71
