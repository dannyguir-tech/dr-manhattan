"""Strategy session manager."""

import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from dr_manhattan.base import Exchange, Strategy
from dr_manhattan.utils import setup_logger

from .models import SessionStatus, StrategySession

logger = setup_logger(__name__)

# Thread cleanup configuration (per CLAUDE.md Rule #4: config in code)
THREAD_GRACE_PERIOD = 10.0  # seconds - initial wait before force-kill
THREAD_FORCE_KILL_TIMEOUT = 5.0  # seconds - timeout for force-kill attempt
THREAD_CLEANUP_TIMEOUT = 5.0  # seconds - timeout during cleanup()

# Status caching configuration (reduces refresh_state() calls)
# 3 seconds provides good balance between freshness and performance
STATUS_CACHE_TTL = 3.0  # seconds - cache lifetime for get_status()
STATUS_CACHE_MAX_SIZE = 100  # Maximum cache entries (prevents memory leak)


class StrategySessionManager:
    """
    Manages background strategy executions.

    Maintains active strategy sessions and provides monitoring/control.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """Ensure singleton instance with thread-safe initialization."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._sessions: Dict[str, StrategySession] = {}
                cls._instance._instance_lock = threading.Lock()
                cls._instance._orphaned_sessions: Dict[str, str] = {}
                cls._instance._status_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
                logger.info("StrategySessionManager initialized")
            else:
                cls._instance._ensure_state()
        return cls._instance

    def __init__(self):
        """No-op: initialization done in __new__ to prevent race conditions."""
        self._ensure_state()

    def _ensure_state(self) -> None:
        """Repair any missing or None-backed internal state."""
        if not hasattr(self, "_instance_lock") or self._instance_lock is None:
            self._instance_lock = threading.Lock()
        if not hasattr(self, "_sessions") or self._sessions is None:
            self._sessions = {}
        if not hasattr(self, "_orphaned_sessions") or self._orphaned_sessions is None:
            self._orphaned_sessions = {}
        if not hasattr(self, "_status_cache") or self._status_cache is None:
            self._status_cache = {}

    def _safe_sessions(self) -> Dict[str, StrategySession]:
        self._ensure_state()
        return self._sessions if isinstance(self._sessions, dict) else {}

    def _safe_orphaned_sessions(self) -> Dict[str, str]:
        self._ensure_state()
        return self._orphaned_sessions if isinstance(self._orphaned_sessions, dict) else {}

    def _safe_status_cache(self) -> Dict[str, Tuple[float, Dict[str, Any]]]:
        self._ensure_state()
        return self._status_cache if isinstance(self._status_cache, dict) else {}

    def create_session(
        self,
        strategy_class: type,
        exchange: Exchange,
        exchange_name: str,
        market_id: str,
        **params,
    ) -> str:
        """
        Create and start strategy in background thread.

        Args:
            strategy_class: Strategy class to instantiate
            exchange: Exchange instance
            exchange_name: Exchange name
            market_id: Market ID to trade
            **params: Strategy parameters (max_position, order_size, etc.)

        Returns:
            session_id for monitoring/control
        """
        session_id = str(uuid.uuid4())

        try:
            duration_minutes = params.pop("duration_minutes", None)
            strategy = strategy_class(exchange=exchange, market_id=market_id, **params)

            session = StrategySession(
                id=session_id,
                strategy_type=strategy_class.__name__,
                exchange_name=exchange_name,
                market_id=market_id,
                strategy=strategy,
                status=SessionStatus.RUNNING,
            )

            thread = threading.Thread(
                target=self._run_strategy,
                args=(session_id, strategy, duration_minutes),
                daemon=True,
            )
            thread.start()
            session.thread = thread

            with self._instance_lock:
                self._sessions[session_id] = session

            logger.info(
                f"Strategy session created: {session_id} "
                f"({strategy_class.__name__} on {exchange_name})"
            )

            return session_id

        except Exception as e:
            logger.error(f"Failed to create strategy session: {e}")
            raise

    def _run_strategy(self, session_id: str, strategy: Strategy, duration_minutes: Optional[int]):
        """Run strategy in background thread."""
        try:
            logger.info(f"Starting strategy execution: {session_id}")
            strategy.run(duration_minutes=duration_minutes)

            with self._instance_lock:
                sessions = self._safe_sessions()
                if session_id in sessions:
                    sessions[session_id].status = SessionStatus.STOPPED
                status_cache = self._safe_status_cache()
                if session_id in status_cache:
                    del status_cache[session_id]

        except Exception as e:
            logger.error(f"Strategy execution failed: {e}")
            with self._instance_lock:
                sessions = self._safe_sessions()
                if session_id in sessions:
                    sessions[session_id].status = SessionStatus.ERROR
                    sessions[session_id].error = str(e)
                status_cache = self._safe_status_cache()
                if session_id in status_cache:
                    del status_cache[session_id]

    def get_session(self, session_id: str) -> StrategySession:
        """
        Get strategy session by ID.

        Args:
            session_id: Session ID

        Returns:
            StrategySession

        Raises:
            ValueError: If session not found
        """
        sessions = self._safe_sessions()
        session = sessions.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        return session

    def _evict_stale_cache_entries(self, now: float) -> None:
        """
        Remove stale cache entries to prevent memory leak.

        Must be called while holding _instance_lock.
        Removes entries older than TTL or exceeding max size.
        """
        status_cache = self._safe_status_cache()
        expired = [
            sid
            for sid, (cached_time, _) in list(status_cache.items())
            if now - cached_time >= STATUS_CACHE_TTL
        ]
        for sid in expired:
            status_cache.pop(sid, None)

        if len(status_cache) > STATUS_CACHE_MAX_SIZE:
            sorted_entries = sorted(
                status_cache.items(),
                key=lambda x: x[1][0],
            )
            entries_to_remove = len(status_cache) - STATUS_CACHE_MAX_SIZE
            for sid, _ in sorted_entries[:entries_to_remove]:
                status_cache.pop(sid, None)

    def get_status(self, session_id: str) -> Dict[str, Any]:
        """
        Get real-time strategy status with caching.

        Uses TTL-based caching to reduce expensive refresh_state() calls.
        Cache TTL is configured by STATUS_CACHE_TTL constant.
        Thread-safe: cache access protected by _instance_lock.

        Args:
            session_id: Session ID

        Returns:
            Status dictionary with NAV, positions, orders, etc.
        """
        now = time.time()

        with self._instance_lock:
            status_cache = self._safe_status_cache()
            if session_id in status_cache:
                cached_time, cached_status = status_cache[session_id]
                if now - cached_time < STATUS_CACHE_TTL:
                    return cached_status

        status = self._compute_status(session_id)

        with self._instance_lock:
            status_cache = self._safe_status_cache()
            self._evict_stale_cache_entries(now)
            if len(status_cache) < STATUS_CACHE_MAX_SIZE:
                status_cache[session_id] = (now, status)

        return status

    def _compute_status(self, session_id: str) -> Dict[str, Any]:
        """
        Compute fresh strategy status (internal, uncached).

        Args:
            session_id: Session ID

        Returns:
            Status dictionary
        """
        session = self.get_session(session_id)
        strategy = session.strategy

        try:
            strategy.refresh_state()
        except Exception as e:
            logger.warning(f"Failed to refresh strategy state: {e}")

        uptime = (datetime.now() - session.created_at).total_seconds()
        orphaned_sessions = self._safe_orphaned_sessions()
        is_orphaned = session_id in orphaned_sessions

        open_orders = getattr(strategy, "open_orders", []) or []
        positions = getattr(strategy, "positions", {}) or {}

        return {
            "session_id": session_id,
            "status": session.status.value,
            "strategy_type": session.strategy_type,
            "exchange": session.exchange_name,
            "market_id": session.market_id,
            "uptime_seconds": uptime,
            "is_running": bool(getattr(strategy, "is_running", False)),
            "thread_alive": session.is_alive(),
            "is_orphaned": is_orphaned,
            "nav": getattr(strategy, "nav", 0.0),
            "cash": getattr(strategy, "cash", 0.0),
            "positions": positions,
            "delta": getattr(strategy, "delta", 0.0),
            "open_orders_count": len(open_orders),
            "error": session.error,
        }

    def pause_strategy(self, session_id: str) -> bool:
        """
        Pause strategy execution.

        Args:
            session_id: Session ID

        Returns:
            True if paused successfully
        """
        session = self.get_session(session_id)
        session.strategy.is_running = False
        session.status = SessionStatus.PAUSED
        logger.info(f"Strategy paused: {session_id}")
        return True

    def resume_strategy(self, session_id: str) -> bool:
        """
        Resume paused strategy.

        Args:
            session_id: Session ID

        Returns:
            True if resumed successfully
        """
        session = self.get_session(session_id)
        if session.status != SessionStatus.PAUSED:
            raise ValueError(f"Strategy not paused: {session_id}")

        session.strategy.is_running = True
        session.status = SessionStatus.RUNNING
        logger.info(f"Strategy resumed: {session_id}")
        return True

    def _force_stop_thread(self, session_id: str, session: StrategySession) -> bool:
        """
        Attempt to force-stop a thread that didn't respond to graceful stop.

        Args:
            session_id: Session ID
            session: Strategy session

        Returns:
            True if thread stopped, False if still running (orphaned)
        """
        strategy = session.strategy
        strategy.is_running = False

        if session.thread and session.thread.is_alive():
            logger.warning(f"Force-stopping strategy thread: {session_id}")
            session.thread.join(timeout=THREAD_FORCE_KILL_TIMEOUT)

            if session.thread.is_alive():
                total_timeout = THREAD_GRACE_PERIOD + THREAD_FORCE_KILL_TIMEOUT
                orphaned_sessions = self._safe_orphaned_sessions()
                orphaned_sessions[session_id] = f"Thread did not terminate after {total_timeout}s"
                logger.error(
                    f"Strategy thread {session_id} is orphaned. "
                    "Thread may still be running in background. "
                    "Consider restarting the MCP server if this persists."
                )
                return False

        return True

    def stop_strategy(self, session_id: str, cleanup: bool = True) -> Dict[str, Any]:
        """
        Stop strategy with force-kill capability.

        Implements a two-phase shutdown:
        1. Graceful stop with THREAD_GRACE_PERIOD timeout
        2. Force-kill with THREAD_FORCE_KILL_TIMEOUT if graceful fails

        Args:
            session_id: Session ID
            cleanup: If True, cancel orders and liquidate positions

        Returns:
            Final status and metrics
        """
        session = self.get_session(session_id)
        strategy = session.strategy

        logger.info(f"Stopping strategy: {session_id} (cleanup={cleanup})")
        strategy.stop()

        thread_stopped = True
        if session.thread and session.thread.is_alive():
            session.thread.join(timeout=THREAD_GRACE_PERIOD)

            if session.thread.is_alive():
                logger.warning(
                    f"Strategy thread {session_id} did not stop within grace period "
                    f"({THREAD_GRACE_PERIOD}s). Attempting force-stop..."
                )
                thread_stopped = self._force_stop_thread(session_id, session)

        with self._instance_lock:
            status_cache = self._safe_status_cache()
            if session_id in status_cache:
                del status_cache[session_id]

        final_status = self._compute_status(session_id)
        session.status = SessionStatus.STOPPED
        final_status["thread_stopped"] = thread_stopped
        if not thread_stopped:
            final_status["warning"] = "Thread is orphaned and may still be running"

        logger.info(f"Strategy stopped: {session_id} (thread_stopped={thread_stopped})")
        return final_status

    def get_metrics(self, session_id: str) -> Dict[str, Any]:
        """
        Get strategy performance metrics.

        Args:
            session_id: Session ID

        Returns:
            Performance metrics
        """
        session = self.get_session(session_id)
        strategy = session.strategy

        strategy.refresh_state()
        uptime = (datetime.now() - session.created_at).total_seconds()

        return {
            "session_id": session_id,
            "uptime_seconds": uptime,
            "current_nav": getattr(strategy, "nav", 0.0),
            "cash": getattr(strategy, "cash", 0.0),
            "positions_value": getattr(strategy, "nav", 0.0) - getattr(strategy, "cash", 0.0),
            "current_delta": getattr(strategy, "delta", 0.0),
            "open_orders": len(getattr(strategy, "open_orders", []) or []),
        }

    def list_sessions(self) -> Dict[str, Any]:
        """
        List all active sessions.

        Returns:
            Dictionary of session_id -> session info
        """
        with self._instance_lock:
            sessions = self._safe_sessions()
            orphaned_sessions = self._safe_orphaned_sessions()
            return {
                sid: {
                    "session_id": sid,
                    "strategy_type": getattr(session, "strategy_type", "unknown"),
                    "exchange": getattr(session, "exchange_name", "unknown"),
                    "market_id": getattr(session, "market_id", "unknown"),
                    "status": getattr(getattr(session, "status", None), "value", "unknown"),
                    "created_at": getattr(session, "created_at", datetime.utcnow()).isoformat(),
                    "is_alive": bool(session and session.is_alive()),
                    "is_orphaned": sid in orphaned_sessions,
                }
                for sid, session in list(sessions.items())
                if session is not None
            }

    def get_orphaned_sessions(self) -> Dict[str, str]:
        """
        Get list of orphaned sessions that failed to terminate.

        Returns:
            Dictionary of session_id -> reason for orphan status
        """
        return dict(self._safe_orphaned_sessions())

    def cleanup(self):
        """
        Stop all strategies with force-kill capability.

        Implements two-phase shutdown for each session:
        1. Graceful stop with THREAD_CLEANUP_TIMEOUT
        2. Force-stop for threads that don't respond
        """
        logger.info("Cleaning up strategy sessions...")
        with self._instance_lock:
            sessions = self._safe_sessions()
            status_cache = self._safe_status_cache()
            failed_sessions = []
            for session_id, session in list(sessions.items()):
                if session is None:
                    continue
                try:
                    logger.info(f"Stopping strategy: {session_id}")
                    session.strategy.stop()

                    if session.thread and session.thread.is_alive():
                        session.thread.join(timeout=THREAD_CLEANUP_TIMEOUT)

                        if session.thread.is_alive():
                            logger.warning(
                                f"Strategy thread {session_id} did not stop "
                                "within cleanup timeout. Attempting force-stop..."
                            )
                            session.strategy.is_running = False
                            session.thread.join(timeout=THREAD_FORCE_KILL_TIMEOUT)

                            if session.thread.is_alive():
                                orphaned_sessions = self._safe_orphaned_sessions()
                                orphaned_sessions[session_id] = "Failed to terminate during cleanup"
                                logger.error(
                                    f"Strategy thread {session_id} is orphaned during cleanup"
                                )
                                failed_sessions.append(session_id)

                except Exception as e:
                    logger.error(f"Error stopping strategy {session_id}: {e}")
                    failed_sessions.append(session_id)

            for session_id in list(sessions.keys()):
                if session_id not in failed_sessions:
                    del sessions[session_id]
                    status_cache.pop(session_id, None)

        logger.info(f"Strategy sessions cleaned up. Orphaned: {len(self._safe_orphaned_sessions())}")
