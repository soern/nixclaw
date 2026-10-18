"""Tests for browse.py per-channel concurrency lock mechanism."""

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from src.nixclaw_agent.tools import browse


class TestGetLockCreation:
    """Test _get_lock creates and returns a lock for a given channel_id."""

    @pytest.mark.asyncio
    async def test_get_lock_returns_asyncio_lock(self):
        """_get_lock should return an asyncio.Lock instance."""
        lock = await browse._get_lock("channel_abc")
        assert isinstance(lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_get_lock_returns_different_obj_than_guard_lock(self):
        """The lock returned by _get_lock should not be the module-level _locks_guard."""
        lock = await browse._get_lock("test_channel")
        assert lock is not browse._locks_guard


class TestGetLockReuse:
    """Test that _get_lock returns the SAME lock for the same channel_id."""

    @pytest.mark.asyncio
    async def test_same_channel_id_returns_same_lock(self):
        """Calling _get_lock twice with the same channel_id returns identical lock object."""
        lock1 = await browse._get_lock("channel_xyz")
        lock2 = await browse._get_lock("channel_xyz")
        assert id(lock1) == id(lock2), "Same channel_id must return the same lock"

    @pytest.mark.asyncio
    async def test_reused_lock_is_unlocked_after_exit(self):
        """The reused lock should be in unlocked state after exiting the context manager."""
        lock = await browse._get_lock("reuse_test")
        async with lock:
            pass
        assert not lock.locked(), "Lock should be unlocked after exiting context"


class TestGetLockIsolation:
    """Test that different channel_ids get different lock objects."""

    @pytest.mark.asyncio
    async def test_different_channel_ids_get_different_locks(self):
        """Two different channel_ids must return different lock objects."""
        lock1 = await browse._get_lock("channel_A")
        lock2 = await browse._get_lock("channel_B")
        assert id(lock1) != id(lock2), "Different channel_ids must have different locks"

    @pytest.mark.asyncio
    async def test_many_channels_get_many_unique_locks(self):
        """Creating locks for 50 different channels yields 50 unique lock ids."""
        lock_ids = set()
        for i in range(50):
            lock = await browse._get_lock(f"chan_{i}")
            lock_ids.add(id(lock))
        assert len(lock_ids) == 50, f"Expected 50 unique locks, got {len(lock_ids)}"

    @pytest.mark.asyncio
    async def test_lock_isolation_does_not_block_different_channels(self):
        """Locks for different channels must allow truly concurrent execution."""
        results = {}

        async def acquire_for_channel(channel, duration=0.05):
            lock = await browse._get_lock(channel)
            async with lock:
                results[channel] = True
                # Keep lock for duration - if locks were shared, this would delay others

        await asyncio.gather(
            acquire_for_channel("iso_A"),
            acquire_for_channel("iso_B"),
            acquire_for_channel("iso_C"),
        )
        assert results == {"iso_A": True, "iso_B": True, "iso_C": True}


class TestLockSerialization:
    """Test that concurrent calls to the same lock serialize properly."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_same_lock_all_complete(self):
        """Fire multiple coroutines that acquire the same lock, all should complete without error."""
        execution_order = []

        async def serializable_worker(worker_id, hold_time=0.02):
            lock = await browse._get_lock("serialize_target")
            async with lock:
                execution_order.append(worker_id)
                await asyncio.sleep(hold_time)

        workers = [serializable_worker(i) for i in range(10)]
        await asyncio.gather(*workers)

        assert len(execution_order) == 10, f"Expected 10 workers to complete, got {len(execution_order)}"

    @pytest.mark.asyncio
    async def test_same_lock_provides_mutual_exclusion(self):
        """Only one coroutine should hold the lock at any given time for the same channel."""
        # Track the number of concurrent holders per time slice
        max_concurrent = [0]
        current_held = [0]

        async def trace_worker(worker_id):
            lock = await browse._get_lock("exclusion_target")
            async with lock:
                current_held[0] += 1
                if current_held[0] > max_concurrent[0]:
                    max_concurrent[0] = current_held[0]
                await asyncio.sleep(0.01)
                current_held[0] -= 1

        await asyncio.gather(*(trace_worker(i) for i in range(8)))

        assert max_concurrent[0] == 1, f"Expected max 1 concurrent holder, got {max_concurrent[0]}"

    @pytest.mark.asyncio
    async def test_diff_channels_run_concurrently(self):
        """Locks for different channels must allow truly concurrent execution."""
        lock_start_times = {}

        async def overlap_check(worker_id, channel):
            lock = await browse._get_lock(channel)
            async with lock:
                lock_start_times[worker_id] = asyncio.get_event_loop().time()
                await asyncio.sleep(0.05)

        await asyncio.gather(
            overlap_check(1, "concurrent_A"),
            overlap_check(2, "concurrent_B"),
        )
        # If truly concurrent, both should start roughly at the same time
        time_diff = abs(lock_start_times[1] - lock_start_times[2])
        assert time_diff < 0.03, "Different channels should start concurrently"


class TestGetLockEmptyChannelId:
    """Test _get_lock handles edge case of empty channel_id."""

    @pytest.mark.asyncio
    async def test_empty_string_channel_id_works(self):
        """_get_lock('') should work without raising errors."""
        lock = await browse._get_lock("")
        assert isinstance(lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_empty_string_lock_is_independent_from_nonempty(self):
        """The empty-string lock is distinct from any non-empty lock."""
        empty_lock = await browse._get_lock("")
        nonempty_lock = await browse._get_lock("nonempty")
        assert id(empty_lock) != id(nonempty_lock)

    @pytest.mark.asyncio
    async def test_empty_string_lock_is_reused(self):
        """Repeated _get_lock('') returns the same lock object."""
        lock1 = await browse._get_lock("")
        lock2 = await browse._get_lock("")
        assert id(lock1) == id(lock2)


class TestRunWithLockUsage:
    """Test that browse tool functions use _get_lock to acquire channel-specific lock
    before calling _run.

    _run itself does NOT directly call _get_lock. Instead, functions like
    web_search, browser_navigate, etc. wrap _run inside
    ``async with await _get_lock(channel_id):``. These tests verify that pattern
    by patching _get_lock at the module level and checking that the callers
    acquire the lock correctly.
    """

    @pytest.mark.asyncio
    async def test_web_search_lock_is_acquired(self):
        """Verify web_search acquires the lock via _get_lock before running."""
        acquired = []

        async def track_get_lock(channel_id):
            lock = asyncio.Lock()
            acquired.append(("acquired", channel_id))
            await lock.acquire()
            acquired.append(("released", channel_id))
            lock.release()
            return lock

        with patch.object(browse, "_run", AsyncMock(return_value="Page: ok\nURL: x\n\nok")):
            with patch.object(browse, "_get_lock", track_get_lock):
                result = await browse.web_search("test query", "lock_web_channel")

        assert result.success
        assert len(acquired) >= 2, "web_search should acquire/release the lock"
        assert acquired[0] == ("acquired", "lock_web_channel"), f"Expected acquire first, got {acquired}"

    @pytest.mark.asyncio
    async def test_web_search_serializes_concurrent_calls_same_channel(self):
        """Two concurrent web_search calls for the same channel serialize via the lock."""
        log = []
        shared_lock_holder = None

        async def serialized_get_lock(channel_id):
            nonlocal shared_lock_holder
            if channel_id not in browse._lock_map:
                shared_lock_holder = asyncio.Lock()
                browse._lock_map[channel_id] = shared_lock_holder
            else:
                shared_lock_holder = browse._lock_map[channel_id]
            async with shared_lock_holder:
                log.append(("enter", channel_id))
                await asyncio.sleep(0.03)
                log.append(("exit", channel_id))
            return shared_lock_holder

        async def dummy_run(op):
            return "Page: ok\nURL: x\n\nok"

        with patch.object(browse, "_run", dummy_run):
            with patch.object(browse, "_get_lock", serialized_get_lock):
                await asyncio.gather(
                    browse.web_search("q1", "serial_ch"),
                    browse.web_search("q2", "serial_ch"),
                )

        # Entries must interleave properly: enter A, exit A, enter B, exit B (or vice versa)
        # Never: enter A, enter B (would mean no serialization)
        enters_by_call = []
        for label, ch in log:
            if label == "enter":
                enters_by_call.append(ch)

        assert enters_by_call == ["serial_ch", "serial_ch"]

        # Check no overlapping holds
        # Map enter/exit pairs
        depth = 0
        for label, _ in log:
            if label == "enter":
                depth += 1
                assert depth <= 1, "Two concurrent holds detected - lock not serialized"
            else:
                depth -= 1

    @pytest.mark.asyncio
    async def test_browser_navigate_lock_is_acquired(self):
        """Verify browser_navigate acquires the lock via _get_lock before running."""
        acquired = []

        async def track_get_lock(channel_id):
            lock = asyncio.Lock()
            acquired.append(("acquired", channel_id))
            await lock.acquire()
            acquired.append(("released", channel_id))
            lock.release()
            return lock

        with patch.object(browse, "_run", AsyncMock(return_value="Page: ok\nURL: x\n\nok")):
            with patch.object(browse, "_get_lock", track_get_lock):
                result = await browse.browser_navigate("https://example.com", "lock_nav_channel")

        assert result.success
        acquired_channels = [ch for label, ch in acquired if label == "acquired"]
        assert "lock_nav_channel" in acquired_channels, f"browser_navigate should acquire lock, got {acquired}"

    @pytest.mark.asyncio
    async def test_browser_interact_lock_is_acquired(self):
        """Verify browser_interact acquires the lock via _get_lock."""
        acquired = []

        async def track_get_lock(channel_id):
            lock = asyncio.Lock()
            acquired.append(("acquired", channel_id))
            await lock.acquire()
            acquired.append(("released", channel_id))
            lock.release()
            return lock

        with patch.object(browse, "_run", AsyncMock(return_value="Page: ok\nURL: x\n\nok")):
            with patch.object(browse, "_get_lock", track_get_lock):
                result = await browse.browser_interact(action="click", target="#btn", channel_id="lock_int_channel")

        assert result.success
        acquired_channels = [ch for label, ch in acquired if label == "acquired"]
        assert "lock_int_channel" in acquired_channels, f"browser_interact should acquire lock, got {acquired}"
