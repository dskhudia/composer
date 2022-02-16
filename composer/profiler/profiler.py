# Copyright 2021 MosaicML. All Rights Reserved.

from __future__ import annotations

import logging
import os
import time
from functools import wraps
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple, Type, Union

from composer.profiler.json_trace import JSONTraceHandler
from composer.profiler.profiler_action import ProfilerAction
from composer.utils import dist, run_directory

if TYPE_CHECKING:
    from composer.core.state import State
    from composer.core.time import Timestamp
    from composer.profiler import ProfilerEventHandler

log = logging.getLogger(__name__)


class Profiler:
    """The Profiler produces a trace of the training graph.

    Specifically, it records:

    #. The duration of each section of the training loop, such as the time it takes to perform a forward pass, backward pass, batch, epoch, etc...

    #. The latency each algorithm and callback adds when executing on each event.

    #. The latency it takes for the dataloader to yield a batch.

    The ``event_handlers`` then record and save this data to a usable trace.

    Args:
        state (State): The state.
        event_handlers (Sequence[ProfilerEventHandler]): Event handlers which record and save profiling data to traces.
        skip_first (int, optional): Number of batches to skip profiling at epoch start. (Default: ``0``)
        wait (int, optional): For each profiling cycle, number of batches to skip at the beginning of the cycle. (Default: ``0``)
        warmup (int, optional): For each profiling cycle, number of batches to be in the warmup state
            after skipping ``wait`` batches.. (Default: ``1``)
        active (int, optional): For each profiling cycle, number of batches to record after warming up. (Default: ``4``)
        repeat (int, optional): Number of profiling cycles to perform per epoch. Set to ``0`` to record the entire epoch. (Default: ``1``)
        merged_trace_file (str, optional): Name of the trace file, relative to the run directory. (Default: ``merged_profiler_trace.json``)
    """

    def __init__(self,
                 state: State,
                 event_handlers: Sequence[ProfilerEventHandler] = tuple(),
                 skip_first: int = 0,
                 wait: int = 0,
                 warmup: int = 1,
                 active: int = 4,
                 repeat: int = 1,
                 merged_trace_file: str = "merged_profiler_trace.json") -> None:
        self._names_to_markers: Dict[str, Marker] = {}
        self._event_handlers = event_handlers if event_handlers else [JSONTraceHandler()]
        self.state = state
        self.skip_first = skip_first
        self.wait = wait
        self.warmup = warmup
        self.active = active
        self.repeat = repeat
        self.merged_trace_file = os.path.join(run_directory.get_run_directory(), merged_trace_file)
        self._action = ProfilerAction.SKIP

    def get_action(self, batch_idx: int) -> ProfilerAction:
        """Get the current :class:`ProfilerAction` for the profiler, based upon the parameters ``skip_first``, ``wait``,
        ``warmup``, ``active``, and ``repeat``.

        The profiler skips the first ``skip_first`` batches in every epoch. Then, it performs a cycle of
        skipping ``wait`` batches, warming up for ``warmup`` batches, and recording ``active`` batches.
        It repeats this cylce up to ``repeat`` times per epoch (or for the entire epoch, if ``repeat`` is 0).
        This logic repeats every epoch.

        Returns:
            ProfilerAction: The current action.
        """
        # do wait, then warump, then active, up to repeat times per cycle
        cycle_len = self.wait + self.warmup + self.active
        if self.repeat != 0 and batch_idx >= cycle_len * self.repeat:
            # exhausted the repeat
            return ProfilerAction.SKIP
        position_in_cycle = batch_idx % cycle_len
        if position_in_cycle < self.wait:
            return ProfilerAction.SKIP
        if position_in_cycle < self.wait + self.warmup:
            return ProfilerAction.WARMUP
        return ProfilerAction.ACTIVE

    @property
    def event_handlers(self):
        """Profiler event handlers."""
        return self._event_handlers

    def merge_traces(self):
        """Merge traces together.

        .. note::

            This method is invoked by the engine. Do not invoke this method directly.
        """
        dist.barrier()
        if not dist.get_local_rank() == 0:
            return
        from composer.profiler.json_trace import JSONTraceHandler
        from composer.profiler.json_trace_merger import merge_traces
        from composer.profiler.torch_profiler import TorchProfiler
        log.info("Merging profiling trace files together")
        trace_folders = []
        for callback in self.state.callbacks:
            if isinstance(callback, JSONTraceHandler):
                trace_folders.append(callback.output_directory)
            if isinstance(callback, TorchProfiler):
                trace_folders.append(callback.tensorboard_trace_handler_dir)

        trace_files = []
        for trace_folder in trace_folders:
            for rootdir, dirnames, filenames in os.walk(trace_folder):
                del dirnames  # unused
                for filename in filenames:
                    filepath = os.path.join(rootdir, filename)
                    if filepath.endswith(".json"):
                        trace_files.append(filepath)
        merge_traces(self.merged_trace_file, *trace_files)

    def marker(
        self,
        name: str,
        actions: Sequence[ProfilerAction] = (ProfilerAction.WARMUP, ProfilerAction.ACTIVE),
        record_instant_on_start: bool = False,
        record_instant_on_finish: bool = False,
        categories: Union[List[str], Tuple[str, ...]] = tuple()) -> Marker:
        """Get a :class:`Marker`.

        If a :class:`Marker` with the specified ``name`` does not already exist, it will be created.
        Otherwise, the existing instance will be returned.

        Args:
            name (str): The name for the :class:`Marker`.
            actions (Sequence[ProfilerAction], optional): :class:`ProfilerAction` states to record on.
                By default, markers will record on :attr:`ProfilerAction.WARMUP` and :attr:`ProfilerAction.ACTIVE`
            record_instant_on_start (bool, optional): Whether to record an instant event whenever the marker is started
            record_instant_on_finish (bool, optional): Whether to record an instant event whenever the marker is finished
            categories (List[str] | Tuple[str, ...], optional): Categories for this marker.

        Returns:
            Marker: [description]
        """
        if name not in self._names_to_markers:
            self._names_to_markers[name] = Marker(
                self,
                name,
                actions=actions,
                record_instant_on_start=record_instant_on_start,
                record_instant_on_finish=record_instant_on_finish,
                categories=categories,
            )
        self._names_to_markers[name].categories = categories
        return self._names_to_markers[name]

    def record_duration_event(self, marker: Marker, is_start: bool, wall_clock_time_ns: int, global_rank: int, pid: int,
                              timestamp: Timestamp):
        """Record a duration event.

        .. note::

            This method should not be invoked directly. Instead, create a :class:`Marker`
            via :meth:`marker`, and then invoke :meth:`Marker.start` or :meth:`Marker.finish`.

        Args:
            marker (Marker): The marker.
            is_start (bool): Whether this is the start or end of the duration event.
            wall_clock_time_ns (int): The :meth:`time.time_ns` corresponding to the event.
            global_rank (int): The `global_rank` where the event was triggered
            pid (int): The `pid` where the event was triggered
            timestamp (Timestamp): The timestamp at which the event was triggered.
        """
        for handler in self._event_handlers:
            handler.process_duration_event(
                name=marker.name,
                categories=marker.categories,
                timestamp=timestamp,
                is_start=is_start,
                wall_clock_time_ns=wall_clock_time_ns,
                global_rank=global_rank,
                pid=pid,
            )

    def record_instant_event(self, marker: Marker, wall_clock_time_ns: int, global_rank: int, pid: int,
                             timestamp: Timestamp):
        """Record an instant event.

        .. note::

            This method should not be invoked directly. Instead, create a :class:`Marker`
            via :meth:`marker`, and then invoke :meth:`Marker.instant`.

        Args:
            marker (Marker): The marker.
            wall_clock_time_ns (int): The :meth:`time.time_ns` corresponding to the event.
            global_rank (int): The `global_rank` where the event was triggered.
            pid (int): The `pid` where the event was triggered.
            timestamp (Timestamp): The timestamp at which the event was triggered.
        """
        for handler in self._event_handlers:
            handler.process_instant_event(
                name=marker.name,
                categories=marker.categories,
                timestamp=timestamp,
                wall_clock_time_ns=wall_clock_time_ns,
                global_rank=global_rank,
                pid=pid,
            )

    def record_counter_event(
        self,
        marker: Marker,
        wall_clock_time_ns: int,
        global_rank: int,
        pid: int,
        values: Dict[str, Union[int, float]],
    ) -> None:
        """Record a counter invent.

        .. note::

            This method should not be invoked directly. Instead, create a :class:`Marker`
            via :meth:`marker`, and then invoke :meth:`Marker.counter`.

        Args:
            marker (str): The name of the event.
            categories (List[str] | Tuple[str, ...]): The categories for the event.
            epoch (Optional[int]): The epoch, if applicable, corresponding to the event.
            step (int): The step, if applicable, corresponding to the event.
            wall_clock_time_ns (int): The :meth:`time.time_ns` corresponding to the event.
            global_rank (int): The `global_rank` corresponding to the event.
            pid (int): The `pid` corresponding to the event.
            values (Dict[str, int | float]): The values corresponding to this counter event
        """
        for handler in self._event_handlers:
            handler.process_counter_event(
                name=marker.name,
                categories=marker.categories,
                wall_clock_time_ns=wall_clock_time_ns,
                global_rank=global_rank,
                pid=pid,
                values=values,
            )


class Marker:
    """Record when something happens or how long something takes.

    .. note::

        :class:`Marker` should not be instantiated directly; instead use :meth:`Profiler.marker`.

    To use a `Marker` to record a duration, you can:

        #. Invoke :meth:`Marker.start` followed by :meth:`Marker.finish`

            .. code-block:: python

                marker = profiler.marker("foo")
                marker.start()
                something_to_measure()
                marker.finish()

        #. Use a :class:`Marker` with a context manager:

            .. code-block:: python

                marker = profiler.marker("foo")
                with marker:
                    something_to_measure()

        #. Use a :class:`Marker` as a decorator:

            .. code-block:: python

                marker = profiler.marker("foo")

                @marker
                def something_to_measure():
                    ...

                something_to_measure()

    To use a :class:`Marker` to record an instant, call :meth:`instant`

    Args:
        profiler (Profiler): The profiler.
        name (str): The name of the event.
        actions (Sequence[ProfilerAction], optional): :class:`ProfilerAction` states to record on.
        record_instant_on_start (bool): Whether to record an instant event whenever the marker is started
        record_instant_on_finish (bool): Whether to record an instant event whenever the marker is finished
        categories (List[str] | Tuple[str, ...]]): Categories corresponding to this event.
    """

    def __init__(self, profiler: Profiler, name: str, actions: Sequence[ProfilerAction], record_instant_on_start: bool,
                 record_instant_on_finish: bool, categories: Union[List[str], Tuple[str, ...]]) -> None:

        self.profiler = profiler
        self.name = name
        self.actions = actions
        self.categories = categories
        self.record_instant_on_start = record_instant_on_start
        self.record_instant_on_finish = record_instant_on_finish
        if name in profiler._names_to_markers:
            if profiler._names_to_markers[name] is not self:
                raise RuntimeError(
                    f"{self.__class__.__name__} should not be instantiated directly. Instead, use {profiler.__class__.__name__}.marker(name)"
                )
        self._started = False
        self._action_at_start = None

    def start(self) -> None:
        """Record the start of a duration event."""
        if self._started:
            raise RuntimeError(
                f"Attempted to start profiler event {self.name}; however, this marker is already started")

        batch_idx = self.profiler.state.timer.batch_in_epoch.value
        self._action_at_start = self.profiler.get_action(batch_idx)
        if self._action_at_start in self.actions:
            wall_clock_time = time.time_ns()
            self.profiler.record_duration_event(
                self,
                is_start=True,
                wall_clock_time_ns=wall_clock_time,
                timestamp=self.profiler.state.timer.get_timestamp(),
                global_rank=dist.get_global_rank(),
                pid=os.getpid(),
            )
            if self.record_instant_on_start:
                self.profiler.record_instant_event(
                    self,
                    timestamp=self.profiler.state.timer.get_timestamp(),
                    wall_clock_time_ns=wall_clock_time,
                    global_rank=dist.get_global_rank(),
                    pid=os.getpid(),
                )
        self._started = True

    def finish(self) -> None:
        """Record the end of a duration event."""
        if not self._started:
            raise RuntimeError(
                f"Attempted to finish profiler event {self.name}; however, this profiler event is not yet started")

        if self._action_at_start in self.actions:
            wall_clock_time = time.time_ns()
            self.profiler.record_duration_event(
                self,
                is_start=False,
                timestamp=self.profiler.state.timer.get_timestamp(),
                wall_clock_time_ns=wall_clock_time,
                global_rank=dist.get_global_rank(),
                pid=os.getpid(),
            )
            if self.record_instant_on_finish:
                self.profiler.record_instant_event(
                    self,
                    wall_clock_time_ns=wall_clock_time,
                    timestamp=self.profiler.state.timer.get_timestamp(),
                    global_rank=dist.get_global_rank(),
                    pid=os.getpid(),
                )
        self._started = False

    def instant(self) -> None:
        """Record an instant event."""
        batch_idx = self.profiler.state.timer.batch_in_epoch.value
        if self.profiler.get_action(batch_idx) in self.actions:
            self.profiler.record_instant_event(
                self,
                wall_clock_time_ns=time.time_ns(),
                timestamp=self.profiler.state.timer.get_timestamp(),
                global_rank=dist.get_global_rank(),
                pid=os.getpid(),
            )

    def counter(self, values: Dict[str, Union[float, int]]) -> None:
        """Record a counter event."""
        batch_idx = self.profiler.state.timer.batch_in_epoch.value
        if self.profiler.get_action(batch_idx) in self.actions:
            self.profiler.record_counter_event(
                self,
                wall_clock_time_ns=time.time_ns(),
                global_rank=dist.get_global_rank(),
                pid=os.getpid(),
                values=values,
            )

    def __enter__(self) -> Marker:
        self.start()
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc: Optional[BaseException],
                 traceback: Optional[TracebackType]) -> None:
        del exc_type, exc, traceback  # unused
        self.finish()

    def __call__(self, func: Optional[Callable[..., Any]] = None) -> Callable[..., Any]:
        if func is None:
            # for decorators of the style @Marker(),
            # return self so it's equivalent to @Marker
            return self

        @wraps(func)
        def wrapped(*args: Any, **kwargs: Any):
            with self:
                func(*args, **kwargs)

        return wrapped