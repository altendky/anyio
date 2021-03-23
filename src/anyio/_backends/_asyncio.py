import array
import asyncio
import concurrent.futures
import math
import socket
import sys
from asyncio.base_events import _run_until_complete_cb  # type: ignore
from collections import OrderedDict, deque
from concurrent.futures import Future
from dataclasses import dataclass
from functools import partial, wraps
from inspect import (
    CORO_RUNNING, CORO_SUSPENDED, GEN_RUNNING, GEN_SUSPENDED, getcoroutinestate, getgeneratorstate)
from io import IOBase
from queue import Empty, Queue
from socket import AddressFamily, SocketKind, SocketType
from threading import Thread, current_thread
from types import TracebackType
from typing import (
    Any, Awaitable, Callable, Collection, Coroutine, Deque, Dict, Generator, List, Optional,
    Sequence, Set, Tuple, Type, TypeVar, Union, cast)
from weakref import WeakKeyDictionary

from .. import CapacityLimiterStatistics, EventStatistics, TaskInfo, abc
from .._core._compat import DeprecatedAsyncContextManager, DeprecatedAwaitable
from .._core._eventloop import claim_worker_thread, threadlocals
from .._core._exceptions import (
    BrokenResourceError, BusyResourceError, ClosedResourceError, EndOfStream)
from .._core._exceptions import ExceptionGroup as BaseExceptionGroup
from .._core._exceptions import WouldBlock
from .._core._sockets import GetAddrInfoReturnType, convert_ipv6_sockaddr
from .._core._synchronization import CapacityLimiter as BaseCapacityLimiter
from .._core._synchronization import Event as BaseEvent
from .._core._synchronization import ResourceGuard
from .._core._tasks import CancelScope as BaseCancelScope
from ..abc import IPSockAddrType, UDPPacketType
from ..lowlevel import RunVar

if sys.version_info >= (3, 8):
    get_coro = asyncio.Task.get_coro
else:
    def get_coro(task: asyncio.Task) -> Union[Coroutine, Generator]:
        return task._coro

if sys.version_info >= (3, 7):
    from asyncio import all_tasks, create_task, current_task, get_running_loop
    from asyncio import run as native_run

    def find_root_task() -> asyncio.Task:
        for task in all_tasks():
            if task._callbacks:
                for cb, context in task._callbacks:  # type: ignore
                    if cb is _run_until_complete_cb or cb.__module__ == 'uvloop.loop':
                        return task

        raise RuntimeError('Cannot find root task for setting cleanup callback')
else:

    _T = TypeVar('_T')

    def native_run(main, *, debug=False):
        # Snatched from Python 3.7
        from asyncio import coroutines, events, tasks

        def _cancel_all_tasks(loop):
            to_cancel = all_tasks(loop)
            if not to_cancel:
                return

            for task in to_cancel:
                task.cancel()

            loop.run_until_complete(
                tasks.gather(*to_cancel, loop=loop, return_exceptions=True))

            for task in to_cancel:
                if task.cancelled():
                    continue
                if task.exception() is not None:
                    loop.call_exception_handler({
                        'message': 'unhandled exception during asyncio.run() shutdown',
                        'exception': task.exception(),
                        'task': task,
                    })

        if events._get_running_loop() is not None:
            raise RuntimeError(
                "asyncio.run() cannot be called from a running event loop")

        if not coroutines.iscoroutine(main):
            raise ValueError("a coroutine was expected, got {!r}".format(main))

        loop = events.new_event_loop()
        try:
            events.set_event_loop(loop)
            loop.set_debug(debug)
            return loop.run_until_complete(main)
        finally:
            try:
                _cancel_all_tasks(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                events.set_event_loop(None)
                loop.close()

    def create_task(coro: Union[Generator[Any, None, _T], Awaitable[_T]], *,  # type: ignore
                    name: Optional[str] = None) -> asyncio.Task:
        return get_running_loop().create_task(coro)

    def get_running_loop() -> asyncio.AbstractEventLoop:
        loop = asyncio._get_running_loop()
        if loop is not None:
            return loop
        else:
            raise RuntimeError('no running event loop')

    def all_tasks(loop: Optional[asyncio.AbstractEventLoop] = None) -> Set[asyncio.Task]:
        """Return a set of all tasks for the loop."""
        from asyncio import Task

        if loop is None:
            loop = get_running_loop()

        return {t for t in Task.all_tasks(loop) if not t.done()}

    def current_task(loop: Optional[asyncio.AbstractEventLoop] = None) -> Optional[asyncio.Task]:
        if loop is None:
            loop = get_running_loop()

        return asyncio.Task.current_task(loop)

    def find_root_task() -> asyncio.Task:
        for task in all_tasks():
            for cb in task._callbacks:
                if cb is _run_until_complete_cb or cb.__module__ == 'uvloop.loop':
                    return task

        raise RuntimeError('Cannot find root task for setting cleanup callback')

T_Retval = TypeVar('T_Retval')

# Check whether there is native support for task names in asyncio (3.8+)
_native_task_names = hasattr(asyncio.Task, 'get_name')


def get_callable_name(func: Callable) -> str:
    module = getattr(func, '__module__', None)
    qualname = getattr(func, '__qualname__', None)
    return '.'.join([x for x in (module, qualname) if x])


#
# Event loop
#

_run_vars = WeakKeyDictionary()  # type: WeakKeyDictionary[asyncio.AbstractEventLoop, Any]

current_token = get_running_loop


def _task_started(task: asyncio.Task) -> bool:
    """Return ``True`` if the task has been started and has not finished."""
    coro = get_coro(task)
    try:
        return getcoroutinestate(coro) in (CORO_RUNNING, CORO_SUSPENDED)
    except AttributeError:
        try:
            return getgeneratorstate(coro) in (GEN_RUNNING, GEN_SUSPENDED)
        except AttributeError:
            # task coro is async_genenerator_asend https://bugs.python.org/issue37771
            raise Exception(f"Cannot determine if task {task} has started or not")


def _maybe_set_event_loop_policy(policy: Optional[asyncio.AbstractEventLoopPolicy],
                                 use_uvloop: bool) -> None:
    # On CPython, use uvloop when possible if no other policy has been given and if not
    # explicitly disabled
    if policy is None and use_uvloop and sys.implementation.name == 'cpython':
        try:
            import uvloop
        except ImportError:
            pass
        else:
            # Test for missing shutdown_default_executor() (uvloop 0.14.0 and earlier)
            if (not hasattr(asyncio.AbstractEventLoop, 'shutdown_default_executor')
                    or hasattr(uvloop.loop.Loop, 'shutdown_default_executor')):
                policy = uvloop.EventLoopPolicy()

    if policy is not None:
        asyncio.set_event_loop_policy(policy)


def run(func: Callable[..., T_Retval], *args, debug: bool = False, use_uvloop: bool = True,
        policy: Optional[asyncio.AbstractEventLoopPolicy] = None) -> T_Retval:
    @wraps(func)
    async def wrapper():
        task = current_task()
        task_state = TaskState(None, get_callable_name(func), None)
        _task_states[task] = task_state
        if _native_task_names:
            task.set_name(task_state.name)

        try:
            return await func(*args)
        finally:
            del _task_states[task]

    _maybe_set_event_loop_policy(policy, use_uvloop)
    return native_run(wrapper(), debug=debug)


#
# Miscellaneous
#

sleep = asyncio.sleep


#
# Timeouts and cancellation
#

CancelledError = asyncio.CancelledError


class CancelScope(BaseCancelScope, DeprecatedAsyncContextManager):
    def __new__(cls, *, deadline: float = math.inf, shield: bool = False):
        return object.__new__(cls)

    def __init__(self, deadline: float = math.inf, shield: bool = False):
        self._deadline = deadline
        self._shield = shield
        self._parent_scope: Optional[CancelScope] = None
        self._cancel_called = False
        self._active = False
        self._timeout_handle: Optional[asyncio.TimerHandle] = None
        self._cancel_handle: Optional[asyncio.Handle] = None
        self._tasks: Set[asyncio.Task] = set()
        self._host_task: Optional[asyncio.Task] = None
        self._timeout_expired = False

    def __enter__(self):
        if self._active:
            raise RuntimeError(
                "Each CancelScope may only be used for a single 'with' block"
            )

        self._host_task = current_task()
        self._tasks.add(self._host_task)
        try:
            task_state = _task_states[self._host_task]
        except KeyError:
            task_name = self._host_task.get_name() if _native_task_names else None
            task_state = TaskState(None, task_name, self)
            _task_states[self._host_task] = task_state
        else:
            self._parent_scope = task_state.cancel_scope
            task_state.cancel_scope = self

        self._timeout()
        self._active = True
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc_val: Optional[BaseException],
                 exc_tb: Optional[TracebackType]) -> Optional[bool]:
        self._active = False
        if self._timeout_handle:
            self._timeout_handle.cancel()
            self._timeout_handle = None

        assert self._host_task is not None
        self._tasks.remove(self._host_task)
        host_task_state = _task_states.get(self._host_task)
        if host_task_state is not None and host_task_state.cancel_scope is self:
            host_task_state.cancel_scope = self._parent_scope

        # Restart the cancellation effort in the nearest directly cancelled parent scope if this
        # one was shielded
        if self._shield:
            scope = self._parent_scope
            while scope is not None:
                if scope._cancel_called and scope._cancel_handle is None:
                    scope._deliver_cancellation()
                    break

                # No point in looking beyond any shielded scope
                if scope._shield:
                    break

                scope = scope._parent_scope

        if exc_val is not None:
            exceptions = exc_val.exceptions if isinstance(exc_val, ExceptionGroup) else [exc_val]
            if all(isinstance(exc, CancelledError) for exc in exceptions):
                if self._timeout_expired:
                    return True
                elif not self._cancel_called:
                    # Task was cancelled natively
                    return None
                elif not self._parent_cancelled():
                    # This scope was directly cancelled
                    return True

        return None

    def _timeout(self):
        if self._deadline != math.inf:
            loop = get_running_loop()
            if loop.time() >= self._deadline:
                self._timeout_expired = True
                self.cancel()
            else:
                self._timeout_handle = loop.call_at(self._deadline, self._timeout)

    def _deliver_cancellation(self) -> None:
        """
        Deliver cancellation to directly contained tasks and nested cancel scopes.

        Schedule another run at the end if we still have tasks eligible for cancellation.
        """
        should_retry = False
        cancellable_tasks: Set[asyncio.Task] = set()
        current = current_task()
        for task in self._tasks:
            # The task is eligible for cancellation if it has started and is not in a cancel
            # scope shielded from this one
            cancel_scope = _task_states[task].cancel_scope
            while cancel_scope is not self:
                if cancel_scope is None or cancel_scope._shield:
                    break
                else:
                    cancel_scope = cancel_scope._parent_scope
            else:
                should_retry = True
                if task is not current and (task is self._host_task or _task_started(task)):
                    cancellable_tasks.add(task)

        for task in cancellable_tasks:
            task.cancel()

        # Schedule another callback if there are still tasks left
        if should_retry:
            self._cancel_handle = get_running_loop().call_soon(self._deliver_cancellation)
        else:
            self._cancel_handle = None

    def _parent_cancelled(self) -> bool:
        # Check whether any parent has been cancelled
        cancel_scope = self._parent_scope
        while cancel_scope is not None and not cancel_scope._shield:
            if cancel_scope._cancel_called:
                return True
            else:
                cancel_scope = cancel_scope._parent_scope

        return False

    def cancel(self) -> DeprecatedAwaitable:
        if not self._cancel_called:
            if self._timeout_handle:
                self._timeout_handle.cancel()
                self._timeout_handle = None

            self._cancel_called = True
            self._deliver_cancellation()

        return DeprecatedAwaitable(self.cancel)

    @property
    def deadline(self) -> float:
        return self._deadline

    @deadline.setter
    def deadline(self, value: float) -> None:
        self._deadline = float(value)
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None

        if self._active and not self._cancel_called:
            self._timeout()

    @property
    def cancel_called(self) -> bool:
        return self._cancel_called

    @property
    def shield(self) -> bool:
        return self._shield


async def checkpoint() -> None:
    await sleep(0)


async def checkpoint_if_cancelled() -> None:
    task = current_task()
    if task is None:
        return

    try:
        cancel_scope = _task_states[task].cancel_scope
    except KeyError:
        return

    while cancel_scope:
        if cancel_scope.cancel_called:
            await sleep(0)
        elif cancel_scope.shield:
            break
        else:
            cancel_scope = cancel_scope._parent_scope


async def cancel_shielded_checkpoint() -> None:
    with CancelScope(shield=True):
        await sleep(0)


def current_effective_deadline():
    try:
        cancel_scope = _task_states[current_task()].cancel_scope
    except KeyError:
        return math.inf

    deadline = math.inf
    while cancel_scope:
        deadline = min(deadline, cancel_scope.deadline)
        if cancel_scope.shield:
            break
        else:
            cancel_scope = cancel_scope._parent_scope

    return deadline


def current_time():
    return get_running_loop().time()


#
# Task states
#

class TaskState:
    """
    Encapsulates auxiliary task information that cannot be added to the Task instance itself
    because there are no guarantees about its implementation.
    """

    __slots__ = 'parent_id', 'name', 'cancel_scope'

    def __init__(self, parent_id: Optional[int], name: Optional[str],
                 cancel_scope: Optional[CancelScope]):
        self.parent_id = parent_id
        self.name = name
        self.cancel_scope = cancel_scope


_task_states = WeakKeyDictionary()  # type: WeakKeyDictionary[asyncio.Task, TaskState]


#
# Task groups
#

class ExceptionGroup(BaseExceptionGroup):
    def __init__(self, exceptions: Sequence[BaseException]):
        super().__init__()
        self.exceptions = exceptions


class _AsyncioTaskStatus(abc.TaskStatus):
    def __init__(self, future: asyncio.Future):
        self._future = future

    def started(self, value=None) -> None:
        self._future.set_result(value)


class TaskGroup(abc.TaskGroup):
    __slots__ = 'cancel_scope', '_active', '_exceptions'

    def __init__(self):
        self.cancel_scope: CancelScope = CancelScope()
        self._active = False
        self._exceptions: List[BaseException] = []

    async def __aenter__(self):
        self.cancel_scope.__enter__()
        self._active = True
        return self

    async def __aexit__(self, exc_type: Optional[Type[BaseException]],
                        exc_val: Optional[BaseException],
                        exc_tb: Optional[TracebackType]) -> Optional[bool]:
        ignore_exception = self.cancel_scope.__exit__(exc_type, exc_val, exc_tb)
        if exc_val is not None:
            self.cancel_scope.cancel()
            self._exceptions.append(exc_val)

        while self.cancel_scope._tasks:
            try:
                await asyncio.wait(self.cancel_scope._tasks)
            except asyncio.CancelledError:
                self.cancel_scope.cancel()

        self._active = False
        if not self.cancel_scope._parent_cancelled():
            exceptions = self._filter_cancellation_errors(self._exceptions)
        else:
            exceptions = self._exceptions

        try:
            if len(exceptions) > 1:
                raise ExceptionGroup(exceptions)
            elif exceptions and exceptions[0] is not exc_val:
                raise exceptions[0]
        except BaseException as exc:
            # Clear the context here, as it can only be done in-flight.
            # If the context is not cleared, it can result in recursive tracebacks (see #145).
            exc.__context__ = None
            raise

        return ignore_exception

    @staticmethod
    def _filter_cancellation_errors(exceptions: Sequence[BaseException]) -> List[BaseException]:
        filtered_exceptions: List[BaseException] = []
        for exc in exceptions:
            if isinstance(exc, ExceptionGroup):
                exc.exceptions = TaskGroup._filter_cancellation_errors(exc.exceptions)
                if len(exc.exceptions) > 1:
                    filtered_exceptions.append(exc)
                elif exc.exceptions:
                    filtered_exceptions.append(exc.exceptions[0])
            elif not isinstance(exc, CancelledError):
                filtered_exceptions.append(exc)

        return filtered_exceptions

    async def _run_wrapped_task(
            self, coro: Coroutine, task_status_future: Optional[asyncio.Future]) -> None:
        # This is the code path for Python 3.6 and 3.7 on which asyncio freaks out if a task raises
        # a BaseException.
        __traceback_hide__ = __tracebackhide__ = True  # noqa: F841
        task = cast(asyncio.Task, current_task())
        try:
            await coro
        except BaseException as exc:
            if task_status_future is None or task_status_future.done():
                self._exceptions.append(exc)
                self.cancel_scope.cancel()
            else:
                task_status_future.set_exception(exc)
        else:
            if task_status_future is not None and not task_status_future.done():
                task_status_future.set_exception(
                    RuntimeError('Child exited without calling task_status.started()'))
        finally:
            if task in self.cancel_scope._tasks:
                self.cancel_scope._tasks.remove(task)
                del _task_states[task]

    def _spawn(self, func: Callable[..., Coroutine], args: tuple, name,
               task_status_future: Optional[asyncio.Future] = None) -> asyncio.Task:
        def task_done(_task: asyncio.Task) -> None:
            # This is the code path for Python 3.8+
            assert _task in self.cancel_scope._tasks
            self.cancel_scope._tasks.remove(_task)
            del _task_states[_task]

            try:
                exc = _task.exception()
            except CancelledError as e:
                exc = e

            if exc is not None:
                if task_status_future is None or task_status_future.done():
                    self._exceptions.append(exc)
                    self.cancel_scope.cancel()
                else:
                    task_status_future.set_exception(exc)
            elif task_status_future is not None and not task_status_future.done():
                task_status_future.set_exception(
                    RuntimeError('Child exited without calling task_status.started()'))

        if not self._active:
            raise RuntimeError('This task group is not active; no new tasks can be spawned.')

        options = {}
        name = name or get_callable_name(func)
        if _native_task_names:
            options['name'] = name

        kwargs = {}
        if task_status_future:
            kwargs['task_status'] = _AsyncioTaskStatus(task_status_future)

        coro = func(*args, **kwargs)
        if not asyncio.iscoroutine(coro):
            raise TypeError(f'Expected an async function, but {func} appears to be synchronous')

        foreign_coro = not hasattr(coro, 'cr_frame') and not hasattr(coro, 'gi_frame')
        if foreign_coro or sys.version_info < (3, 8):
            coro = self._run_wrapped_task(coro, task_status_future)

        task = create_task(coro, **options)
        if not foreign_coro and sys.version_info >= (3, 8):
            task.add_done_callback(task_done)

        # Make the spawned task inherit the task group's cancel scope
        _task_states[task] = TaskState(parent_id=id(current_task()), name=name,
                                       cancel_scope=self.cancel_scope)
        self.cancel_scope._tasks.add(task)
        return task

    def spawn(self, func: Callable[..., Coroutine], *args, name=None) -> DeprecatedAwaitable:
        self._spawn(func, args, name)
        return DeprecatedAwaitable(self.spawn)

    async def start(self, func: Callable[..., Coroutine], *args, name=None) -> None:
        future: asyncio.Future = asyncio.Future()
        task = self._spawn(func, args, name, future)

        # If the task raises an exception after sending a start value without a switch point
        # between, the task group is cancelled and this method never proceeds to process the
        # completed future. That's why we have to have a shielded cancel scope here.
        with CancelScope(shield=True):
            try:
                return await future
            except CancelledError:
                task.cancel()
                raise


#
# Threads
#

_Retval_Queue_Type = Tuple[Optional[T_Retval], Optional[BaseException]]


def _thread_pool_worker(work_queue: Queue, workers: Set[Thread],
                        idle_workers: Set[Thread]) -> None:
    func: Callable
    args: tuple
    future: asyncio.Future
    limiter: CapacityLimiter
    thread = current_thread()
    while True:
        try:
            func, args, future = work_queue.get(timeout=10)
        except Empty:
            workers.remove(thread)
            return
        finally:
            idle_workers.discard(thread)

        if func is None:
            # Shutdown command received
            workers.remove(thread)
            return

        if not future.cancelled():
            with claim_worker_thread('asyncio'):
                loop = threadlocals.loop = future._loop
                try:
                    result = func(*args)
                except BaseException as exc:
                    idle_workers.add(thread)
                    if not loop.is_closed() and not future.cancelled():
                        loop.call_soon_threadsafe(future.set_exception, exc)
                else:
                    idle_workers.add(thread)
                    if not loop.is_closed() and not future.cancelled():
                        loop.call_soon_threadsafe(future.set_result, result)
        else:
            idle_workers.add(thread)

        work_queue.task_done()


_threadpool_work_queue: RunVar[Queue] = RunVar('_threadpool_work_queue')
_threadpool_idle_workers: RunVar[Set[Thread]] = RunVar('_threadpool_idle_workers')
_threadpool_workers: RunVar[Set[Thread]] = RunVar('_threadpool_workers')


def _loop_shutdown_callback(f: asyncio.Future) -> None:
    """This is called when the root task has finished."""
    for _ in range(len(_threadpool_workers.get())):
        _threadpool_work_queue.get().put_nowait((None, None, None))


async def run_sync_in_worker_thread(
        func: Callable[..., T_Retval], *args, cancellable: bool = False,
        limiter: Optional['CapacityLimiter'] = None) -> T_Retval:
    await checkpoint()

    # If this is the first run in this event loop thread, set up the necessary variables
    try:
        work_queue = _threadpool_work_queue.get()
        idle_workers = _threadpool_idle_workers.get()
        workers = _threadpool_workers.get()
    except LookupError:
        work_queue = Queue()
        idle_workers = set()
        workers = set()
        _threadpool_work_queue.set(work_queue)
        _threadpool_idle_workers.set(idle_workers)
        _threadpool_workers.set(workers)
        find_root_task().add_done_callback(_loop_shutdown_callback)

    async with (limiter or current_default_thread_limiter()):
        with CancelScope(shield=not cancellable):
            future: asyncio.Future = asyncio.Future()
            work_queue.put_nowait((func, args, future))
            if not idle_workers:
                args = (work_queue, workers, idle_workers)
                thread = Thread(target=_thread_pool_worker, args=args, name='AnyIO worker thread')
                workers.add(thread)
                thread.start()

            return await future


def run_sync_from_thread(func: Callable[..., T_Retval], *args,
                         loop: Optional[asyncio.AbstractEventLoop] = None) -> T_Retval:
    @wraps(func)
    def wrapper():
        try:
            f.set_result(func(*args))
        except BaseException as exc:
            f.set_exception(exc)
            if not isinstance(exc, Exception):
                raise

    f: concurrent.futures.Future[T_Retval] = Future()
    loop = loop or threadlocals.loop
    loop.call_soon_threadsafe(wrapper)
    return f.result()


def run_async_from_thread(func: Callable[..., Coroutine[Any, Any, T_Retval]], *args) -> T_Retval:
    f: concurrent.futures.Future[T_Retval] = asyncio.run_coroutine_threadsafe(
        func(*args), threadlocals.loop)
    return f.result()


class BlockingPortal(abc.BlockingPortal):
    __slots__ = '_loop'

    def __init__(self):
        super().__init__()
        self._loop = get_running_loop()

    def _spawn_task_from_thread(self, func: Callable, args: tuple, kwargs: Dict[str, Any],
                                name, future: Future) -> None:
        run_sync_from_thread(
            partial(self._task_group.spawn, name=name), self._call_func, func, args, kwargs,
            future, loop=self._loop)


#
# Subprocesses
#

@dataclass
class StreamReaderWrapper(abc.ByteReceiveStream):
    _stream: asyncio.StreamReader

    async def receive(self, max_bytes: int = 65536) -> bytes:
        data = await self._stream.read(max_bytes)
        if data:
            return data
        else:
            raise EndOfStream

    async def aclose(self) -> None:
        self._stream.feed_eof()


@dataclass
class StreamWriterWrapper(abc.ByteSendStream):
    _stream: asyncio.StreamWriter

    async def send(self, item: bytes) -> None:
        self._stream.write(item)
        await self._stream.drain()

    async def aclose(self) -> None:
        self._stream.close()


@dataclass
class Process(abc.Process):
    _process: asyncio.subprocess.Process
    _stdin: Optional[abc.ByteSendStream]
    _stdout: Optional[abc.ByteReceiveStream]
    _stderr: Optional[abc.ByteReceiveStream]

    async def aclose(self) -> None:
        if self._stdin:
            await self._stdin.aclose()
        if self._stdout:
            await self._stdout.aclose()
        if self._stderr:
            await self._stderr.aclose()

        await self.wait()

    async def wait(self) -> int:
        return await self._process.wait()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()

    def send_signal(self, signal: int) -> None:
        self._process.send_signal(signal)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode

    @property
    def stdin(self) -> Optional[abc.ByteSendStream]:
        return self._stdin

    @property
    def stdout(self) -> Optional[abc.ByteReceiveStream]:
        return self._stdout

    @property
    def stderr(self) -> Optional[abc.ByteReceiveStream]:
        return self._stderr


async def open_process(command, *, shell: bool, stdin: int, stdout: int, stderr: int):
    await checkpoint()
    if shell:
        process = await asyncio.create_subprocess_shell(command, stdin=stdin, stdout=stdout,
                                                        stderr=stderr)
    else:
        process = await asyncio.create_subprocess_exec(*command, stdin=stdin, stdout=stdout,
                                                       stderr=stderr)

    stdin_stream = StreamWriterWrapper(process.stdin) if process.stdin else None
    stdout_stream = StreamReaderWrapper(process.stdout) if process.stdout else None
    stderr_stream = StreamReaderWrapper(process.stderr) if process.stderr else None
    return Process(process, stdin_stream, stdout_stream, stderr_stream)


#
# Sockets and networking
#


class StreamProtocol(asyncio.Protocol):
    read_queue: Deque[bytes]
    read_event: asyncio.Event
    write_future: asyncio.Future
    exception: Optional[Exception] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.read_queue = deque()
        self.read_event = asyncio.Event()
        self.write_future = asyncio.Future()
        self.write_future.set_result(None)
        cast(asyncio.Transport, transport).set_write_buffer_limits(0)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if exc:
            self.exception = BrokenResourceError()
            self.exception.__cause__ = exc

        self.read_event.set()
        self.write_future = asyncio.Future()
        if self.exception:
            self.write_future.set_exception(self.exception)
        else:
            self.write_future.set_result(None)

    def data_received(self, data: bytes) -> None:
        self.read_queue.append(data)
        self.read_event.set()

    def eof_received(self) -> Optional[bool]:
        self.read_event.set()
        return True

    def pause_writing(self) -> None:
        self.write_future = asyncio.Future()

    def resume_writing(self) -> None:
        self.write_future.set_result(None)


class DatagramProtocol(asyncio.DatagramProtocol):
    read_queue: Deque[Tuple[bytes, IPSockAddrType]]
    read_event: asyncio.Event
    write_event: asyncio.Event
    exception: Optional[Exception] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.read_queue = deque(maxlen=100)  # arbitrary value
        self.read_event = asyncio.Event()
        self.write_event = asyncio.Event()
        self.write_event.set()

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self.read_event.set()
        self.write_event.set()

    def datagram_received(self, data: bytes, addr: IPSockAddrType) -> None:
        addr = convert_ipv6_sockaddr(addr)
        self.read_queue.append((data, addr))
        self.read_event.set()

    def error_received(self, exc: Exception) -> None:
        self.exception = exc

    def pause_writing(self) -> None:
        self.write_event.clear()

    def resume_writing(self) -> None:
        self.write_event.set()


class SocketStream(abc.SocketStream):
    def __init__(self, transport: asyncio.Transport, protocol: StreamProtocol):
        self._transport = transport
        self._protocol = protocol
        self._receive_guard = ResourceGuard('reading from')
        self._send_guard = ResourceGuard('writing to')
        self._closed = False

    @property
    def _raw_socket(self) -> socket.socket:
        return self._transport.get_extra_info('socket')

    async def receive(self, max_bytes: int = 65536) -> bytes:
        with self._receive_guard:
            await checkpoint()
            if not self._protocol.read_event.is_set() and not self._transport.is_closing():
                self._transport.resume_reading()
                await self._protocol.read_event.wait()
                self._transport.pause_reading()

            try:
                chunk = self._protocol.read_queue.popleft()
            except IndexError:
                if self._closed:
                    raise ClosedResourceError from None
                elif self._protocol.exception:
                    raise self._protocol.exception
                else:
                    raise EndOfStream

            if len(chunk) > max_bytes:
                # Split the oversized chunk
                chunk, leftover = chunk[:max_bytes], chunk[max_bytes:]
                self._protocol.read_queue.appendleft(leftover)

            # If the read queue is empty, clear the flag so that the next call will block until
            # data is available
            if not self._protocol.read_queue:
                self._protocol.read_event.clear()

        return chunk

    async def send(self, item: bytes) -> None:
        with self._send_guard:
            await checkpoint()
            try:
                self._transport.write(item)
            except RuntimeError as exc:
                if self._protocol.write_future.exception():
                    await self._protocol.write_future
                elif self._closed:
                    raise ClosedResourceError from None
                elif self._transport.is_closing():
                    raise BrokenResourceError from exc
                else:
                    raise

            await self._protocol.write_future

    async def send_eof(self) -> None:
        try:
            self._transport.write_eof()
        except OSError:
            pass

    async def aclose(self) -> None:
        if not self._transport.is_closing():
            self._closed = True
            try:
                self._transport.write_eof()
            except OSError:
                pass

            self._transport.close()
            await sleep(0)
            self._transport.abort()


class UNIXSocketStream(abc.SocketStream):
    _receive_future: Optional[asyncio.Future] = None
    _send_future: Optional[asyncio.Future] = None
    _closing = False

    def __init__(self, raw_socket: socket.SocketType):
        self.__raw_socket = raw_socket
        self._loop = get_running_loop()
        self._receive_guard = ResourceGuard('reading from')
        self._send_guard = ResourceGuard('writing to')

    @property
    def _raw_socket(self) -> SocketType:
        return self.__raw_socket

    def _wait_until_readable(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future:
        def callback(f):
            del self._receive_future
            loop.remove_reader(self.__raw_socket)

        f = self._receive_future = asyncio.Future()
        self._loop.add_reader(self.__raw_socket, f.set_result, None)
        f.add_done_callback(callback)
        return f

    def _wait_until_writable(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future:
        def callback(f):
            del self._send_future
            loop.remove_writer(self.__raw_socket)

        f = self._send_future = asyncio.Future()
        self._loop.add_writer(self.__raw_socket, f.set_result, None)
        f.add_done_callback(callback)
        return f

    async def send_eof(self) -> None:
        with self._send_guard:
            self._raw_socket.shutdown(socket.SHUT_WR)

    async def receive(self, max_bytes: int = 65536) -> bytes:
        loop = get_running_loop()
        await checkpoint()
        with self._receive_guard:
            while True:
                try:
                    data = self.__raw_socket.recv(max_bytes)
                except BlockingIOError:
                    await self._wait_until_readable(loop)
                except OSError as exc:
                    if self._closing:
                        raise ClosedResourceError from None
                    else:
                        raise BrokenResourceError from exc
                else:
                    if not data:
                        raise EndOfStream

                    return data

    async def send(self, item: bytes) -> None:
        loop = get_running_loop()
        await checkpoint()
        with self._send_guard:
            view = memoryview(item)
            while view:
                try:
                    bytes_sent = self.__raw_socket.send(item)
                except BlockingIOError:
                    await self._wait_until_writable(loop)
                except OSError as exc:
                    if self._closing:
                        raise ClosedResourceError from None
                    else:
                        raise BrokenResourceError from exc
                else:
                    view = view[bytes_sent:]

    async def receive_fds(self, msglen: int, maxfds: int) -> Tuple[bytes, List[int]]:
        if not isinstance(msglen, int) or msglen < 0:
            raise ValueError('msglen must be a non-negative integer')
        if not isinstance(maxfds, int) or maxfds < 1:
            raise ValueError('maxfds must be a positive integer')

        loop = get_running_loop()
        fds = array.array("i")
        await checkpoint()
        with self._receive_guard:
            while True:
                try:
                    message, ancdata, flags, addr = self.__raw_socket.recvmsg(
                        msglen, socket.CMSG_LEN(maxfds * fds.itemsize))
                except BlockingIOError:
                    await self._wait_until_readable(loop)
                except OSError as exc:
                    if self._closing:
                        raise ClosedResourceError from None
                    else:
                        raise BrokenResourceError from exc
                else:
                    if not message and not ancdata:
                        raise EndOfStream

                    break

        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if cmsg_level != socket.SOL_SOCKET or cmsg_type != socket.SCM_RIGHTS:
                raise RuntimeError(f'Received unexpected ancillary data; message = {message}, '
                                   f'cmsg_level = {cmsg_level}, cmsg_type = {cmsg_type}')

            fds.frombytes(cmsg_data[:len(cmsg_data) - (len(cmsg_data) % fds.itemsize)])

        return message, list(fds)

    async def send_fds(self, message: bytes, fds: Collection[Union[int, IOBase]]) -> None:
        if not message:
            raise ValueError('message must not be empty')
        if not fds:
            raise ValueError('fds must not be empty')

        loop = get_running_loop()
        filenos: List[int] = []
        for fd in fds:
            if isinstance(fd, int):
                filenos.append(fd)
            elif isinstance(fd, IOBase):
                filenos.append(fd.fileno())

        fdarray = array.array("i", filenos)
        await checkpoint()
        with self._send_guard:
            while True:
                try:
                    self.__raw_socket.sendmsg([message],
                                              [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fdarray)])
                    break
                except BlockingIOError:
                    await self._wait_until_writable(loop)
                except OSError as exc:
                    if self._closing:
                        raise ClosedResourceError from None
                    else:
                        raise BrokenResourceError from exc

    async def aclose(self) -> None:
        if not self._closing:
            self._closing = True
            if self.__raw_socket.fileno() != -1:
                self.__raw_socket.close()

            if self._receive_future:
                self._receive_future.set_result(None)
            if self._send_future:
                self._send_future.set_result(None)


class TCPSocketListener(abc.SocketListener):
    _accept_scope: Optional[CancelScope] = None
    _closed = False

    def __init__(self, raw_socket: socket.SocketType):
        self.__raw_socket = raw_socket
        self._loop = cast(asyncio.BaseEventLoop, get_running_loop())
        self._accept_guard = ResourceGuard('accepting connections from')

    @property
    def _raw_socket(self) -> socket.socket:
        return self.__raw_socket

    async def accept(self) -> abc.SocketStream:
        if self._closed:
            raise ClosedResourceError

        with self._accept_guard:
            await checkpoint()
            with CancelScope() as self._accept_scope:
                try:
                    client_sock, _addr = await self._loop.sock_accept(self._raw_socket)
                except asyncio.CancelledError:
                    # Workaround for https://bugs.python.org/issue41317
                    try:
                        self._loop.remove_reader(self._raw_socket)
                    except (ValueError, NotImplementedError):
                        pass

                    if self._closed:
                        raise ClosedResourceError from None

                    raise
                finally:
                    self._accept_scope = None

        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        transport, protocol = await self._loop.connect_accepted_socket(StreamProtocol, client_sock)
        return SocketStream(cast(asyncio.Transport, transport), cast(StreamProtocol, protocol))

    async def aclose(self) -> None:
        if self._closed:
            return

        self._closed = True
        if self._accept_scope:
            # Workaround for https://bugs.python.org/issue41317
            try:
                self._loop.remove_reader(self._raw_socket)
            except (ValueError, NotImplementedError):
                pass

            self._accept_scope.cancel()
            await sleep(0)

        self._raw_socket.close()


class UNIXSocketListener(abc.SocketListener):
    def __init__(self, raw_socket: socket.SocketType):
        self.__raw_socket = raw_socket
        self._loop = get_running_loop()
        self._accept_guard = ResourceGuard('accepting connections from')
        self._closed = False

    async def accept(self) -> abc.SocketStream:
        await checkpoint()
        with self._accept_guard:
            while True:
                try:
                    client_sock, _ = self.__raw_socket.accept()
                    return UNIXSocketStream(client_sock)
                except BlockingIOError:
                    f: asyncio.Future = asyncio.Future()
                    self._loop.add_reader(self.__raw_socket, f.set_result, None)
                    f.add_done_callback(lambda _: self._loop.remove_reader(self.__raw_socket))
                    await f
                except OSError as exc:
                    if self._closed:
                        raise ClosedResourceError from None
                    else:
                        raise BrokenResourceError from exc

    async def aclose(self) -> None:
        self._closed = True
        self.__raw_socket.close()

    @property
    def _raw_socket(self) -> SocketType:
        return self.__raw_socket


class UDPSocket(abc.UDPSocket):
    def __init__(self, transport: asyncio.DatagramTransport, protocol: DatagramProtocol):
        self._transport = transport
        self._protocol = protocol
        self._receive_guard = ResourceGuard('reading from')
        self._send_guard = ResourceGuard('writing to')
        self._closed = False

    @property
    def _raw_socket(self) -> SocketType:
        return self._transport.get_extra_info('socket')

    async def aclose(self) -> None:
        if not self._transport.is_closing():
            self._closed = True
            self._transport.close()

    async def receive(self) -> Tuple[bytes, IPSockAddrType]:
        with self._receive_guard:
            await checkpoint()

            # If the buffer is empty, ask for more data
            if not self._protocol.read_queue and not self._transport.is_closing():
                self._protocol.read_event.clear()
                await self._protocol.read_event.wait()

            try:
                return self._protocol.read_queue.popleft()
            except IndexError:
                if self._closed:
                    raise ClosedResourceError from None
                else:
                    raise BrokenResourceError from None

    async def send(self, item: UDPPacketType) -> None:
        with self._send_guard:
            await checkpoint()
            await self._protocol.write_event.wait()
            if self._closed:
                raise ClosedResourceError
            elif self._transport.is_closing():
                raise BrokenResourceError
            else:
                self._transport.sendto(*item)


class ConnectedUDPSocket(abc.ConnectedUDPSocket):
    def __init__(self, transport: asyncio.DatagramTransport, protocol: DatagramProtocol):
        self._transport = transport
        self._protocol = protocol
        self._receive_guard = ResourceGuard('reading from')
        self._send_guard = ResourceGuard('writing to')
        self._closed = False

    @property
    def _raw_socket(self) -> SocketType:
        return self._transport.get_extra_info('socket')

    async def aclose(self) -> None:
        if not self._transport.is_closing():
            self._closed = True
            self._transport.close()

    async def receive(self) -> bytes:
        with self._receive_guard:
            await checkpoint()

            # If the buffer is empty, ask for more data
            if not self._protocol.read_queue and not self._transport.is_closing():
                self._protocol.read_event.clear()
                await self._protocol.read_event.wait()

            try:
                packet = self._protocol.read_queue.popleft()
            except IndexError:
                if self._closed:
                    raise ClosedResourceError from None
                else:
                    raise BrokenResourceError from None

            return packet[0]

    async def send(self, item: bytes) -> None:
        with self._send_guard:
            await checkpoint()
            await self._protocol.write_event.wait()
            if self._closed:
                raise ClosedResourceError
            elif self._transport.is_closing():
                raise BrokenResourceError
            else:
                self._transport.sendto(item)


async def connect_tcp(host: str, port: int,
                      local_addr: Optional[Tuple[str, int]] = None) -> SocketStream:
    transport, protocol = cast(
        Tuple[asyncio.Transport, StreamProtocol],
        await get_running_loop().create_connection(StreamProtocol, host, port,
                                                   local_addr=local_addr)
    )
    transport.pause_reading()
    return SocketStream(transport, protocol)


async def connect_unix(path: str) -> UNIXSocketStream:
    await checkpoint()
    loop = get_running_loop()
    raw_socket = socket.socket(socket.AF_UNIX)
    raw_socket.setblocking(False)
    while True:
        try:
            raw_socket.connect(path)
        except BlockingIOError:
            f: asyncio.Future = asyncio.Future()
            loop.add_writer(raw_socket, f.set_result, None)
            f.add_done_callback(lambda _: loop.remove_writer(raw_socket))
            await f
        else:
            return UNIXSocketStream(raw_socket)


async def create_udp_socket(
    family: socket.AddressFamily,
    local_address: Optional[IPSockAddrType],
    remote_address: Optional[IPSockAddrType],
    reuse_port: bool
) -> Union[UDPSocket, ConnectedUDPSocket]:
    result = await get_running_loop().create_datagram_endpoint(
        DatagramProtocol, local_addr=local_address, remote_addr=remote_address, family=family,
        reuse_port=reuse_port)
    transport = cast(asyncio.DatagramTransport, result[0])
    protocol = cast(DatagramProtocol, result[1])
    if protocol.exception:
        transport.close()
        raise protocol.exception

    if not remote_address:
        return UDPSocket(transport, protocol)
    else:
        return ConnectedUDPSocket(transport, protocol)


async def getaddrinfo(host: Union[bytearray, bytes, str], port: Union[str, int, None], *,
                      family: Union[int, AddressFamily] = 0, type: Union[int, SocketKind] = 0,
                      proto: int = 0, flags: int = 0) -> GetAddrInfoReturnType:
    # https://github.com/python/typeshed/pull/4304
    result = await get_running_loop().getaddrinfo(
        host, port, family=family, type=type, proto=proto, flags=flags)  # type: ignore[arg-type]
    return cast(GetAddrInfoReturnType, result)


async def getnameinfo(sockaddr: IPSockAddrType, flags: int = 0) -> Tuple[str, str]:
    # https://github.com/python/typeshed/pull/4305
    result = await get_running_loop().getnameinfo(sockaddr, flags)
    return cast(Tuple[str, str], result)


_read_events: RunVar[Dict[Any, asyncio.Event]] = RunVar('read_events')
_write_events: RunVar[Dict[Any, asyncio.Event]] = RunVar('write_events')


async def wait_socket_readable(sock: socket.SocketType) -> None:
    await checkpoint()
    try:
        read_events = _read_events.get()
    except LookupError:
        read_events = {}
        _read_events.set(read_events)

    if read_events.get(sock):
        raise BusyResourceError('reading from') from None

    loop = get_running_loop()
    event = read_events[sock] = asyncio.Event()
    loop.add_reader(sock, event.set)
    try:
        await event.wait()
    finally:
        if read_events.pop(sock, None) is not None:
            loop.remove_reader(sock)
            readable = True
        else:
            readable = False

    if not readable:
        raise ClosedResourceError


async def wait_socket_writable(sock: socket.SocketType) -> None:
    await checkpoint()
    try:
        write_events = _write_events.get()
    except LookupError:
        write_events = {}
        _write_events.set(write_events)

    if write_events.get(sock):
        raise BusyResourceError('writing to') from None

    loop = get_running_loop()
    event = write_events[sock] = asyncio.Event()
    loop.add_writer(sock.fileno(), event.set)
    try:
        await event.wait()
    finally:
        if write_events.pop(sock, None) is not None:
            loop.remove_writer(sock)
            writable = True
        else:
            writable = False

    if not writable:
        raise ClosedResourceError


#
# Synchronization
#

class Event(BaseEvent):
    def __new__(cls):
        return object.__new__(cls)

    def __init__(self):
        self._event = asyncio.Event()

    def set(self) -> DeprecatedAwaitable:
        self._event.set()
        return DeprecatedAwaitable(self.set)

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self):
        await checkpoint()
        await self._event.wait()

    def statistics(self) -> EventStatistics:
        return EventStatistics(len(self._event._waiters))


class CapacityLimiter(BaseCapacityLimiter):
    _total_tokens: float = 0

    def __new__(cls, total_tokens: float):
        return object.__new__(cls)

    def __init__(self, total_tokens: float):
        self._borrowers: Set[Any] = set()
        self._wait_queue: Dict[Any, asyncio.Event] = OrderedDict()
        self.total_tokens = total_tokens

    async def __aenter__(self):
        await self.acquire()

    async def __aexit__(self, exc_type: Optional[Type[BaseException]],
                        exc_val: Optional[BaseException],
                        exc_tb: Optional[TracebackType]) -> None:
        self.release()

    @property
    def total_tokens(self) -> float:
        return self._total_tokens

    @total_tokens.setter
    def total_tokens(self, value: float) -> None:
        if not isinstance(value, int) and not math.isinf(value):
            raise TypeError('total_tokens must be an int or math.inf')
        if value < 1:
            raise ValueError('total_tokens must be >= 1')

        old_value = self._total_tokens
        self._total_tokens = value
        events = []
        for event in self._wait_queue.values():
            if value <= old_value:
                break

            if not event.is_set():
                events.append(event)
                old_value += 1

        for event in events:
            event.set()

    @property
    def borrowed_tokens(self) -> int:
        return len(self._borrowers)

    @property
    def available_tokens(self) -> float:
        return self._total_tokens - len(self._borrowers)

    def acquire_nowait(self) -> DeprecatedAwaitable:
        self.acquire_on_behalf_of_nowait(current_task())
        return DeprecatedAwaitable(self.acquire_nowait)

    def acquire_on_behalf_of_nowait(self, borrower) -> DeprecatedAwaitable:
        if borrower in self._borrowers:
            raise RuntimeError("this borrower is already holding one of this CapacityLimiter's "
                               "tokens")

        if self._wait_queue or len(self._borrowers) >= self._total_tokens:
            raise WouldBlock

        self._borrowers.add(borrower)
        return DeprecatedAwaitable(self.acquire_on_behalf_of_nowait)

    async def acquire(self) -> None:
        return await self.acquire_on_behalf_of(current_task())

    async def acquire_on_behalf_of(self, borrower) -> None:
        try:
            self.acquire_on_behalf_of_nowait(borrower)
        except WouldBlock:
            event = asyncio.Event()
            self._wait_queue[borrower] = event
            try:
                await event.wait()
            except BaseException:
                self._wait_queue.pop(borrower, None)
                raise

            self._borrowers.add(borrower)

    def release(self) -> None:
        self.release_on_behalf_of(current_task())

    def release_on_behalf_of(self, borrower) -> None:
        try:
            self._borrowers.remove(borrower)
        except KeyError:
            raise RuntimeError("this borrower isn't holding any of this CapacityLimiter's "
                               "tokens") from None

        # Notify the next task in line if this limiter has free capacity now
        if self._wait_queue and len(self._borrowers) < self._total_tokens:
            event = self._wait_queue.popitem()[1]
            event.set()

    def statistics(self) -> CapacityLimiterStatistics:
        return CapacityLimiterStatistics(self.borrowed_tokens, self.total_tokens,
                                         tuple(self._borrowers), len(self._wait_queue))


_default_thread_limiter: RunVar[CapacityLimiter] = RunVar('_default_thread_limiter')


def current_default_thread_limiter():
    try:
        return _default_thread_limiter.get()
    except LookupError:
        limiter = CapacityLimiter(40)
        _default_thread_limiter.set(limiter)
        return limiter


#
# Operating system signals
#

class _SignalReceiver(DeprecatedAsyncContextManager):
    def __init__(self, signals: Tuple[int, ...]):
        self._signals = signals
        self._loop = get_running_loop()
        self._signal_queue: Deque[int] = deque()
        self._future: asyncio.Future = asyncio.Future()
        self._handled_signals: Set[int] = set()

    def _deliver(self, signum: int) -> None:
        self._signal_queue.append(signum)
        if not self._future.done():
            self._future.set_result(None)

    def __enter__(self):
        for sig in set(self._signals):
            self._loop.add_signal_handler(sig, self._deliver, sig)
            self._handled_signals.add(sig)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for sig in self._handled_signals:
            self._loop.remove_signal_handler(sig)

    def __aiter__(self):
        return self

    async def __anext__(self) -> int:
        await checkpoint()
        if not self._signal_queue:
            self._future = asyncio.Future()
            await self._future

        return self._signal_queue.popleft()


def open_signal_receiver(*signals: int) -> _SignalReceiver:
    return _SignalReceiver(signals)


#
# Testing and debugging
#

def _create_task_info(task: asyncio.Task) -> TaskInfo:
    task_state = _task_states.get(task)
    if task_state is None:
        name = task.get_name() if _native_task_names else None  # type: ignore
        parent_id = None
    else:
        name = task_state.name
        parent_id = task_state.parent_id

    return TaskInfo(id(task), parent_id, name, get_coro(task))


def get_current_task() -> TaskInfo:
    return _create_task_info(current_task())  # type: ignore


def get_running_tasks() -> List[TaskInfo]:
    return [_create_task_info(task) for task in all_tasks() if not task.done()]


async def wait_all_tasks_blocked() -> None:
    this_task = current_task()
    while True:
        for task in all_tasks():
            if task is this_task:
                continue

            if task._fut_waiter is None:  # type: ignore[attr-defined]
                await sleep(0.1)
                break
        else:
            return


class TestRunner(abc.TestRunner):
    def __init__(self, debug: bool = False, use_uvloop: bool = True,
                 policy: Optional[asyncio.AbstractEventLoopPolicy] = None):
        _maybe_set_event_loop_policy(policy, use_uvloop)
        self._loop = asyncio.new_event_loop()
        self._loop.set_debug(debug)
        asyncio.set_event_loop(self._loop)

    def _cancel_all_tasks(self):
        to_cancel = all_tasks(self._loop)
        if not to_cancel:
            return

        for task in to_cancel:
            task.cancel()

        self._loop.run_until_complete(
            asyncio.gather(*to_cancel, loop=self._loop, return_exceptions=True))

        for task in to_cancel:
            if task.cancelled():
                continue
            if task.exception() is not None:
                raise task.exception()

    def close(self) -> None:
        try:
            self._cancel_all_tasks()
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            self._loop.close()

    def call(self, func: Callable[..., Awaitable], *args, **kwargs):
        def exception_handler(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
            exceptions.append(context['exception'])

        exceptions: List[Exception] = []
        self._loop.set_exception_handler(exception_handler)
        try:
            retval = self._loop.run_until_complete(func(*args, **kwargs))
        except Exception as exc:
            retval = None
            exceptions.append(exc)
        finally:
            self._loop.set_exception_handler(None)

        if len(exceptions) == 1:
            raise exceptions[0]
        elif exceptions:
            raise ExceptionGroup(exceptions)

        return retval
