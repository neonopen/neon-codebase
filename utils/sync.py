'''Tools for synchronization

Author: Mark Desnoyer (desnoyer@neon-lab.com)
Copyright 2014 Neon Labs
'''
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
  sys.path.insert(0, __base_path__)

import concurrent.futures
import contextlib
import functools
import threading
import tornado.ioloop

def optional_sync(func):
    '''A decorator that makes an asyncronous function optionally synchronous.

    If you have an asynchronous function that has a special "callback"
    argument, this decorator will change "callback" so that it's
    optional. If callback is None, then the function will be
    synchronous. If it is not None, the function will be asyncronous.

    Also, if your function returns a Future, then if you add the
    special "async=True" keywork argument, an asynchronous call will
    be made and the Future will be returned. This is useful to avoid a
    tornado.gen.Task wrapping.

    To use it, make sure this decorator is on the outside. For example
    @optional_sync
    @tornado.gen.coroutine
    def do_something_async(url):
      response = yield tornado.httpclient.AsyncHTTPClient().fetch(url)
      raise tornado.gen.Return(random.shuffle(response))

    Then, a synchronous call would look like:
    weird_response = do_something_async('http://hi.com')

    And an asynchronous call would be:
    do_something_async('http://hi.com', callback=process_response)

    or if it is in a @tornado.gen.coroutine, 

    weird_response = yield tornado.gen.Task(do_something_async,
    'http://hi.com')

    or if your async function returns a Future (like tornado.gen.coroutine):

    weird_response = yield do_something_async('http://hi.com', async=True)

    Note that inside the function, you must use
    tornado.ioloop.IOLoop.current() to get the current io
    loop. Otherwise it will hang.
    '''
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if 'callback' in kwargs:
            if kwargs['callback'] is not None:
                return func(*args, **kwargs)
            kwargs.pop('callback')
        if 'async' in kwargs:
            async = kwargs['async']
            kwargs.pop('async')
            if async:
                return func(*args, **kwargs)

        with bounded_io_loop() as io_loop:
            return io_loop.run_sync(lambda : func(*args, **kwargs))

    return wrapper


@contextlib.contextmanager
def bounded_io_loop():
    '''This context manager allows you to have a new ioloop set as the
    current one.

    When the context manager is done, the last current io_loop is returned.

    Example:
    with bounded_io_loop() as ioloop:
      ioloop.run_sync()
    '''
    old_ioloop = tornado.ioloop.IOLoop.current()

    temp_ioloop = tornado.ioloop.IOLoop()
    temp_ioloop.make_current()

    try:
        yield temp_ioloop

    finally:
        old_ioloop.make_current()
        temp_ioloop.close()

class LockAquireThread(threading.Thread):
    '''A thread that will set a future when a lock is aquired.'''
    def __init__(self, lock):
        super(LockAquireThread, self).__init__()
        self.lock = lock
        self.future = concurrent.futures.Future()
        self.daemon = True

    def run(self):
        try:
            if not self.future.set_running_or_notify_cancel():
                return
            self.lock.acquire()
            self.future.set_result(True)
        except Exception as e:
            self.future.set_exception(e)
        

class FutureLock(object):
    '''Object that wrap a lock but returns a Future on aquire().

    Can be used to use syncronization primitives in coroutines. e.g.

    _lock = FutureLock(multiprocessing.Semaphore())

    yield _lock.acquire()
    try:
      do something
    finally:
      _lock.release()
    '''
    def __init__(self, lock):
        self.lock = lock

    def acquire(self):
        '''Exactly like normal acquire but returns a Future if it's not ready.'''
        if self.lock.acquire(False):
            # We have the lock
            future = concurrent.futures.Future()
            future.set_result(True)
            return future

        # We need to wait, so setup the future
        thread = LockAquireThread(self.lock)
        thread.start()
        return thread.future

    def release(self):
        self.lock.release()
